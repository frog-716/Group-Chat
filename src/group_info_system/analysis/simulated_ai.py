from __future__ import annotations

import re

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import EventAnalysisResult, EventCandidate

LOW_VALUE = {"好", "好的", "收到", "谢谢", "ok", "OK", "哈哈", "👍", "1"}
HOWTO = ("步骤", "先", "然后", "执行", "配置", "操作", "安装")
PITFALLS = ("注意", "风险", "失败", "错误", "不要", "问题")
NEXT = ("明天", "后续", "下一步", "待办", "计划", "继续")


class SimulatedAIAnalyzer:
    """Deterministic stand-in for a structured LLM during credential-free MVP review."""

    name = "simulated-structured-ai-v1"
    model = "mock-rules-v1"
    prompt_version = "mock-event-v1"
    schema_version = "event-v2"

    def analyze(self, messages: list[StoredMessage]) -> EventAnalysisResult:
        valuable = [row for row in messages if self._valuable(row.content)]
        events: list[EventCandidate] = []
        seen: set[tuple[str, int]] = set()
        for message in valuable:
            report_section = self._report_section(message.content)
            event_type = self._event_type(message.content, report_section)
            key = (report_section, message.version_id)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventCandidate(
                    event_type=event_type,  # type: ignore[arg-type]
                    report_section=report_section,  # type: ignore[arg-type]
                    title=self._title(message.content),
                    summary=self._summary(event_type),
                    analysis_kind=(
                        "source_fact"
                        if event_type in {"progress", "decision", "information"}
                        else "analysis"
                    ),
                    event_time=message.sent_at,
                    message_version_ids=(message.version_id,),
                )
            )
        return EventAnalysisResult(
            analyzer=self.name,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            events=tuple(events[:12]),
        )

    @staticmethod
    def _valuable(content: str) -> bool:
        text = content.strip()
        return bool(text and text not in LOW_VALUE and not re.fullmatch(r"[\W_]+", text))

    @staticmethod
    def _report_section(content: str) -> str:
        if any(word in content for word in PITFALLS):
            return "pitfalls"
        if any(word in content for word in NEXT):
            return "next"
        if any(word in content for word in HOWTO):
            return "howto"
        return "today"

    @staticmethod
    def _event_type(content: str, report_section: str) -> str:
        if "？" in content or "?" in content or any(word in content for word in ("是否", "吗")):
            return "question"
        if any(word in content for word in ("决定", "确定", "统一", "采用")):
            return "decision"
        if report_section == "pitfalls":
            return "risk"
        if report_section in {"howto", "next"}:
            return "action"
        if any(word in content for word in ("完成", "已经", "已", "上线", "发布")):
            return "progress"
        return "information"

    @staticmethod
    def _title(content: str) -> str:
        compact = re.sub(r"\s+", " ", content).strip("，。！？!?；; ")
        return compact[:26] + ("…" if len(compact) > 26 else "")

    @staticmethod
    def _summary(event_type: str) -> str:
        return {
            "progress": "该事件记录了一项可核验的进展，具体结果见原始证据。",
            "decision": "该事件记录了一项已经形成的决定或统一口径，具体内容见原始证据。",
            "action": "该事件包含需要执行的方法或后续行动，具体内容见原始证据。",
            "risk": "该事件指出了可能影响结果的风险或约束，具体内容见原始证据。",
            "question": "该事件提出了需要进一步回答或澄清的问题，具体内容见原始证据。",
            "information": "该事件提供了与报告范围相关的事实背景，具体内容见原始证据。",
        }[event_type]
