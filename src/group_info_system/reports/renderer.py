from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from group_info_system.domain.reports import Report


def template_environment() -> Environment:
    template_dir = Path(__file__).resolve().parents[1] / "web" / "templates"
    return Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(("html", "xml")),
    )


def render_report(report: Report) -> str:
    return template_environment().get_template("report.html").render(report=report)


def write_report_html(report: Report, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(report), encoding="utf-8")
