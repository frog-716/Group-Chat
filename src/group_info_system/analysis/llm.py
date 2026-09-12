from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import EventAnalysisResult, EventCandidate

from .prompts import VersionedPrompt, load_event_extraction_prompt
from .validator import validate_event_analysis


class LLMTransport(Protocol):
    """Provider boundary for one structured text completion."""

    def complete(self, *, model: str, system_prompt: str, user_prompt: str) -> str: ...


class LLMEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: tuple[EventCandidate, ...]


class LLMAnalyzerError(RuntimeError):
    pass


def build_event_request(messages: list[StoredMessage]) -> dict[str, object]:
    """Build the provider-neutral Event-v2 input shared by all LLM transports."""

    return {
        "contract": "event-v2",
        "messages": [
            {
                "message_version_id": message.version_id,
                "evidence_id": message.evidence_id,
                "sent_at": message.sent_at.isoformat(),
                "chat_name": message.chat_name,
                "sender_name": message.sender_name,
                "message_type": message.message_type,
                "content": message.content,
            }
            for message in messages
        ],
    }


class LLMEventAnalyzer:
    """Database-free adapter from message inputs to validated Event-v2 output."""

    name = "llm-event-analyzer"
    schema_version = "event-v2"

    def __init__(
        self,
        *,
        transport: LLMTransport,
        model: str,
        prompt: VersionedPrompt | None = None,
    ) -> None:
        self.transport = transport
        self.model = model
        self.prompt = prompt or load_event_extraction_prompt()
        self.prompt_version = self.prompt.version

    def analyze(self, messages: list[StoredMessage]) -> EventAnalysisResult:
        request = build_event_request(messages)
        try:
            raw_output = self.transport.complete(
                model=self.model,
                system_prompt=self.prompt.text,
                user_prompt=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception as exc:
            raise LLMAnalyzerError("LLM transport failed") from exc
        try:
            payload = LLMEventPayload.model_validate_json(raw_output)
        except ValidationError as exc:
            raise LLMAnalyzerError("LLM output is not valid Event-v2 JSON") from exc

        result = EventAnalysisResult(
            analyzer=self.name,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            events=payload.events,
        )
        errors = validate_event_analysis(result, messages)
        if errors:
            raise LLMAnalyzerError("Event-v2 validation failed: " + "; ".join(errors))
        return result
