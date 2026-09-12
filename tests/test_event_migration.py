from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def test_event_migration_preserves_legacy_analysis_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", database_url)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0001_initial")

    digest = "a" * 64
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO analysis_runs
            (id, analyzer, input_sha256, window_start, window_end, status,
             structured_output, created_at)
            VALUES (1, 'legacy-analyzer', ?, '2026-09-01', '2026-09-02',
                    'succeeded', '{}', '2026-09-02')""",
            (digest,),
        )
        connection.execute(
            """INSERT INTO reports
            (id, analysis_run_id, window_start, window_end, status, report_json, created_at)
            VALUES (1, 1, '2026-09-01', '2026-09-02', 'succeeded', '{}', '2026-09-02')"""
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT analyzer, model, prompt_version, schema_version, input_hash "
            "FROM analysis_runs WHERE id = 1"
        ).fetchone()
        report_count = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        tables = {
            value[0]
            for value in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert row == ("legacy-analyzer", "legacy", "legacy", "mvp-v1", digest)
    assert report_count == 1
    assert {"analysis_inputs", "events", "event_evidence"}.issubset(tables)


def test_event_contract_migration_backfills_existing_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "event-v1.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", database_url)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0002_event_driven_analysis")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO collection_runs
            (id, provider, chat_external_id, started_at, status, fetched_count, inserted_count)
            VALUES (1, 'simulated', 'chat-1', '2026-09-12', 'succeeded', 1, 1)"""
        )
        connection.execute(
            """INSERT INTO chats (id, provider, external_id, display_name, created_at)
            VALUES (1, 'simulated', 'chat-1', '测试群', '2026-09-12')"""
        )
        connection.execute(
            """INSERT INTO messages (id, chat_id, platform_message_id, first_seen_at)
            VALUES (1, 1, 'message-1', '2026-09-12')"""
        )
        connection.execute(
            """INSERT INTO messages (id, chat_id, platform_message_id, first_seen_at)
            VALUES (2, 1, 'message-2', '2026-09-12')"""
        )
        connection.execute(
            """INSERT INTO message_versions
            (id, message_id, collection_run_id, payload_sha256, evidence_id, sent_at,
             sender_id, sender_name, message_type, content, is_system, is_deleted,
             raw_payload, observed_at)
            VALUES (1, 1, 1, ?, 'msgv_legacy', '2026-09-12 01:00:00', 'member-1',
                    '成员一', 'text', '风险信息', 0, 0, '{}', '2026-09-12')""",
            ("b" * 64,),
        )
        connection.execute(
            """INSERT INTO message_versions
            (id, message_id, collection_run_id, payload_sha256, evidence_id, sent_at,
             sender_id, sender_name, message_type, content, is_system, is_deleted,
             raw_payload, observed_at)
            VALUES (2, 2, 1, ?, 'msgv_legacy_2', '2026-09-12 01:05:00', 'member-1',
                    '成员一', 'text', '风险补充', 0, 0, '{}', '2026-09-12')""",
            ("d" * 64,),
        )
        connection.execute(
            """INSERT INTO analysis_runs
            (id, analyzer, model, prompt_version, schema_version, input_hash,
             window_start, window_end, status, structured_output, created_at)
            VALUES (1, 'mock', 'mock', 'v1', 'event-v1', ?, '2026-09-12',
                    '2026-09-13', 'succeeded', '{}', '2026-09-13')""",
            ("c" * 64,),
        )
        connection.execute(
            """INSERT INTO events
            (id, analysis_run_id, category, title, summary, analysis_kind, created_at)
            VALUES (1, 1, 'pitfalls', '权限风险', '存在权限风险', 'analysis', '2026-09-13')"""
        )
        connection.execute(
            """INSERT INTO event_evidence (id, event_id, message_version_id, ordinal)
            VALUES (1, 1, 1, 0)"""
        )
        connection.execute(
            """INSERT INTO event_evidence (id, event_id, message_version_id, ordinal)
            VALUES (2, 1, 2, 1)"""
        )
        connection.execute(
            """INSERT INTO events
            (id, analysis_run_id, category, title, summary, analysis_kind, created_at)
            VALUES (2, 1, 'howto', '执行步骤', '存在执行步骤', 'analysis', '2026-09-13')"""
        )
        connection.execute(
            """INSERT INTO event_evidence (id, event_id, message_version_id, ordinal)
            VALUES (3, 2, 1, 0)"""
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event_type, report_section, event_time, time_range_start, time_range_end "
            "FROM events ORDER BY id"
        ).fetchall()

    assert rows == [
        ("risk", "pitfalls", None, "2026-09-12 01:00:00", "2026-09-12 01:05:00"),
        ("action", "howto", "2026-09-12 01:00:00", None, None),
    ]
