# Group Information System Rules

- 原始消息版本是事实源，只追加，不覆盖、不删除。
- Collector 只负责获取消息并输出统一 `RawMessage`，不得生成分析或报告。
- Event、Analysis、Report 都是可重建的派生层。
- 每条实质报告结论必须能回查到确切消息版本和逐字证据。
- 真实群聊、凭证、数据库和运行产物只放在被 Git 忽略的本地路径。
- 未经明确授权，不发布报告，不接触用户未授权的聊天数据。
- MVP 优先；不引入 SPA、消息队列或微服务。

