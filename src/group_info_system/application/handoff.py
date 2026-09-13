from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from group_info_system.analysis.llm import LLMEventPayload, build_event_request
from group_info_system.analysis.prompts import VersionedPrompt, load_event_extraction_prompt
from group_info_system.analysis.validator import validate_event_analysis
from group_info_system.application.analysis import analysis_input_hash
from group_info_system.application.execution_scope import (
    ExecutionScope,
    ensure_scope_collections_complete,
    get_scope,
)
from group_info_system.db.models import AnalysisRunRow
from group_info_system.db.repositories import (
    MessageRepository,
    ReportRepository,
    StoredMessage,
    db_datetime_as_utc,
)
from group_info_system.domain.analysis import EventAnalysisResult
from group_info_system.domain.handoff import (
    AnalysisInputEnvelope,
    AnalysisWindowEnvelope,
    PromptEnvelope,
    ResponseSchemaEnvelope,
    WorkBuddyRequestEnvelope,
    WorkBuddyResponseEnvelope,
)

ANALYZER_NAME = "llm-event-analyzer"
EVENT_SCHEMA_VERSION = "event-v2"


class AnalysisHandoffError(RuntimeError):
    pass


class AnalysisResponseMismatch(AnalysisHandoffError):
    pass


class AnalysisResponseInvalid(AnalysisHandoffError):
    pass


class AnalysisAlreadyFinalized(AnalysisHandoffError):
    pass


class WorkBuddyTransportFailed(AnalysisHandoffError):
    pass


@dataclass(frozen=True)
class AnalysisRequestOutcome:
    envelope: WorkBuddyRequestEnvelope
    input_count: int


@dataclass(frozen=True)
class AnalysisResponseOutcome:
    analysis_run_id: int
    event_count: int
    replayed: bool


class _ResponseIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol_version: str
    request_id: str
    analysis_run_id: int
    input_hash: str
    model: str
    prompt_version: str
    schema_version: str


def _strict_schema(schema: dict[str, object]) -> dict[str, object]:
    schema = copy.deepcopy(schema)

    def visit(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            node.pop("default", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def analysis_request_id(
    *,
    analysis_run_id: int,
    input_hash: str,
    model: str,
    prompt_version: str,
    schema_version: str,
) -> str:
    material = ":".join(
        (
            "group-info.llm-request/v1",
            str(analysis_run_id),
            input_hash,
            model,
            prompt_version,
            schema_version,
        )
    )
    return "arq_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _make_request_envelope(
    *,
    run: AnalysisRunRow,
    prompt: VersionedPrompt,
    messages: list[StoredMessage],
) -> WorkBuddyRequestEnvelope:
    request = build_event_request(messages)
    request_id = analysis_request_id(
        analysis_run_id=run.id,
        input_hash=run.input_hash,
        model=run.model,
        prompt_version=run.prompt_version,
        schema_version=run.schema_version,
    )
    return WorkBuddyRequestEnvelope(
        request_id=request_id,
        analysis_run_id=run.id,
        model=run.model,
        prompt=PromptEnvelope(version=prompt.version, system_prompt=prompt.text),
        response_schema=ResponseSchemaEnvelope(
            version=run.schema_version,
            json_schema=_strict_schema(LLMEventPayload.model_json_schema()),
        ),
        input=AnalysisInputEnvelope(
            input_hash=run.input_hash,
            window=AnalysisWindowEnvelope(
                start=db_datetime_as_utc(run.window_start).isoformat(),
                end=db_datetime_as_utc(run.window_end).isoformat(),
            ),
            user_prompt=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def create_analysis_request(
    *,
    session: Session,
    model: str,
    window_start: datetime,
    window_end: datetime,
    prompt: VersionedPrompt | None = None,
    execution_scope_id: int | None = None,
) -> AnalysisRequestOutcome:
    selected_prompt = prompt or load_event_extraction_prompt()
    scope: ExecutionScope | None = None
    message_repository = MessageRepository(session)
    if execution_scope_id is not None:
        scope = get_scope(session, execution_scope_id)
        ensure_scope_collections_complete(session, scope)
        window_start = scope.window_start
        window_end = scope.window_end
        scope_groups = tuple((group.provider, group.external_id) for group in scope.groups)
        messages = message_repository.current_reportable_for_scope(
            window_start, window_end, scope_groups
        )
    else:
        messages = message_repository.current_reportable(window_start, window_end)
    repository = ReportRepository(session)
    run = repository.start_analysis(
        analyzer=ANALYZER_NAME,
        model=model,
        prompt_version=selected_prompt.version,
        schema_version=EVENT_SCHEMA_VERSION,
        input_hash=analysis_input_hash(messages),
        window_start=window_start,
        window_end=window_end,
        execution_scope_id=execution_scope_id,
    )
    run_id = run.id
    repository.save_analysis_inputs(analysis_run_id=run_id, messages=messages)
    session.commit()

    try:
        envelope = _make_request_envelope(
            run=run,
            prompt=selected_prompt,
            messages=messages,
        )
    except Exception as exc:
        repository.fail_analysis(run)
        session.commit()
        exc.__dict__["run_id"] = run_id
        raise
    return AnalysisRequestOutcome(envelope=envelope, input_count=len(messages))


def mark_analysis_failed(*, session: Session, analysis_run_id: int) -> None:
    repository = ReportRepository(session)
    run = repository.get_analysis_run(analysis_run_id)
    if run is not None and run.status == "running":
        repository.fail_analysis(run)
        session.commit()


def _raise_with_run_id(error: AnalysisHandoffError, analysis_run_id: int) -> NoReturn:
    error.__dict__["run_id"] = analysis_run_id
    raise error


def _expected_request_id(run: AnalysisRunRow) -> str:
    return analysis_request_id(
        analysis_run_id=run.id,
        input_hash=run.input_hash,
        model=run.model,
        prompt_version=run.prompt_version,
        schema_version=run.schema_version,
    )


def _validate_identity(
    *,
    identity: _ResponseIdentity,
    run: AnalysisRunRow,
    analysis_run_id: int,
    frozen_input_hash: str,
) -> None:
    expected = {
        "protocol_version": "group-info.llm-response/v1",
        "request_id": _expected_request_id(run),
        "analysis_run_id": analysis_run_id,
        "input_hash": run.input_hash,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "schema_version": run.schema_version,
    }
    actual = identity.model_dump()
    mismatches = [key for key, value in expected.items() if actual[key] != value]
    if frozen_input_hash != run.input_hash:
        mismatches.append("frozen_analysis_input")
    if mismatches:
        raise AnalysisResponseMismatch(
            "WorkBuddy Response 关联字段不匹配：" + ", ".join(mismatches)
        )


def import_analysis_response(
    *,
    session: Session,
    analysis_run_id: int,
    raw_response: str,
) -> AnalysisResponseOutcome:
    repository = ReportRepository(session)
    run = repository.get_analysis_run(analysis_run_id)
    if run is None:
        _raise_with_run_id(
            AnalysisResponseMismatch(f"Analysis Run 不存在：{analysis_run_id}"),
            analysis_run_id,
        )

    messages = repository.analysis_messages(analysis_run_id)
    frozen_input_hash = analysis_input_hash(messages)
    try:
        raw_payload = json.loads(raw_response)
        identity = _ResponseIdentity.model_validate(raw_payload)
        _validate_identity(
            identity=identity,
            run=run,
            analysis_run_id=analysis_run_id,
            frozen_input_hash=frozen_input_hash,
        )
    except (json.JSONDecodeError, ValidationError, AnalysisResponseMismatch) as exc:
        if run.status == "running":
            repository.fail_analysis(run)
            session.commit()
        error = AnalysisResponseMismatch(str(exc))
        _raise_with_run_id(error, analysis_run_id)

    try:
        envelope = WorkBuddyResponseEnvelope.model_validate(raw_payload)
        if envelope.transport.status == "failed":
            raise WorkBuddyTransportFailed(
                envelope.transport.error_code or "WorkBuddy LLM Transport failed"
            )
        if envelope.output is None:  # guarded by the envelope validator
            raise AnalysisResponseInvalid("WorkBuddy Response 缺少 output")
        result = EventAnalysisResult(
            analyzer=run.analyzer,
            model=run.model,
            prompt_version=run.prompt_version,
            schema_version=run.schema_version,
            events=envelope.output.events,
        )
        errors = validate_event_analysis(result, messages)
        if errors:
            raise AnalysisResponseInvalid("Event 校验失败：" + "；".join(errors))

        if run.status == "succeeded":
            existing = EventAnalysisResult.model_validate_json(run.structured_output or "{}")
            if existing != result:
                _raise_with_run_id(
                    AnalysisAlreadyFinalized(
                        f"Analysis Run {analysis_run_id} 已由不同响应完成"
                    ),
                    analysis_run_id,
                )
            return AnalysisResponseOutcome(
                analysis_run_id=analysis_run_id,
                event_count=len(result.events),
                replayed=True,
            )
        if run.status != "running":
            raise AnalysisAlreadyFinalized(
                f"Analysis Run {analysis_run_id} 状态为 {run.status}，不能导入响应"
            )

        repository.save_events(
            analysis_run_id=analysis_run_id,
            analysis=result,
            messages=messages,
        )
        repository.finish_analysis(run, structured_output=result.model_dump_json())
        session.commit()
        return AnalysisResponseOutcome(
            analysis_run_id=analysis_run_id,
            event_count=len(result.events),
            replayed=False,
        )
    except AnalysisAlreadyFinalized as exc:
        _raise_with_run_id(exc, analysis_run_id)
    except Exception as exc:  # noqa: BLE001 - persist a terminal run state at this boundary.
        session.rollback()
        failed_run = session.get(AnalysisRunRow, analysis_run_id)
        if failed_run is not None and failed_run.status == "running":
            repository.fail_analysis(failed_run)
            session.commit()
        if isinstance(exc, AnalysisHandoffError):
            error = exc
        else:
            error = AnalysisResponseInvalid(str(exc))
        _raise_with_run_id(error, analysis_run_id)
