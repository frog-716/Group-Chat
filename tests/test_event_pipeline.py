from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from group_info_system.application.collection import collect_into_store
from group_info_system.application.reporting import build_report_from_store
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.db.models import AnalysisInputRow, AnalysisRunRow, EventEvidenceRow, EventRow
from group_info_system.db.session import session_factory
from group_info_system.domain.analysis import EventAnalysisResult, EventCandidate


class InvalidEvidenceAnalyzer:
    name = "invalid-evidence-test"
    model = "none"
    prompt_version = "test-v1"
    schema_version = "event-v2"

    def analyze(self, _messages):  # type: ignore[no-untyped-def]
        return EventAnalysisResult(
            analyzer=self.name,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            events=(
                EventCandidate(
                    event_type="information",
                    report_section="today",
                    title="无效事件",
                    summary="引用了不存在的消息版本",
                    analysis_kind="analysis",
                    event_time=datetime(2026, 9, 10, tzinfo=UTC),
                    message_version_ids=(999_999,),
                ),
            ),
        )


def test_event_cannot_reference_message_version_outside_analysis_input(
    migrated_database: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests" / "fixtures" / "simulated_messages.json"
    start = datetime(2026, 9, 9, 22, tzinfo=UTC)
    end = datetime(2026, 9, 10, 22, tzinfo=UTC)
    sessions = session_factory(migrated_database)
    with sessions() as session:
        collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture),
            chat_id="demo-product-group",
            since=start,
            until=end,
        )

    with sessions() as session, pytest.raises(ValueError, match="message_version_id"):
        build_report_from_store(
            session=session,
            analyzer=InvalidEvidenceAnalyzer(),
            window_start=start,
            window_end=end,
            output_dir=tmp_path / "output",
            virtual_data=True,
        )

    with sessions() as session:
        run = session.scalar(select(AnalysisRunRow))
        assert run is not None
        assert run.status == "failed"
        assert run.structured_output is None
        assert session.scalar(select(func.count()).select_from(AnalysisInputRow)) == 6
        assert session.scalar(select(func.count()).select_from(EventRow)) == 0
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 0
