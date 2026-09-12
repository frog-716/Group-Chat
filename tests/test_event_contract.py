from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from group_info_system.analysis.validator import validate_event_analysis
from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import (
    EventAnalysisResult,
    EventCandidate,
    EventTimeRange,
)


def stored_message() -> StoredMessage:
    return StoredMessage(
        version_id=1,
        evidence_id="msgv_test",
        provider="simulated",
        chat_id="chat-1",
        chat_name="测试群",
        platform_message_id="message-1",
        sent_at=datetime(2026, 9, 12, 1, tzinfo=UTC),
        sender_name="成员一",
        content="版本已经发布。",
        message_type="text",
        is_system=False,
        is_deleted=False,
    )


def candidate(**overrides) -> EventCandidate:  # type: ignore[no-untyped-def]
    values = {
        "event_type": "progress",
        "report_section": "today",
        "title": "版本发布",
        "summary": "版本 1.2 已完成发布。",
        "analysis_kind": "source_fact",
        "event_time": datetime(2026, 9, 12, 1, tzinfo=UTC),
        "message_version_ids": (1,),
    }
    values.update(overrides)
    return EventCandidate(**values)


def analysis(*events: EventCandidate) -> EventAnalysisResult:
    return EventAnalysisResult(
        analyzer="contract-test",
        model="none",
        prompt_version="test-v1",
        schema_version="event-v2",
        events=events,
    )


def test_event_contract_requires_evidence_meaningful_text_and_one_time_shape() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        candidate(message_version_ids=())
    with pytest.raises(ValidationError, match="cannot be blank"):
        candidate(summary="   ")
    with pytest.raises(ValidationError, match="exactly one"):
        candidate(event_time=None)
    with pytest.raises(ValidationError, match="exactly one"):
        candidate(
            time_range=EventTimeRange(
                start=datetime(2026, 9, 12, 1, tzinfo=UTC),
                end=datetime(2026, 9, 12, 2, tzinfo=UTC),
            )
        )
    with pytest.raises(ValidationError, match="must not be after"):
        EventTimeRange(
            start=datetime(2026, 9, 12, 2, tzinfo=UTC),
            end=datetime(2026, 9, 12, 1, tzinfo=UTC),
        )


def test_event_contract_accepts_time_range() -> None:
    value = candidate(
        event_time=None,
        time_range=EventTimeRange(
            start=datetime(2026, 9, 12, 1, tzinfo=UTC),
            end=datetime(2026, 9, 12, 1, tzinfo=UTC) + timedelta(minutes=5),
        ),
    )
    assert value.event_time is None
    assert value.time_range and value.time_range.end > value.time_range.start


def test_output_validator_rejects_duplicates_generic_wrappers_and_exact_rewrites() -> None:
    message = stored_message()
    duplicate = candidate()
    generic = candidate(
        title="通用包装",
        summary="群内出现了一条值得保留的信息；请回查原文。",
        event_type="information",
    )
    rewrite = candidate(title="原文改写", summary=message.content)
    errors = validate_event_analysis(
        analysis(duplicate, duplicate, generic, rewrite),
        [message],
    )
    assert any("完全重复" in error for error in errors)
    assert any("通用消息包装" in error for error in errors)
    assert any("单消息原文改写" in error for error in errors)


def test_output_validator_defensively_rejects_evidence_free_event() -> None:
    invalid = EventCandidate.model_construct(
        event_type="information",
        report_section="today",
        title="无证据",
        summary="这个 Event 没有证据。",
        analysis_kind="analysis",
        event_time=datetime(2026, 9, 12, 1, tzinfo=UTC),
        time_range=None,
        message_version_ids=(),
    )
    errors = validate_event_analysis(analysis(invalid), [stored_message()])
    assert errors == ["Event 1 没有 evidence"]
