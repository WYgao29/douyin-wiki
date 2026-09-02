# 项目文档

- [Gateway Agent 接入指南](gateway-agents.md)：OpenClaw、Hermes 等 Agent 的 MCP 接入和事件流程。
- [抖音登录与短链作品类型误判排障记录](troubleshooting-auth-and-shortlink-type.md)：处理“Chrome 已登录但仍提示授权”，以及图文短链被误判成视频的问题。
- [macOS 授权引导与短链类型校正设计](superpowers/specs/2026-08-30-macos-auth-guidance-and-shortlink-resolution-design.md)：系统弹框、独立授权助手、自动验证和重试的实现边界。
- [macOS 授权引导与短链类型校正实施计划](superpowers/plans/2026-08-30-macos-auth-guidance-and-shortlink-resolution.md)：按 TDD 修复解析器并实现独立授权助手的任务清单。
- [MCP 规则下沉设计备忘](design/mcp-rules-downstream-plan.md)：后期版本的开箱即用 MCP 工作流方案，当前暂缓实施。
- [知识库扩展性 Handoff](superpowers/handoffs/2026-09-01-douyin-wiki-scalability.md)：面向几百到几千篇知识条目的架构建议、实施顺序和验收基线。
- `design/`：已经执行或用于后续迭代的产品与界面设计资料。
- `screenshots/`：Web v0.1.1 在桌面、平板、手机和亮暗主题下的视觉验收记录。

用户安装、运行、CLI、专题和发行包说明以仓库根目录的 [README](../README.md) 为准。
