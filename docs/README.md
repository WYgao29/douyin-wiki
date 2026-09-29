# 项目文档

安装、运行、CLI 和发行包说明见根目录 [README](../README.md)，功能变更见 [更新日志](../CHANGELOG.md)。

| 文档 | 用途 |
| --- | --- |
| [项目结构与处理流程](architecture.md) | 入口、分析模式、模块职责、数据落点与开发验证 |
| [Gateway Agent 接入指南](gateway-agents.md) | MCP 接入、校正分析、批量选择与事件接续 |
| [收藏批量导入](favorites-import.md) | 网页、CLI/MCP 操作和验证边界 |
| [授权与任务排障](troubleshooting.md) | 登录、作品类型、待处理任务和失败重试 |
| [Service 协作 API](service-collaboration-api.md) | Service 与 mixin 的命名、锁和发布约定 |
| [Web UI 设计系统](web-ui-design-system.md) | 界面令牌、组件和交互约定 |

## 开发中的功能

[画面检索方案](design/visual-search-indexed-plan.md)仍用于独立分支 `codex/visual-search-phase1` 的开发；当前主线尚未集成此功能。该方案保留原有设计范围，实际进度以对应分支为准。

## 文件维护

正式测试保存在 `tests/`，按功能命名。已完成的实施计划、逐轮审查、临时脚本、日志和截图不作为当前使用文档长期堆放；重要修复进入更新日志与回归测试。临时验证使用独立 Vault，产物放在被忽略的 `.test-*/`；可重新生成的发行包放在 `dist/`。
