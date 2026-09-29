# Service mixin 协作 API

`DouyinWikiService` 由 `service.py` 与六个 mixin 共同组成。方法按调用边界命名：

- 被其他 Service 文件调用的方法使用无前导下划线的协作名。
- 仅定义文件内使用的辅助方法保留 `_`；构造函数等 Python 特殊方法不受此约定影响。
- `*_locked` 表示原有锁前提，重命名不改变获取锁的责任。
- 协作方法并不自动成为 Web、CLI 或 MCP 接口；外部接口仍由各适配层显式定义。

`process_*` 调度入口、资料发布和证据处理等跨 mixin 操作使用协作方法。调用
`persist_entry_documents_and_bundle_locked` 前须持有 `entry_operations_locked()`；
不持锁的调用方使用 `persist_entry_documents_and_bundle`。Worker 发布资料还会在
SQLite 写事务中校验租约，避免已失去任务所有权的执行者覆盖新结果。

后续拆分文件时，应同时核对定义、跨文件调用、测试及字符串形式的 mock 名称。
