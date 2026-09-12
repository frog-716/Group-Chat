# Phase 6B：WorkBuddy 与飞书妙搭集成设计

状态：设计冻结候选，不包含实现。

## 1. 结论

Phase 6B 保持以下所有权边界：

- Group-Chat 是事实、派生数据、运行状态和报告契约的唯一所有者。
- WorkBuddy 是外部调度器，同时为一次分析提供 LLM Transport；它不拥有消息、Event、Report 或运行状态。
- 飞书妙搭只读取 Group-Chat 输出的成功报告，不访问数据库，不执行分析，不修正报告内容。
- 日报与月报都从选定窗口内的不可变 `MessageVersion` 开始。月报不拼接日报，也不把历史 Event 当作事实源。
- WorkBuddy 返回的模型输出必须由 Group-Chat 解析并通过 Event-v2 validator 后，才能写入 `Event` 与 `EventEvidence`。
- 现有 `CollectionRun / AnalysisRun / Report` 足以表达运行生命周期。本阶段不新增状态表、核心字段或 migration。

目标链路：

```text
WorkBuddy Skill
  -> group-info collect
  -> Group-Chat 冻结 AnalysisInput 并生成 LLM Request
  -> WorkBuddy LLM Transport
  -> Group-Chat 校验 Event-v2 并写入 Event/EventEvidence
  -> group-info report daily|monthly
  -> Group-Chat 生成 Report 与妙搭展示包
  -> 飞书妙搭只读展示
```

## 2. 非目标

Phase 6B 不引入：

- Group-Chat 内部 scheduler、cron 或自动触发器；
- WorkBuddy SDK、状态库或任务表；
- WorkBuddy 对 SQLite 的直接访问；
- WorkBuddy 对 Event、Evidence 或 Report 的自行落库；
- 飞书妙搭内的分析、事实修正或报告生成逻辑；
- 新的数据库核心模型；
- Event-v2、Report Builder 或 Collector 的修改。

本文出现的命令扩展和展示包是后续实现接口，不代表本阶段已经可执行。

## 3. 共同时间与输入规则

所有窗口使用半开区间 `[start, end)`，时间必须是带时区的 ISO 8601。业务默认时区为 `Asia/Shanghai`，业务日切点由 Skill 配置，建议沿用当前流程的 `06:00`，不得在日报和月报中分别硬编码。

### 3.1 日报窗口

`report_date` 表示窗口结束所在的业务日期。例如日切点为 `06:00`，`report_date=2026-09-12`：

```text
start = 2026-09-11T06:00:00+08:00
end   = 2026-09-12T06:00:00+08:00
```

### 3.2 月报窗口

`month=2026-09` 与相同日切点对应：

```text
start = 2026-09-01T06:00:00+08:00
end   = 2026-10-01T06:00:00+08:00
```

月报创建新的 `AnalysisRun`，其 `AnalysisInput` 必须直接绑定该月窗口内的 `MessageVersion`。日报、日报 Report 和日报 Event 都不能成为月报输入的替代品。月内已有 Event 最多只能用于临时检索提示，任何最终 Event 仍须引用本次月度 `AnalysisInput` 中的 `message_version_id`。

### 3.3 完整性规则

- 默认 `allow_partial=false`。
- 任一目标群采集失败时，不开始分析，不生成“成功”报告。
- 零消息不是成功空报告：AnalysisRun 应失败并由 Skill 报告“窗口内没有可分析消息”。
- 不得因为模型上下文限制静默截断消息。v1 超限时以 `INPUT_TOO_LARGE` 失败；先用真实月度规模验证，再单独设计分块，不在本阶段预建复杂的合并体系。

## 4. WorkBuddy `daily-report` Skill

### 4.1 输入

```yaml
chat_ids:                  # 必填，一个或多个授权群 chat_id
report_date:               # 可选，默认最近一个已结束业务日
timezone: Asia/Shanghai
day_cutoff: "06:00"
model:                     # 必填，实际提供给 Transport 的模型标识
output_root: var/output
allow_partial: false       # v1 固定 false
```

Skill 配置可以保存群 ID、时间策略和模型选择，但不得保存运行状态副本、飞书 token、消息正文或数据库快照。

### 4.2 步骤

1. 根据 `report_date/timezone/day_cutoff` 计算唯一的 `[start, end)`。
2. 对每个授权群调用 `group-info collect --source feishu --chat-id ... --since ... --until ...`。
3. 检查每次退出码与 `CollectionRun` ID。任一失败即停止。
4. 请求 Group-Chat 创建一个 `AnalysisRun`、冻结 `AnalysisInput`，并导出一份 LLM Request。
5. WorkBuddy 将请求中的 `system_prompt` 与 `user_prompt` 原样交给其 LLM Transport，不增删消息、不改写 prompt。
6. 将 LLM Response 原样交回 Group-Chat。
7. Group-Chat 校验关联字段、解析 Event-v2、执行 validator，并在一个事务中写入 Event/Evidence、将 AnalysisRun 置为 `succeeded`。
8. 调用 `group-info report daily --analysis-run-id <id>`。
9. 仅在 Report 状态为 `succeeded` 且 JSON/HTML 已生成后，请求 Group-Chat 导出妙搭展示包。
10. Skill 返回 Group-Chat 的 run ID、report ID 和产物位置，不自行生成另一份报告。

### 4.3 成功输出

```json
{
  "skill": "daily-report",
  "status": "succeeded",
  "period": {
    "kind": "daily",
    "start": "2026-09-11T06:00:00+08:00",
    "end": "2026-09-12T06:00:00+08:00",
    "timezone": "Asia/Shanghai"
  },
  "collection_run_ids": [101],
  "analysis_run_id": 202,
  "report_id": 303,
  "artifacts": {
    "report_json": "var/output/2026-09-12/report.json",
    "report_html": "var/output/2026-09-12/report.html",
    "miaoda_json": "var/output/2026-09-12/miaoda.json"
  }
}
```

这是一次执行结果，不是 WorkBuddy 的持久化状态模型。权威状态仍以 Group-Chat 中对应 run ID 为准。

### 4.4 失败与重试

- `collect` 可用相同窗口安全重试，幂等性由消息身份与版本 hash 保证。
- LLM 调用可使用同一 `request_id` 重试；WorkBuddy 不修改失败输出。
- Analysis Response 只能提交到请求指定的、仍为 `running` 的 AnalysisRun。
- 同一 response 重复提交应返回既有成功结果，不重复插入 Event；不同 response 提交到已成功 run 必须返回冲突。
- LLM 重试耗尽时，Skill 必须请求 Group-Chat 将该 AnalysisRun 置为 `failed`，不能只在 WorkBuddy 留一条失败记录。
- 报告生成失败由 Group-Chat 将 Report 状态置为 `failed`；重试应创建新的 Report 运行记录并保留失败历史。

## 5. WorkBuddy `monthly-report` Skill

### 5.1 输入

```yaml
chat_ids:
month: "2026-09"           # 必填，业务时区中的自然月
timezone: Asia/Shanghai
day_cutoff: "06:00"
model:
output_root: var/output
allow_partial: false
```

### 5.2 步骤

1. 依据共同时间规则计算完整月度窗口。
2. 对目标群执行显式窗口采集；重复采集仍由 Group-Chat 保证幂等。
3. 从该窗口的 `MessageVersion` 创建全新的月度 AnalysisRun 与 AnalysisInput。
4. 通过同一 LLM 交接协议提取 Event-v2；不得把日报文本作为模型输入。
5. Group-Chat 校验并持久化 Event/Evidence。
6. 调用后续应提供的附加接口 `group-info report monthly --analysis-run-id <id>`。
7. Group-Chat 的月报投影优先使用该 run 的原始消息和 evidence，Event 仅负责组织候选语义。
8. Group-Chat 生成月报 JSON、HTML 和妙搭展示包；WorkBuddy 只转交产物位置。

当前 Phase 6A 只有 `report daily`。`report monthly` 是实现月报 Skill 的明确前置能力，但它是 Group-Chat 的 Report workflow，不应在 WorkBuddy Skill 中临时拼装日报来替代。

### 5.3 Event-v2 在月报中的使用

Event-v2 不变。月报投影遵守：

- `event_type`、`event_time/time_range` 和 evidence 是主要依据；
- `report_section=today` 在月报展示时解释为“本期已发生/已决定”，仅改变展示标题，不改枚举值；
- `next` 仍只放窗口结束时尚未执行的行动；
- `howto` 与 `pitfalls` 语义保持不变；
- Report Builder 必须通过 EventEvidence 回查本次 AnalysisInput 中的原始消息；
- 不创建 canonical event，也不跨 AnalysisRun 修改或合并历史 Event。

## 6. LLM 调用交接协议

### 6.1 所有权

Group-Chat 负责：

- 创建 AnalysisRun 与冻结 AnalysisInput；
- 选择并版本化 prompt、Event schema 与 validator；
- 构造请求并计算 `input_hash`；
- 校验返回值并写入 Event/EventEvidence；
- 更新 AnalysisRun 状态。

WorkBuddy 负责：

- 使用请求指定的模型执行一次结构化模型调用；
- 原样传递 system/user prompt；
- 返回模型原始结构化输出和实际模型标识；
- 在暂时性错误时执行有限重试。

WorkBuddy 不得解释 Event-v2、补 evidence、修正模型 JSON、查询 SQLite 或直接落库。

### 6.2 Request Envelope

媒体类型：`application/vnd.group-info.llm-request+json;version=1`

```json
{
  "protocol_version": "group-info.llm-request/v1",
  "request_id": "0199...",
  "analysis_run_id": 202,
  "purpose": "event_extraction",
  "model": "configured-workbuddy-model",
  "prompt": {
    "version": "event-extraction-v2",
    "system_prompt": "..."
  },
  "response_schema": {
    "version": "event-v2",
    "json_schema": {}
  },
  "input": {
    "input_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "window": {
      "start": "2026-09-11T22:00:00Z",
      "end": "2026-09-12T22:00:00Z"
    },
    "user_prompt": "{\"contract\":\"event-v2\",\"messages\":[...]}"
  }
}
```

示例中的空 `json_schema` 仅为缩写；实际请求必须携带由 Group-Chat 的 Event-v2
Pydantic 模型生成的完整 JSON Schema，WorkBuddy 不自行维护另一份 schema。

`user_prompt` 中的每条消息只包含当前 Analyzer 已使用的字段：

- `message_version_id`
- `evidence_id`
- `sent_at`
- `chat_name`
- `sender_name`
- `message_type`
- `content`

不得发送 `raw_payload`、飞书 token、Keychain 内容、数据库路径或未进入 AnalysisInput 的消息。

### 6.3 Response Envelope

媒体类型：`application/vnd.group-info.llm-response+json;version=1`

```json
{
  "protocol_version": "group-info.llm-response/v1",
  "request_id": "0199...",
  "analysis_run_id": 202,
  "input_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "model": "actual-workbuddy-model",
  "prompt_version": "event-extraction-v2",
  "schema_version": "event-v2",
  "output": {
    "events": []
  },
  "transport": {
    "status": "succeeded",
    "attempt": 1
  }
}
```

`transport` 仅用于本次调用诊断，不进入 Event 或 Report。token 用量、延迟与 WorkBuddy 内部调用 ID 可以出现在临时日志中，但本阶段不为其增加数据库字段。

### 6.4 Group-Chat 接收校验顺序

1. 协议版本受支持。
2. `analysis_run_id` 存在且状态为 `running`。
3. `request_id` 与原请求一致。
4. `input_hash/model/prompt_version/schema_version` 与 AnalysisRun 一致。
5. `output` 能解析为 Event-v2。
6. 每个 evidence 引用属于本次 AnalysisInput。
7. Event-v2 validator 全部通过。
8. Event、EventEvidence 和 AnalysisRun 成功状态在同一事务提交。

任何一步失败都不得写入部分 Event。模型失败、超时和协议错误最终都映射为 Group-Chat 中该 AnalysisRun 的 `failed`；详细错误通过 CLI 错误包和运行日志返回，不另建 WorkBuddy 状态库。

### 6.5 交接安全性与幂等

- Request/Response 文件只写入 Git 忽略的 `var/`，权限遵循本机用户权限。
- `request_id + analysis_run_id + input_hash` 构成关联键，禁止仅按文件名关联。
- 对成功 AnalysisRun 重放完全相同 Response 返回既有结果；不同 Response 返回 `ANALYSIS_ALREADY_FINALIZED`。
- WorkBuddy 不长期保存包含消息正文的请求；调用完成后按其临时文件策略清理。
- 模型不可获得 Collector 原始 payload 或凭证。

## 7. Group-Chat → 飞书妙搭数据输出协议

### 7.1 原则

- 妙搭只消费 `succeeded` Report。
- 传输对象是展示投影，不是 SQLite 行镜像。
- 现有 Report 内容保持不变，外层增加传输和期间元数据。
- 同一 `report_id` 的展示包内容不可原地改变；重新生成使用新的 report ID。
- v1 先支持本地 UTF-8 JSON 产物。未来通过 HTTPS 或飞书数据连接器交付时复用相同 payload，不改变妙搭字段语义。

### 7.2 Presentation Envelope

媒体类型：`application/vnd.group-info.miaoda-report+json;version=1`

```json
{
  "protocol_version": "group-info.miaoda-report/v1",
  "exported_at": "2026-09-12T08:10:00Z",
  "content_sha256": "...",
  "report_ref": {
    "report_id": 303,
    "analysis_run_id": 202,
    "status": "succeeded",
    "kind": "daily",
    "period_key": "2026-09-12",
    "period": {
      "start": "2026-09-11T06:00:00+08:00",
      "end": "2026-09-12T06:00:00+08:00",
      "timezone": "Asia/Shanghai"
    }
  },
  "report": {
    "schema_version": "mvp-v1",
    "title": "群聊信息报告 · 2026-09-12",
    "source_scope": {},
    "sections": [],
    "exclusions": [],
    "evidence_notes": "...",
    "generated_by": "llm-event-analyzer"
  }
}
```

月报使用 `kind=monthly`、`period_key=YYYY-MM`，其余结构相同。`report` 字段直接承载 Group-Chat 已验证的报告契约，WorkBuddy 和妙搭不得再次总结或改写。

`content_sha256` 对去除该字段后的规范化 JSON 计算，用于检测传输损坏，不用于替代访问控制。

### 7.3 妙搭字段映射

| 妙搭用途 | 来源字段 | 规则 |
| --- | --- | --- |
| 报告唯一键 | `report_ref.report_id` | 不以日期作为唯一键 |
| 日报/月报筛选 | `report_ref.kind` | `daily` 或 `monthly` |
| 期间筛选 | `period_key`、`period.start/end` | 前端不自行计算窗口 |
| 标题 | `report.title` | 原样展示 |
| 分区 | `report.sections[].id/title` | 按 payload 顺序展示 |
| 条目 | `sections[].items[]` | 不重新分类 |
| 证据 | `evidence_ids/evidence_quotes` | 可折叠展示，逐字保持 |
| 来源范围 | `report.source_scope` | 只读元数据 |
| 生成方式 | `report.generated_by` | 用于透明度标识 |

### 7.4 禁止输出

展示包不得包含：

- `raw_payload`；
- 飞书认证信息、token 或 Keychain 数据；
- 数据库连接串和本机绝对路径；
- 未被报告条目引用的完整消息正文；
- WorkBuddy prompt、模型思维过程或内部日志；
- 失败或仍在运行中的 Report 内容。

### 7.5 读取语义

后续若提供 HTTP 读取接口，应保持只读：

- 按 `report_id` 获取不可变展示包；
- 列表查询只返回摘要和 ID，不返回全部 evidence；
- 使用 `content_sha256` 作为 ETag 基础；
- 不提供妙搭回写 Event/Report 的接口；
- 发布鉴权、网络暴露和飞书侧连接配置另行评审，不属于 Phase 6B。

## 8. 运行状态与恢复

不新增 WorkBuddy 状态表。恢复依据如下：

| 步骤 | 权威状态 | WorkBuddy 恢复动作 |
| --- | --- | --- |
| 采集 | `CollectionRun` | 查询 run；失败或未知时用原窗口重试 collect |
| LLM 前 | `AnalysisRun=running` + AnalysisInput | 用同一 request 继续 Transport |
| LLM 后 | `AnalysisRun=succeeded/failed` | 成功则继续报告，失败则结束本次执行 |
| 报告 | `Report=succeeded/failed` | 成功则导出妙搭包，失败则创建新的报告运行 |

为了让 Skill 在进程中断后恢复，后续实现可以增加只读的 run 查询 CLI/API；它只查询现有表，不产生新状态，也不改变 Phase 6A 三个主命令。

## 9. 后续实现顺序与验收条件

### 9.1 建议顺序

1. 实现 LLM Request 导出、Response 接收和 AnalysisRun 终态提交。
2. 将 WorkBuddy Transport 接到现有 `LLMEventAnalyzer`，不绕过 Event-v2 validator。
3. 用固定 Eval Cases 做一次端到端离线交接回归。
4. 实现 `report monthly`，月度 AnalysisInput 直接选取 MessageVersion。
5. 实现 `miaoda-report/v1` 文件导出。
6. 最后在 WorkBuddy 中配置 daily/monthly Skill；调度策略仍留在 WorkBuddy。

### 9.2 验收条件

- WorkBuddy 不存在 Group-Chat run 状态副本。
- 模型请求不包含 raw payload 或凭证。
- 错误 Response 不产生任何 Event/Evidence。
- 重放相同 Response 不产生重复 Event。
- 日报和月报的每条实质结论均能回查本次 AnalysisInput 的 MessageVersion。
- 月报输入来自整月原始消息，而不是日报拼接。
- 妙搭展示包只来自成功 Report，且不包含禁止字段。
- Group-Chat 仓库中仍没有 scheduler、cron 或 WorkBuddy 自动化代码。
