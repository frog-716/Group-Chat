from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from group_info_system.config import Settings, get_settings
from group_info_system.db.repositories import ReportRepository
from group_info_system.db.session import session_factory
from group_info_system.domain.reports import Report
from group_info_system.reports.renderer import render_report


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    sessions = session_factory(settings.database_url)
    app = FastAPI(title="群聊信息系统", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def latest_report() -> HTMLResponse:
        with sessions() as session:
            row = ReportRepository(session).latest()
        if row is None:
            return HTMLResponse(
                "<main><h1>暂无报告</h1><p>请先运行模拟闭环或生成报告。</p></main>",
                status_code=200,
            )
        report = Report.model_validate(json.loads(row.report_json))
        return HTMLResponse(render_report(report))

    return app


app = create_app()
