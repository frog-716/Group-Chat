from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from group_info_system.analysis.codex_transport import CodexCLITransport
from group_info_system.analysis.eval import EventEvalResult, evaluate_event_result
from group_info_system.analysis.llm import LLMAnalyzerError, LLMEventAnalyzer, LLMEventPayload
from group_info_system.db.repositories import StoredMessage
from group_info_system.domain.analysis import EventAnalysisResult

DEFAULT_MODEL = "gpt-5.6-terra"


class RecordingEvalTransport:
    """Eval-only wrapper that preserves fixture output when validation rejects it."""

    def __init__(self) -> None:
        self.transport = CodexCLITransport()
        self.last_output: str | None = None

    def complete(self, *, model: str, system_prompt: str, user_prompt: str) -> str:
        self.last_output = self.transport.complete(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        return self.last_output


def _stored_messages(case: dict[str, Any]) -> list[StoredMessage]:
    return [
        StoredMessage(
            version_id=item["version_id"],
            evidence_id=item["evidence_id"],
            provider="eval-fixture",
            chat_id="llm-eval-chat",
            chat_name="LLM Eval 测试群",
            platform_message_id=f"eval-{item['version_id']}",
            sent_at=datetime.fromisoformat(item["sent_at"]),
            sender_name=item["sender_name"],
            content=item["content"],
            message_type="text",
            is_system=False,
            is_deleted=False,
        )
        for item in case["messages"]
    ]


def _case_report(evaluation: EventEvalResult, result: Any) -> dict[str, Any]:
    return {
        "case_id": evaluation.case_id,
        "passed": evaluation.passed,
        "event_count": evaluation.event_count,
        "expected_event_count": evaluation.expected_event_count,
        "event_type_accuracy": evaluation.event_type_accuracy,
        "report_section_accuracy": evaluation.report_section_accuracy,
        "evidence_accuracy": evaluation.evidence_accuracy,
        "time_shape_accuracy": evaluation.time_shape_accuracy,
        "event_boundary_accuracy": evaluation.event_boundary_accuracy,
        "source_copy_accuracy": evaluation.source_copy_accuracy,
        "source_copy_check_count": evaluation.source_copy_check_count,
        "failures": list(evaluation.failures),
        "events": [event.model_dump(mode="json", exclude_none=True) for event in result.events],
    }


def _failed_case_report(
    *,
    case: dict[str, Any],
    analyzer: LLMEventAnalyzer,
    raw_output: str | None,
    error: LLMAnalyzerError,
) -> dict[str, Any]:
    if raw_output is not None:
        try:
            payload = LLMEventPayload.model_validate_json(raw_output)
        except ValueError:
            payload = None
        if payload is not None:
            result = EventAnalysisResult(
                analyzer=analyzer.name,
                model=analyzer.model,
                prompt_version=analyzer.prompt_version,
                schema_version=analyzer.schema_version,
                events=payload.events,
            )
            evaluation = evaluate_event_result(
                case_id=case["case_id"],
                result=result,
                expectations=case["expectations"],
                source_text_by_version={
                    message["version_id"]: message["content"] for message in case["messages"]
                },
            )
            report = _case_report(evaluation, result)
            report["passed"] = False
            report["failures"] = [str(error), *report["failures"]]
            return report

    expected_count = case["expectations"]["event_count"]
    return {
        "case_id": case["case_id"],
        "passed": False,
        "event_count": 0,
        "expected_event_count": expected_count,
        "event_type_accuracy": 0.0,
        "report_section_accuracy": 0.0,
        "evidence_accuracy": 0.0,
        "time_shape_accuracy": 0.0,
        "event_boundary_accuracy": 0.0,
        "source_copy_accuracy": 0.0,
        "source_copy_check_count": sum(
            "max_source_similarity" in event
            for event in case["expectations"]["required_events"]
        ),
        "failures": [str(error)],
        "events": [],
    }


def run_eval(*, fixture_path: Path, output_path: Path, model: str) -> dict[str, Any]:
    suite = json.loads(fixture_path.read_text(encoding="utf-8"))
    transport = RecordingEvalTransport()
    analyzer = LLMEventAnalyzer(
        transport=transport,
        model=model,
    )
    case_reports: list[dict[str, Any]] = []
    for case in suite["cases"]:
        transport.last_output = None
        try:
            result = analyzer.analyze(_stored_messages(case))
        except LLMAnalyzerError as exc:
            case_reports.append(
                _failed_case_report(
                    case=case,
                    analyzer=analyzer,
                    raw_output=transport.last_output,
                    error=exc,
                )
            )
            continue
        evaluation = evaluate_event_result(
            case_id=case["case_id"],
            result=result,
            expectations=case["expectations"],
            source_text_by_version={
                message["version_id"]: message["content"] for message in case["messages"]
            },
        )
        case_reports.append(_case_report(evaluation, result))

    total_events = sum(case["event_count"] for case in case_reports)
    expected_events = sum(case["expected_event_count"] for case in case_reports)
    metric_weight = sum(
        max(case["event_count"], case["expected_event_count"]) for case in case_reports
    )

    def weighted_accuracy(field: str) -> float:
        if metric_weight == 0:
            return 1.0
        weighted_total = sum(
            case[field] * max(case["event_count"], case["expected_event_count"])
            for case in case_reports
        )
        return weighted_total / metric_weight

    source_copy_weight = sum(case["source_copy_check_count"] for case in case_reports)
    source_copy_accuracy = (
        sum(
            case["source_copy_accuracy"] * case["source_copy_check_count"]
            for case in case_reports
        )
        / source_copy_weight
        if source_copy_weight
        else 1.0
    )

    report = {
        "eval_suite_version": suite["schema_version"],
        "generated_at": datetime.now(UTC).isoformat(),
        "transport": "codex-cli",
        "model": model,
        "prompt_version": analyzer.prompt_version,
        "schema_version": analyzer.schema_version,
        "summary": {
            "case_count": len(case_reports),
            "passed_case_count": sum(case["passed"] for case in case_reports),
            "event_count": total_events,
            "expected_event_count": expected_events,
            "event_type_accuracy": weighted_accuracy("event_type_accuracy"),
            "report_section_accuracy": weighted_accuracy("report_section_accuracy"),
            "evidence_accuracy": weighted_accuracy("evidence_accuracy"),
            "time_shape_accuracy": weighted_accuracy("time_shape_accuracy"),
            "event_boundary_accuracy": weighted_accuracy("event_boundary_accuracy"),
            "source_copy_accuracy": source_copy_accuracy,
        },
        "cases": case_reports,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real-model offline Event eval")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/llm_event_eval_cases.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/eval/phase5c/report.json"),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    report = run_eval(fixture_path=args.fixture, output_path=args.output, model=args.model)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
