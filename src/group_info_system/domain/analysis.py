from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EventType = Literal["progress", "decision", "action", "risk", "question", "information"]
ReportSection = Literal["today", "howto", "pitfalls", "next"]
AnalysisCategory = ReportSection
AnalysisKind = Literal["source_fact", "user_quote", "analysis", "hypothesis"]


class EventTimeRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event times must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def range_is_ordered(self) -> EventTimeRange:
        if self.start > self.end:
            raise ValueError("event time range start must not be after end")
        return self


class EventCandidate(BaseModel):
    """A meaningful occurrence backed by exact immutable message versions.

    An event describes a fact, decision, action, risk, question, or useful piece of
    information. It must not be a generic wrapper around one source message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: EventType
    report_section: ReportSection
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    analysis_kind: AnalysisKind
    event_time: datetime | None = None
    time_range: EventTimeRange | None = None
    message_version_ids: tuple[int, ...] = Field(min_length=1)

    @field_validator("title", "summary")
    @classmethod
    def meaningful_text_is_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event title and summary cannot be blank")
        return value

    @field_validator("event_time")
    @classmethod
    def normalize_event_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event times must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("message_version_ids")
    @classmethod
    def evidence_versions_are_unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(value)) != len(value):
            raise ValueError("event evidence versions must be unique")
        return value

    @model_validator(mode="after")
    def exactly_one_time_shape(self) -> EventCandidate:
        if (self.event_time is None) == (self.time_range is None):
            raise ValueError("event must have exactly one of event_time or time_range")
        return self


class EventAnalysisResult(BaseModel):
    """Structured output of an analyzer before events are persisted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyzer: str
    model: str
    prompt_version: str
    schema_version: str
    events: tuple[EventCandidate, ...]


class AnalysisItem(BaseModel):
    """Compatibility projection consumed by the existing Report Builder."""

    model_config = ConfigDict(frozen=True)

    category: AnalysisCategory
    title: str
    summary: str
    analysis_kind: AnalysisKind
    evidence_ids: tuple[str, ...]


class AnalysisResult(BaseModel):
    """Compatibility projection from persisted events into the report contract."""

    model_config = ConfigDict(frozen=True)

    analyzer: str
    items: tuple[AnalysisItem, ...]
