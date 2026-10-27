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


def split_telegram_text(text: str, max_chars: int = 4000) -> list[str]:
    """Split text into chunks of at most max_chars without cutting mid-sentence or mid-paragraph."""
    if not text or len(text) <= max_chars:
        return [text] if text else [""]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    cur = ""
    for para in paragraphs:
        if len(para) > max_chars:
            lines = para.split("\n")
            for line in lines:
                if len(line) > max_chars:
                    words = line.split(" ")
                    for w in words:
                        candidate = f"{cur} {w}".strip() if cur else w
                        if len(candidate) > max_chars and cur:
                            chunks.append(cur)
                            cur = w
                        else:
                            cur = candidate
                else:
                    candidate = f"{cur}\n{line}".strip() if cur else line
                    if len(candidate) > max_chars and cur:
                        chunks.append(cur)
                        cur = line
                    else:
                        cur = candidate
        else:
            candidate = f"{cur}\n\n{para}".strip() if cur else para
            if len(candidate) > max_chars and cur:
                chunks.append(cur)
                cur = para
            else:
                cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [text]


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
        from apa.core.settings import get_settings

        split_enabled = getattr(get_settings(), "chat_split_long_messages", True)
        chunks = split_telegram_text(text, max_chars=4000) if split_enabled else [text]
        last_id = None
        for i, chunk in enumerate(chunks):
            # Only attach inline buttons/keyboard to the final message chunk
            chunk_markup = markup if i == len(chunks) - 1 else None
            message = await self._bot.send_message(self._chat_id, chunk, reply_markup=chunk_markup)
            last_id = message.message_id
        return last_id

    async def aclose(self) -> None:
        await self._bot.session.close()
