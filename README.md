# 群聊信息系统 MVP

以不可变原始消息为事实源，将群聊消息标准化入库，经结构化分析生成带逐字证据的 `report.json` 和本地 HTML 页面。

## 当前闭环

```text
SimulatedCollector / FeishuCollector
  -> RawMessage
  -> SQLite append-only storage
  -> structured analysis
  -> report.json
  -> Jinja HTML
```

Collector 与报告层通过 `RawMessage` 隔离。更换数据源不会改变分析或报告契约。

## 项目状态

Phase 3 真实飞书采集验证已通过：真实群消息可以经 `FeishuCollector` 标准化为
`RawMessage`，幂等写入 SQLite，并由 Mock Analyzer 生成符合 `mvp-v1` 契约的
`report.json` 与 HTML Portal。

Phase 5B 已完成真实模型离线 Eval。真实 Transport 仅接受 prompt 并返回结构化响应，
不读取或写入数据库，也不生成报告；生产流程仍使用 Mock Analyzer，尚未接入真实模型。

Phase 6A 已冻结生产 CLI：`collect`、`analyze`、`report daily` 可独立运行并通过
run ID 串联，供外部调度器调用。

Phase 6B 已形成 WorkBuddy 与飞书妙搭的集成设计：WorkBuddy 只负责编排和提供
LLM Transport，Group-Chat 继续独占运行状态、Event 校验与报告生成，妙搭只读展示。
详见 [Phase 6B 集成设计](docs/phase-6b-workbuddy-integration-design.md)。

Phase 6C 已实现 `analyze-request` / `analyze-response`，并用真实飞书消息和真实模型
完成首次离线 Handoff 闭环。协议与验证记录见
[Phase 6C WorkBuddy Handoff](docs/phase-6c-workbuddy-handoff.md)。

Phase 6D 已冻结 WorkBuddy `daily-report` Skill、只读运行查询 CLI 和失败恢复策略的
接口设计；本阶段不实现 WorkBuddy Skill 或自动化。详见
[Phase 6D Daily Report Skill 设计](docs/phase-6d-daily-report-skill.md)。

Phase 6D-1 已实现 `runs latest/show` 只读观察接口。查询直接读取现有三张运行表，
不加载消息、Event、分析输出或报告正文，也不创建新的运行状态。

Phase 6D-2 已在用户 Skill 目录安装 `daily-report`。Skill 只编排 Group-Chat CLI、
调用 WorkBuddy LLM Transport 并报告失败；不包含 scheduler、checkpoint、SQLite 访问
或报告生成代码。配置示例随 Skill 一并提供。

Phase 6E 已收敛生产边界：当同步 watermark 已达到或越过请求窗口终点时，采集作为
`WINDOW_ALREADY_SYNCED` 成功 no-op 结束，不调用飞书、不推进游标、不创建消息版本；
空消息窗口也可经 Event-v2 validator 生成 `events=[]` 的成功 AnalysisRun 和明确写明
“今日暂无有效事件”的日报。WorkBuddy 的具体模型不再由 Skill 固定。

Phase 6F 已配置 WorkBuddy 原生 Schedule：每天 `06:10`（`Asia/Shanghai`）触发
`Group-Chat daily-report production`，仅负责调用已安装的 `daily-report` Skill。
项目不包含 scheduler/cron；WorkBuddy 客户端中的原生 Schedule 是实际生效配置，
配置清单中的 `recommended_run_at` 仅作部署记录。

## 本地运行

```bash
uv sync
uv run alembic upgrade head
uv run group-info serve
```

数据库写入 `var/group_info.db`，报告写入 `var/output/<日期>/`。全部运行时路径均被 Git 忽略。

真实群配置同样只保存在本地。首次配置时复制示例并填写实际群标识：

```bash
cp config/groups.example.yaml config/groups.local.yaml
uv run group-info groups validate
```

模拟闭环仅用于本地回归测试，不是生产采集流程的依赖：

```bash
uv run group-info demo
```

模拟输入固定保存在 `tests/fixtures/`，执行后的一次性演示产物写入
`var/output/demo/`。

## 飞书增量采集

先确保本机 `lark-cli` 已完成用户身份授权，然后运行：

```bash
uv run group-info collect --source feishu --chat-id oc_xxx --chat-name "目标群名称"
```

该命令只采集并入库，不运行分析或生成报告。同步游标由应用层持久化；Collector 使用重叠时间窗获取数据，数据库用平台消息身份和 payload hash 保证幂等，同时保留消息编辑后的新版本。

若现有 watermark 已达到或越过请求窗口终点，命令返回成功，`fetched=0`、`inserted=0`、
`reason_code=WINDOW_ALREADY_SYNCED`。对应 CollectionRun 通过现有 `error_code` 兼容字段
记录该原因码，`runs latest/show` 则按成功语义输出为 `reason_code`；这是 no-op 结果，
不是失败，也不会调用飞书接口或修改 watermark。

生产 workflow 分为独立的分析和报告步骤。分析命令会先冻结本次
`Analysis Input`，当前默认使用 Mock Analyzer：

```bash
uv run group-info analyze \
  --start 2026-09-10T06:00:00+08:00 \
  --end 2026-09-11T06:00:00+08:00
```

从上一步 JSON 输出取得 `analysis_run_id` 后生成日报：

```bash
uv run group-info report daily --analysis-run-id 42
```

由 WorkBuddy 提供 LLM Transport 时，先导出冻结请求：

```bash
uv run group-info analyze-request \
  --model <workbuddy-selected-model> \
  --start 2026-09-11T06:00:00+08:00 \
  --end 2026-09-12T06:00:00+08:00 \
  --output var/handoff/daily/request.json
```

WorkBuddy 返回符合协议的 Response 后，由 Group-Chat 校验并导入：

```bash
uv run group-info analyze-response \
  --analysis-run-id 42 \
  --input var/handoff/daily/response.json
```

Request/Response 仅放在 Git 忽略的 `var/`。WorkBuddy 不写数据库，所有 Event 必须
经过 Group-Chat 的 Event-v2 validator 后才能持久化。

三个命令均以退出码表示成功或失败，并输出稳定 JSON。`collect-feishu` 与
`build-report` 暂时保留为兼容命令，新调度流程应使用 `collect`、`analyze`、
`report daily`。

## WorkBuddy 调用边界

WorkBuddy 只需顺序调用 CLI、检查退出码并传递 `analysis_run_id`：

```text
group-info collect
  -> group-info analyze
  -> group-info report daily --analysis-run-id <上一命令返回值>
```

调度频率、重试和通知由 WorkBuddy 管理；Group-Chat 不包含 scheduler、cron 或
WorkBuddy 依赖。Collector、Analyzer 和 Report workflow 的业务逻辑均位于应用层，
CLI 只负责参数解析、依赖装配及 JSON 输入输出。

恢复时可按固定窗口查询现有运行记录：

```bash
uv run group-info runs latest \
  --start 2026-09-11T06:00:00+08:00 \
  --end 2026-09-12T06:00:00+08:00 \
  --chat-id oc_xxx \
  --kind all

uv run group-info runs show --kind analysis --id 42
```

`runs latest` 的 `--chat-id` 可重复；`--kind` 支持 `all`、`collection`、`analysis`
和 `report`。`runs show` 支持 `collection`、`analysis` 和 `report`。不存在的 ID
返回稳定错误码 `RUN_NOT_FOUND`。

## Phase 3 验证记录

2026-09-11 使用真实飞书测试群完成最近 24 小时窗口验证：

- 首次采集读取并写入 3 条真实消息及 3 个消息版本；核心字段与原始 payload 映射完整。
- 完全相同窗口再次采集仍读取 3 条，新增消息和消息版本均为 0，历史版本快照未变化。
- Mock Analyzer 基于 3 条真实消息生成 3 个分析条目，3 个 evidence 引用均可回查。
- `report.json` 通过 `mvp-v1` 契约校验，HTML Portal 与健康检查均返回 HTTP 200。
- 11 项回归测试和 Ruff 静态检查通过。

验证记录不包含消息正文、成员信息、认证凭据或 token。

## 验证命令

```bash
uv run pytest -q
uv run ruff check .
```

## Phase 5B 离线 Eval

固定 Eval Cases 位于 `tests/fixtures/llm_event_eval_cases.json`。真实模型验证必须手动执行，
不会被生产命令或测试套件自动触发：

```bash
uv run python -m group_info_system.analysis.run_real_eval \
  --model gpt-5.6-terra \
  --output var/eval/phase5b/report.json
```

2026-09-12 使用 `gpt-5.6-terra`、`event-extraction-v1`、`event-v2` 连续执行两轮：

- 两轮 Event 数量均为预期的 6，`event_type` 与 evidence 准确率均为 100%。
- 完整案例通过率分别为 3/5 和 4/5；`report_section` 准确率分别为 66.7% 和 83.3%。
- 多消息事件的 `event_time` / `time_range` 选择不符合 prompt 约定，且单消息多事件存在摘要语义重叠。
- 结论：Event-v2 无需修改；需要先形成 Prompt v2 并重新 Eval，暂不进入 WorkBuddy 流程。

Eval 报告写入被 Git 忽略的 `var/eval/`，不包含真实群消息、认证凭据或 token。
