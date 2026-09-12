from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from group_info_system.analysis.eval import evaluate_event_result
from group_info_system.analysis.llm import LLMAnalyzerError, LLMEventAnalyzer
from group_info_system.analysis.prompts import load_event_extraction_prompt
from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.collection import collect_into_store
from group_info_system.application.reporting import build_report_from_store
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.db.models import AnalysisRunRow, EventEvidenceRow, EventRow
from group_info_system.db.repositories import StoredMessage
from group_info_system.db.session import session_factory
from group_info_system.domain.analysis import EventAnalysisResult


class StaticTransport:
    def __init__(self, output: str):
        self.output = output
        self.calls: list[dict[str, str]] = []

    def complete(self, *, model: str, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(
            {"model": model, "system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        return self.output


class FirstMessageEventTransport:
    def complete(self, *, model: str, system_prompt: str, user_prompt: str) -> str:
        del model, system_prompt
        message = json.loads(user_prompt)["messages"][0]
        return json.dumps(
            {
                "events": [
                    {
                        "event_type": "information",
                        "report_section": "today",
                        "title": "离线 LLM Adapter 事件",
                        "summary": "该输入包含一项可由指定消息版本核验的有效背景。",
                        "analysis_kind": "source_fact",
                        "event_time": message["sent_at"],
                        "message_version_ids": [message["message_version_id"]],
                    }
                ]
            },
            ensure_ascii=False,
        )


def load_cases() -> list[dict]:
    path = Path(__file__).parent / "fixtures" / "llm_event_eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def stored_messages(case: dict) -> list[StoredMessage]:
    return [
        StoredMessage(
            version_id=item["version_id"],
            evidence_id=item["evidence_id"],
            provider="simulated",
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


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["case_id"])
def test_llm_adapter_passes_fixed_event_eval_cases(case: dict) -> None:
    transport = StaticTransport(json.dumps(case["reference_output"], ensure_ascii=False))
    analyzer = LLMEventAnalyzer(transport=transport, model="offline-eval-model")
    messages = stored_messages(case)

    result = analyzer.analyze(messages)
    evaluation = evaluate_event_result(
        case_id=case["case_id"],
        result=result,
        expectations=case["expectations"],
        source_text_by_version={message.version_id: message.content for message in messages},
    )

    assert evaluation.passed, evaluation.failures
    assert evaluation.event_count == case["expectations"]["event_count"]
    assert evaluation.event_type_accuracy == 1.0
    assert evaluation.report_section_accuracy == 1.0
    assert evaluation.evidence_accuracy == 1.0
    assert evaluation.time_shape_accuracy == 1.0
    assert evaluation.event_boundary_accuracy == 1.0
    assert evaluation.source_copy_accuracy == 1.0
    assert result.schema_version == "event-v2"
    assert result.prompt_version == "event-extraction-v2"
    assert len(transport.calls) == 1
    request = json.loads(transport.calls[0]["user_prompt"])
    assert request["contract"] == "event-v2"
    assert {item["message_version_id"] for item in request["messages"]} == {
        message.version_id for message in messages
    }
    assert {item["evidence_id"] for item in request["messages"]} == {
        message.evidence_id for message in messages
    }


def test_mock_and_llm_analyzers_share_event_result_contract() -> None:
    case = load_cases()[0]
    messages = stored_messages(case)
    transport = StaticTransport(json.dumps(case["reference_output"], ensure_ascii=False))

    llm_result = LLMEventAnalyzer(transport=transport, model="offline-eval-model").analyze(messages)
    mock_result = SimulatedAIAnalyzer().analyze(messages)

    assert isinstance(llm_result, EventAnalysisResult)
    assert isinstance(mock_result, EventAnalysisResult)


def test_llm_adapter_rejects_non_json_and_unvalidated_event_output() -> None:
    case = load_cases()[0]
    messages = stored_messages(case)

    with pytest.raises(LLMAnalyzerError, match="valid Event-v2 JSON"):
        LLMEventAnalyzer(transport=StaticTransport("not json"), model="offline-eval-model").analyze(
            messages
        )

    copied_source = {
        "events": [
            {
                "event_type": "information",
                "report_section": "today",
                "title": "原文复制",
                "summary": messages[0].content,
                "analysis_kind": "source_fact",
                "event_time": messages[0].sent_at.isoformat(),
                "message_version_ids": [messages[0].version_id],
            }
        ]
    }
    with pytest.raises(LLMAnalyzerError, match="validation failed"):
        LLMEventAnalyzer(
            transport=StaticTransport(json.dumps(copied_source, ensure_ascii=False)),
            model="offline-eval-model",
        ).analyze(messages)


def test_prompt_is_versioned_and_targets_events_not_reports() -> None:
    prompt = load_event_extraction_prompt()
    assert prompt.version == "event-extraction-v2"
    assert len(prompt.sha256) == 64
    assert "不要生成日报、月报或文章" in prompt.text
    assert "不逐消息摘要" in prompt.text
    assert "引用支持它的全部必要 message_version_id" in prompt.text
    assert "今天做出的未来安排" in prompt.text
    assert "两个或更多 message_version_id" in prompt.text
    assert "高度相似" in prompt.text


def test_eval_v2_detects_time_boundary_and_near_copy_regressions() -> None:
    cases = {case["case_id"]: case for case in load_cases()}

    time_case = cases["multi_message_one_event"]
    wrong_time = copy.deepcopy(time_case["reference_output"])
    wrong_time["events"][0].pop("time_range")
    wrong_time["events"][0]["event_time"] = time_case["messages"][0]["sent_at"]
    time_result = LLMEventAnalyzer(
        transport=StaticTransport(json.dumps(wrong_time, ensure_ascii=False)),
        model="offline-eval-model",
    ).analyze(stored_messages(time_case))
    time_evaluation = evaluate_event_result(
        case_id=time_case["case_id"],
        result=time_result,
        expectations=time_case["expectations"],
    )
    assert time_evaluation.time_shape_accuracy == 0.0
    assert any("expected time_range" in failure for failure in time_evaluation.failures)

    boundary_case = cases["one_message_multiple_events"]
    crossed_boundary = copy.deepcopy(boundary_case["reference_output"])
    crossed_boundary["events"][0]["summary"] = "已决定周五发布并由小王完成回归检查。"
    boundary_result = LLMEventAnalyzer(
        transport=StaticTransport(json.dumps(crossed_boundary, ensure_ascii=False)),
        model="offline-eval-model",
    ).analyze(stored_messages(boundary_case))
    boundary_evaluation = evaluate_event_result(
        case_id=boundary_case["case_id"],
        result=boundary_result,
        expectations=boundary_case["expectations"],
    )
    assert boundary_evaluation.event_boundary_accuracy == 0.5
    assert any("crosses event boundary" in failure for failure in boundary_evaluation.failures)

    copy_case = cases["reusable_method_howto"]
    near_copy = copy.deepcopy(copy_case["reference_output"])
    near_copy["events"][0]["summary"] = copy_case["messages"][0]["content"] + "数据"
    copy_result = LLMEventAnalyzer(
        transport=StaticTransport(json.dumps(near_copy, ensure_ascii=False)),
        model="offline-eval-model",
    ).analyze(stored_messages(copy_case))
    copy_evaluation = evaluate_event_result(
        case_id=copy_case["case_id"],
        result=copy_result,
        expectations=copy_case["expectations"],
        source_text_by_version={
            message["version_id"]: message["content"] for message in copy_case["messages"]
        },
    )
    assert copy_evaluation.source_copy_accuracy == 0.0
    assert any("source similarity" in failure for failure in copy_evaluation.failures)


def test_llm_adapter_integrates_with_event_and_report_pipeline_offline(
    migrated_database: str, tmp_path: Path
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "simulated_messages.json"
    start = datetime.fromisoformat("2026-09-09T22:00:00+00:00")
    end = datetime.fromisoformat("2026-09-10T22:00:00+00:00")
    sessions = session_factory(migrated_database)
    with sessions() as session:
        collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture),
            chat_id="llm-offline-pipeline",
            since=start,
            until=end,
        )

    analyzer = LLMEventAnalyzer(
        transport=FirstMessageEventTransport(),
        model="offline-eval-model",
    )
    with sessions() as session:
        envelope = build_report_from_store(
            session=session,
            analyzer=analyzer,
            window_start=start,
            window_end=end,
            output_dir=tmp_path / "llm-report",
            virtual_data=True,
        )

    with sessions() as session:
        run = session.scalar(select(AnalysisRunRow).order_by(AnalysisRunRow.id.desc()))
        assert run is not None
        assert run.analyzer == "llm-event-analyzer"
        assert run.model == "offline-eval-model"
        assert run.prompt_version == "event-extraction-v2"
        assert run.schema_version == "event-v2"
        assert session.scalar(select(func.count()).select_from(EventRow)) == 1
        assert session.scalar(select(func.count()).select_from(EventEvidenceRow)) == 1

    assert sum(len(section.items) for section in envelope.report.sections) == 1
    assert envelope.report.generated_by == "llm-event-analyzer"
    assert (tmp_path / "llm-report" / "report.json").is_file()
    assert (tmp_path / "llm-report" / "report.html").is_file()
