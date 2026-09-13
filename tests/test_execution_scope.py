from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import group_info_system.cli as cli_module
from group_info_system.application.execution_scope import (
    create_or_get_scope,
)
from group_info_system.application.handoff import create_analysis_request
from group_info_system.cli import main
from group_info_system.db.models import (
    AnalysisInputRow,
    AnalysisRunRow,
    CollectionRunRow,
    ExecutionScopeGroupRow,
    ExecutionScopeRow,
)
from group_info_system.db.repositories import MessageRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.messages import CollectionBatch, RawMessage, SyncCursor

START = datetime(2026, 9, 11, 22, 0, tzinfo=UTC)
END = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)

CONFIG = """
version: group-info/groups/v1
defaults:
  timezone: Asia/Shanghai
  day_cutoff: '06:00'
groups:
  - key: group-a
    provider: feishu
    external_id: oc_a
    display_name: 群 A
    status: active
  - key: group-b
    provider: feishu
    external_id: oc_b
    display_name: 群 B
    status: active
"""


def write_config(tmp_path: Path, content: str = CONFIG) -> Path:
    path = tmp_path / "groups.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def create_scope(database_url: str, config_path: Path, *, kind: str = "daily-report") -> int:
    sessions = session_factory(database_url)
    with sessions() as session:
        scope, reused = create_or_get_scope(
            session=session,
            kind=kind,
            window_start=START,
            window_end=END,
            config_path=config_path,
        )
        assert reused is False
        return scope.id


def test_scope_create_reuses_before_reading_changed_registry(
    migrated_database: str, tmp_path: Path
) -> None:
    config = write_config(tmp_path)
    scope_id = create_scope(migrated_database, config)
    config.write_text(CONFIG.replace("external_id: oc_b", "external_id: oc_changed"), encoding="utf-8")
    sessions = session_factory(migrated_database)
    with sessions() as session:
        scope, reused = create_or_get_scope(
            session=session,
            kind="daily-report",
            window_start=START,
            window_end=END,
            config_path=tmp_path / "missing.yaml",
        )
    assert reused is True
    assert scope.id == scope_id
    assert [group.external_id for group in scope.groups] == ["oc_a", "oc_b"]


def test_scope_create_handles_unique_conflict_by_returning_existing(
    migrated_database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_config(tmp_path)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        original_flush = session.flush
        injected = False

        def racing_flush(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal injected
            if not injected:
                injected = True
                with sessions() as other:
                    row = ExecutionScopeRow(
                        kind="daily-report",
                        window_start=START,
                        window_end=END,
                        scope_fingerprint="a" * 64,
                    )
                    other.add(row)
                    other.commit()
                raise IntegrityError("insert", {}, Exception("unique"))
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", racing_flush)
        scope, reused = create_or_get_scope(
            session=session,
            kind="daily-report",
            window_start=START,
            window_end=END,
            config_path=config,
        )
    assert reused is True
    assert scope.id == 1


def test_scope_create_requires_active_registry(migrated_database: str, tmp_path: Path) -> None:
    config = write_config(tmp_path, CONFIG.replace("status: active", "status: paused"))
    sessions = session_factory(migrated_database)
    with sessions() as session:
        with pytest.raises(ValueError, match="至少需要一个 active"):
            create_or_get_scope(
                session=session,
                kind="daily-report",
                window_start=START,
                window_end=END,
                config_path=config,
            )
        assert session.scalar(select(func.count()).select_from(ExecutionScopeRow)) == 0


def test_scope_groups_external_identity_is_unique(
    migrated_database: str, tmp_path: Path
) -> None:
    scope_id = create_scope(migrated_database, write_config(tmp_path))
    sessions = session_factory(migrated_database)
    with sessions() as session:
        session.add(
            ExecutionScopeGroupRow(
                execution_scope_id=scope_id,
                registry_key="duplicate",
                provider="feishu",
                external_id="oc_a",
                ordinal=99,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_scope_show_returns_only_metadata_and_groups(
    migrated_database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = write_config(tmp_path)
    scope_id = create_scope(migrated_database, config)
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", migrated_database)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    assert main(["execution-scope", "show", "--id", str(scope_id)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope_id"] == scope_id
    assert len(payload["groups"]) == 2
    assert "content" not in json.dumps(payload)


class ScopeCollector:
    provider = "feishu"

    def fetch(self, *, chat_id, since, until, cursor=None):  # type: ignore[no-untyped-def]
        raw = RawMessage(
            provider="feishu",
            chat_id=chat_id,
            platform_message_id=f"msg-{chat_id}",
            sent_at=since,
            sender_id="member",
            sender_name="成员",
            content=f"来自 {chat_id}",
            raw_payload={"chat_id": chat_id},
        )
        return CollectionBatch(
            provider="feishu",
            chat_id=chat_id,
            messages=(raw,),
            next_cursor=SyncCursor(provider="feishu", chat_id=chat_id, watermark=raw.sent_at),
        )


def test_scoped_collect_uses_scope_window_and_binds_run(
    migrated_database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = write_config(tmp_path, CONFIG.replace("external_id: oc_b", "external_id: oc_b\n    status: paused", 1))
    scope_id = create_scope(migrated_database, config)
    monkeypatch.setattr(cli_module, "FeishuCollector", lambda **_kwargs: ScopeCollector())
    monkeypatch.setattr(cli_module, "upgrade_database", lambda _url: None)
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", migrated_database)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    assert (
        main(
            [
                "collect",
                "--scope-id",
                str(scope_id),
                "--group-key",
                "group-a",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_scope_id"] == scope_id
    sessions = session_factory(migrated_database)
    with sessions() as session:
        run = session.get(CollectionRunRow, payload["collection_run_id"])
        assert run is not None
        assert run.execution_scope_id == scope_id
        assert run.requested_since == START.replace(tzinfo=None)
        assert run.requested_until == END.replace(tzinfo=None)


def test_scoped_collect_rejects_second_window_and_unknown_group(
    migrated_database: str, tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    config = write_config(tmp_path)
    scope_id = create_scope(migrated_database, config)
    monkeypatch.setattr(cli_module, "upgrade_database", lambda _url: None)
    monkeypatch.setenv("GROUP_INFO_DATABASE_URL", migrated_database)
    from group_info_system.config import get_settings

    get_settings.cache_clear()
    assert main(["collect", "--scope-id", str(scope_id), "--group-key", "missing"]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "SCOPE_GROUP_NOT_FOUND"
    assert (
        main(
            [
                "collect",
                "--scope-id",
                str(scope_id),
                "--group-key",
                "group-a",
                "--since",
                START.isoformat(),
                "--until",
                END.isoformat(),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error_code"] == "ValueError"


def _seed_scope_collection(
    database_url: str,
    scope_id: int,
    group_key: str,
    external_id: str,
    *,
    legacy: bool = False,
    with_message: bool = False,
) -> None:
    sessions = session_factory(database_url)
    with sessions() as session:
        run = CollectionRunRow(
            provider="feishu",
            chat_external_id=external_id,
            requested_since=START,
            requested_until=END,
            status="succeeded",
            fetched_count=0,
            inserted_count=0,
            execution_scope_id=None if legacy else scope_id,
        )
        session.add(run)
        session.flush()
        if with_message:
            MessageRepository(session).ingest(
                (
                    RawMessage(
                        provider="feishu",
                        chat_id=external_id,
                        platform_message_id=f"message-{external_id}",
                        sent_at=START,
                        sender_id="member",
                        sender_name="成员",
                        content=f"来自 {external_id}",
                        raw_payload={"external_id": external_id},
                    ),
                ),
                run,
            )
        session.commit()


def test_scoped_analysis_requires_every_scope_collection_and_binds_scope(
    migrated_database: str, tmp_path: Path
) -> None:
    config = write_config(tmp_path)
    scope_id = create_scope(migrated_database, config)
    _seed_scope_collection(migrated_database, scope_id, "group-a", "oc_a")
    sessions = session_factory(migrated_database)
    with sessions() as session, pytest.raises(Exception) as caught:
        create_analysis_request(
            session=session,
            model="mock-model",
            window_start=START,
            window_end=END,
            execution_scope_id=scope_id,
        )
        assert getattr(caught.value, "error_code", None) == "SCOPE_COLLECTION_INCOMPLETE"
        assert session.scalar(select(func.count()).select_from(AnalysisRunRow)) == 0

    _seed_scope_collection(migrated_database, scope_id, "group-b", "oc_b", with_message=True)
    _seed_scope_collection(migrated_database, scope_id, "outside", "oc_outside", legacy=True, with_message=True)
    _seed_scope_collection(migrated_database, scope_id, "group-a", "oc_a", with_message=True)
    with sessions() as session:
        outcome = create_analysis_request(
            session=session,
            model="mock-model",
            window_start=START,
            window_end=END,
            execution_scope_id=scope_id,
        )
        run = session.get(AnalysisRunRow, outcome.envelope.analysis_run_id)
        assert run is not None and run.execution_scope_id == scope_id
        assert run.window_start == START.replace(tzinfo=None)
        assert run.window_end == END.replace(tzinfo=None)
        assert outcome.input_count == 2
        input_ids = session.scalars(
            select(AnalysisInputRow.message_version_id).where(
                AnalysisInputRow.analysis_run_id == outcome.envelope.analysis_run_id
            )
        ).all()
        assert len(input_ids) == 2


def test_legacy_collection_does_not_satisfy_scoped_analysis(
    migrated_database: str, tmp_path: Path
) -> None:
    scope_id = create_scope(migrated_database, write_config(tmp_path))
    _seed_scope_collection(migrated_database, scope_id, "group-a", "oc_a", legacy=True)
    sessions = session_factory(migrated_database)
    with sessions() as session, pytest.raises(Exception) as caught:
        create_analysis_request(
            session=session,
            model="mock-model",
            window_start=START,
            window_end=END,
            execution_scope_id=scope_id,
        )
    assert getattr(caught.value, "error_code", None) == "SCOPE_COLLECTION_INCOMPLETE"
