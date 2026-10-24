"""Outbound Telegram. Only two kinds of message may ever be sent (blueprint 4.2c):
the Arbiter's one-line summary per transaction, and emergency alerts. Debate content never."""

import re
from typing import Protocol

from apa.core.logging import get_logger

log = get_logger(__name__)

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)

_LINK = re.compile(r"/(undo|release|dismiss)_([0-9a-f]{6})")
_ACT = re.compile(r"/(confirm|cancel)_([0-9a-f]{12})")


def buttons_for(text: str) -> Buttons:
    """Tap targets for the command links a message already carries, so the text itself is unchanged:
    the summary's /undo_<token>, a held transaction's /release_ and /dismiss_, a lockdown's /unlock."""
    found = dict(_LINK.findall(text))
    row = []
    if "undo" in found:
        row.append(("↩️ Undo", f"c:undo:{found['undo']}"))
    if "release" in found:
        row.append(("✅ Lepas", f"c:release:{found['release']}"))
    if "dismiss" in found:
        row.append(("🗑️ Buang", f"c:dismiss:{found['dismiss']}"))
    rows = [row] if row else []
    act = dict(_ACT.findall(text))
    if "confirm" in act:  # an action card: the owner must confirm before the assistant changes anything
        cancel = act.get("cancel", act["confirm"])
        rows.append([("✅ Ya, lakukan", f"a:ok:{act['confirm']}"), ("❌ Batal", f"a:no:{cancel}")])
    if "/unlock" in text:
        rows.append([("🔓 Cabut lockdown", "m:unlock")])
    return rows


def keyboard(rows: Buttons):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


class Notifier(Protocol):
    async def send(self, text: str) -> int | None:
        """Send to the owner. Returns the Telegram message id, or None if nothing was sent."""
        ...


class LogNotifier:
    """Used when no bot token is configured: messages go to the log instead."""

    async def send(self, text: str) -> int | None:
        log.info("notify_not_sent", text=text)
        return None


class TelegramNotifier:
    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str) -> int | None:
        from apa import gif

        text, animation = gif.split(text)  # a reply may end with [[gif:URL]]: sent as an animation with the words as its caption
        rows = buttons_for(text)
        markup = keyboard(rows) if rows else None
        if animation:
            try:
                message = await self._bot.send_animation(self._chat_id, animation, caption=text[:1000] or None, reply_markup=markup)
                return message.message_id
            except Exception as e:  # the link could not be fetched or shown: the words and the link still go out
                log.warning("gif_send_failed", error=type(e).__name__)
                text = f"{text}\n{animation}".strip()
        message = await self._bot.send_message(self._chat_id, text, reply_markup=markup)
        return message.message_id

    async def aclose(self) -> None:
        await self._bot.session.close()
