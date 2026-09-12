from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    summary: str
    analysis_kind: Literal["source_fact", "user_quote", "analysis", "hypothesis"]
    source_label: str
    evidence_ids: tuple[str, ...]
    evidence_quotes: dict[str, str]


class ReportSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: Literal["today", "howto", "pitfalls", "next"]
    title: str
    items: tuple[ReportItem, ...] = ()


class SourceScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    message_count: int
    source_groups: tuple[str, ...]
    virtual_data: bool


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["mvp-v1"] = "mvp-v1"
    title: str
    source_scope: SourceScope
    sections: tuple[ReportSection, ...]
    exclusions: tuple[dict[str, str], ...] = ()
    evidence_notes: str
    generated_by: str


class ReportEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    report: Report
    report_id: int | None = None
    output_files: tuple[str, ...] = Field(default_factory=tuple)
