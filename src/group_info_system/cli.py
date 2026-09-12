from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from alembic import command
from alembic.config import Config

from group_info_system.analysis.simulated_ai import SimulatedAIAnalyzer
from group_info_system.application.analysis import analyze_into_store
from group_info_system.application.collection import (
    collect_into_store,
    collect_into_store_with_outcome,
)
from group_info_system.application.handoff import (
    create_analysis_request,
    import_analysis_response,
    mark_analysis_failed,
)
from group_info_system.application.reporting import (
    build_daily_report_from_analysis,
    build_report_from_store,
)
from group_info_system.collectors.feishu import FeishuCollector
from group_info_system.collectors.simulated import SimulatedCollector
from group_info_system.config import get_settings
from group_info_system.db.repositories import (
    RunNotFoundError,
    latest_run_records,
    show_run_record,
)
from group_info_system.db.session import read_only_session_factory, session_factory

ROOT = Path(__file__).resolve().parents[2]


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须包含时区偏移")
    return parsed.astimezone(UTC)


def upgrade_database(database_url: str) -> None:
    os.environ["GROUP_INFO_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def demo_command() -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    tz = ZoneInfo(settings.timezone)
    start = datetime(2026, 9, 10, 6, 0, tzinfo=tz).astimezone(UTC)
    end = datetime(2026, 9, 11, 6, 0, tzinfo=tz).astimezone(UTC)
    fixture = ROOT / "tests" / "fixtures" / "simulated_messages.json"
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        batch = collect_into_store(
            session=session,
            collector=SimulatedCollector(fixture),
            chat_id="demo-product-group",
            since=start,
            until=end,
        )
    with sessions() as session:
        envelope = build_report_from_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=start,
            window_end=end,
            output_dir=settings.output_dir / "demo",
            virtual_data=True,
        )
    print(
        json.dumps(
            {
                "fetched": len(batch.messages),
                "report_id": envelope.report_id,
                "outputs": envelope.output_files,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def collect_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    until = args.until or datetime.now(UTC)
    since = args.since or until - timedelta(days=30)
    if args.source != "feishu":
        raise ValueError(f"不支持的 Collector：{args.source}")
    collector = FeishuCollector(
        cli_path=settings.lark_cli,
        chat_name=args.chat_name,
        local_timezone=settings.timezone,
    )
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        outcome = collect_into_store_with_outcome(
            session=session,
            collector=collector,
            chat_id=args.chat_id,
            since=since,
            until=until,
        )
    print(
        json.dumps(
            {
                "command": "collect",
                "status": "succeeded",
                "collection_run_id": outcome.collection_run_id,
                "provider": outcome.batch.provider,
                "chat_id": outcome.batch.chat_id,
                "mode": outcome.batch.mode,
                "fetched": len(outcome.batch.messages),
                "inserted": outcome.inserted_count,
                "reason_code": outcome.reason_code,
                "next_cursor": outcome.batch.next_cursor.model_dump(mode="json")
                if outcome.batch.next_cursor
                else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def collect_feishu_command(args: argparse.Namespace) -> int:
    args.source = "feishu"
    return collect_command(args)


def analyze_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    if args.analyzer != "mock":
        raise ValueError(f"当前未配置 Analyzer：{args.analyzer}")
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        outcome = analyze_into_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=args.start,
            window_end=args.end,
        )
    print(
        json.dumps(
            {
                "command": "analyze",
                "status": "succeeded",
                "analysis_run_id": outcome.analysis_run_id,
                "input_count": outcome.input_count,
                "event_count": outcome.event_count,
                "analyzer": outcome.analyzer,
                "model": outcome.model,
                "prompt_version": outcome.prompt_version,
                "schema_version": outcome.schema_version,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.write("\n")


def analyze_request_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        outcome = create_analysis_request(
            session=session,
            model=args.model,
            window_start=args.start,
            window_end=args.end,
        )
        envelope_json = outcome.envelope.model_dump_json(indent=2)
        if args.output is not None:
            try:
                _write_private_file(args.output, envelope_json)
            except Exception as exc:
                mark_analysis_failed(
                    session=session,
                    analysis_run_id=outcome.envelope.analysis_run_id,
                )
                exc.__dict__["run_id"] = outcome.envelope.analysis_run_id
                raise

    if args.output is None:
        print(envelope_json)
    else:
        print(
            json.dumps(
                {
                    "command": "analyze-request",
                    "status": "succeeded",
                    "analysis_run_id": outcome.envelope.analysis_run_id,
                    "request_id": outcome.envelope.request_id,
                    "input_hash": outcome.envelope.input.input_hash,
                    "input_count": outcome.input_count,
                    "output": str(args.output),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def analyze_response_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    raw_response = (
        sys.stdin.read() if args.input == "-" else Path(args.input).read_text("utf-8")
    )
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        outcome = import_analysis_response(
            session=session,
            analysis_run_id=args.analysis_run_id,
            raw_response=raw_response,
        )
    print(
        json.dumps(
            {
                "command": "analyze-response",
                "status": "succeeded",
                "analysis_run_id": outcome.analysis_run_id,
                "event_count": outcome.event_count,
                "replayed": outcome.replayed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def report_daily_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        envelope = build_daily_report_from_analysis(
            session=session,
            analysis_run_id=args.analysis_run_id,
            output_root=settings.output_dir,
            local_timezone=settings.timezone,
            output_dir=args.output_dir,
            virtual_data=False,
        )
    print(
        json.dumps(
            {
                "command": "report daily",
                "status": "succeeded",
                "analysis_run_id": args.analysis_run_id,
                "report_id": envelope.report_id,
                "outputs": envelope.output_files,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def runs_latest_command(args: argparse.Namespace) -> int:
    if args.start >= args.end:
        raise ValueError("start 必须早于 end")
    settings = get_settings()
    sessions = read_only_session_factory(settings.database_url)
    with sessions() as session:
        result = latest_run_records(
            session=session,
            window_start=args.start,
            window_end=args.end,
            chat_ids=tuple(args.chat_id or ()),
            kind=args.kind,
        )
    print(
        json.dumps(
            {
                "command": "runs latest",
                "status": "succeeded",
                "query": {
                    "start": args.start.isoformat(),
                    "end": args.end.isoformat(),
                    "chat_ids": args.chat_id or [],
                    "kind": args.kind,
                },
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def runs_show_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    sessions = read_only_session_factory(settings.database_url)
    with sessions() as session:
        result = show_run_record(session=session, kind=args.kind, run_id=args.id)
    print(
        json.dumps(
            {
                "command": "runs show",
                "status": "succeeded",
                "kind": args.kind,
                "run": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_report_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    upgrade_database(settings.database_url)
    sessions = session_factory(settings.database_url)
    with sessions() as session:
        envelope = build_report_from_store(
            session=session,
            analyzer=SimulatedAIAnalyzer(),
            window_start=args.start,
            window_end=args.end,
            output_dir=settings.output_dir / args.end.date().isoformat(),
            virtual_data=False,
        )
    print(json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def _failure_payload(args: argparse.Namespace, exc: Exception) -> dict[str, object]:
    command_name = args.command
    if args.command == "report":
        command_name = f"report {args.report_command}"
    elif args.command == "runs":
        command_name = f"runs {args.runs_command}"
    payload: dict[str, object] = {
        "command": command_name,
        "status": "failed",
        "error_code": (
            exc.error_code if isinstance(exc, RunNotFoundError) else type(exc).__name__
        ),
    }
    run_id = getattr(exc, "run_id", None)
    if run_id is not None:
        key = {
            "collect": "collection_run_id",
            "collect-feishu": "collection_run_id",
            "analyze": "analysis_run_id",
            "analyze-request": "analysis_run_id",
            "analyze-response": "analysis_run_id",
            "report": "report_id",
        }.get(args.command, "run_id")
        payload[key] = run_id
    return payload


def _add_collection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--chat-name", help="可选群显示名；不影响同步身份")
    parser.add_argument("--since", type=parse_datetime)
    parser.add_argument("--until", type=parse_datetime)


def _add_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", required=True, type=parse_datetime)
    parser.add_argument("--end", required=True, type=parse_datetime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="群聊信息系统 MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="升级数据库")
    subparsers.add_parser("demo", help="运行全模拟闭环")

    collect = subparsers.add_parser("collect", help="调用 Collector 并写入原始消息层")
    collect.add_argument("--source", default="feishu", help="Collector 名称，当前为 feishu")
    _add_collection_arguments(collect)

    analyze = subparsers.add_parser("analyze", help="冻结 Analysis Input 并生成 Event")
    analyze.add_argument("--analyzer", default="mock", help="Analyzer 名称，当前默认 mock")
    _add_window_arguments(analyze)

    analyze_request = subparsers.add_parser(
        "analyze-request", help="冻结 Analysis Input 并输出 WorkBuddy LLM Request"
    )
    analyze_request.add_argument("--model", required=True, help="WorkBuddy 实际调用的模型")
    analyze_request.add_argument("--output", type=Path, help="私有 Request JSON 输出路径")
    _add_window_arguments(analyze_request)

    analyze_response = subparsers.add_parser(
        "analyze-response", help="校验并导入 WorkBuddy LLM Response"
    )
    analyze_response.add_argument("--analysis-run-id", required=True, type=int)
    analyze_response.add_argument(
        "--input", required=True, help="Response JSON 路径；使用 - 从 stdin 读取"
    )

    report = subparsers.add_parser("report", help="从已持久化 Event 生成报告")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    daily = report_subparsers.add_parser("daily", help="生成日报 JSON 与 HTML")
    daily.add_argument("--analysis-run-id", required=True, type=int)
    daily.add_argument("--output-dir", type=Path)

    runs = subparsers.add_parser("runs", help="只读查询现有运行记录")
    runs_subparsers = runs.add_subparsers(dest="runs_command", required=True)
    runs_latest = runs_subparsers.add_parser("latest", help="按窗口查询最新运行")
    _add_window_arguments(runs_latest)
    runs_latest.add_argument("--chat-id", action="append", help="可重复，仅过滤 CollectionRun")
    runs_latest.add_argument(
        "--kind",
        choices=("all", "collection", "analysis", "report"),
        default="all",
    )
    runs_show = runs_subparsers.add_parser("show", help="按类型和 ID 查询一个运行")
    runs_show.add_argument("--kind", required=True, choices=("collection", "analysis", "report"))
    runs_show.add_argument("--id", required=True, type=int)

    legacy_collect = subparsers.add_parser(
        "collect-feishu", help="兼容旧命令；等价于 collect --source feishu"
    )
    _add_collection_arguments(legacy_collect)
    legacy_report = subparsers.add_parser("build-report", help="兼容旧的一步式分析报告命令")
    _add_window_arguments(legacy_report)

    serve = subparsers.add_parser("serve", help="启动本地报告门户")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init-db":
            settings = get_settings()
            upgrade_database(settings.database_url)
            return 0
        if args.command == "demo":
            return demo_command()
        if args.command == "collect":
            return collect_command(args)
        if args.command == "analyze":
            return analyze_command(args)
        if args.command == "analyze-request":
            return analyze_request_command(args)
        if args.command == "analyze-response":
            return analyze_response_command(args)
        if args.command == "report" and args.report_command == "daily":
            return report_daily_command(args)
        if args.command == "runs" and args.runs_command == "latest":
            return runs_latest_command(args)
        if args.command == "runs" and args.runs_command == "show":
            return runs_show_command(args)
        if args.command == "collect-feishu":
            return collect_feishu_command(args)
        if args.command == "build-report":
            return build_report_command(args)
        if args.command == "serve":
            uvicorn.run("group_info_system.web.app:app", host=args.host, port=args.port)
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary emits a stable failure envelope.
        print(json.dumps(_failure_payload(args, exc), ensure_ascii=False), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
