from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from group_info_system.domain.analysis import EventAnalysisResult

from .validator import normalize_text


@dataclass(frozen=True)
class EventEvalResult:
    case_id: str
    event_count: int
    expected_event_count: int
    event_type_accuracy: float
    report_section_accuracy: float
    evidence_accuracy: float
    time_shape_accuracy: float
    event_boundary_accuracy: float
    source_copy_accuracy: float
    source_copy_check_count: int
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def evaluate_event_result(
    *,
    case_id: str,
    result: EventAnalysisResult,
    expectations: dict[str, Any],
    source_text_by_version: dict[int, str] | None = None,
) -> EventEvalResult:
    failures: list[str] = []
    expected_count = expectations.get("event_count", len(expectations.get("required_events", [])))
    if expected_count is not None and len(result.events) != expected_count:
        failures.append(f"expected {expected_count} events, got {len(result.events)}")

    required_events = expectations.get("required_events", [])
    time_shape_correct = 0
    boundary_correct = 0
    source_copy_correct = 0
    source_copy_check_count = 0
    for expected in required_events:
        expected_evidence = set(expected["message_version_ids"])
        semantic_event = next(
            (
                event
                for event in result.events
                if event.event_type == expected["event_type"]
                and set(event.message_version_ids) == expected_evidence
            ),
            None,
        )
        matched_event = next(
            (
                event
                for event in result.events
                if event.event_type == expected["event_type"]
                and event.report_section == expected["report_section"]
                and set(event.message_version_ids) == expected_evidence
            ),
            None,
        )
        if matched_event is None:
            failures.append(
                "missing event "
                f"{expected['event_type']}/{expected['report_section']} "
                f"with evidence {sorted(expected_evidence)}"
            )
        if semantic_event is None:
            if "max_source_similarity" in expected:
                source_copy_check_count += 1
            continue

        expected_time_shape = expected.get("time_shape")
        actual_time_shape = "time_range" if semantic_event.time_range is not None else "event_time"
        if expected_time_shape is None or actual_time_shape == expected_time_shape:
            time_shape_correct += 1
        else:
            failures.append(
                f"expected {expected_time_shape} for "
                f"{expected['event_type']}/{expected['report_section']}, "
                f"got {actual_time_shape}"
            )

        forbidden_terms = expected.get("summary_must_not_contain", [])
        found_terms = [term for term in forbidden_terms if term in semantic_event.summary]
        if not found_terms:
            boundary_correct += 1
        else:
            failures.append(
                f"{expected['event_type']}/{expected['report_section']} summary crosses "
                f"event boundary via {found_terms}"
            )

        max_similarity = expected.get("max_source_similarity")
        if max_similarity is not None:
            source_copy_check_count += 1
            source_text = None
            if source_text_by_version is not None and len(expected_evidence) == 1:
                source_text = source_text_by_version[next(iter(expected_evidence))]
            if source_text is None:
                failures.append(
                    f"missing source text for {expected['event_type']}/"
                    f"{expected['report_section']} copy check"
                )
            else:
                similarity = SequenceMatcher(
                    None,
                    normalize_text(semantic_event.summary),
                    normalize_text(source_text),
                ).ratio()
                if similarity <= max_similarity:
                    source_copy_correct += 1
                else:
                    failures.append(
                        f"{expected['event_type']}/{expected['report_section']} summary "
                        f"source similarity {similarity:.3f} exceeds {max_similarity:.3f}"
                    )

    actual_count = len(result.events)
    metric_denominator = max(len(required_events), actual_count, 1)
    if not required_events and not result.events:
        type_correct = section_correct = evidence_correct = 1
        time_shape_correct = boundary_correct = 1
    else:
        expected_types = Counter(item["event_type"] for item in required_events)
        actual_types = Counter(event.event_type for event in result.events)
        type_correct = sum((expected_types & actual_types).values())

        expected_sections = Counter(item["report_section"] for item in required_events)
        actual_sections = Counter(event.report_section for event in result.events)
        section_correct = sum((expected_sections & actual_sections).values())

        expected_evidence_sets = Counter(
            tuple(sorted(item["message_version_ids"])) for item in required_events
        )
        actual_evidence_sets = Counter(
            tuple(sorted(event.message_version_ids)) for event in result.events
        )
        evidence_correct = sum((expected_evidence_sets & actual_evidence_sets).values())

    return EventEvalResult(
        case_id=case_id,
        event_count=actual_count,
        expected_event_count=expected_count,
        event_type_accuracy=type_correct / metric_denominator,
        report_section_accuracy=section_correct / metric_denominator,
        evidence_accuracy=evidence_correct / metric_denominator,
        time_shape_accuracy=time_shape_correct / metric_denominator,
        event_boundary_accuracy=boundary_correct / metric_denominator,
        source_copy_accuracy=(
            source_copy_correct / source_copy_check_count
            if source_copy_check_count
            else 1.0
        ),
        source_copy_check_count=source_copy_check_count,
        failures=tuple(failures),
    )
