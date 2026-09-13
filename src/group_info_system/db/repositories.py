from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from group_info_system.domain.analysis import EventAnalysisResult
from group_info_system.domain.messages import RawMessage, SyncCursor

from .models import (
    AnalysisInputRow,
    AnalysisRunRow,
    ChatRow,
    CollectionRunRow,
    EventEvidenceRow,
    EventRow,
    MessageRow,
    MessageVersionRow,
    ReportRow,
    SyncCursorRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def db_datetime_as_utc(value: datetime) -> datetime:
    """Restore UTC tzinfo lost by SQLite's timezone-naive datetime storage."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_payload(message: RawMessage) -> str:
    return json.dumps(
        message.raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True)
class StoredMessage:
    version_id: int
    evidence_id: str
    provider: str
    chat_id: str
    chat_name: str
    platform_message_id: str
    sent_at: datetime
    sender_name: str
    content: str
    message_type: str
    is_system: bool
    is_deleted: bool


@dataclass(frozen=True)
class StoredEvent:
    id: int
    event_type: str
    report_section: str
    title: str
    summary: str
    analysis_kind: str
    evidence_ids: tuple[str, ...]


class MessageRepository:
    def __init__(self, session: Session):
        self.session = session

    def start_run(
        self,
        provider: str,
        chat_id: str,
        since: datetime | None,
        until: datetime | None,
        execution_scope_id: int | None = None,
    ) -> CollectionRunRow:
        row = CollectionRunRow(
            provider=provider,
            chat_external_id=chat_id,
            requested_since=since,
            requested_until=until,
            status="running",
            execution_scope_id=execution_scope_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_run(
        self,
        run: CollectionRunRow,
        *,
        fetched: int,
        inserted: int,
        reason_code: str | None = None,
    ) -> None:
        run.status = "succeeded"
        run.finished_at = utcnow()
        run.fetched_count = fetched
        run.inserted_count = inserted
        run.error_code = reason_code[:128] if reason_code else None

    def fail_run(self, run: CollectionRunRow, error_code: str) -> None:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_code = error_code[:128]

    def ingest(self, messages: tuple[RawMessage, ...], run: CollectionRunRow) -> int:
        inserted = 0
        for raw in messages:
            chat = self.session.scalar(
                select(ChatRow).where(
                    ChatRow.provider == raw.provider,
                    ChatRow.external_id == raw.chat_id,
                )
            )
            if chat is None:
                chat = ChatRow(
                    provider=raw.provider,
                    external_id=raw.chat_id,
                    display_name=raw.chat_name or raw.chat_id,
                )
                self.session.add(chat)
                self.session.flush()
            elif raw.chat_name and chat.display_name == raw.chat_id:
                chat.display_name = raw.chat_name

            message = self.session.scalar(
                select(MessageRow).where(
                    MessageRow.chat_id == chat.id,
                    MessageRow.platform_message_id == raw.platform_message_id,
                )
            )
            if message is None:
                message = MessageRow(chat_id=chat.id, platform_message_id=raw.platform_message_id)
                self.session.add(message)
                self.session.flush()

            payload = canonical_payload(raw)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            exists = self.session.scalar(
                select(MessageVersionRow.id).where(
                    MessageVersionRow.message_id == message.id,
                    MessageVersionRow.payload_sha256 == digest,
                )
            )
            if exists is not None:
                continue
            evidence_material = (
                f"{raw.provider}\n{raw.chat_id}\n{raw.platform_message_id}\n{digest}"
            ).encode()
            evidence_id = "msgv_" + hashlib.sha256(evidence_material).hexdigest()[:20]
            self.session.add(
                MessageVersionRow(
                    message_id=message.id,
                    collection_run_id=run.id,
                    payload_sha256=digest,
                    evidence_id=evidence_id,
                    sent_at=raw.sent_at,
                    sender_id=raw.sender_id,
                    sender_name=raw.sender_name,
                    message_type=raw.message_type,
                    content=raw.content,
                    is_system=raw.is_system,
                    is_deleted=raw.is_deleted,
                    raw_payload=payload,
                )
            )
            inserted += 1
        self.session.flush()
        return inserted

    def current_reportable(self, start: datetime, end: datetime) -> list[StoredMessage]:
        return self._current_reportable_rows(start, end)

    def current_reportable_for_scope(
        self,
        start: datetime,
        end: datetime,
        scope_groups: tuple[tuple[str, str], ...],
    ) -> list[StoredMessage]:
        if not scope_groups:
            return []
        return self._current_reportable_rows(start, end, scope_groups)

    def _current_reportable_rows(
        self,
        start: datetime,
        end: datetime,
        scope_groups: tuple[tuple[str, str], ...] | None = None,
    ) -> list[StoredMessage]:
        latest = (
            select(MessageVersionRow.message_id, func.max(MessageVersionRow.id).label("version_id"))
            .group_by(MessageVersionRow.message_id)
            .subquery()
        )
        statement = (
            select(MessageVersionRow, MessageRow, ChatRow)
            .join(latest, MessageVersionRow.id == latest.c.version_id)
            .join(MessageRow, MessageVersionRow.message_id == MessageRow.id)
            .join(ChatRow, MessageRow.chat_id == ChatRow.id)
            .where(
                MessageVersionRow.sent_at >= start,
                MessageVersionRow.sent_at < end,
                MessageVersionRow.is_system.is_(False),
                MessageVersionRow.is_deleted.is_(False),
            )
            .order_by(MessageVersionRow.sent_at, MessageVersionRow.id)
        )
        if scope_groups is not None:
            predicates = [
                (ChatRow.provider == provider) & (ChatRow.external_id == external_id)
                for provider, external_id in scope_groups
            ]
            statement = statement.where(
                or_(*predicates)
            )
        rows = self.session.execute(statement).all()
        return [
            StoredMessage(
                version_id=version.id,
                evidence_id=version.evidence_id,
                provider=chat.provider,
                chat_id=chat.external_id,
                chat_name=chat.display_name,
                platform_message_id=message.platform_message_id,
                sent_at=db_datetime_as_utc(version.sent_at),
                sender_name=version.sender_name,
                content=version.content,
                message_type=version.message_type,
                is_system=version.is_system,
                is_deleted=version.is_deleted,
            )
            for version, message, chat in rows
        ]

    def get_cursor(self, provider: str, chat_id: str) -> SyncCursor | None:
        row = self.session.scalar(
            select(SyncCursorRow).where(
                SyncCursorRow.provider == provider,
                SyncCursorRow.chat_external_id == chat_id,
            )
        )
        if row is None:
            return None
        return SyncCursor(
            provider=provider,
            chat_id=chat_id,
            watermark=db_datetime_as_utc(row.watermark),
        )

    def save_cursor(self, cursor: SyncCursor) -> None:
        row = self.session.scalar(
            select(SyncCursorRow).where(
                SyncCursorRow.provider == cursor.provider,
                SyncCursorRow.chat_external_id == cursor.chat_id,
            )
        )
        if row is None:
            self.session.add(
                SyncCursorRow(
                    provider=cursor.provider,
                    chat_external_id=cursor.chat_id,
                    watermark=cursor.watermark,
                )
            )
        elif cursor.watermark > db_datetime_as_utc(row.watermark):
            row.watermark = cursor.watermark
            row.updated_at = utcnow()


class ReportRepository:
    def __init__(self, session: Session):
        self.session = session

    def save_analysis(
        self,
        *,
        analyzer: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        input_hash: str,
        window_start: datetime,
        window_end: datetime,
        structured_output: str,
    ) -> AnalysisRunRow:
        row = AnalysisRunRow(
            analyzer=analyzer,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            input_hash=input_hash,
            window_start=window_start,
            window_end=window_end,
            status="succeeded",
            structured_output=structured_output,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def start_analysis(
        self,
        *,
        analyzer: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        input_hash: str,
        window_start: datetime,
        window_end: datetime,
        execution_scope_id: int | None = None,
    ) -> AnalysisRunRow:
        row = AnalysisRunRow(
            analyzer=analyzer,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            input_hash=input_hash,
            window_start=window_start,
            window_end=window_end,
            status="running",
            structured_output=None,
            execution_scope_id=execution_scope_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_analysis(self, row: AnalysisRunRow, *, structured_output: str) -> None:
        row.status = "succeeded"
        row.structured_output = structured_output

    def fail_analysis(self, row: AnalysisRunRow) -> None:
        row.status = "failed"
        row.structured_output = None

    def save_analysis_inputs(
        self, *, analysis_run_id: int, messages: list[StoredMessage]
    ) -> None:
        for ordinal, message in enumerate(messages):
            self.session.add(
                AnalysisInputRow(
                    analysis_run_id=analysis_run_id,
                    message_version_id=message.version_id,
                    ordinal=ordinal,
                )
            )
        self.session.flush()

    def save_events(
        self,
        *,
        analysis_run_id: int,
        analysis: EventAnalysisResult,
        messages: list[StoredMessage],
    ) -> list[StoredEvent]:
        by_version = {message.version_id: message for message in messages}
        stored: list[StoredEvent] = []
        for candidate in analysis.events:
            evidence = [by_version[value] for value in candidate.message_version_ids]
            row = EventRow(
                analysis_run_id=analysis_run_id,
                event_type=candidate.event_type,
                report_section=candidate.report_section,
                title=candidate.title,
                summary=candidate.summary,
                analysis_kind=candidate.analysis_kind,
                event_time=candidate.event_time,
                time_range_start=(candidate.time_range.start if candidate.time_range else None),
                time_range_end=(candidate.time_range.end if candidate.time_range else None),
            )
            self.session.add(row)
            self.session.flush()
            for ordinal, message in enumerate(evidence):
                self.session.add(
                    EventEvidenceRow(
                        event_id=row.id,
                        message_version_id=message.version_id,
                        ordinal=ordinal,
                    )
                )
            stored.append(
                StoredEvent(
                    id=row.id,
                    event_type=row.event_type,
                    report_section=row.report_section,
                    title=row.title,
                    summary=row.summary,
                    analysis_kind=row.analysis_kind,
                    evidence_ids=tuple(message.evidence_id for message in evidence),
                )
            )
        self.session.flush()
        return stored

    def save_report(
        self,
        *,
        analysis_run_id: int,
        window_start: datetime,
        window_end: datetime,
        report_json: str,
    ) -> ReportRow:
        row = ReportRow(
            analysis_run_id=analysis_run_id,
            window_start=window_start,
            window_end=window_end,
            status="succeeded",
            report_json=report_json,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def start_report(
        self,
        *,
        analysis_run_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> ReportRow:
        row = ReportRow(
            analysis_run_id=analysis_run_id,
            window_start=window_start,
            window_end=window_end,
            status="running",
            report_json="",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish_report(self, row: ReportRow, *, report_json: str) -> None:
        row.status = "succeeded"
        row.report_json = report_json

    def fail_report(self, row: ReportRow) -> None:
        row.status = "failed"
        row.report_json = ""

    def get_analysis_run(self, analysis_run_id: int) -> AnalysisRunRow | None:
        return self.session.get(AnalysisRunRow, analysis_run_id)

    def analysis_messages(self, analysis_run_id: int) -> list[StoredMessage]:
        rows = self.session.execute(
            select(AnalysisInputRow, MessageVersionRow, MessageRow, ChatRow)
            .join(
                MessageVersionRow,
                AnalysisInputRow.message_version_id == MessageVersionRow.id,
            )
            .join(MessageRow, MessageVersionRow.message_id == MessageRow.id)
            .join(ChatRow, MessageRow.chat_id == ChatRow.id)
            .where(AnalysisInputRow.analysis_run_id == analysis_run_id)
            .order_by(AnalysisInputRow.ordinal)
        ).all()
        return [
            StoredMessage(
                version_id=version.id,
                evidence_id=version.evidence_id,
                provider=chat.provider,
                chat_id=chat.external_id,
                chat_name=chat.display_name,
                platform_message_id=message.platform_message_id,
                sent_at=db_datetime_as_utc(version.sent_at),
                sender_name=version.sender_name,
                content=version.content,
                message_type=version.message_type,
                is_system=version.is_system,
                is_deleted=version.is_deleted,
            )
            for _input, version, message, chat in rows
        ]

    def analysis_events(self, analysis_run_id: int) -> list[StoredEvent]:
        event_rows = self.session.scalars(
            select(EventRow)
            .where(EventRow.analysis_run_id == analysis_run_id)
            .order_by(EventRow.id)
        ).all()
        stored: list[StoredEvent] = []
        for event in event_rows:
            evidence_ids = tuple(
                self.session.scalars(
                    select(MessageVersionRow.evidence_id)
                    .join(
                        EventEvidenceRow,
                        EventEvidenceRow.message_version_id == MessageVersionRow.id,
                    )
                    .where(EventEvidenceRow.event_id == event.id)
                    .order_by(EventEvidenceRow.ordinal)
                ).all()
            )
            stored.append(
                StoredEvent(
                    id=event.id,
                    event_type=event.event_type,
                    report_section=event.report_section,
                    title=event.title,
                    summary=event.summary,
                    analysis_kind=event.analysis_kind,
                    evidence_ids=evidence_ids,
                )
            )
        return stored

    def latest(self) -> ReportRow | None:
        return self.session.scalar(
            select(ReportRow)
            .where(ReportRow.status == "succeeded")
            .order_by(ReportRow.id.desc())
            .limit(1)
        )


class RunNotFoundError(LookupError):
    error_code = "RUN_NOT_FOUND"


def _run_datetime_json(value: datetime | None) -> str | None:
    return db_datetime_as_utc(value).isoformat() if value is not None else None


def _collection_run_columns() -> tuple:
    return (
        CollectionRunRow.id,
        CollectionRunRow.provider,
        CollectionRunRow.chat_external_id,
        CollectionRunRow.status,
        CollectionRunRow.requested_since,
        CollectionRunRow.requested_until,
        CollectionRunRow.fetched_count,
        CollectionRunRow.inserted_count,
        CollectionRunRow.started_at,
        CollectionRunRow.finished_at,
        CollectionRunRow.error_code,
    )


def _analysis_run_columns() -> tuple:
    return (
        AnalysisRunRow.id,
        AnalysisRunRow.analyzer,
        AnalysisRunRow.model,
        AnalysisRunRow.prompt_version,
        AnalysisRunRow.schema_version,
        AnalysisRunRow.input_hash,
        AnalysisRunRow.window_start,
        AnalysisRunRow.window_end,
        AnalysisRunRow.status,
        AnalysisRunRow.created_at,
    )


def _report_run_columns() -> tuple:
    return (
        ReportRow.id,
        ReportRow.analysis_run_id,
        ReportRow.window_start,
        ReportRow.window_end,
        ReportRow.status,
        ReportRow.created_at,
    )


def _collection_run_json(row: Any) -> dict[str, object]:
    reason_code = row.error_code if row.status == "succeeded" else None
    error_code = row.error_code if row.status == "failed" else None
    return {
        "collection_run_id": row.id,
        "provider": row.provider,
        "chat_id": row.chat_external_id,
        "status": row.status,
        "requested_since": _run_datetime_json(row.requested_since),
        "requested_until": _run_datetime_json(row.requested_until),
        "fetched_count": row.fetched_count,
        "inserted_count": row.inserted_count,
        "started_at": _run_datetime_json(row.started_at),
        "finished_at": _run_datetime_json(row.finished_at),
        "reason_code": reason_code,
        "error_code": error_code,
    }


def _analysis_run_json(row: Any) -> dict[str, object]:
    return {
        "analysis_run_id": row.id,
        "analyzer": row.analyzer,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "schema_version": row.schema_version,
        "input_hash": row.input_hash,
        "window_start": _run_datetime_json(row.window_start),
        "window_end": _run_datetime_json(row.window_end),
        "status": row.status,
        "created_at": _run_datetime_json(row.created_at),
    }


def _report_run_json(row: Any) -> dict[str, object]:
    return {
        "report_id": row.id,
        "analysis_run_id": row.analysis_run_id,
        "window_start": _run_datetime_json(row.window_start),
        "window_end": _run_datetime_json(row.window_end),
        "status": row.status,
        "created_at": _run_datetime_json(row.created_at),
    }


def latest_run_records(
    *,
    session: Session,
    window_start: datetime,
    window_end: datetime,
    chat_ids: tuple[str, ...],
    kind: str,
) -> dict[str, object]:
    """Project existing run rows to JSON without reading derived or source content."""

    collections: list[dict[str, object]] = []
    analysis: dict[str, object] | None = None
    report: dict[str, object] | None = None

    if kind in {"all", "collection"}:
        statement = select(*_collection_run_columns()).where(
            CollectionRunRow.requested_since == window_start,
            CollectionRunRow.requested_until == window_end,
        )
        if chat_ids:
            statement = statement.where(CollectionRunRow.chat_external_id.in_(chat_ids))
        rows = session.execute(statement.order_by(CollectionRunRow.id.desc())).all()
        latest_by_chat: dict[str, Any] = {}
        for row in rows:
            latest_by_chat.setdefault(row.chat_external_id, row)
        collections = [
            _collection_run_json(latest_by_chat[chat_id])
            for chat_id in sorted(latest_by_chat)
        ]

    analysis_row = None
    if kind in {"all", "analysis", "report"}:
        analysis_row = session.execute(
            select(*_analysis_run_columns())
            .where(
                AnalysisRunRow.window_start == window_start,
                AnalysisRunRow.window_end == window_end,
            )
            .order_by(AnalysisRunRow.id.desc())
            .limit(1)
        ).one_or_none()
        if kind in {"all", "analysis"} and analysis_row is not None:
            analysis = _analysis_run_json(analysis_row)

    if kind in {"all", "report"} and analysis_row is not None:
        report_row = session.execute(
            select(*_report_run_columns())
            .where(ReportRow.analysis_run_id == analysis_row.id)
            .order_by(ReportRow.id.desc())
            .limit(1)
        ).one_or_none()
        if report_row is not None:
            report = _report_run_json(report_row)

    return {"collections": collections, "analysis": analysis, "report": report}


def show_run_record(*, session: Session, kind: str, run_id: int) -> dict[str, object]:
    """Read one existing run row while deliberately excluding payload columns."""

    if kind == "collection":
        row = session.execute(
            select(*_collection_run_columns()).where(CollectionRunRow.id == run_id)
        ).one_or_none()
        result = _collection_run_json(row) if row is not None else None
    elif kind == "analysis":
        row = session.execute(
            select(*_analysis_run_columns()).where(AnalysisRunRow.id == run_id)
        ).one_or_none()
        result = _analysis_run_json(row) if row is not None else None
    else:
        row = session.execute(
            select(*_report_run_columns()).where(ReportRow.id == run_id)
        ).one_or_none()
        result = _report_run_json(row) if row is not None else None
    if result is None:
        raise RunNotFoundError(f"{kind} run 不存在：{run_id}")
    return result
