from __future__ import annotations

import re

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import EventAnalysisResult

GENERIC_SUMMARY_PREFIXES = (
    "群内出现了一条值得保留的信息",
    "这条消息提供了可执行的方法",
    "这条消息指出了需要规避的问题",
    "这条消息提出了后续行动",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()


def validate_event_analysis(
    analysis: EventAnalysisResult, messages: list[StoredMessage]
) -> list[str]:
    """Validate objective contract rules before persisting analyzer output.

    Semantic paraphrase quality remains an Analyzer prompt/evaluation concern. This
    validator enforces a deterministic lower bound without pretending to understand
    meaning itself.
    """

    errors: list[str] = []
    by_version = {message.version_id: message for message in messages}
    known_versions = set(by_version)
    seen: dict[tuple[str, str, str, str], int] = {}
    for index, event in enumerate(analysis.events, start=1):
        evidence_ids = tuple(event.message_version_ids)
        if not evidence_ids:
            errors.append(f"Event {index} 没有 evidence")
            continue
        unknown = sorted(set(evidence_ids) - known_versions)
        if unknown:
            errors.append(
                f"Event {index} 引用了不属于本次分析输入的 message_version_id："
                + ", ".join(str(value) for value in unknown)
            )
            continue

        title = normalize_text(event.title)
        summary = normalize_text(event.summary)
        if not title or not summary:
            errors.append(f"Event {index} 是空意义 Event")
            continue
        if event.summary.startswith(GENERIC_SUMMARY_PREFIXES):
            errors.append(f"Event {index} 只是通用消息包装")
        if len(evidence_ids) == 1:
            source = normalize_text(by_version[evidence_ids[0]].content)
            if summary == source:
                errors.append(f"Event {index} 只是单消息原文改写")

        duplicate_key = (event.event_type, event.report_section, title, summary)
        first_index = seen.get(duplicate_key)
        if first_index is not None:
            errors.append(f"Event {index} 与 Event {first_index} 完全重复")
        else:
            seen[duplicate_key] = index
    return errors
