from __future__ import annotations

from typing import Protocol

from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import EventAnalysisResult


class Analyzer(Protocol):
    name: str

    model: str
    prompt_version: str
    schema_version: str

    def analyze(self, messages: list[StoredMessage]) -> EventAnalysisResult: ...
