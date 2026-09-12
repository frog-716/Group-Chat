from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from group_info_system.domain.analysis import AnalysisResult
from group_info_system.reports.builder import build_report
from group_info_system.reports.validator import validate_report


def test_report_layer_does_not_import_collectors() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "group_info_system" / "reports"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(name.startswith("group_info_system.collectors") for name in imports)


def test_report_builder_accepts_empty_events_and_messages() -> None:
    start = datetime(2027, 1, 1, tzinfo=UTC)
    report = build_report(
        messages=[],
        analysis=AnalysisResult(analyzer="empty-test", items=()),
        window_start=start,
        window_end=start + timedelta(days=1),
        virtual_data=False,
    )

    assert report.source_scope.message_count == 0
    assert all(not section.items for section in report.sections)
    assert report.exclusions == (
        {"code": "NO_VALID_EVENTS", "message": "今日暂无有效事件"},
    )
    assert validate_report(report, []) == []
