# Phase 6C：WorkBuddy LLM Handoff

## CLI

### 导出请求

```bash
group-info analyze-request \
  --model <model> \
  --start <ISO-8601> \
  --end <ISO-8601> \
  --output var/handoff/<run>/request.json
```

该命令创建状态为 `running` 的 AnalysisRun，冻结 AnalysisInput，并输出
`group-info.llm-request/v1`。不传 `--output` 时，Envelope 写到 stdout；传入路径时使用
权限 `0600` 创建新文件，拒绝覆盖已有文件。

### 导入响应

```bash
group-info analyze-response \
  --analysis-run-id <id> \
  --input var/handoff/<run>/response.json
```

`--input -` 表示从 stdin 读取。导入顺序固定为：

1. 校验协议版本、CLI run ID、`request_id`、`input_hash`、model、prompt 和 schema。
2. 从冻结的 AnalysisInput 重新计算 input hash。
3. 解析 WorkBuddy Response。
4. 执行 Event-v2 validator，包括 evidence 范围校验。
5. 在同一事务中写入 Event/EventEvidence 并将 AnalysisRun 置为 `succeeded`。

关联或输出校验失败时，不写入部分 Event，并将仍在运行的 AnalysisRun 置为 `failed`。
完全相同的成功 Response 可以安全重放；不同 Response 不能覆盖已成功的 AnalysisRun。

## Request Envelope

媒体类型：`application/vnd.group-info.llm-request+json;version=1`

```json
{
  "protocol_version": "group-info.llm-request/v1",
  "request_id": "arq_<32 lowercase hex>",
  "analysis_run_id": 10,
  "purpose": "event_extraction",
  "model": "gpt-5.6-terra",
  "prompt": {
    "version": "event-extraction-v2",
    "system_prompt": "..."
  },
  "response_schema": {
    "version": "event-v2",
    "json_schema": {}
  },
  "input": {
    "input_hash": "<64 lowercase hex>",
    "window": {
      "start": "2026-09-11T01:50:00+00:00",
      "end": "2026-09-12T01:50:00+00:00"
    },
    "user_prompt": "{\"contract\":\"event-v2\",\"messages\":[...]}"
  }
}
```

实际 `json_schema` 是 Group-Chat 从 Event-v2 Pydantic 模型生成的完整严格 schema。
消息输入不包含 raw payload、凭证或数据库路径。

## Response Envelope

媒体类型：`application/vnd.group-info.llm-response+json;version=1`

```json
{
  "protocol_version": "group-info.llm-response/v1",
  "request_id": "arq_<与 Request 相同>",
  "analysis_run_id": 10,
  "input_hash": "<与 Request 相同>",
  "model": "gpt-5.6-terra",
  "prompt_version": "event-extraction-v2",
  "schema_version": "event-v2",
  "output": {
    "events": []
  },
  "transport": {
    "status": "succeeded",
    "attempt": 1,
    "error_code": null
  }
}
```

Transport 失败时使用 `status=failed`、`output=null`，并可提供不含敏感信息的
`error_code`。WorkBuddy 不得修改 Event、补 evidence 或直接写数据库。

## 数据库影响

没有 migration 和核心模型变化：

- Request Export 复用 AnalysisRun 与 AnalysisInput。
- Response Import 复用 Event 与 EventEvidence。
- `request_id` 由 run ID、input hash、model、prompt version 和 schema version
  确定性计算，不建立第二份状态记录。
- Request/Response 文件是 Git 忽略的短期交接产物，不是事实源。

## 真实验证记录

2026-09-12 使用已授权飞书群的最近 24 小时窗口完成首次真实闭环：

- CollectionRun 11：读取 5 条真实消息，本次新增 2 个消息版本。
- AnalysisRun 10：冻结 5 个 AnalysisInput；模型为 `gpt-5.6-terra`，prompt 为
  `event-extraction-v2`，schema 为 `event-v2`。
- WorkBuddy Transport 返回 1 个 Event；导入后生成 1 个 EventEvidence。
- 完全相同 Response 再次导入返回 `replayed=true`，Event 与 Evidence 数量保持 1。
- Report 10：生成 `var/output/2026-09-12/report.json` 与 `report.html`；JSON 通过
  `mvp-v1` 解析，HTML 文件结构检查通过。

记录不包含聊天正文、发送者、token 或原始 payload。
