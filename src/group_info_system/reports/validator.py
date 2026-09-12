from __future__ import annotations

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.reports import Report

from .builder import SECTION_TITLES


def validate_report(report: Report, messages: list[StoredMessage]) -> list[str]:
    errors: list[str] = []
    if [section.id for section in report.sections] != list(SECTION_TITLES):
        errors.append("报告分区 ID 或顺序错误")
    if [section.title for section in report.sections] != list(SECTION_TITLES.values()):
        errors.append("报告分区标题或顺序错误")
    by_key = {row.evidence_id: row for row in messages}
    for section in report.sections:
        for item in section.items:
            if not item.evidence_ids:
                errors.append(f"{section.id}/{item.title} 缺少证据")
            if set(item.evidence_quotes) != set(item.evidence_ids):
                errors.append(f"{section.id}/{item.title} 证据摘录与 ID 不一致")
            for key in item.evidence_ids:
                message = by_key.get(key)
                quote = item.evidence_quotes.get(key, "")
                if message is None:
                    errors.append(f"{section.id}/{item.title} 引用了不存在的证据 {key}")
                elif not quote or quote not in message.content:
                    errors.append(f"{section.id}/{item.title} 的证据摘录不在原消息中")
    if report.source_scope.message_count != len(messages):
        errors.append("报告消息数与输入不一致")
    return errors
