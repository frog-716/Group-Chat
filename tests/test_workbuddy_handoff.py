from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.collection import collect_into_store
from group_info_system.application.handoff import (
    AnalysisResponseInvalid,
    AnalysisResponseMismatch,
    create_analysis_request,
    import_analysis_response,
)
from group_info_system.application.reporting import build_daily_report_from_analysis
from group_info_system.cli import main
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.config import get_settings
from group_info_system.db.models import (
    AnalysisInputRow,
    AnalysisRunRow,
    EventEvidenceRow,
    EventRow,
    ReportRow,
)
from group_info_system.db.repositories import ReportRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.handoff import WorkBuddyRequestEnvelope

START = datetime(2026, 9, 9, 22, 0, tzinfo=UTC)
END = datetime(2026, 9, 10, 22, 0, tzinfo=UTC)
MODEL = "workbuddy-test-model"


def _fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "simulated_messages.json"


def _seed(database_url: str) -> None:
    sessions = session_factory(database_url)
    with sessions() as session:
        collect_into_store(
            session=session,
            collector=SimulatedCollector(_fixture()),
            chat_id="workbuddy-handoff-chat",
            since=START,
            until=END,
        )


def _response(
    envelope: WorkBuddyRequestEnvelope,
    events: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "protocol_version": "group-info.llm-response/v1",
            "request_id": envelope.request_id,
            "analysis_run_id": envelope.analysis_run_id,
            "input_hash": envelope.input.input_hash,
            "model": envelope.model,
            "prompt_version": envelope.prompt.version,
            "schema_version": envelope.response_schema.version,
            "output": {"events": events},
            "transport": {"status": "succeeded", "attempt": 1, "error_code": None},
        },
        ensure_ascii=False,
    )


def test_response_import_is_validated_persisted_and_idempotent(
    migrated_database: str,
) -> None:
    _seed(migrated_database)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        request = create_analysis_request(
            session=session,
            model=MODEL,
            window_start=START,
            window_end=END,
        )
        messages = ReportRepository(session).analysis_messages(
            request.envelope.analysis_run_id
        )
        assert request.envelope.response_schema.version == "event-v2"
        assert request.envelope.response_schema.json_schema["type"] == "object"
        assert "raw_payload" not in request.envelope.input.user_prompt
        mock_events = [
            event.model_dump(mode="json")
            for event in SimulatedAIAnalyzer().analyze(messages).events
        ]
        raw_response = _response(request.envelope, mock_events)

        imported = import_analysis_response(
            session=session,
            analysis_run_id=request.envelope.analysis_run_id,
            raw_response=raw_response,
        )
        replayed = import_analysis_response(
            session=session,
            analysis_run_id=request.envelope.analysis_run_id,
            raw_response=raw_response,
        )

        assert imported.event_count == 5
        assert imported.replayed is False
        assert replayed.event_count == 5
        assert replayed.replayed is True
        assert session.scalar(select(func.count()).select_from(EventRow)) == 5
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 5
        run = session.get(AnalysisRunRow, request.envelope.analysis_run_id)
        assert run is not None and run.status == "succeeded"


def test_empty_handoff_response_succeeds_and_builds_empty_report(
    migrated_database: str,
    tmp_path: Path,
) -> None:
    sessions = session_factory(migrated_database)
    with sessions() as session:
        request = create_analysis_request(
            session=session,
            model=MODEL,
            window_start=START,
            window_end=END,
        )
        assert request.input_count == 0
        assert json.loads(request.envelope.input.user_prompt)["messages"] == []

        imported = import_analysis_response(
            session=session,
            analysis_run_id=request.envelope.analysis_run_id,
            raw_response=_response(request.envelope, []),
        )
        assert imported.event_count == 0
        assert imported.replayed is False

        report = build_daily_report_from_analysis(
            session=session,
            analysis_run_id=request.envelope.analysis_run_id,
            output_root=tmp_path,
            local_timezone="Asia/Shanghai",
            output_dir=tmp_path / "empty-report",
        )

        payload = json.loads((tmp_path / "empty-report" / "report.json").read_text("utf-8"))
        assert payload["source_scope"]["message_count"] == 0
        assert payload["exclusions"] == [
            {"code": "NO_VALID_EVENTS", "message": "今日暂无有效事件"}
        ]
        assert "今日暂无有效事件" in (
            tmp_path / "empty-report" / "report.html"
        ).read_text("utf-8")
        assert report.report_id is not None
        assert session.scalar(select(func.count()).select_from(AnalysisInputRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 0
        stored_report = session.get(ReportRow, report.report_id)
        assert stored_report is not None and stored_report.status == "succeeded"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("request_id", "arq_00000000000000000000000000000000"),
        ("input_hash", "0" * 64),
        ("schema_version", "event-v999"),
    ],
)
def test_mismatched_response_fails_run_without_events(
    migrated_database: str,
    field: str,
    invalid_value: str,
) -> None:
    _seed(migrated_database)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        request = create_analysis_request(
            session=session,
            model=MODEL,
            window_start=START,
            window_end=END,
        )
        payload = json.loads(_response(request.envelope, []))
        payload[field] = invalid_value

        with pytest.raises(AnalysisResponseMismatch, match=field):
            import_analysis_response(
                session=session,
                analysis_run_id=request.envelope.analysis_run_id,
                raw_response=json.dumps(payload),
            )

        run = session.get(AnalysisRunRow, request.envelope.analysis_run_id)
        assert run is not None and run.status == "failed"
        assert session.scalar(select(func.count()).select_from(EventRow)) == 0


def test_correlated_invalid_evidence_fails_run_without_partial_events(
    migrated_database: str,
) -> None:
    _seed(migrated_database)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        request = create_analysis_request(
            session=session,
            model=MODEL,
            window_start=START,
            window_end=END,
        )
        first_message = json.loads(request.envelope.input.user_prompt)["messages"][0]
        invalid_event = {
            "event_type": "information",
            "report_section": "today",
            "title": "无法回查的事件",
            "summary": "该结论引用了本次分析输入之外的消息版本。",
            "analysis_kind": "source_fact",
            "event_time": first_message["sent_at"],
            "time_range": None,
            "message_version_ids": [999999],
        }

        with pytest.raises(AnalysisResponseInvalid, match="不属于本次分析输入"):
            import_analysis_response(
                session=session,
                analysis_run_id=request.envelope.analysis_run_id,
                raw_response=_response(request.envelope, [invalid_event]),
            )

        run = session.get(AnalysisRunRow, request.envelope.analysis_run_id)
        assert run is not None and run.status == "failed"
        assert session.scalar(select(func.count()).select_from(EventRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 0


def test_cli_request_response_and_daily_report_close_the_loop(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed(migrated_database)
    request_path = tmp_path / "handoff" / "request.json"
    response_path = tmp_path / "handoff" / "response.json"
    output_root = tmp_path / "output"
    monkeypatch.setenv("GROUP_INFO_OUTPUT_DIR", str(output_root))
    get_settings.cache_clear()

    assert (
        main(
            [
                "analyze-request",
                "--model",
                MODEL,
                "--start",
                START.isoformat(),
                "--end",
                END.isoformat(),
                "--output",
                str(request_path),
            ]
        )
        == 0
    )
    request_result = json.loads(capsys.readouterr().out)
    request_payload = json.loads(request_path.read_text("utf-8"))
    request = WorkBuddyRequestEnvelope.model_validate(request_payload)
    first_message = json.loads(request.input.user_prompt)["messages"][0]
    event = {
        "event_type": "information",
        "report_section": "today",
        "title": "测试输入包含一项有效背景",
        "summary": "该事件保留了一项能够由冻结消息版本核验的背景信息。",
        "analysis_kind": "source_fact",
        "event_time": first_message["sent_at"],
        "time_range": None,
        "message_version_ids": [first_message["message_version_id"]],
    }
    response_path.write_text(
        _response(request, [event]),
        encoding="utf-8",
    )

    analysis_run_id = request_result["analysis_run_id"]
    assert (
        main(
            [
                "analyze-response",
                "--analysis-run-id",
                str(analysis_run_id),
                "--input",
                str(response_path),
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported == {
        "command": "analyze-response",
        "status": "succeeded",
        "analysis_run_id": analysis_run_id,
        "event_count": 1,
        "replayed": False,
    }

    assert main(["report", "daily", "--analysis-run-id", str(analysis_run_id)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded"
    assert (output_root / "2026-09-11" / "report.json").is_file()
    assert (output_root / "2026-09-11" / "report.html").is_file()
