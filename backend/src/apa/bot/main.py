"""Telegram bot. Silent by design: only the Arbiter summary or an emergency alert is ever sent.

Plain text is stored as an event and parsed in the worker; the bot itself never replies to it.
/private sessions live in process memory only (never Redis/Postgres/logs).
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import BotCommand, CallbackQuery, Message
from arq import ArqRedis, create_pool

from apa import currency
from apa.bot.commands import handle_command, parse_command
from apa.bot.menu import BOT_COMMANDS, handle_callback, menu_key, parse_action_command, view_for
from apa.bot.photo import handle_photo
from apa.chat import batch_flow
from apa.chat.actions import resolve
from apa.core.db import SessionLocal
from apa.core.logging import configure_logging, get_logger
from apa.core.redis import arq_redis_settings
from apa.core.sanitize import sanitize_untrusted
from apa.core.settings import get_settings
from apa.ingestion.intake import store_and_enqueue
from apa.integrations.evidence_store import DynamicEvidenceStore, EvidenceStore
from apa.integrations.llm import LLMClient
from apa.integrations.notify import buttons_for, keyboard
from apa.private_mode.session import PrivateManager
from apa.reconcile.intake import intake_statement, looks_like_statement

log = get_logger(__name__)
dp = Dispatcher()


def _owner_only(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == get_settings().telegram_owner_id


async def ingest_text(arq: ArqRedis, message: Message) -> int | None:
    """Store a manual text entry as an event. Keyed on (chat, message id) so a redelivered
    Telegram update never creates a second transaction."""
    clean = sanitize_untrusted(message.text or "")
    if message.from_user is not None:  # keep the name Telegram shows for the owner on their card (never overwrites one they set)
        from apa import about

        await _quietly(about.note_name(SessionLocal, message.from_user.first_name))
    async with SessionLocal() as session:
        return await store_and_enqueue(
            session,
            arq,
            source="telegram_shorthand",
            idempotency_key=f"tg:{message.chat.id}:{message.message_id}",
            payload={
                "text": clean.text, "sent_at": message.date.isoformat(),
                **({"reply_to": r.message_id} if (r := getattr(message, "reply_to_message", None)) else {}),
            },
            clean=clean,
        )


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


class AiogramMessenger:
    """The Telegram side of a /private session: send, and delete (the purge)."""

    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> int | None:
        return (await self._bot.send_message(chat_id, text)).message_id

    async def delete(self, chat_id: int, message_ids: list[int]) -> int:
        try:
            await self._bot.delete_messages(chat_id, message_ids)
            return len(message_ids)
        except Exception as e:  # e.g. one message already gone or older than 48 h: fall back to one by one
            log.warning("private_bulk_delete_failed", error=type(e).__name__)
        deleted = 0
        for mid in message_ids:
            try:
                await self._bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception:  # noqa: S110 - nothing more can be done for a message Telegram will not delete
                pass
        return deleted


async def _quietly(coro) -> None:
    """Private-session handlers must never let an exception reach aiogram's error log: it prints the whole
    update, which here would be the owner's private text."""
    try:
        await coro
    except Exception as e:
        log.warning("private_handler_failed", error=type(e).__name__)


async def _diary(message: Message) -> None:
    """`/diary <text>`: one entry in the owner's private journal. It is saved straight to the encrypted store (it never goes to
    the event store, the queue, the chat history or any AI) and the owner's message is deleted from the chat. The reply does
    not repeat the text."""
    from apa import assistant_memory
    from apa.core.secrets import SecretsUnavailable

    text = message.text.strip().split(None, 1)[1].strip() if len(message.text.split(None, 1)) > 1 else ""
    if not text:
        await message.answer("Write /diary followed by your entry. It is saved encrypted, never shown to any AI, and I delete your "
                             "message from this chat. The Diary page in the dashboard is more private still.")
        return
    try:
        await assistant_memory.journal_add(SessionLocal, datetime.now(_tz()).date(), text, shared=False)
    except SecretsUnavailable:
        await message.answer("The diary is off: the server has no SECRETS_KEY, so nothing can be stored safely. Your message was not saved.")
        return
    await _quietly(message.delete())  # Telegram keeps what was typed unless it is deleted
    await message.answer("✅ Saved to your private diary (encrypted, not shared with the AI).")


@dp.message(F.func(_owner_only), F.text.startswith("/"))
async def command(message: Message, arq: ArqRedis, private: PrivateManager) -> None:
    word = message.text.strip().lower().split()[0].split("@")[0]
    if word in ("/private", "/privat"):
        await _quietly(private.start(message.chat.id, message.from_user.id, message.message_id))
        return
    if word in ("/selesai", "/endprivate"):
        private.track(message.chat.id, message.message_id)
        await _quietly(private.end(message.chat.id, "owner"))
        return
    if word in ("/diary", "/jurnal"):
        await _quietly(_diary(message))  # a failure must not reach aiogram's error log: it would print the diary text
        return
    if word in ("/apk", "/unduh_apk"):
        from aiogram.types import FSInputFile

        from apa.gateway.routers.update import _find_local_apk, _root

        local_apk = _find_local_apk(_root())
        if local_apk is not None and local_apk.exists():
            await message.answer_document(
                FSInputFile(str(local_apk), filename="apa-latest.apk"),
                caption="📱 Android APK (apa-latest.apk)\n\n"
                "GitHub: https://github.com/frostfire1/Autonomous-Personal-Assistance/releases/latest",
            )
            return
    if word in ("/exe", "/tracker", "/unduh_exe"):
        from aiogram.types import FSInputFile

        from apa.gateway.routers.update import _find_local_exe, _root

        local_exe = _find_local_exe(_root())
        if local_exe is not None and local_exe.exists():
            await message.answer_document(
                FSInputFile(str(local_exe), filename="APA-Tracker.exe"),
                caption="💻 Windows Desktop Tracker (APA-Tracker.exe)\n\n"
                "GitHub: https://github.com/frostfire1/Autonomous-Personal-Assistance/releases/latest",
            )
            return
    key = menu_key(message.text)
    if key is not None:  # /start /menu /hari_ini ...: an interactive view
        v = await view_for(SessionLocal, key, _tz())
        await message.answer(v.text, reply_markup=keyboard(v.rows) if v.rows else None)
        return
    act = parse_action_command(message.text)
    if act is not None:  # typed /confirm_<nonce> or /cancel_<nonce> on an action card
        reply = await resolve(SessionLocal, arq, act[1], act[0], actor=message.from_user.id, tz=_tz())
        await message.answer(reply)
        return
    parsed = parse_command(message.text)
    if parsed is None:  # an unknown command is ignored: the bot stays silent by design
        return
    reply = await handle_command(SessionLocal, arq, *parsed)
    if reply:
        await message.answer(reply)


@dp.callback_query(F.func(lambda cb: cb.from_user.id == get_settings().telegram_owner_id), F.data)
async def button(cb: CallbackQuery, arq: ArqRedis) -> None:
    v = await handle_callback(SessionLocal, arq, cb.data, _tz(), actor=cb.from_user.id)
    await cb.answer()  # stop the button's spinner either way
    if v is None or cb.message is None:
        return
    markup = keyboard(v.rows) if v.rows else None
    if v.replace:
        await cb.message.edit_text(v.text, reply_markup=markup)
    else:
        await cb.message.edit_reply_markup(reply_markup=None)  # the tap is spent: drop the buttons
        await cb.message.answer(v.text)


@dp.message(F.func(_owner_only), F.document)
async def document_entry(
    message: Message, bot: Bot, arq: ArqRedis, store: EvidenceStore, private: PrivateManager
) -> None:
    """A file. A PDF that is a bank statement is reconciled against the ledger; an image goes the photo way (tried as a receipt, else
    identified); text, CSV, JSON and any other PDF are read and handed to the assistant."""
    import io

    from apa.ingestion.file_intake import extract_text, handle_file, looks_like_bank_statement
    from apa.ingestion.photo_intake import intake_photo

    doc = message.document
    if private.active(message.chat.id) or doc is None:
        return
    mime = doc.mime_type or ""
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await message.answer("File terlalu besar (batas 15 MB).")
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    data = buf.getvalue()
    if mime in ("image/jpeg", "image/png", "image/webp"):
        res = await intake_photo(SessionLocal, arq, store, chat_id=message.chat.id, message_id=message.message_id, data=data, mime=mime,
                                 caption=message.caption, sent_at=message.date, media_group_id=getattr(message, "media_group_id", None))
        if res.status == "duplicate":
            await message.answer("Foto itu sudah pernah dikirim.")
        elif res.status in ("rejected", "too_many"):
            await message.answer(f"Foto tidak bisa diproses: {res.detail}")
        return
    text, unreadable = extract_text(data, doc.file_name or "", mime) if mime == "application/pdf" else ("", None)
    # a PDF is a bank statement when its caption, name or text says so, and also when it has no readable text (a scan): as it was before files were read
    if mime == "application/pdf" and (looks_like_statement(message.caption) or unreadable or looks_like_bank_statement(doc.file_name or "", text)):
        result = await intake_statement(
            SessionLocal, arq, store, chat_id=message.chat.id, message_id=message.message_id, data=data,
            mime="application/pdf", caption=message.caption, sent_at=message.date,
        )
        if result.startswith("rejected"):
            await message.answer(f"Mutasi tidak bisa diproses: {result.removeprefix('rejected: ')}")
        elif result == "duplicate":
            await message.answer("Mutasi ini sudah pernah dikirim.")
        return
    reply = await handle_file(SessionLocal, arq, chat_id=message.chat.id, message_id=message.message_id, name=doc.file_name or "file", mime=mime,
                              data=data, caption=message.caption, sent_at=message.date)
    if reply:
        await message.answer(reply)


@dp.message(F.func(_owner_only), F.photo)
async def photo_entry(
    message: Message, bot: Bot, arq: ArqRedis, store: EvidenceStore, private: PrivateManager
) -> None:
    if private.active(message.chat.id):  # nothing enters the ledger from inside a private session
        return
    if looks_like_statement(message.caption):  # "mutasi bca oktober" with a photo: a statement, not a receipt
        import io

        buf = io.BytesIO()
        await bot.download(message.photo[-1], destination=buf)
        await intake_statement(
            SessionLocal, arq, store, chat_id=message.chat.id, message_id=message.message_id,
            data=buf.getvalue(),
            mime="image/jpeg", caption=message.caption, sent_at=message.date,
        )
        return
    reply = await handle_photo(SessionLocal, arq, store, bot, message)
    if reply:
        await message.answer(reply)


@dp.message(F.func(_owner_only), F.text, ~F.text.startswith("/"))
async def text_entry(message: Message, arq: ArqRedis, private: PrivateManager) -> None:
    if private.active(
        message.chat.id
    ):  # FIRST: private text never reaches ingestion, Redis, logs or the ledger
        await _quietly(
            private.message(message.chat.id, message.from_user.id, message.message_id, message.text)
        )
        return
    # an open draft of payments: ok / batal / "1-5 breakfast" / "hapus 2" are decided by code, instantly
    reply = await batch_flow.handle_text(SessionLocal, arq, message.text, _tz(), actor=message.from_user.id)
    if reply is not None:
        rows = buttons_for(reply)
        await message.answer(reply, reply_markup=keyboard(rows) if rows else None)
        return
    try:  # the answer takes a few seconds: show that something is happening
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception as e:  # cosmetic; never block the entry on it
        log.info("typing_indicator_failed", error=str(e))
    event_id = await ingest_text(arq, message)
    log.info("telegram_text", event_id=event_id, duplicate=event_id is None)


async def main() -> None:
    configure_logging()
    s = get_settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    arq = await create_pool(arq_redis_settings())
    currency_task = currency.start_refresher(SessionLocal)  # keep the home currency fresh
    bot = Bot(s.telegram_bot_token)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    private = PrivateManager(
        LLMClient(), AiogramMessenger(bot), SessionLocal, owner_id=s.telegram_owner_id, tz=_tz()
    )
    try:
        # `arq`, `store` and `private` are injected into handlers by parameter name
        await dp.start_polling(bot, arq=arq, store=DynamicEvidenceStore(), private=private)
    finally:
        currency_task.cancel()
        await private.end_all(
            "shutdown"
        )  # a stopping bot purges the open session instead of leaving it behind


if __name__ == "__main__":
    asyncio.run(main())
