"""arq worker. Cron schedule follows docs/decisions.md D1 (times are Asia/Jakarta via TZ).

/private (A-06) content must never be enqueued here: Redis persists to disk.
"""

import math
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from arq import Retry, cron

from apa import currency
from apa.chat import batch, batch_flow
from apa.core.db import SessionLocal
from apa.core.logging import configure_logging, get_logger
from apa.core.redis import arq_redis_settings
from apa.core.settings import get_settings
from apa.db.models.ops import InboundEvent
from apa.health import huawei
from apa.health import jobs as health_jobs
from apa.ingestion import templates
from apa.ingestion.bank_parser import (
    ParsedNotification,
    classify_skip,
    parse_notification,
)
from apa.ingestion.llm_extract import LLMUnavailable
from apa.ingestion.packages import approved_wallets
from apa.ingestion.shorthand import ParsedShorthand, parse_shorthand
from apa.integrations.evidence_store import DynamicEvidenceStore
from apa.integrations.llm import LLMClient
from apa.integrations.notify import LogNotifier, TelegramNotifier
from apa.news import jobs as news_jobs
from apa.news.telegram import daily_telegram
from apa.pipeline.chat import run_chat, run_master
from apa.pipeline.commit import DebateUnavailable, record_reply, run_commit, run_release
from apa.pipeline.extract import notify_unrecorded, run_extract
from apa.pipeline.receipt import AlbumStillArriving, run_read_receipt
from apa.pipeline.reconcile import run_reconcile
from apa.prices import check as price_check
from apa.reconcile import audit as ledger_audit
from apa.reporting import digest
from apa.sheets import client as sheets_client
from apa.sheets import sync as sheets_sync_mod
from apa.steward.runner import run_steward

log = get_logger(__name__)


LLM_BUDGET_KEY = "apa:notification_llm:"


async def _llm_budget_ok(redis: Any | None) -> bool:
    """At most NOTIFICATION_LLM_PER_HOUR unrecognised notifications an hour may reach the model. Recognised ones never
    count (regex handles them) and a skipped one is still on the bank statement, which the reconciliation finds."""
    limit = get_settings().notification_llm_per_hour
    if redis is None or limit <= 0:
        return True
    key = f"{LLM_BUDGET_KEY}{datetime.now(UTC):%Y%m%d%H}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 7200)
    except Exception:
        return True  # Redis trouble must not make payments vanish
    return n <= limit


async def process_inbound_event(ctx: dict, event_id: int) -> str:
    """Deterministic parse; records the outcome on the event row and hands off to commit_event.

      parsed     regex matched; parse stored in payload['parsed'], then commit_event is enqueued
      skipped    not a money movement (OTP, failed, promo); Android notifications only
      needs_llm  unrecognised or ambiguous wording; waits for the NL/LLM extraction fallback
    `source` selects the parser: android_listener -> bank_parser, telegram_shorthand -> shorthand.
    """
    async with SessionLocal() as session:
        event = await session.get(InboundEvent, event_id)
        if event is None:
            log.warning("event_missing", event_id=event_id)
            return "missing"
        if event.status != "received":  # job redelivery: already handled
            return event.status

        text = event.payload.get("text", "")
        skip: str | None = None
        via_template = False
        batch_items = None
        tz = ZoneInfo(get_settings().timezone)
        if event.source == "telegram_shorthand":
            sent = event.payload.get("sent_at")
            sent_at = datetime.fromisoformat(sent) if sent else event.received_at
            # several payments in one message: code reads the list itself (no model), the owner reviews it
            batch_items = batch.parse_batch(text, sent_at, tz)
            parsed = None if batch_items else parse_shorthand(text)
        else:  # android_listener
            package = event.payload.get("package", "")
            wallets = await approved_wallets(session)  # built-in banks and wallets plus the owner's own picks
            skip = classify_skip(text) or (None if package in wallets else "unknown_package")
            parsed = None if skip else parse_notification(package, text, wallets)
            if parsed is None and not skip:  # wording the fixed rules do not know: a template learned from the model?
                parsed = await templates.apply(session, package, text, wallets)
                if parsed is templates.SKIP:  # a format the model once judged not a transaction: skipped without asking again
                    parsed, skip = None, "learned_skip"
                via_template = parsed is not None

        if batch_items:
            event.status = "skipped"
            event.payload = {**event.payload, "skip_reason": "batch_draft"}
        elif parsed:
            event.status = "parsed"
            event.payload = {**event.payload, "parsed": _parsed_dict(parsed), **({"parsed_by": "template"} if via_template else {})}
        elif skip:
            event.status = "skipped"
            event.payload = {**event.payload, "skip_reason": skip}
        elif event.source == "android_listener" and not await _llm_budget_ok(ctx.get("redis")):
            event.status = "skipped"  # a flood of unknown notifications must not become a flood of model calls
            event.payload = {**event.payload, "skip_reason": "flood_guard"}
        else:
            event.status = "needs_llm"
        event.processed_at = datetime.now(UTC)
        status = event.status
        await session.commit()
    log.info("event_processed", event_id=event_id, source=event.source, status=status)
    if event.source == "android_listener" and status == "skipped" and ctx.get("notifier") is not None:
        try:  # telling the owner is a courtesy: it must never fail the event
            await notify_unrecorded(
                ctx.get("sessionmaker", SessionLocal), ctx["notifier"], event_id, event.payload.get("package", ""), text,
                event.payload.get("skip_reason", ""),
            )
        except Exception as exc:
            log.warning("unrecorded_notice_failed", error=type(exc).__name__)
    # Enqueue only after the commit above so the next job always sees the status it expects.
    if batch_items and ctx.get("redis") is not None:
        card = await batch_flow.start(ctx.get("sessionmaker", SessionLocal), ctx["redis"], batch_items, tz)
        await record_reply(ctx.get("sessionmaker", SessionLocal), ctx["notifier"], card, event_id=event_id)
    # free text code could not classify goes to the master router; notifications keep the extractor
    llm_job = "master_event" if event.source == "telegram_shorthand" else "extract_event"
    next_job = {"parsed": "commit_event", "needs_llm": llm_job}.get(status)
    if next_job and ctx.get("redis") is not None:
        await ctx["redis"].enqueue_job(next_job, event_id)
    return status


COMMIT_MAX_TRIES = 3
DEBATE_RETRY_DELAY_S = 30


async def commit_event(ctx: dict, event_id: int) -> str:
    """breakers -> debate -> Arbiter -> ledger. See apa.pipeline.commit."""
    try:
        return await run_commit(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], event_id,
            job_try=ctx.get("job_try", 1), max_tries=COMMIT_MAX_TRIES,
        )
    except DebateUnavailable:
        log.warning("debate_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=DEBATE_RETRY_DELAY_S) from None


EXTRACT_MAX_TRIES = COMMIT_MAX_TRIES
EXTRACT_RETRY_DELAY_S = 15


async def extract_event(ctx: dict, event_id: int) -> str:
    """LLM fallback for text the regex parsers could not read. See apa.pipeline.extract."""
    try:
        return await run_extract(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx.get("redis"),
            event_id, job_try=ctx.get("job_try", 1), max_tries=EXTRACT_MAX_TRIES,
        )
    except LLMUnavailable:
        log.warning("extract_llm_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=EXTRACT_RETRY_DELAY_S) from None


async def master_event(ctx: dict, event_id: int) -> str:
    """The master router for free text: one call decides the intent and which agents are needed."""
    try:
        return await run_master(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx.get("redis"), event_id,
            job_try=ctx.get("job_try", 1), max_tries=EXTRACT_MAX_TRIES,
        )
    except LLMUnavailable:
        log.warning("master_llm_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=EXTRACT_RETRY_DELAY_S) from None


async def chat_event(ctx: dict, event_id: int) -> str:
    """Conversational answer to owner text that is not a transaction. See apa.pipeline.chat."""
    try:
        return await run_chat(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx.get("redis"), event_id,
            job_try=ctx.get("job_try", 1), max_tries=EXTRACT_MAX_TRIES,
        )
    except LLMUnavailable:
        log.warning("chat_llm_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=EXTRACT_RETRY_DELAY_S) from None


async def read_receipt(ctx: dict, event_id: int) -> str:
    """Vision reading of a receipt photo. See apa.pipeline.receipt."""
    try:
        return await run_read_receipt(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx["store"],
            ctx.get("redis"), event_id,
            job_try=ctx.get("job_try", 1), max_tries=EXTRACT_MAX_TRIES,
            settle_seconds=ctx.get("album_settle_seconds"),
        )
    except AlbumStillArriving as e:
        log.info("album_still_arriving", event_id=event_id, wait=round(e.wait, 1))
        raise Retry(defer=max(1, math.ceil(e.wait))) from None
    except LLMUnavailable:
        log.warning("receipt_llm_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=EXTRACT_RETRY_DELAY_S) from None


async def release_quarantined(ctx: dict, tx_id: int) -> str:
    """Owner approved a held transaction (/release_<token>): debate and commit it now."""
    try:
        return await run_release(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], tx_id,
            job_try=ctx.get("job_try", 1), max_tries=COMMIT_MAX_TRIES,
        )
    except DebateUnavailable:
        log.warning("release_debate_unavailable_retrying", tx_id=tx_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=DEBATE_RETRY_DELAY_S) from None


def _parsed_dict(p: ParsedNotification | ParsedShorthand) -> dict:
    """Dataclass -> JSON-safe dict (Decimal becomes str so amounts stay exact in JSONB)."""
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(p).items()}


async def sheets_sync(ctx: dict) -> None:
    """Mirror the ledger into the owner's Google Sheet. Idle until a service account and sheet are set up;
    a failure is reported at most once a day."""
    settings = get_settings()
    sheet_id = sheets_client.spreadsheet_id(settings.sheets_spreadsheet_id, settings.sheets_url)
    if not (settings.sheets_credentials_file and sheet_id):
        return
    sm = ctx.get("sessionmaker", SessionLocal)
    tz = ZoneInfo(settings.timezone)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            client = sheets_client.SheetsClient(http, settings.sheets_credentials_file, sheet_id)
            await sheets_sync_mod.run_sync(sm, client, ctx.get("redis"), tz, datetime.now(tz))
    except sheets_client.SheetsError as e:
        log.warning("sheets_sync_failed", error=str(e))
        now = datetime.now(UTC)
        await digest.deliver(sm, ctx["notifier"], ctx.get("redis"), f"sheets:{now:%Y-%m-%d}",
                             f"⚠️ Sinkron Google Sheets gagal: {e}")


async def cyberramen_poll(ctx: dict) -> None:
    """Cyber intelligence: new CVEs, CISA KEV, EPSS. Urgent ones reach Telegram once."""
    await news_jobs.run_cyber_poll(
        ctx.get("sessionmaker", SessionLocal), ctx["notifier"], ctx.get("redis"), datetime.now(UTC),
        nvd_key=get_settings().nvd_api_key,
    )


async def news_ingest(ctx: dict) -> None:
    """News: read every feed, store new articles, push only the genuinely urgent."""
    await news_jobs.run_news_ingest(
        ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx.get("redis"),
        datetime.now(UTC),
    )


async def briefing_precompute(ctx: dict) -> None:
    """Ahead of the wake-up time (BRIEFING_WAKE_TIME minus BRIEFING_LEAD_MINUTES): voice clips ready for the
    app and the detailed briefing in Telegram."""
    tz = ZoneInfo(get_settings().timezone)
    await news_jobs.run_briefing_precompute(
        ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx.get("redis"), tz, datetime.now(tz),
        notifier=ctx["notifier"],
        reuse_since=datetime.now(tz).replace(hour=_PRE_H, minute=_PRE_M, second=0, microsecond=0),
    )


PHONE_ALERT_EVERY_S = 12 * 3600


async def phone_watch(ctx: dict) -> str:
    """Tells the owner when the phone stops collecting: the app has not checked in, or its notification listener is
    disconnected (a phone maker stopped the app). Silent otherwise, and at most one message per kind per 12 hours."""
    import json

    redis = ctx.get("redis")
    if redis is None:
        return "no_redis"
    raw = await redis.get("apa:phone:heartbeat")
    if not raw:
        return "never_seen"  # the app has never reported: nothing to watch yet
    hb = json.loads(raw)
    age_h = (datetime.now(UTC).timestamp() - hb["ts"]) / 3600
    kind = text = None
    if age_h >= get_settings().phone_silent_hours:  # bank notifications are probably not being collected
        kind, text = "silent", (
            f"\U0001F4F5 HP belum melapor ke server sejak {int(age_h)} jam. Notifikasi bank tidak terkumpul selama itu. "
            "Buka aplikasi APA di HP; kalau sering terjadi, nyalakan Autostart dan pilih Tanpa batasan di pengaturan baterai."
        )
    elif not hb.get("listener", True):
        kind, text = "listener", (
            "\U0001F514 Pendengar notifikasi di HP terputus (HP mematikan aplikasi), jadi notifikasi bank tidak terbaca. "
            "Buka APA lalu ketuk «Re-link notification listener», dan pastikan Autostart menyala."
        )
    if kind is None:
        return "ok"
    if not await redis.set(f"apa:phone:alert:{kind}", "1", ex=PHONE_ALERT_EVERY_S, nx=True):
        return "already_told"
    await ctx["notifier"].send(text)
    return kind


async def briefing_telegram(ctx: dict) -> int:
    """The complete text briefing to Telegram on request (the app's "play now" buttons). -> messages sent."""
    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    sm = ctx.get("sessionmaker", SessionLocal)
    sent = 0
    for i, text in enumerate(await daily_telegram(sm, ctx.get("redis"), tz, now, llm=ctx.get("llm")), start=1):
        sent += bool(await digest.deliver(sm, ctx["notifier"], None, f"briefing-manual:{now:%Y%m%d%H%M%S}:{i}", text))
    return sent


async def huawei_healthkit_sync(ctx: dict) -> None:
    """Huawei Health Kit cloud sync. Idle until HUAWEI_CLIENT_ID/SECRET and SECRETS_KEY are set and the owner
    has authorised once; a failure is reported at most once a day."""
    if not huawei.configured():
        return
    sm = ctx.get("sessionmaker", SessionLocal)
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            added = await huawei.sync(sm, http, datetime.now(UTC))
        log.info("huawei_synced", **added)
    except huawei.HuaweiError as e:
        log.warning("huawei_sync_failed", error=str(e))
        now = datetime.now(UTC)
        await digest.deliver(sm, ctx["notifier"], ctx.get("redis"), f"huawei:{now:%Y-%m-%d}",
                             f"⚠️ Sinkron Huawei Health Kit gagal: {e}")


async def health_tick(ctx: dict) -> None:
    """A-03: keep today's intake estimate current, warn at 80% of a limit, notice a bad night, run the stress
    shield. Silent unless there is something to say."""
    tz = ZoneInfo(get_settings().timezone)
    await health_jobs.run_health_tick(
        ctx.get("sessionmaker", SessionLocal), ctx["notifier"], ctx.get("redis"), tz, datetime.now(tz)
    )


async def price_tick(ctx: dict) -> None:
    """A-09: take the owner's receipt prices (and the optional PRICE_FEED_URL), look for a real staple rise,
    and cover it from discretionary budget within the 15% band. Silent when nothing moved."""
    settings = get_settings()
    sm = ctx.get("sessionmaker", SessionLocal)
    tz = ZoneInfo(settings.timezone)
    if settings.price_feed_url:
        async with httpx.AsyncClient(timeout=20) as http:
            await price_check.pull_feed(sm, http, settings.price_feed_url)
    await price_check.run_price_check(sm, ctx["notifier"], ctx.get("redis"), tz, datetime.now(tz))


async def steward_tick(ctx: dict) -> None:
    """The data steward: repair what fell through (stuck messages, unread photos, uncategorised spending)."""
    await run_steward(
        ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["store"], ctx["notifier"], ctx.get("redis"),
        tz=ZoneInfo(get_settings().timezone),
    )


async def _sync_youtube(sm: Any, now: datetime) -> None:
    """Refresh the liked videos and follows when a YouTube account is connected; a failure is only logged."""
    import httpx

    from apa import youtube_api

    if not (youtube_api.configured() and await youtube_api.connected(sm)):
        return
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            await youtube_api.sync(sm, http, now.date())
    except Exception as exc:
        log.warning("youtube_sync_failed", error=type(exc).__name__)


async def care_checkin(ctx: dict) -> str:
    """A caring message when the owner's own data suggests it (see apa.care). Silent when nothing stands out."""
    from apa import care

    if not get_settings().care_checkins or ctx.get("redis") is None or ctx.get("notifier") is None:
        return "off"
    tz = ZoneInfo(get_settings().timezone)
    try:
        return await care.run(ctx.get("sessionmaker", SessionLocal), ctx.get("llm"), ctx["redis"], ctx["notifier"], tz, datetime.now(tz))
    except Exception as exc:
        log.warning("care_checkin_failed", error=type(exc).__name__)
        return "failed"


async def companion_chat(ctx: dict) -> str:
    """The assistant starts a conversation now and then (see apa.companion). Quiet unless it has something real to say."""
    from apa import companion

    if not get_settings().companion_chats or ctx.get("redis") is None or ctx.get("notifier") is None or ctx.get("llm") is None:
        return "off"
    tz = ZoneInfo(get_settings().timezone)
    try:
        return await companion.run(ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["redis"], ctx["notifier"], tz, datetime.now(tz))
    except Exception as exc:
        log.warning("companion_failed", error=type(exc).__name__)
        return "failed"


async def memory_digest(ctx: dict) -> str:
    """Each night the assistant writes the day's diary entry from the day's chat (nothing when there was little to say)."""
    from apa import assistant_memory

    tz = ZoneInfo(get_settings().timezone)
    sm = ctx.get("sessionmaker", SessionLocal)
    from apa import profile

    try:
        await profile.refresh(sm, tz)  # patterns from the data: no model involved, so it runs even when the model is down
    except Exception as exc:
        log.warning("profile_refresh_failed", error=type(exc).__name__)
    from apa import retention

    now = datetime.now(tz)
    try:
        written = await assistant_memory.summarize_day(sm, ctx["llm"], now.date(), tz)
    except Exception as exc:  # a model outage skips one night; the dashboard button can fill it in
        log.warning("memory_digest_failed", error=type(exc).__name__)
        written = None
    try:  # days the diary missed are written first, then chat older than the retention window is erased
        await _sync_youtube(sm, now)
        from apa import content

        await content.digest(sm, ctx["llm"], now.date())
        await retention.catch_up(sm, ctx["llm"], tz, now.date())
        await retention.prune(sm, tz, now, get_settings().chat_keep_days)
    except Exception as exc:
        log.warning("chat_retention_failed", error=type(exc).__name__)
    return "written" if written else "skipped"


async def weekly_digest(ctx: dict) -> None:
    """A-12: the week in numbers, pushed Sunday evening without being asked."""
    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    sm = ctx.get("sessionmaker", SessionLocal)
    if text := await digest.weekly(sm, tz, now):
        year, week, _ = now.isocalendar()
        await digest.deliver(sm, ctx["notifier"], ctx.get("redis"), f"weekly:{year}-W{week:02d}", text)


async def reconcile_event(ctx: dict, event_id: int) -> str:
    """A bank statement the owner sent: read it, match it against the ledger, fix what the ledger missed."""
    try:
        return await run_reconcile(
            ctx.get("sessionmaker", SessionLocal), ctx["llm"], ctx["notifier"], ctx["store"], event_id,
            job_try=ctx.get("job_try", 1), max_tries=EXTRACT_MAX_TRIES,
        )
    except LLMUnavailable:
        log.warning("reconcile_llm_unavailable_retrying", event_id=event_id, job_try=ctx.get("job_try", 1))
        raise Retry(defer=EXTRACT_RETRY_DELAY_S) from None


async def weekly_reconciliation(ctx: dict) -> None:
    """A-11, Sunday night: the ledger checks itself (double counts, cash below zero). Silent if clean."""
    await ledger_audit.weekly_audit(
        ctx.get("sessionmaker", SessionLocal), ctx["notifier"], ctx.get("redis"), datetime.now(UTC)
    )


async def monthly_digest(ctx: dict) -> None:
    """arq cron has no 'last day of month', so this fires daily and self-gates."""
    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    if (now + timedelta(days=1)).day != 1:
        return
    sm = ctx.get("sessionmaker", SessionLocal)
    if text := await digest.monthly(sm, tz, now):
        await digest.deliver(sm, ctx["notifier"], ctx.get("redis"), f"monthly:{now:%Y-%m}", text)


async def reminder_sweep(ctx: dict) -> None:
    log.info("reminder_sweep", status="not_implemented")


async def supplement_stock_tick(ctx: dict) -> None:
    log.info("supplement_stock_tick", status="not_implemented")


async def cold_storage_archive(ctx: dict) -> None:
    log.info("cold_storage_archive", status="not_implemented")


async def startup(ctx: dict) -> None:
    configure_logging()
    s = get_settings()
    ctx["llm"] = LLMClient()
    ctx["store"] = DynamicEvidenceStore(s)
    ctx["currency_task"] = currency.start_refresher(SessionLocal)  # keep the home currency fresh
    if s.telegram_bot_token:
        from aiogram import Bot

        ctx["notifier"] = TelegramNotifier(Bot(s.telegram_bot_token), s.telegram_owner_id)
    else:
        ctx["notifier"] = LogNotifier()  # no token: summaries and alerts only reach the log


async def shutdown(ctx: dict) -> None:
    await ctx["llm"].aclose()
    if hasattr(ctx["notifier"], "aclose"):
        await ctx["notifier"].aclose()


_cfg = get_settings()
_PRE_H, _PRE_M = news_jobs.precompute_time(_cfg.briefing_wake_time, _cfg.briefing_lead_minutes)


class WorkerSettings:
    redis_settings = arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_tries = COMMIT_MAX_TRIES
    functions = [
        process_inbound_event, commit_event, extract_event, master_event, chat_event, reconcile_event,
        read_receipt, release_quarantined, briefing_telegram, phone_watch, memory_digest, care_checkin, companion_chat,
    ]
    cron_jobs = [
        cron(sheets_sync, second=0, run_at_startup=False),  # every 60 s
        cron(phone_watch, minute={5, 35}, run_at_startup=False),
        cron(memory_digest, hour=23, minute=55, run_at_startup=False),
        cron(care_checkin, hour={9, 11, 14, 16, 19}, minute=20, run_at_startup=False),
        cron(companion_chat, hour={9, 13, 20}, minute=45, run_at_startup=False),
        cron(steward_tick, minute={3, 13, 23, 33, 43, 53}, run_at_startup=False),  # every 10 min
        cron(cyberramen_poll, minute={0, 10, 20, 30, 40, 50}),
        cron(news_ingest, minute={0, 15, 30, 45}),
        cron(briefing_precompute, hour=_PRE_H, minute=_PRE_M),
        cron(huawei_healthkit_sync, minute={0, 30}),
        cron(health_tick, minute={7, 22, 37, 52}),
        cron(price_tick, hour=8, minute=0),
        cron(weekly_digest, weekday="sun", hour=20, minute=0),
        cron(weekly_reconciliation, weekday="sun", hour=22, minute=0),
        cron(monthly_digest, hour=20, minute=0),
        cron(reminder_sweep, hour=7, minute=0),
        cron(supplement_stock_tick, hour=0, minute=5),
        cron(cold_storage_archive, hour=3, minute=0),
    ]
