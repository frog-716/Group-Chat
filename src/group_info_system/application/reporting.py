from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from group_info_system.analysis.base import Analyzer
from group_info_system.application.analysis import analyze_into_store
from group_info_system.db.models import ReportRow
from group_info_system.db.repositories import ReportRepository, db_datetime_as_utc
from group_info_system.domain.analysis import AnalysisItem, AnalysisResult
from group_info_system.domain.reports import ReportEnvelope
from group_info_system.reports.builder import build_report
from group_info_system.reports.renderer import write_report_html
from group_info_system.reports.validator import validate_report


def build_daily_report_from_analysis(
    *,
    session: Session,
    analysis_run_id: int,
    output_root: Path,
    local_timezone: str,
    output_dir: Path | None = None,
    virtual_data: bool = False,
) -> ReportEnvelope:
    repository = ReportRepository(session)
    analysis_run = repository.get_analysis_run(analysis_run_id)
    if analysis_run is None:
        raise ValueError(f"Analysis Run 不存在：{analysis_run_id}")
    if analysis_run.status != "succeeded":
        raise ValueError(
            f"Analysis Run {analysis_run_id} 状态为 {analysis_run.status}，不能生成报告"
        )

    window_start = db_datetime_as_utc(analysis_run.window_start)
    window_end = db_datetime_as_utc(analysis_run.window_end)
    messages = repository.analysis_messages(analysis_run_id)
    events = repository.analysis_events(analysis_run_id)

    report_repository = ReportRepository(session)
    report_row = report_repository.start_report(
        analysis_run_id=analysis_run_id,
        window_start=window_start,
        window_end=window_end,
    )
    report_id = report_row.id
    session.commit()

    try:
        analysis = AnalysisResult(
            analyzer=analysis_run.analyzer,
            items=tuple(
                AnalysisItem(
                    category=event.report_section,  # type: ignore[arg-type]
                    title=event.title,
                    summary=event.summary,
                    analysis_kind=event.analysis_kind,  # type: ignore[arg-type]
                    evidence_ids=event.evidence_ids,
                )
                for event in events
            ),
        )
        report = build_report(
            messages=messages,
            analysis=analysis,
            window_start=window_start,
            window_end=window_end,
            virtual_data=virtual_data,
        )
        errors = validate_report(report, messages)
        if errors:
            raise ValueError("报告校验失败：" + "；".join(errors))

        destination = output_dir or (
            output_root / window_end.astimezone(ZoneInfo(local_timezone)).date().isoformat()
        )
        destination.mkdir(parents=True, exist_ok=True)
        report_json = report.model_dump_json(indent=2)
        report_path = destination / "report.json"
        html_path = destination / "report.html"
        report_path.write_text(report_json + "\n", encoding="utf-8")
        write_report_html(report, html_path)

        report_repository.finish_report(report_row, report_json=report_json)
        session.commit()
        return ReportEnvelope(
            report=report,
            report_id=report_id,
            output_files=(str(report_path), str(html_path)),
        )
    except Exception as exc:
        session.rollback()
        failed_row = session.get(ReportRow, report_id)
        if failed_row is not None:
            report_repository.fail_report(failed_row)
            session.commit()
        exc.__dict__["run_id"] = report_id
        raise


def build_report_from_store(
    *,
    session: Session,
    analyzer: Analyzer,
    window_start: datetime,
    window_end: datetime,
    output_dir: Path,
    virtual_data: bool,
) -> ReportEnvelope:
    """Compatibility workflow used by the existing demo and end-to-end tests."""

    analysis = analyze_into_store(
        session=session,
        analyzer=analyzer,
        window_start=window_start,
        window_end=window_end,
    )
    return build_daily_report_from_analysis(
        session=session,
        analysis_run_id=analysis.analysis_run_id,
        output_root=output_dir.parent,
        local_timezone="Asia/Shanghai",
        output_dir=output_dir,
        virtual_data=virtual_data,
    )
