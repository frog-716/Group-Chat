from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from group_info_system.collectors.base import Collector
from group_info_system.db.models import CollectionRunRow
from group_info_system.db.repositories import MessageRepository
from group_info_system.domain.messages import CollectionBatch

WINDOW_ALREADY_SYNCED = "WINDOW_ALREADY_SYNCED"


@dataclass(frozen=True)
class CollectionOutcome:
    collection_run_id: int
    batch: CollectionBatch
    inserted_count: int
    reason_code: str | None = None


def collect_into_store(
    *,
    session: Session,
    collector: Collector,
    chat_id: str,
    since: datetime,
    until: datetime,
) -> CollectionBatch:
    return collect_into_store_with_outcome(
        session=session,
        collector=collector,
        chat_id=chat_id,
        since=since,
        until=until,
    ).batch


def collect_into_store_with_outcome(
    *,
    session: Session,
    collector: Collector,
    chat_id: str,
    since: datetime,
    until: datetime,
) -> CollectionOutcome:
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("since and until must be timezone-aware")
    if since >= until:
        raise ValueError("since must be earlier than until")

    repository = MessageRepository(session)
    cursor = repository.get_cursor(collector.provider, chat_id)
    run = repository.start_run(collector.provider, chat_id, since, until)
    run_id = run.id
    session.commit()
    try:
        if cursor is not None and cursor.watermark >= until:
            batch = CollectionBatch(
                provider=collector.provider,
                chat_id=chat_id,
                messages=(),
                next_cursor=cursor,
                mode="incremental",
            )
            repository.finish_run(
                run,
                fetched=0,
                inserted=0,
                reason_code=WINDOW_ALREADY_SYNCED,
            )
            session.commit()
            return CollectionOutcome(
                collection_run_id=run_id,
                batch=batch,
                inserted_count=0,
                reason_code=WINDOW_ALREADY_SYNCED,
            )

        batch = collector.fetch(
            chat_id=chat_id,
            since=since,
            until=until,
            cursor=cursor,
        )
        inserted = repository.ingest(batch.messages, run)
        if batch.next_cursor:
            repository.save_cursor(batch.next_cursor)
        repository.finish_run(run, fetched=len(batch.messages), inserted=inserted)
        session.commit()
        return CollectionOutcome(
            collection_run_id=run_id,
            batch=batch,
            inserted_count=inserted,
        )
    except Exception as exc:
        session.rollback()
        failed_run = session.get(CollectionRunRow, run_id)
        if failed_run is not None:
            repository.fail_run(failed_run, type(exc).__name__)
            session.commit()
        exc.__dict__["run_id"] = run_id
        raise
