from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.analysis import analyze_into_store
from group_info_system.application.collection import collect_into_store
from group_info_system.cli import main
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.config import get_settings
from group_info_system.db.models import (
    AnalysisInputRow,
    AnalysisRunRow,
    CollectionRunRow,
    EventEvidenceRow,
    EventRow,
    ReportRow,
)
from group_info_system.db.repositories import ReportRepository
from group_info_system.db.session import session_factory

START = "2026-09-09T22:00:00+00:00"
END = "2026-09-10T22:00:00+00:00"


def _fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "simulated_messages.json"


def test_production_cli_steps_run_independently(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    output_root = tmp_path / "output"
    monkeypatch.setenv("GROUP_INFO_OUTPUT_DIR", str(output_root))
    get_settings.cache_clear()
    monkeypatch.setattr(
        "group_info_system.cli.FeishuCollector",
        lambda **_kwargs: SimulatedCollector(_fixture()),
    )

    assert (
        main(
            [
                "collect",
                "--source",
                "feishu",
                "--chat-id",
                "cli-workflow-chat",
                "--since",
                START,
                "--until",
                END,
            ]
        )
        == 0
    )
    collected = json.loads(capsys.readouterr().out)
    assert collected["command"] == "collect"
    assert collected["status"] == "succeeded"
    assert collected["fetched"] == 8
    assert collected["inserted"] == 8
    assert collected["collection_run_id"] > 0

    assert main(["analyze", "--start", START, "--end", END]) == 0
    analyzed = json.loads(capsys.readouterr().out)
    assert analyzed["command"] == "analyze"
    assert analyzed["status"] == "succeeded"
    assert analyzed["input_count"] == 6
    assert analyzed["event_count"] == 5
    analysis_run_id = analyzed["analysis_run_id"]

    assert main(["report", "daily", "--analysis-run-id", str(analysis_run_id)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["command"] == "report daily"
    assert reported["status"] == "succeeded"
    assert reported["analysis_run_id"] == analysis_run_id
    assert reported["report_id"] > 0
    assert (output_root / "2026-09-11" / "report.json").is_file()
    report_html = output_root / "2026-09-11" / "report.html"
    assert report_html.is_file()
    assert 'href="../index.html"' in report_html.read_text(encoding="utf-8")
    report_index = output_root / "index.html"
    assert report_index.is_file()
    index_html = report_index.read_text(encoding="utf-8")
    assert 'href="2026-09-11/report.html"' in index_html
    assert "6 条消息 · 5 条结论" in index_html
    assert "MVP 产品群（模拟）" in index_html

    sessions = session_factory(migrated_database)
    with sessions() as session:
        collection_run = session.scalar(select(CollectionRunRow))
        analysis_run = session.scalar(select(AnalysisRunRow))
        report_run = session.scalar(select(ReportRow))
        assert collection_run is not None and collection_run.status == "succeeded"
        assert analysis_run is not None and analysis_run.status == "succeeded"
        assert report_run is not None and report_run.status == "succeeded"


def test_empty_window_mock_analysis_and_report_succeed_without_fake_events(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    output_root = tmp_path / "output"
    monkeypatch.setenv("GROUP_INFO_OUTPUT_DIR", str(output_root))
    get_settings.cache_clear()
    exit_code = main(
        [
            "analyze",
            "--start",
            "2027-01-01T00:00:00+00:00",
            "--end",
            "2027-01-02T00:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    analyzed = json.loads(captured.out)
    assert exit_code == 0
    assert analyzed["status"] == "succeeded"
    assert analyzed["command"] == "analyze"
    assert analyzed["input_count"] == 0
    assert analyzed["event_count"] == 0

    analysis_run_id = analyzed["analysis_run_id"]
    assert main(["report", "daily", "--analysis-run-id", str(analysis_run_id)]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported["status"] == "succeeded"
    report_path = output_root / "2027-01-02" / "report.json"
    html_path = output_root / "2027-01-02" / "report.html"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["source_scope"]["message_count"] == 0
    assert all(not section["items"] for section in payload["sections"])
    assert payload["exclusions"] == [
        {"code": "NO_VALID_EVENTS", "message": "今日暂无有效事件"}
    ]
    assert "今日暂无有效事件" in html_path.read_text(encoding="utf-8")

    sessions = session_factory(migrated_database)
    with sessions() as session:
        run = session.get(AnalysisRunRow, analysis_run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert json.loads(run.structured_output or "{}")["events"] == []
        assert session.scalar(select(func.count()).select_from(AnalysisInputRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 0
        report = session.get(ReportRow, reported["report_id"])
        assert report is not None and report.status == "succeeded"


def test_report_failure_leaves_failed_row_and_portal_ignores_it(
    migrated_database: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GROUP_INFO_OUTPUT_DIR", str(tmp_path / "output"))
    get_settings.cache_clear()
    sessions = session_factory(migrated_database)
    start = datetime.fromisoformat(START).astimezone(UTC)
    end = datetime.fromisoformat(END).astimezone(UTC)
    with sessions() as session:
        collect_into_store(
            session=session,
            collector=SimulatedCollector(_fixture()),
            chat_id="report-failure-chat",
            since=start,
            until=end,
        )
    with sessions() as session:
        analysis = analyze_into_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=start,
            window_end=end,
        )

    blocked_output = tmp_path / "not-a-directory"
    blocked_output.write_text("occupied", encoding="utf-8")
    exit_code = main(
        [
            "report",
            "daily",
            "--analysis-run-id",
            str(analysis.analysis_run_id),
            "--output-dir",
            str(blocked_output),
        ]
    )
    captured = capsys.readouterr()
    failure = json.loads(captured.err.splitlines()[-1])
    assert exit_code == 1
    assert failure["command"] == "report daily"
    assert failure["status"] == "failed"
    assert failure["report_id"] > 0

    with sessions() as session:
        row = session.get(ReportRow, failure["report_id"])
        assert row is not None
        assert row.status == "failed"
        assert ReportRepository(session).latest() is None
