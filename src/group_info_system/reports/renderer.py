from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from group_info_system.domain.reports import Report


@dataclass(frozen=True)
class ReportIndexEntry:
    report_date: str
    month: str
    day: str
    title: str
    href: str
    message_count: int
    event_count: int
    source_groups: tuple[str, ...]


@dataclass(frozen=True)
class ReportMonth:
    label: str
    reports: tuple[ReportIndexEntry, ...]


def template_environment() -> Environment:
    template_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    return Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(("html", "xml")),
    )


def render_report(report: Report, *, overview_href: str = "../index.html") -> str:
    return template_environment().get_template("report.html").render(
        report=report,
        overview_href=overview_href,
    )


def write_report_html(
    report: Report,
    output: Path,
    *,
    overview_href: str = "../index.html",
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(report, overview_href=overview_href),
        encoding="utf-8",
    )


def _report_date(report: Report, report_path: Path) -> str:
    candidates = (report_path.parent.name, report.title.rsplit("·", 1)[-1].strip())
    for candidate in candidates:
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            continue
    return report.source_scope.end.date().isoformat()


def report_index_entries(output_root: Path) -> tuple[ReportIndexEntry, ...]:
    entries: list[ReportIndexEntry] = []
    for report_path in output_root.glob("*/report.json"):
        html_path = report_path.with_suffix(".html")
        if not html_path.is_file():
            continue
        try:
            report = Report.model_validate_json(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        report_date = _report_date(report, report_path)
        entries.append(
            ReportIndexEntry(
                report_date=report_date,
                month=report_date[:7],
                day=report_date[5:],
                title=report.title,
                href=html_path.relative_to(output_root).as_posix(),
                message_count=report.source_scope.message_count,
                event_count=sum(len(section.items) for section in report.sections),
                source_groups=report.source_scope.source_groups,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.report_date, reverse=True))


def render_report_index(output_root: Path) -> str:
    entries = report_index_entries(output_root)
    month_labels = tuple(dict.fromkeys(entry.month for entry in entries))
    months = tuple(
        ReportMonth(
            label=month,
            reports=tuple(entry for entry in entries if entry.month == month),
        )
        for month in month_labels
    )
    return template_environment().get_template("report_index.html").render(
        months=months,
        report_count=len(entries),
        message_count=sum(entry.message_count for entry in entries),
        event_count=sum(entry.event_count for entry in entries),
    )


def write_report_index(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / "index.html"
    temporary = output_root / ".index.html.tmp"
    temporary.write_text(render_report_index(output_root), encoding="utf-8")
    temporary.replace(output)
    return output
