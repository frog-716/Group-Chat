from __future__ import annotations

from datetime import datetime

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import AnalysisResult
from group_info_system.domain.reports import Report, ReportItem, ReportSection, SourceScope

SECTION_TITLES = {
    "today": "今天群里发生了什么",
    "howto": "怎么跟着做",
    "pitfalls": "容易踩坑的地方",
    "next": "接下来继续追什么",
}
NO_EVENTS_MESSAGE = "今日暂无有效事件"


def build_report(
    *,
    messages: list[StoredMessage],
    analysis: AnalysisResult,
    window_start: datetime,
    window_end: datetime,
    virtual_data: bool,
) -> Report:
    by_id = {message.evidence_id: message for message in messages}
    sections = []
    for section_id, title in SECTION_TITLES.items():
        items = []
        for insight in analysis.items:
            if insight.category != section_id:
                continue
            evidence = [by_id[value] for value in insight.evidence_ids if value in by_id]
            if not evidence:
                continue
            ids = tuple(row.evidence_id for row in evidence)
            items.append(
                ReportItem(
                    title=insight.title,
                    summary=insight.summary,
                    analysis_kind=insight.analysis_kind,
                    source_label="、".join(sorted({row.chat_name for row in evidence})),
                    evidence_ids=ids,
                    evidence_quotes={row.evidence_id: row.content for row in evidence},
                )
            )
        sections.append(ReportSection(id=section_id, title=title, items=tuple(items)))  # type: ignore[arg-type]
    return Report(
        title=f"群聊信息报告 · {window_end.astimezone().date().isoformat()}",
        source_scope=SourceScope(
            start=window_start,
            end=window_end,
            message_count=len(messages),
            source_groups=tuple(sorted({row.chat_name for row in messages})),
            virtual_data=virtual_data,
        ),
        sections=tuple(sections),
        exclusions=(
            ()
            if analysis.items
            else ({"code": "NO_VALID_EVENTS", "message": NO_EVENTS_MESSAGE},)
        ),
        evidence_notes="每条结论引用不可变消息版本；摘录必须与入库正文逐字一致。",
        generated_by=analysis.analyzer,
    )
