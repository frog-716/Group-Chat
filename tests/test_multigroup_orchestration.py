from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from group_info_system.application.collection import collect_into_store_with_outcome
from group_info_system.application.execution_scope import create_or_get_scope, get_scope
from group_info_system.application.handoff import create_analysis_request, import_analysis_response
from group_info_system.application.reporting import build_daily_report_from_analysis
from group_info_system.db.models import (
    AnalysisRunRow,
    CollectionRunRow,
    ExecutionScopeRow,
    ReportRow,
)
from group_info_system.db.session import session_factory
from group_info_system.domain.handoff import WorkBuddyResponseEnvelope
from group_info_system.domain.messages import CollectionBatch, RawMessage, SyncCursor

START = datetime(2026, 9, 11, 22, 0, tzinfo=UTC)
END = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)


def registry_config(tmp_path: Path, groups: str) -> Path:
    path = tmp_path / "groups.yaml"
    path.write_text(
        "version: group-info/groups/v1\n"
        "defaults:\n  timezone: Asia/Shanghai\n  day_cutoff: '06:00'\n"
        f"groups:\n{groups}",
        encoding="utf-8",
    )
    return path


TWO_GROUPS = (
    "  - key: group-a\n    provider: feishu\n    external_id: oc_a\n"
    "    display_name: 群 A\n    status: active\n"
    "  - key: group-b\n    provider: feishu\n    external_id: oc_b\n"
    "    display_name: 群 B\n    status: active\n"
)


class MockCollector:
    provider = "feishu"

    def __init__(self, *, fail_ids: set[str] | None = None, empty: bool = True) -> None:
        self.fail_ids = fail_ids or set()
        self.empty = empty
        self.calls: list[str] = []

    def fetch(self, *, chat_id, since, until, cursor=None):  # type: ignore[no-untyped-def]
        self.calls.append(chat_id)
        if chat_id in self.fail_ids:
            raise RuntimeError(f"mock failure for {chat_id}")
        messages = ()
        if not self.empty:
            messages = (
                RawMessage(
                    provider="feishu",
                    chat_id=chat_id,
                    platform_message_id=f"msg-{chat_id}",
                    sent_at=since,
                    sender_id="member",
                    sender_name="成员",
                    content=f"来自 {chat_id}",
                    raw_payload={"chat_id": chat_id},
                ),
            )
        return CollectionBatch(
            provider="feishu",
            chat_id=chat_id,
            messages=messages,
            next_cursor=SyncCursor(provider="feishu", chat_id=chat_id, watermark=until),
        )


class MockTransport:
    def __init__(self) -> None:
        self.calls = 0

    def response(self, request) -> str:  # type: ignore[no-untyped-def]
        self.calls += 1
        return WorkBuddyResponseEnvelope(
            protocol_version="group-info.llm-response/v1",
            request_id=request.request_id,
            analysis_run_id=request.analysis_run_id,
            input_hash=request.input.input_hash,
            model=request.model,
            prompt_version=request.prompt.version,
            schema_version=request.response_schema.version,
            output={"events": []},
            transport={"status": "succeeded", "attempt": 1, "error_code": None},
        ).model_dump_json()


def make_scope(database_url: str, config: Path) -> int:
    with session_factory(database_url)() as session:
        scope, reused = create_or_get_scope(
            session=session, kind="daily-report", window_start=START,
            window_end=END, config_path=config,
        )
        assert not reused
        return scope.id


def collect_scope(database_url: str, scope_id: int, collector: MockCollector) -> list[int]:
    with session_factory(database_url)() as session:
        scope = get_scope(session, scope_id)
        ids = []
        for group in scope.groups:
            try:
                outcome = collect_into_store_with_outcome(
                    session=session, collector=collector, chat_id=group.external_id,
                    since=scope.window_start, until=scope.window_end,
                    execution_scope_id=scope.id,
                )
                ids.append(outcome.collection_run_id)
            except RuntimeError:
                # Collection owns the failed run; the orchestrator stops at this group.
                break
        return ids


def finish_empty_report(database_url: str, scope_id: int, output_dir: Path, transport: MockTransport) -> tuple[int, int]:
    with session_factory(database_url)() as session:
        outcome = create_analysis_request(
            session=session, model="mock-runtime", window_start=START,
            window_end=END, execution_scope_id=scope_id,
        )
        import_analysis_response(session=session, analysis_run_id=outcome.envelope.analysis_run_id,
                                 raw_response=transport.response(outcome.envelope))
        report = build_daily_report_from_analysis(
            session=session, analysis_run_id=outcome.envelope.analysis_run_id,
            output_root=output_dir.parent, output_dir=output_dir,
            local_timezone="Asia/Shanghai",
        )
        return outcome.envelope.analysis_run_id, report.report_id


def test_two_active_groups_one_analysis_one_llm_one_report(migrated_database: str, tmp_path: Path) -> None:
    scope_id = make_scope(migrated_database, registry_config(tmp_path, TWO_GROUPS))
    collector = MockCollector(empty=False)
    assert len(collect_scope(migrated_database, scope_id, collector)) == 2
    transport = MockTransport()
    analysis_id, report_id = finish_empty_report(migrated_database, scope_id, tmp_path / "report", transport)
    assert transport.calls == 1
    with session_factory(migrated_database)() as session:
        assert session.get(AnalysisRunRow, analysis_id).status == "succeeded"  # type: ignore[union-attr]
        assert session.get(ReportRow, report_id).status == "succeeded"  # type: ignore[union-attr]
        assert session.scalar(select(func.count()).select_from(CollectionRunRow)) == 2


def test_partial_collection_failure_preserves_runs_and_blocks_analysis(
    migrated_database: str, tmp_path: Path
) -> None:
    scope_id = make_scope(migrated_database, registry_config(tmp_path, TWO_GROUPS))
    collector = MockCollector(fail_ids={"oc_b"})
    assert len(collect_scope(migrated_database, scope_id, collector)) == 1
    with session_factory(migrated_database)() as session:
        statuses = session.scalars(select(CollectionRunRow.status).order_by(CollectionRunRow.id)).all()
        assert statuses == ["succeeded", "failed"]
        assert session.scalar(select(func.count()).select_from(AnalysisRunRow)) == 0
        assert session.scalar(select(func.count()).select_from(ReportRow)) == 0


def test_zero_messages_both_groups_still_produce_valid_report(
    migrated_database: str, tmp_path: Path
) -> None:
    scope_id = make_scope(migrated_database, registry_config(tmp_path, TWO_GROUPS))
    assert len(collect_scope(migrated_database, scope_id, MockCollector(empty=True))) == 2
    analysis_id, report_id = finish_empty_report(
        migrated_database, scope_id, tmp_path / "empty-report", MockTransport()
    )
    with session_factory(migrated_database)() as session:
        assert session.get(AnalysisRunRow, analysis_id).status == "succeeded"  # type: ignore[union-attr]
        assert session.get(ReportRow, report_id).status == "succeeded"  # type: ignore[union-attr]


def test_registry_drift_and_recovery_keep_same_scope(migrated_database: str, tmp_path: Path) -> None:
    config = registry_config(tmp_path, TWO_GROUPS)
    scope_id = make_scope(migrated_database, config)
    config.write_text(config.read_text().replace("oc_b", "oc_c").replace("group-b", "group-c"), encoding="utf-8")
    with session_factory(migrated_database)() as session:
        reused_scope, reused = create_or_get_scope(
            session=session, kind="daily-report", window_start=START,
            window_end=END, config_path=config,
        )
        assert reused and reused_scope.id == scope_id
        assert [group.external_id for group in reused_scope.groups] == ["oc_a", "oc_b"]
    first = MockCollector(fail_ids={"oc_b"})
    collect_scope(migrated_database, scope_id, first)
    retry = MockCollector()
    collect_scope(migrated_database, scope_id, retry)
    # Existing watermark makes A a no-op; only failed B needs a transport fetch.
    assert retry.calls == ["oc_b"]
    with session_factory(migrated_database)() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionScopeRow)) == 1


def test_single_active_scope_path_remains_valid(migrated_database: str, tmp_path: Path) -> None:
    one = TWO_GROUPS.replace("  - key: group-b\n    provider: feishu\n    external_id: oc_b\n    display_name: 群 B\n    status: active\n", "")
    scope_id = make_scope(migrated_database, registry_config(tmp_path, one))
    assert len(collect_scope(migrated_database, scope_id, MockCollector())) == 1
