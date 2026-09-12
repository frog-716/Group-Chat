from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from group_info_system.analysis.base import Analyzer
from group_info_system.analysis.validator import validate_event_analysis
from group_info_system.db.models import AnalysisRunRow
from group_info_system.db.repositories import MessageRepository, ReportRepository, StoredMessage


@dataclass(frozen=True)
class AnalysisOutcome:
    analysis_run_id: int
    input_count: int
    event_count: int
    analyzer: str
    model: str
    prompt_version: str
    schema_version: str


def analysis_input_hash(messages: list[StoredMessage]) -> str:
    material = json.dumps(
        [
            {
                "message_version_id": row.version_id,
                "evidence_id": row.evidence_id,
                "content": row.content,
            }
            for row in messages
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def analyze_into_store(
    *,
    session: Session,
    analyzer: Analyzer,
    window_start: datetime,
    window_end: datetime,
) -> AnalysisOutcome:
    messages = MessageRepository(session).current_reportable(window_start, window_end)
    repository = ReportRepository(session)
    run = repository.start_analysis(
        analyzer=analyzer.name,
        model=analyzer.model,
        prompt_version=analyzer.prompt_version,
        schema_version=analyzer.schema_version,
        input_hash=analysis_input_hash(messages),
        window_start=window_start,
        window_end=window_end,
    )
    run_id = run.id
    repository.save_analysis_inputs(analysis_run_id=run_id, messages=messages)
    session.commit()

    try:
        result = analyzer.analyze(messages)
        errors = validate_event_analysis(result, messages)
        if errors:
            raise ValueError("Event 校验失败：" + "；".join(errors))
        repository.save_events(
            analysis_run_id=run_id,
            analysis=result,
            messages=messages,
        )
        repository.finish_analysis(run, structured_output=result.model_dump_json())
        session.commit()
        return AnalysisOutcome(
            analysis_run_id=run_id,
            input_count=len(messages),
            event_count=len(result.events),
            analyzer=result.analyzer,
            model=result.model,
            prompt_version=result.prompt_version,
            schema_version=result.schema_version,
        )
    except Exception as exc:
        session.rollback()
        failed_run = session.get(AnalysisRunRow, run_id)
        if failed_run is not None:
            repository.fail_analysis(failed_run)
            session.commit()
        exc.__dict__["run_id"] = run_id
        raise
