# Phase 6D：WorkBuddy Daily Report Skill 设计

状态：接口设计，不包含 WorkBuddy Skill 实现、自动化或 scheduler。

## 1. 结论

`daily-report` Skill 只负责编排已经冻结的 Group-Chat CLI 和 WorkBuddy LLM
Transport。所有持久化状态继续由 Group-Chat 的 `CollectionRun / AnalysisRun / Report`
提供；WorkBuddy 不建立任务表、检查点表或运行状态副本。

日报成功的唯一标准是：目标群采集均成功、一个 AnalysisRun 成功、一个关联 Report
成功，并且 Group-Chat 返回的 JSON/HTML 产物存在。LLM 返回成功但尚未导入，不能视为
日报成功。

## 2. Skill 接口

### 2.1 名称与用途

```text
name: daily-report
purpose: 为一个已结束业务日采集授权群消息，调用 WorkBuddy LLM 提取 Event-v2，
         并由 Group-Chat 生成日报。
```

### 2.2 输入

```yaml
report_date: "2026-09-12"       # 可选；默认最近一个已结束业务日
chat_ids:                         # 必填；仅限已授权群
  - "oc_xxx"
chat_names:                       # 可选，仅作显示名，不参与群身份
  oc_xxx: "目标群"
timezone: "Asia/Shanghai"        # v1 默认值
day_cutoff: "06:00"              # v1 默认值
run_at: "06:10"                  # 建议执行时间，只是外部调度策略
model: null                      # WorkBuddy 每次调用时自主选择实际模型
output_root: "var/output"
allow_partial: false              # v1 固定为 false
```

Skill 不接受飞书 token、数据库连接串、prompt 文本或 Event schema。它们分别由本机认证
环境和 Group-Chat 管理。

### 2.3 输出

```json
{
  "skill": "daily-report",
  "status": "succeeded",
  "report_date": "2026-09-12",
  "window": {
    "start": "2026-09-11T06:00:00+08:00",
    "end": "2026-09-12T06:00:00+08:00",
    "timezone": "Asia/Shanghai"
  },
  "collection_run_ids": [101],
  "analysis_run_id": 202,
  "report_id": 303,
  "outputs": [
    "var/output/2026-09-12/report.json",
    "var/output/2026-09-12/report.html"
  ]
}
```

这是本次调用的返回值，不是第二份持久化状态。恢复时必须重新查询 Group-Chat。

## 3. 执行时间与窗口

### 3.1 执行时间

建议由 WorkBuddy 在每天 `06:10 Asia/Shanghai` 调用 Skill。日报业务窗口在 `06:00`
结束，预留 10 分钟用于平台消息可见性和采集延迟。

Group-Chat 不保存或触发这个时间策略，不增加 cron。手工调用时也必须使用同一窗口算法。

### 3.2 窗口算法

使用半开区间 `[start, end)`：

```text
end   = report_date 当天 day_cutoff
start = end - 1 个业务日
```

例如：

```text
report_date = 2026-09-12
timezone    = Asia/Shanghai
day_cutoff  = 06:00

start = 2026-09-11T06:00:00+08:00
end   = 2026-09-12T06:00:00+08:00
```

传给 CLI 的时间必须带时区。Skill 计算一次后，在 collect、查询、分析和恢复中始终复用
完全相同的 start/end；不得用每一步的“最近 24 小时”重新计算。

## 4. CLI 调用顺序

### 4.1 正常路径

对每个群执行：

```bash
group-info collect \
  --source feishu \
  --chat-id <chat_id> \
  --chat-name <chat_name> \
  --since <start> \
  --until <end>
```

全部群采集成功后导出 LLM Request：

```bash
group-info analyze-request \
  --model <model> \
  --start <start> \
  --end <end> \
  --output <private-request-path>
```

WorkBuddy 自主选择支持结构化输出的模型，将实际模型标识传入 `analyze-request`，并生成
符合 Event-v2 与 `group-info.llm-response/v1` 的 Response 后：

```bash
group-info analyze-response \
  --analysis-run-id <analysis_run_id> \
  --input <private-response-path>
```

最后只用成功的 AnalysisRun 生成报告：

```bash
group-info report daily \
  --analysis-run-id <analysis_run_id>
```

每一步必须检查进程退出码和 JSON `status`。后一步只使用前一步 JSON 返回的 run ID，
不得从 SQLite 自行猜测 ID。

### 4.2 LLM Handoff

1. Group-Chat 创建 `AnalysisRun=running` 并冻结 AnalysisInput。
2. Request 包含确定性的 `request_id`、`input_hash`、Prompt v2 和 Event-v2 schema。
3. WorkBuddy 将 `system_prompt/user_prompt` 原样交给其 LLM Transport。
4. WorkBuddy 不解析、不修改、不补全模型产生的 Event。
5. WorkBuddy 用 Request 中的关联字段包装原始模型输出。
6. Group-Chat 校验 request ID、input hash、model、prompt、schema 和 evidence。
7. 只有 Event-v2 validator 通过后，Group-Chat 才写 Event/EventEvidence 并完成
   AnalysisRun。

Request/Response 只能放在 Git 忽略的私有临时路径，建议：

```text
var/handoff/daily/<report_date>/<attempt-id>/request.json
var/handoff/daily/<report_date>/<attempt-id>/response.json
```

这些文件是短期交接载荷，不是运行状态正本。

## 5. 成功判断

### 5.1 分步成功

| 步骤 | 必要条件 |
| --- | --- |
| collect | 每个目标群命令退出码为 0，JSON `status=succeeded`，存在 `collection_run_id` |
| LLM Transport | 返回符合 Response Envelope 的结构化响应；单独这一项不构成 Analysis 成功 |
| analyze-response | 退出码为 0，JSON `status=succeeded`，对应 AnalysisRun 为 `succeeded` |
| report daily | 退出码为 0，JSON `status=succeeded`，Report 为 `succeeded`，JSON/HTML 文件存在 |

`event_count=0` 可以是合法结果：无消息，或窗口内没有值得沉淀的 Event 时，都不能为了让
报告“非空”而伪造 Event。是否成功由 validator 和运行状态决定，不由 Event 数量决定。

### 5.2 整体成功

只有以下关系同时成立才返回 Skill 成功：

```text
all CollectionRun.status == succeeded
AnalysisRun.status == succeeded
Report.analysis_run_id == AnalysisRun.id
Report.status == succeeded
report.json exists
report.html exists
```

## 6. 只读运行查询 CLI 设计

Phase 6D-1 已按本节接口实现查询命令；它是现有运行表的只读观察入口，不是新的
业务抽象层。

### 6.1 窗口查询

```bash
group-info runs latest \
  --start <start> \
  --end <end> \
  [--chat-id <chat_id>]... \
  [--kind all|collection|analysis|report]
```

规则：

- `--start/--end` 必填且按数据库中的规范化 UTC 精确匹配；
- `--chat-id` 可重复，只过滤 CollectionRun；
- `--kind` 默认 `all`；
- Collection 对每个 chat 返回该窗口 ID 最大的一条运行；
- Analysis 返回该窗口 ID 最大的一条运行；
- Report 返回 `analysis_run_id` 等于上述最新 AnalysisRun 的 ID 最大的一条运行，避免把
  旧分析的成功报告误认为新分析已经完成；
- 没有匹配记录时对应字段为 `null` 或空数组，命令仍以退出码 0 返回；
- 命令只查询 `CollectionRun / AnalysisRun / Report`，不读取消息正文、Event 或
  `report_json`。

响应：

```json
{
  "command": "runs latest",
  "status": "succeeded",
  "query": {
    "start": "2026-09-10T22:00:00Z",
    "end": "2026-09-11T22:00:00Z",
    "chat_ids": ["oc_xxx"],
    "kind": "all"
  },
  "collections": [
    {
      "collection_run_id": 101,
      "chat_id": "oc_xxx",
      "status": "succeeded",
      "fetched_count": 12,
      "inserted_count": 2,
      "started_at": "...",
      "finished_at": "...",
      "error_code": null
    }
  ],
  "analysis": {
    "analysis_run_id": 202,
    "status": "succeeded",
    "analyzer": "llm-event-analyzer",
    "model": "<workbuddy-model>",
    "prompt_version": "event-extraction-v2",
    "schema_version": "event-v2",
    "input_hash": "...",
    "created_at": "..."
  },
  "report": {
    "report_id": 303,
    "analysis_run_id": 202,
    "status": "succeeded",
    "created_at": "..."
  }
}
```

### 6.2 按 ID 查询

```bash
group-info runs show --kind collection --id <collection_run_id>
group-info runs show --kind analysis --id <analysis_run_id>
group-info runs show --kind report --id <report_id>
```

返回字段与 `runs latest` 相同。ID 不存在时返回退出码 1 和稳定错误码
`RUN_NOT_FOUND`。该接口用于 WorkBuddy 已持有 run ID 时确认终态。

### 6.3 既有 Request 重导出

为了恢复停在 LLM Handoff 中间的 `running` AnalysisRun，设计一个只读重导出模式：

```bash
group-info analyze-request \
  --analysis-run-id <running_analysis_run_id> \
  --output <new-private-request-path>
```

该模式从既有 AnalysisInput 重建完全相同的 `request_id/input_hash/prompt/schema`，不创建
AnalysisRun、不修改状态。只有 analyzer、model、prompt 和 schema 仍受当前程序支持时才允许
导出；否则返回失败，让旧 run 保持可审计状态。

现有创建模式保持不变，`--analysis-run-id` 与 `--model/--start/--end` 互斥。

## 7. 恢复策略

### 7.1 总原则

每次恢复先调用 `runs latest` 或 `runs show`，再根据 Group-Chat 权威状态决定续跑点。
WorkBuddy 不凭本地布尔值判断完成，也不删除失败记录。

### 7.2 恢复矩阵

| 失败点 | Group-Chat 状态 | 恢复动作 | 禁止动作 |
| --- | --- | --- | --- |
| collect 失败 | CollectionRun=`failed` | 对失败或缺失群使用完全相同窗口重试；新建 CollectionRun，依赖消息层幂等 | 删除失败 run；扩大或漂移窗口 |
| WorkBuddy 在 collect 后中断 | 部分群成功、部分未知 | `runs latest` 按 chat 检查；补跑失败/缺失群 | 建 WorkBuddy 检查点表 |
| LLM Transport 失败 | AnalysisRun=`running`，尚无 Event | 提交 `transport.status=failed` 使该 run 进入 `failed`；重新 collect 后创建新 AnalysisRun | WorkBuddy 直接改数据库；复用 failed run 写 Event |
| LLM 调用时进程中断 | AnalysisRun=`running` | `runs show` 确认状态；从已有私有 Request 重试，文件缺失则按 ID 只读重导出 | 新建重复状态；把日报当输入 |
| Response 文件读取失败 | AnalysisRun 通常仍为 `running` | 修复文件路径/权限后，用同一 run 重试 import | 在未导入前继续 report |
| Response 关联或 Event 校验失败 | AnalysisRun=`failed`，无部分 Event | 保留失败 run；重新 collect 并创建新的 AnalysisRun/Request | 手工修补模型 JSON 后写入旧 run |
| Import 已成功但 WorkBuddy 丢失返回 | AnalysisRun=`succeeded` | `runs show` 确认后直接进入 report；相同 Response 重放也应幂等 | 再建 AnalysisRun |
| report 失败 | AnalysisRun=`succeeded`，Report=`failed` | 使用同一 analysis_run_id 重跑 `report daily`，产生新的 Report 记录 | 重跑 LLM；覆盖失败 Report |
| Report 已成功但 WorkBuddy 中断 | Report=`succeeded` | `runs latest/show` 确认，返回既有 report ID；不再生成 | 新建重复 Report |

### 7.3 重试上限

重试次数、退避和通知属于 WorkBuddy 的执行策略，不写入 Group-Chat。建议单次 Skill 调用内：

- collect 暂时性失败最多重试 2 次；
- LLM Transport 暂时性失败最多重试 2 次；
- response import 的契约或 validator 失败不原地重试，直接创建新的 AnalysisRun；
- report 文件系统暂时性失败最多重试 1 次，每次产生独立 Report 记录。

这些数字是 Skill 默认策略，不是 scheduler 配置，也不是数据库状态。

## 8. 安全和边界

- WorkBuddy 只能访问 Request 中经过筛选的消息字段，不能读取 SQLite 或 raw payload。
- Request/Response 临时文件权限应为 `0600`，使用完按运行环境策略清理。
- Skill 不调用妙搭、不发送报告、不对外发布。
- Skill 不生成 Event 或 Report；它只把模型原始输出交给 Group-Chat。
- Group-Chat 继续独占 validator、EventEvidence 和 Report Builder。
- `runs` CLI 必须是纯查询，不能借查询修复、补写或迁移状态。

## 9. 后续实现验收条件

- `runs latest/show` 对数据库执行只读查询，测试前后表行数与内容不变。
- 同一窗口、多群查询能返回每个群各自最新 CollectionRun。
- `running/succeeded/failed` 三种状态都能稳定序列化。
- Request 按 ID 重导出不会创建新的 AnalysisRun 或 AnalysisInput。
- 四类失败恢复路径均有 CLI 集成测试。
- 原有 Phase 6C 闭环和全部回归测试继续通过。
- 仓库不出现 scheduler、cron、WorkBuddy SDK 或独立状态库。

## 10. Phase 6D-1 实施记录

`runs latest/show` 已直接基于现有 `CollectionRun / AnalysisRun / Report` 表实现：

- 不新增 run service、数据模型、表、缓存或 migration；
- 使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`；
- SQL 显式选择允许字段，不加载 `structured_output` 或 `report_json`；
- 不查询 Message、MessageVersion、Event 或 EventEvidence；
- 查询前后测试数据库逻辑快照一致；真实数据库文件查询前后 SHA-256 一致；
- `running/succeeded/failed` 均可序列化；不存在 ID 返回 `RUN_NOT_FOUND`。

## 11. Phase 6D-2 实施记录

`daily-report` 已安装为用户级 Skill：

```text
/Users/frog/.codex/skills/daily-report/
├── SKILL.md
├── config.example.yaml
├── agents/openai.yaml
└── references/
    ├── workflow.md
    └── recovery.md
```

Skill 没有 scripts、执行器或持久化状态。模拟场景测试直接调用真实 Group-Chat CLI
边界，覆盖正常闭环、LLM 失败后新建 AnalysisRun、报告失败后复用成功 AnalysisRun，
以及重复采集/响应重放/成功状态复用。
