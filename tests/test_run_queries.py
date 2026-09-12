from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

import group_info_system.cli as cli_module
from group_info_system.cli import main
from group_info_system.config import get_settings
from group_info_system.db.models import AnalysisRunRow, CollectionRunRow, ReportRow
from group_info_system.db.session import create_db_engine, session_factory

START = datetime(2026, 9, 11, 22, 0, tzinfo=UTC)
END = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)


def _database_path(database_url: str) -> Path:
    return Path(database_url.removeprefix("sqlite:///")).resolve()


def _logical_snapshot(database_url: str) -> tuple[str, ...]:
    database_path = _database_path(database_url)
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def _seed_runs(database_url: str) -> dict[str, list[tuple[int, str]]]:
    sessions = session_factory(database_url)
    statuses = ("running", "succeeded", "failed")
    result: dict[str, list[tuple[int, str]]] = {
        "collection": [],
        "analysis": [],
        "report": [],
    }
    with sessions() as session:
        analyses: list[AnalysisRunRow] = []
        for index, status in enumerate(statuses, start=1):
            collection = CollectionRunRow(
                provider="feishu",
                chat_external_id=f"chat-{status}",
                started_at=START + timedelta(minutes=index),
                finished_at=None if status == "running" else START + timedelta(minutes=index + 1),
                status=status,
                requested_since=START,
                requested_until=END,
                fetched_count=index,
                inserted_count=index,
                error_code=(
                    "TEST_FAILURE"
                    if status == "failed"
                    else "WINDOW_ALREADY_SYNCED"
                    if status == "succeeded"
                    else None
                ),
            )
            analysis = AnalysisRunRow(
                analyzer="llm-event-analyzer",
                model="test-model",
                prompt_version="event-extraction-v2",
                schema_version="event-v2",
                input_hash=str(index) * 64,
                window_start=START,
                window_end=END,
                status=status,
                structured_output="SENSITIVE_STRUCTURED_OUTPUT",
                created_at=START + timedelta(minutes=index),
            )
            session.add_all((collection, analysis))
            session.flush()
            analyses.append(analysis)
            result["collection"].append((collection.id, status))
            result["analysis"].append((analysis.id, status))

        for index, (status, analysis) in enumerate(zip(statuses, analyses, strict=True), start=1):
            report = ReportRow(
                analysis_run_id=analysis.id,
                window_start=START,
                window_end=END,
                status=status,
                report_json="SENSITIVE_REPORT_JSON",
                created_at=START + timedelta(minutes=index),
            )
            session.add(report)
            session.flush()
            result["report"].append((report.id, status))
        session.commit()
    return result


def test_runs_queries_are_read_only_and_emit_all_existing_statuses(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = _seed_runs(migrated_database)
    get_settings.cache_clear()
    before = _logical_snapshot(migrated_database)
    monkeypatch.setattr(
        cli_module,
        "upgrade_database",
        lambda _database_url: pytest.fail("runs CLI must not run migrations"),
    )

    assert (
        main(
            [
                "runs",
                "latest",
                "--start",
                START.isoformat(),
                "--end",
                END.isoformat(),
                "--kind",
                "all",
            ]
        )
        == 0
    )
    latest = json.loads(capsys.readouterr().out)
    assert latest["command"] == "runs latest"
    assert latest["status"] == "succeeded"
    assert {row["status"] for row in latest["collections"]} == {
        "running",
        "succeeded",
        "failed",
    }
    assert latest["analysis"]["status"] == "failed"
    assert latest["report"]["status"] == "failed"
    collections_by_status = {row["status"]: row for row in latest["collections"]}
    assert collections_by_status["succeeded"]["reason_code"] == "WINDOW_ALREADY_SYNCED"
    assert collections_by_status["succeeded"]["error_code"] is None
    assert collections_by_status["failed"]["reason_code"] is None
    assert collections_by_status["failed"]["error_code"] == "TEST_FAILURE"
    assert "structured_output" not in json.dumps(latest)
    assert "report_json" not in json.dumps(latest)
    assert "SENSITIVE" not in json.dumps(latest)

    for kind, runs in ids.items():
        for run_id, expected_status in runs:
            assert (
                main(
                    [
                        "runs",
                        "show",
                        "--kind",
                        kind,
                        "--id",
                        str(run_id),
                    ]
                )
                == 0
            )
            shown = json.loads(capsys.readouterr().out)
            assert shown["command"] == "runs show"
            assert shown["kind"] == kind
            assert shown["run"]["status"] == expected_status
            assert "SENSITIVE" not in json.dumps(shown)

    after = _logical_snapshot(migrated_database)
    assert after == before


def test_runs_latest_queries_only_allowed_run_columns(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_runs(migrated_database)
    engine = create_db_engine(migrated_database)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_sql(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement.casefold())

    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(cli_module, "read_only_session_factory", lambda _url: sessions)

    assert (
        main(
            [
                "runs",
                "latest",
                "--start",
                START.isoformat(),
                "--end",
                END.isoformat(),
            ]
        )
        == 0
    )
    capsys.readouterr()
    sql = "\n".join(statements)
    assert "collection_runs" in sql
    assert "analysis_runs" in sql
    assert "reports" in sql
    for forbidden in (
        "message_versions",
        "messages",
        "events",
        "event_evidence",
        "structured_output",
        "report_json",
    ):
        assert forbidden not in sql


def test_runs_show_missing_id_returns_stable_error(
    migrated_database: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_settings.cache_clear()
    exit_code = main(
        ["runs", "show", "--kind", "analysis", "--id", "999999"]
    )
    captured = capsys.readouterr()
    failure = json.loads(captured.err)
    assert exit_code == 1
    assert failure == {
        "command": "runs show",
        "status": "failed",
        "error_code": "RUN_NOT_FOUND",
    }
