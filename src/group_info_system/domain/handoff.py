from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from group_info_system.analysis.llm import LLMEventPayload


class AnalysisWindowEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str
    end: str


class PromptEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    system_prompt: str


class ResponseSchemaEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    json_schema: dict[str, Any]


class AnalysisInputEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    window: AnalysisWindowEnvelope
    user_prompt: str


class WorkBuddyRequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: Literal["group-info.llm-request/v1"] = "group-info.llm-request/v1"
    request_id: str = Field(pattern=r"^arq_[0-9a-f]{32}$")
    analysis_run_id: int = Field(gt=0)
    purpose: Literal["event_extraction"] = "event_extraction"
    model: str = Field(min_length=1)
    prompt: PromptEnvelope
    response_schema: ResponseSchemaEnvelope
    input: AnalysisInputEnvelope


class TransportEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["succeeded", "failed"]
    attempt: int = Field(ge=1)
    error_code: str | None = None


class WorkBuddyResponseEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_version: Literal["group-info.llm-response/v1"]
    request_id: str
    analysis_run_id: int
    input_hash: str
    model: str
    prompt_version: str
    schema_version: str
    output: LLMEventPayload | None
    transport: TransportEnvelope

    @model_validator(mode="after")
    def output_matches_transport_status(self) -> WorkBuddyResponseEnvelope:
        if self.transport.status == "succeeded" and self.output is None:
            raise ValueError("successful transport must include output")
        if self.transport.status == "failed" and self.output is not None:
            raise ValueError("failed transport must not include output")
        return self
