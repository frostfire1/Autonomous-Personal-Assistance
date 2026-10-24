"""Shared intake: store a sanitized InboundEvent idempotently and enqueue it.

Used by the Android HTTP endpoint and the Telegram bot so both get identical dedupe,
sanitization and queueing behaviour.
"""

from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from apa.core.sanitize import Sanitized
from apa.db.models.ops import InboundEvent


class Enqueuer(Protocol):
    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any: ...


async def store_and_enqueue(
    session: AsyncSession,
    arq: Enqueuer,
    *,
    source: str,
    idempotency_key: str,
    payload: dict[str, Any],
    clean: Sanitized,
    job: str = "process_inbound_event",
) -> int | None:
    """Return the new event id, or None when `idempotency_key` was already stored (replay).

    A replay is never re-enqueued. The commit happens before the enqueue so the worker can
    never receive an id that is not yet visible.
    """
    stmt = (
        insert(InboundEvent)
        .values(
            idempotency_key=idempotency_key,
            source=source,
            payload=payload,
            injection_flags=clean.injection_flags,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(InboundEvent.id)
    )
    event_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    if event_id is not None:
        await arq.enqueue_job(job, event_id)
    return event_id
