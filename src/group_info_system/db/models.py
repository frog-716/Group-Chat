from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ChatRow(Base):
    __tablename__ = "chats"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_chats_provider_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionRunRow(Base):
    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    chat_external_id: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    requested_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128))


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "platform_message_id", name="uq_messages_chat_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"))
    platform_message_id: Mapped[str] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    chat: Mapped[ChatRow] = relationship()
    versions: Mapped[list[MessageVersionRow]] = relationship(back_populates="message")


class MessageVersionRow(Base):
    __tablename__ = "message_versions"
    __table_args__ = (
        UniqueConstraint("message_id", "payload_sha256", name="uq_message_versions_payload"),
        Index("idx_message_versions_sent_at", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    collection_run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id"))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    evidence_id: Mapped[str] = mapped_column(String(32), unique=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sender_id: Mapped[str] = mapped_column(String(255))
    sender_name: Mapped[str] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    message: Mapped[MessageRow] = relationship(back_populates="versions")


class SyncCursorRow(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("provider", "chat_external_id", name="uq_sync_cursors_provider_chat"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    chat_external_id: Mapped[str] = mapped_column(String(255))
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisRunRow(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    analyzer: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(128))
    schema_version: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    structured_output: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisInputRow(Base):
    __tablename__ = "analysis_inputs"
    __table_args__ = (
        UniqueConstraint(
            "analysis_run_id", "message_version_id", name="uq_analysis_inputs_run_version"
        ),
        UniqueConstraint("analysis_run_id", "ordinal", name="uq_analysis_inputs_run_ordinal"),
        Index("idx_analysis_inputs_message_version", "message_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    message_version_id: Mapped[int] = mapped_column(ForeignKey("message_versions.id"))
    ordinal: Mapped[int] = mapped_column(Integer)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("idx_events_analysis_run", "analysis_run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    event_type: Mapped[str] = mapped_column(String(32), server_default="information")
    report_section: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    analysis_kind: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_range_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    time_range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventEvidenceRow(Base):
    __tablename__ = "event_evidence"
    __table_args__ = (
        UniqueConstraint("event_id", "message_version_id", name="uq_event_evidence_version"),
        UniqueConstraint("event_id", "ordinal", name="uq_event_evidence_ordinal"),
        Index("idx_event_evidence_message_version", "message_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    message_version_id: Mapped[int] = mapped_column(ForeignKey("message_versions.id"))
    ordinal: Mapped[int] = mapped_column(Integer)


class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    report_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
