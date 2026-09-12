from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.collection import collect_into_store
from group_info_system.application.reporting import build_report_from_store
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.db.models import EventEvidenceRow, EventRow, MessageRow, MessageVersionRow
from group_info_system.db.repositories import MessageRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.analysis import (
    EventAnalysisResult,
    EventCandidate,
    EventTimeRange,
)

START = datetime(2026, 9, 11, 16, tzinfo=UTC)
END = datetime(2026, 9, 12, 16, tzinfo=UTC)


def collect_quality_fixture(database_url: str) -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests" / "fixtures" / "event_quality_messages.json"
    sessions = session_factory(database_url)
    with sessions() as session:
        collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture, chat_name="Event 质量测试群"),
            chat_id="event-quality-chat",
            since=START,
            until=END,
        )


def test_existing_mock_characterizes_classification_granularity_and_duplicates(
    migrated_database: str, tmp_path: Path
) -> None:
    collect_quality_fixture(migrated_database)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        messages = MessageRepository(session).current_reportable(START, END)
        analysis = SimulatedAIAnalyzer().analyze(messages)

    assert len(messages) == 12
    assert len(analysis.events) == 11
    assert Counter(event.report_section for event in analysis.events) == {
        "today": 7,
        "howto": 1,
        "pitfalls": 2,
        "next": 1,
    }
    assert Counter(event.event_type for event in analysis.events) == {
        "progress": 3,
        "decision": 2,
        "action": 2,
        "risk": 2,
        "question": 1,
        "information": 1,
    }
    assert all(len(event.message_version_ids) == 1 for event in analysis.events)
    assert all(event.event_time is not None and event.time_range is None for event in analysis.events)

    by_platform_id = {message.platform_message_id: message for message in messages}
    compound_version = by_platform_id["quality-007"].version_id
    compound_events = [
        event for event in analysis.events if compound_version in event.message_version_ids
    ]
    assert [event.report_section for event in compound_events] == ["pitfalls"]
    assert [event.event_type for event in compound_events] == ["risk"]

    duplicate_versions = {
        by_platform_id["quality-008"].version_id,
        by_platform_id["quality-009"].version_id,
    }
    duplicate_events = [
        event
        for event in analysis.events
        if set(event.message_version_ids).issubset(duplicate_versions)
        and set(event.message_version_ids)
    ]
    assert len(duplicate_events) == 2
    assert len(
        {
            (event.event_type, event.report_section, event.title, event.summary)
            for event in duplicate_events
        }
    ) == 1

    output = tmp_path / "mock-output"
    with sessions() as session, pytest.raises(ValueError, match="完全重复"):
        build_report_from_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=START,
            window_end=END,
            output_dir=output,
            virtual_data=True,
        )


class MultiRelationMockAnalyzer:
    name = "multi-relation-mock-v1"
    model = "test-rules-v1"
    prompt_version = "event-quality-v1"
    schema_version = "event-v2"

    def analyze(self, messages):  # type: ignore[no-untyped-def]
        by_id = {message.platform_message_id: message for message in messages}
        return EventAnalysisResult(
            analyzer=self.name,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            events=(
                EventCandidate(
                    event_type="decision",
                    report_section="today",
                    title="月报统计口径已经确定",
                    summary="月报采用自然月，并统一使用 Asia/Shanghai 时区。",
                    analysis_kind="source_fact",
                    time_range=EventTimeRange(
                        start=by_id["quality-005"].sent_at,
                        end=by_id["quality-006"].sent_at,
                    ),
                    message_version_ids=(
                        by_id["quality-005"].version_id,
                        by_id["quality-006"].version_id,
                    ),
                ),
                EventCandidate(
                    event_type="risk",
                    report_section="pitfalls",
                    title="权限不足风险",
                    summary="当前存在权限不足风险。",
                    analysis_kind="analysis",
                    event_time=by_id["quality-007"].sent_at,
                    message_version_ids=(by_id["quality-007"].version_id,),
                ),
                EventCandidate(
                    event_type="action",
                    report_section="next",
                    title="完成用户授权",
                    summary="下一步需要先完成用户授权。",
                    analysis_kind="analysis",
                    event_time=by_id["quality-007"].sent_at,
                    message_version_ids=(by_id["quality-007"].version_id,),
                ),
            ),
        )


def test_event_relations_and_report_projection_support_many_to_many(
    migrated_database: str, tmp_path: Path
) -> None:
    collect_quality_fixture(migrated_database)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        envelope = build_report_from_store(
            session=session,
            analyzer=MultiRelationMockAnalyzer(),
            window_start=START,
            window_end=END,
            output_dir=tmp_path / "relation-output",
            virtual_data=True,
        )

    with sessions() as session:
        evidence_counts = session.execute(
            select(EventRow.title, func.count(EventEvidenceRow.id))
            .join(EventEvidenceRow, EventEvidenceRow.event_id == EventRow.id)
            .group_by(EventRow.id)
            .order_by(EventRow.id)
        ).all()
        compound_reference_count = session.scalar(
            select(func.count(EventEvidenceRow.id))
            .join(
                MessageVersionRow,
                MessageVersionRow.id == EventEvidenceRow.message_version_id,
            )
            .join(MessageRow, MessageRow.id == MessageVersionRow.message_id)
            .where(MessageRow.platform_message_id == "quality-007")
        )

    assert evidence_counts == [
        ("月报统计口径已经确定", 2),
        ("权限不足风险", 1),
        ("完成用户授权", 1),
    ]
    assert compound_reference_count == 2

    report_items = {
        item.title: item for section in envelope.report.sections for item in section.items
    }
    assert len(report_items["月报统计口径已经确定"].evidence_ids) == 2
    assert report_items["权限不足风险"].evidence_ids == report_items["完成用户授权"].evidence_ids
    assert all(item.evidence_quotes for item in report_items.values())
