from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from group_info_system.cli import main
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.config import get_settings
from group_info_system.db.models import AnalysisRunRow, EventRow, MessageVersionRow, ReportRow
from group_info_system.db.session import session_factory
from group_info_system.domain.handoff import WorkBuddyRequestEnvelope

START = datetime(2026, 9, 9, 22, 0, tzinfo=UTC)
END = datetime(2026, 9, 10, 22, 0, tzinfo=UTC)
CHAT_ID = "daily-report-simulated-chat"
MODEL = "workbuddy-simulated-model"


def _fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "simulated_messages.json"


def _configure(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    output_root = tmp_path / "output"
    monkeypatch.setenv("GROUP_INFO_OUTPUT_DIR", str(output_root))
    get_settings.cache_clear()
    monkeypatch.setattr(
        "group_info_system.cli.FeishuCollector",
        lambda **_kwargs: SimulatedCollector(_fixture()),
    )
    return output_root


def _collect(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert (
        main(
            [
                "collect",
                "--source",
                "feishu",
                "--chat-id",
                CHAT_ID,
                "--since",
                START.isoformat(),
                "--until",
                END.isoformat(),
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _request(
    *,
    path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], WorkBuddyRequestEnvelope]:
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
                str(path),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    envelope = WorkBuddyRequestEnvelope.model_validate_json(path.read_text("utf-8"))
    return result, envelope


def _response_payload(
    request: WorkBuddyRequestEnvelope,
    *,
    transport_status: str = "succeeded",
) -> dict[str, object]:
    first_message = json.loads(request.input.user_prompt)["messages"][0]
    output = None
    error_code = "SIMULATED_LLM_FAILURE"
    if transport_status == "succeeded":
        output = {
            "events": [
                {
                    "event_type": "information",
                    "report_section": "today",
                    "title": "模拟输入形成一项可核验背景",
                    "summary": "该事件记录了一项能够从冻结消息版本回查的有效背景。",
                    "analysis_kind": "source_fact",
                    "event_time": first_message["sent_at"],
                    "time_range": None,
                    "message_version_ids": [first_message["message_version_id"]],
                }
            ]
        }
        error_code = None
    return {
        "protocol_version": "group-info.llm-response/v1",
        "request_id": request.request_id,
        "analysis_run_id": request.analysis_run_id,
        "input_hash": request.input.input_hash,
        "model": request.model,
        "prompt_version": request.prompt.version,
        "schema_version": request.response_schema.version,
        "output": output,
        "transport": {
            "status": transport_status,
            "attempt": 1,
            "error_code": error_code,
        },
    }


def _write_response(
    path: Path,
    request: WorkBuddyRequestEnvelope,
    *,
    transport_status: str = "succeeded",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _response_payload(request, transport_status=transport_status),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _import_response(
    *,
    request: WorkBuddyRequestEnvelope,
    response_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    exit_code = main(
        [
            "analyze-response",
            "--analysis-run-id",
            str(request.analysis_run_id),
            "--input",
            str(response_path),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(
        captured.out if exit_code == 0 else captured.err.splitlines()[-1]
    )
    return exit_code, payload


def _report(
    *,
    analysis_run_id: int,
    capsys: pytest.CaptureFixture[str],
    output_dir: Path | None = None,
) -> tuple[int, dict[str, object]]:
    argv = ["report", "daily", "--analysis-run-id", str(analysis_run_id)]
    if output_dir is not None:
        argv.extend(("--output-dir", str(output_dir)))
    exit_code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(
        captured.out if exit_code == 0 else captured.err.splitlines()[-1]
    )
    return exit_code, payload


def _latest(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    assert (
        main(
            [
                "runs",
                "latest",
                "--start",
                START.isoformat(),
                "--end",
                END.isoformat(),
                "--chat-id",
                CHAT_ID,
                "--kind",
                "all",
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _database_snapshot(database_url: str) -> tuple[str, ...]:
    path = Path(database_url.removeprefix("sqlite:///")).resolve()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def test_daily_report_skill_success_path(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = _configure(tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _latest(capsys)["analysis"] is None
    collected = _collect(capsys)
    request_result, request = _request(
        path=tmp_path / "handoff" / "success" / "request.json",
        capsys=capsys,
    )
    response_path = tmp_path / "handoff" / "success" / "response.json"
    _write_response(response_path, request)
    import_code, imported = _import_response(
        request=request,
        response_path=response_path,
        capsys=capsys,
    )
    report_code, report = _report(
        analysis_run_id=request.analysis_run_id,
        capsys=capsys,
    )

    assert collected["status"] == "succeeded"
    assert request_result["status"] == "succeeded"
    assert import_code == 0 and imported["status"] == "succeeded"
    assert report_code == 0 and report["status"] == "succeeded"
    assert (output_root / "2026-09-11" / "report.json").is_file()
    assert (output_root / "2026-09-11" / "report.html").is_file()
    latest = _latest(capsys)
    assert latest["collections"][0]["status"] == "succeeded"
    assert latest["analysis"]["status"] == "succeeded"
    assert latest["report"]["status"] == "succeeded"


def test_daily_report_skill_recovers_from_llm_failure_with_new_analysis(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _collect(capsys)
    _, failed_request = _request(
        path=tmp_path / "handoff" / "llm-failed" / "request.json",
        capsys=capsys,
    )
    failed_response = tmp_path / "handoff" / "llm-failed" / "response.json"
    _write_response(failed_response, failed_request, transport_status="failed")
    failed_code, failure = _import_response(
        request=failed_request,
        response_path=failed_response,
        capsys=capsys,
    )
    assert failed_code == 1
    assert failure["error_code"] == "WorkBuddyTransportFailed"
    assert (
        main(
            [
                "runs",
                "show",
                "--kind",
                "analysis",
                "--id",
                str(failed_request.analysis_run_id),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["run"]["status"] == "failed"

    _, retry_request = _request(
        path=tmp_path / "handoff" / "llm-retry" / "request.json",
        capsys=capsys,
    )
    retry_response = tmp_path / "handoff" / "llm-retry" / "response.json"
    _write_response(retry_response, retry_request)
    retry_code, _result = _import_response(
        request=retry_request,
        response_path=retry_response,
        capsys=capsys,
    )
    report_code, _report_result = _report(
        analysis_run_id=retry_request.analysis_run_id,
        capsys=capsys,
    )
    assert retry_code == 0
    assert report_code == 0
    assert retry_request.analysis_run_id != failed_request.analysis_run_id
    assert _latest(capsys)["analysis"]["analysis_run_id"] == retry_request.analysis_run_id


def test_daily_report_skill_recovers_report_without_repeating_analysis(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(tmp_path=tmp_path, monkeypatch=monkeypatch)
    _collect(capsys)
    _, request = _request(
        path=tmp_path / "handoff" / "report" / "request.json",
        capsys=capsys,
    )
    response_path = tmp_path / "handoff" / "report" / "response.json"
    _write_response(response_path, request)
    assert _import_response(
        request=request,
        response_path=response_path,
        capsys=capsys,
    )[0] == 0

    blocked = tmp_path / "blocked-output"
    blocked.write_text("not a directory", encoding="utf-8")
    failed_code, failed_report = _report(
        analysis_run_id=request.analysis_run_id,
        output_dir=blocked,
        capsys=capsys,
    )
    assert failed_code == 1
    assert failed_report["status"] == "failed"
    assert _latest(capsys)["report"]["status"] == "failed"

    retry_output = tmp_path / "recovered-output"
    retry_code, recovered = _report(
        analysis_run_id=request.analysis_run_id,
        output_dir=retry_output,
        capsys=capsys,
    )
    assert retry_code == 0
    assert recovered["analysis_run_id"] == request.analysis_run_id
    assert _latest(capsys)["report"]["report_id"] == recovered["report_id"]

    sessions = session_factory(migrated_database)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRunRow)) == 1
        reports = session.scalars(select(ReportRow).order_by(ReportRow.id)).all()
        assert [row.status for row in reports] == ["failed", "succeeded"]


def test_daily_report_skill_repeat_uses_observed_success_without_new_runs(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(tmp_path=tmp_path, monkeypatch=monkeypatch)
    first_collect = _collect(capsys)
    second_collect = _collect(capsys)
    assert first_collect["inserted"] == 8
    assert second_collect["inserted"] == 0

    _, request = _request(
        path=tmp_path / "handoff" / "repeat" / "request.json",
        capsys=capsys,
    )
    response_path = tmp_path / "handoff" / "repeat" / "response.json"
    _write_response(response_path, request)
    first_import = _import_response(
        request=request,
        response_path=response_path,
        capsys=capsys,
    )
    replay_import = _import_response(
        request=request,
        response_path=response_path,
        capsys=capsys,
    )
    assert first_import[1]["replayed"] is False
    assert replay_import[1]["replayed"] is True
    assert _report(analysis_run_id=request.analysis_run_id, capsys=capsys)[0] == 0

    before = _database_snapshot(migrated_database)
    observed = _latest(capsys)
    after = _database_snapshot(migrated_database)
    assert observed["collections"][0]["status"] == "succeeded"
    assert observed["analysis"]["status"] == "succeeded"
    assert observed["report"]["status"] == "succeeded"
    assert after == before

    sessions = session_factory(migrated_database)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(MessageVersionRow)) == 8
        assert session.scalar(select(func.count()).select_from(AnalysisRunRow)) == 1
        assert session.scalar(select(func.count()).select_from(EventRow)) == 1
        assert session.scalar(select(func.count()).select_from(ReportRow)) == 1
