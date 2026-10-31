"""Devices management, real-time device status, and Ingest Key rotation."""

import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from apa import auth_keys, usage
from apa.core.db import SessionLocal
from apa.core.logging import get_logger
from apa.core.settings import get_settings
from apa.db.models.intel import AppUsage
from apa.gateway.routers.ingest import require_ingest_key

log = get_logger(__name__)
router = APIRouter(prefix="/devices", tags=["devices"], dependencies=[Depends(require_ingest_key)])


def _format_relative(seconds_ago: float) -> str:
    """Format seconds ago into human readable relative time."""
    if seconds_ago < 60:
        return "just now"
    mins = int(seconds_ago // 60)
    if mins < 60:
        return f"{mins}m ago"
    hrs = int(mins // 60)
    if hrs < 24:
        return f"{hrs}h ago"
    days = int(hrs // 24)
    return f"{days}d ago"


def _format_duration(seconds: int) -> str:
    """Format seconds into '2h 15m' or '45m'."""
    if seconds <= 0:
        return "0m"
    hrs = seconds // 3600
    rem_mins = (seconds % 3600) // 60
    if hrs > 0:
        return f"{hrs}h {rem_mins}m"
    return f"{rem_mins}m"


class RotateKeyRequest(BaseModel):
    new_key: str | None = Field(
        default=None,
        description="Optional custom key; if omitted, a secure 256-bit key is generated",
    )


@router.get("")
async def list_devices(request: Request) -> dict[str, Any]:
    """Retrieve full device list, online status, today's active screen times, and ingest key state."""
    tz = ZoneInfo(get_settings().timezone)
    now = datetime.now(tz)
    today = now.date()

    redis = getattr(request.app.state, "arq", None) if hasattr(request.app, "state") else None
    sm = (
        getattr(request.app.state, "sessionmaker", SessionLocal)
        if hasattr(request.app, "state")
        else SessionLocal
    )

    # 1. Auth Key State
    key_cfg = await auth_keys.current(sm)
    active_k = key_cfg.active_key or get_settings().ingest_api_key
    prev_k = key_cfg.previous_key
    is_leaked = active_k in auth_keys.LEAKED_SAMPLE_KEYS

    key_status = {
        "active_key": active_k,
        "active_key_masked": auth_keys.mask_key(active_k),
        "previous_key": prev_k,
        "previous_key_masked": auth_keys.mask_key(prev_k) if prev_k else "",
        "dual_acceptance": key_cfg.dual_acceptance and bool(prev_k),
        "rotated_at": key_cfg.rotated_at.isoformat() if key_cfg.rotated_at else None,
        "is_leaked_sample": is_leaked,
    }

    # 2. Query App Usage from DB for recent device activity
    device_rows = []
    today_rows = []
    try:
        async with sm() as s:
            device_rows = (
                await s.execute(
                    select(AppUsage.device, AppUsage.kind, func.max(AppUsage.day)).group_by(
                        AppUsage.device, AppUsage.kind
                    )
                )
            ).all()

            today_rows = (
                await s.execute(
                    select(AppUsage.device, AppUsage.app, AppUsage.seconds).where(AppUsage.day == today)
                )
            ).all()
    except Exception as e:
        log.warning("devices_query_failed", error=str(e))

    today_device_seconds: dict[str, int] = defaultdict(int)
    today_device_apps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    overall_apps: dict[str, int] = defaultdict(int)

    for dev, app_name, sec in today_rows:
        today_device_seconds[dev] += sec
        today_device_apps[dev].append({
            "app": app_name,
            "seconds": sec,
            "category": usage.category(app_name),
            "formatted": _format_duration(sec),
        })
        overall_apps[app_name] += sec

    for dev in today_device_apps:
        today_device_apps[dev].sort(key=lambda x: -x["seconds"])

    # 3. Read Phone Heartbeat from Redis
    phone_hb = None
    if redis is not None:
        try:
            raw_hb = await redis.get("apa:phone:heartbeat")
            if raw_hb:
                phone_hb = json.loads(raw_hb)
        except Exception as e:
            log.warning("phone_hb_read_error", error=str(e))

    now_epoch = time.time()
    devices_list: list[dict[str, Any]] = []
    seen_device_names: set[str] = set()

    # Android Phone Device Entry
    if phone_hb:
        ts = float(phone_hb.get("ts", 0))
        sec_ago = max(0.0, now_epoch - ts)
        if sec_ago < 900:  # 15 mins
            st = "online"
        elif sec_ago < 7200:  # 2 hours
            st = "idle"
        else:
            st = "offline"

        android_dev_name = next(
            (d for d, k, _ in device_rows if k == "android"),
            "Android Phone",
        )
        seen_device_names.add(android_dev_name)
        today_s = today_device_seconds.get(android_dev_name, 0)
        friendly_phone_name = (
            "Android Phone"
            if android_dev_name.startswith("2302") or android_dev_name == "Android Phone"
            else android_dev_name
        )

        devices_list.append({
            "id": android_dev_name,
            "name": friendly_phone_name,
            "raw_id": android_dev_name,
            "kind": "android",
            "status": st,
            "listener_connected": bool(phone_hb.get("listener", True)),
            "pending_notifications": int(phone_hb.get("pending", 0)),
            "version": str(phone_hb.get("version", "1.0.0")),
            "last_seen_ts": ts,
            "last_seen_iso": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
            "last_seen_relative": _format_relative(sec_ago),
            "today_screen_time_s": today_s,
            "today_screen_time_formatted": _format_duration(today_s),
            "top_apps": today_device_apps.get(android_dev_name, [])[:6],
        })

    # Desktop and other devices
    for dev_name, kind, last_day in device_rows:
        if dev_name in seen_device_names:
            continue
        seen_device_names.add(dev_name)

        tracker_hb = None
        if redis is not None:
            try:
                raw_tb = await redis.get(f"apa:device:heartbeat:{dev_name}")
                if raw_tb:
                    tracker_hb = json.loads(raw_tb)
            except Exception:
                pass

        today_s = today_device_seconds.get(dev_name, 0)
        if tracker_hb:
            t_ts = float(tracker_hb.get("ts", 0))
            sec_ago = max(0.0, now_epoch - t_ts)
            st = "online" if sec_ago < 900 else ("idle" if sec_ago < 7200 else "offline")
            last_ts = t_ts
            last_iso = datetime.fromtimestamp(t_ts, tz=UTC).isoformat()
            last_rel = _format_relative(sec_ago)
        elif today_s > 0:
            st = "online"
            last_ts = now_epoch
            last_iso = now.isoformat()
            last_rel = "active today"
        elif last_day == today:
            st = "idle"
            last_ts = now_epoch - 3600
            last_iso = now.isoformat()
            last_rel = "earlier today"
        else:
            st = "offline"
            last_ts = 0
            last_iso = last_day.isoformat() if last_day else ""
            days_ago = (today - last_day).days if last_day else 99
            last_rel = f"{days_ago}d ago"

        devices_list.append({
            "id": dev_name,
            "name": dev_name,
            "raw_id": dev_name,
            "kind": kind or "desktop",
            "status": st,
            "listener_connected": None,
            "pending_notifications": 0,
            "version": tracker_hb.get("version", "1.0.0") if tracker_hb else "1.0.0",
            "last_seen_ts": last_ts,
            "last_seen_iso": last_iso,
            "last_seen_relative": last_rel,
            "today_screen_time_s": today_s,
            "today_screen_time_formatted": _format_duration(today_s),
            "top_apps": today_device_apps.get(dev_name, [])[:6],
        })

    # Summary
    total_today_sec = sum(today_device_seconds.values())
    online_count = sum(1 for d in devices_list if d["status"] == "online")

    top_apps_sorted = sorted(
        [
            {
                "app": a,
                "seconds": s,
                "category": usage.category(a),
                "formatted": _format_duration(s),
            }
            for a, s in overall_apps.items()
        ],
        key=lambda x: -x["seconds"],
    )[:10]

    return {
        "summary": {
            "total_devices": len(devices_list),
            "online_devices": online_count,
            "total_screen_time_today_s": total_today_sec,
            "total_screen_time_today_formatted": _format_duration(total_today_sec),
            "dual_key_active": key_status["dual_acceptance"],
        },
        "key_status": key_status,
        "devices": devices_list,
        "top_apps_today": top_apps_sorted,
    }


@router.post("/rotate-key")
async def rotate_ingest_key(body: RotateKeyRequest, request: Request) -> dict[str, Any]:
    """Rotate the system Ingest Key. The current key is retained as accepted fallback during grace period."""
    sm = (
        getattr(request.app.state, "sessionmaker", SessionLocal)
        if hasattr(request.app, "state")
        else SessionLocal
    )
    new_cfg = await auth_keys.rotate_key(body.new_key, sm)

    return {
        "ok": True,
        "message": (
            "Ingest key rotated successfully. Dual-key acceptance is ON "
            "(previous key remains accepted until revoked)."
        ),
        "active_key": new_cfg.active_key,
        "previous_key": new_cfg.previous_key,
        "dual_acceptance": new_cfg.dual_acceptance,
        "rotated_at": new_cfg.rotated_at.isoformat() if new_cfg.rotated_at else None,
    }


@router.post("/revoke-previous-key")
async def revoke_previous_ingest_key(request: Request) -> dict[str, Any]:
    """Revoke the previous accepted key. From now on, only the active key is accepted."""
    sm = (
        getattr(request.app.state, "sessionmaker", SessionLocal)
        if hasattr(request.app, "state")
        else SessionLocal
    )
    new_cfg = await auth_keys.revoke_previous_key(sm)

    return {
        "ok": True,
        "message": "Previous key revoked. Only the new active key will be accepted.",
        "active_key": new_cfg.active_key,
        "previous_key": "",
        "dual_acceptance": False,
    }
