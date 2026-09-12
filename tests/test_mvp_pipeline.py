from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.collection import collect_into_store
from group_info_system.application.reporting import build_report_from_store
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.config import Settings
from group_info_system.db.models import (
    AnalysisInputRow,
    AnalysisRunRow,
    EventEvidenceRow,
    EventRow,
)
from group_info_system.db.session import session_factory
from group_info_system.web.app import create_app


def test_simulated_messages_to_database_analysis_json_and_html(
    migrated_database: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests" / "fixtures" / "simulated_messages.json"
    start = datetime(2026, 9, 9, 22, tzinfo=UTC)
    end = datetime(2026, 9, 10, 22, tzinfo=UTC)
    sessions = session_factory(migrated_database)

    with sessions() as session:
        batch = collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture),
            chat_id="demo-product-group",
            since=start,
            until=end,
        )
    assert len(batch.messages) == 8

    output = tmp_path / "output"
    with sessions() as session:
        envelope = build_report_from_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=start,
            window_end=end,
            output_dir=output,
            virtual_data=True,
        )
    payload = json.loads((output / "report.json").read_text(encoding="utf-8"))
    html = (output / "report.html").read_text(encoding="utf-8")
    assert payload["schema_version"] == "mvp-v1"
    assert payload["source_scope"]["message_count"] == 6
    assert payload["source_scope"]["source_groups"] == ["MVP 产品群（模拟）"]
    assert [section["id"] for section in payload["sections"]] == [
        "today",
        "howto",
        "pitfalls",
        "next",
    ]
    assert "模拟数据演示" in html
    assert "msgv_" in html
    assert envelope.report_id is not None

    with sessions() as session:
        run = session.scalar(select(AnalysisRunRow))
        assert run is not None
        assert run.analyzer == "simulated-structured-ai-v1"
        assert run.model == "mock-rules-v1"
        assert run.prompt_version == "mock-event-v1"
        assert run.schema_version == "event-v2"
        assert len(run.input_hash) == 64
        assert session.scalar(select(func.count()).select_from(AnalysisInputRow)) == 6
        assert session.scalar(select(func.count()).select_from(EventRow)) == 5
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 5
        orphan_evidence = session.execute(
            text(
                "SELECT ee.id FROM event_evidence ee "
                "LEFT JOIN message_versions mv ON mv.id = ee.message_version_id "
                "WHERE mv.id IS NULL"
            )
        ).all()
        assert orphan_evidence == []

    # A second collection uses the persisted cursor and does not duplicate raw versions.
    with sessions() as session:
        second_batch = collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture),
            chat_id="demo-product-group",
            since=start,
            until=end,
        )
    assert second_batch.mode == "incremental"
    with sessions() as session:
        assert len(session.execute(text("SELECT id FROM message_versions")).all()) == 8

    app = create_app(Settings(database_url=migrated_database, output_dir=output))

    async def request_report():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/")

    response = asyncio.run(request_report())
    assert response.status_code == 200
    assert "用户访谈完成了第一轮" in response.text
    assert "模拟数据演示" in response.text
