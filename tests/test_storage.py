from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from group_info_system.db.models import MessageVersionRow
from group_info_system.db.repositories import MessageRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.messages import RawMessage


def raw(content: str = "原始正文") -> RawMessage:
    return RawMessage(
        provider="simulated",
        chat_id="chat-1",
        platform_message_id="message-1",
        sent_at=datetime(2026, 9, 10, 1, tzinfo=UTC),
        sender_id="member-1",
        sender_name="成员一",
        content=content,
        raw_payload={"message_id": "message-1", "content": content},
    )


def test_identical_payload_is_idempotent_and_edit_adds_version(migrated_database: str) -> None:
    sessions = session_factory(migrated_database)
    with sessions() as session:
        repository = MessageRepository(session)
        run = repository.start_run("simulated", "chat-1", None, None)
        assert repository.ingest((raw(),), run) == 1
        assert repository.ingest((raw(),), run) == 0
        assert repository.ingest((raw("编辑后的正文"),), run) == 1
        repository.finish_run(run, fetched=3, inserted=2)
        session.commit()
        assert session.scalar(select(func.count()).select_from(MessageVersionRow)) == 2
        evidence_ids = session.scalars(
            select(MessageVersionRow.evidence_id).order_by(MessageVersionRow.id)
        ).all()
        assert len(set(evidence_ids)) == 2
        assert all(value.startswith("msgv_") for value in evidence_ids)


def test_raw_versions_cannot_be_updated_or_deleted(migrated_database: str) -> None:
    sessions = session_factory(migrated_database)
    with sessions() as session:
        repository = MessageRepository(session)
        run = repository.start_run("simulated", "chat-1", None, None)
        repository.ingest((raw(),), run)
        repository.finish_run(run, fetched=1, inserted=1)
        session.commit()
        with pytest.raises(Exception, match="append-only"):
            session.execute(text("UPDATE message_versions SET content='changed'"))
            session.commit()
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(text("DELETE FROM message_versions"))
            session.commit()
