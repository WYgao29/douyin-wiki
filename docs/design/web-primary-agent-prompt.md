# 抖库 Web 主入口改造——Agent 执行提示词

下面代码块中的内容可以直接发送给负责实现的编码 Agent。

```text
你现在负责改造本地仓库 `douyin-wiki`，目标是让 Web 成为用户日常操作的主入口。

你必须在下面这个独立 worktree 中工作：

- 工作目录：`/Users/weisengao/Documents/ChatGPT/douyin-wiki-web-primary`
- Git 分支：`codex/web-primary-operation`
- 任务入口：`/Users/weisengao/Documents/ChatGPT/douyin-wiki-web-primary/AGENT_TASK.md`

原始仓库 `/Users/weisengao/Documents/ChatGPT/douyin-wiki` 中存在用户未提交内容。除只读查看下列方案文档外，不得在原始仓库执行任何写操作、格式化、提交、重置或清理。所有代码、测试和文档修改都必须写入独立 worktree。

首先完整阅读并严格执行：

1. `/Users/weisengao/Documents/ChatGPT/douyin-wiki-web-primary/AGENT_TASK.md`
2. `/Users/weisengao/Documents/ChatGPT/douyin-wiki-web-primary/docs/design/web-primary-operation-plan.md`
3. 工作目录中的 `AGENTS.md`（如果存在）
4. `/Users/weisengao/Documents/ChatGPT/douyin-wiki/docs/operation-guide.md`（只读）
5. 工作目录中的 `docs/favorites-import.md`
6. 工作目录中的 `docs/superpowers/specs/2026-09-08-favorites-import-design.md`
7. 工作目录中的 `docs/design/ui-redesign-execution-plan.md`

这不是只修改收藏弹窗的任务。你需要实现以下完整闭环：

- Web 授权中心：分别展示并操作“抖音账号授权”和“视频下载授权”。
- 全局任务中心：任务列表、详情、父子任务、进度、时间线、失败重试和等待本人操作。
- 单条链接：完善灵感、证据时间范围、媒体保留、长视频和 AI 消耗确认，并在提交后进入任务详情。
- 博主主页：清点、选择、确认、批次进度、历史、手动同步及导入以前跳过的作品。
- 我的收藏：按“授权 → 更新 → 选择 → 确认 → 执行”重做为线性页面，支持增量/完整更新、历史和缓存管理。
- 人工动作：授权恢复、长视频批准、ASR/OCR 复核、失败任务重试。
- AI 状态：准确区分 provider、local、gateway；不得把 gateway 的等待状态说成后台正在自动完成。
- 系统设置：Worker/队列健康、doctor、存储，以及 maintenance/database rebuild 的预览和确认流程。
- 删除边界：导入历史、收藏缓存、任务日志、知识资料和废纸篓必须使用不同语义，不得互相误删。

实施要求：

1. 先检查 `git status`，记录并保护用户已有未提交改动。不得 reset、checkout、覆盖或清理不属于本任务的变化。
2. 先阅读现有代码和测试，建立 CLI/MCP/Web 能力对照，不要凭文档猜测实现。
3. 保留 FastAPI、Jinja2、原生 HTML/CSS/JavaScript；不引入前端框架、构建链、外部 CDN 或远程字体。
4. Web 必须调用应用服务层，不能通过 subprocess 拼接 CLI 命令实现业务功能。
5. 复用现有任务、收藏、博主、授权、Worker 和数据库逻辑。必要的数据库变更使用增量迁移并保证旧数据兼容。
6. 保持 `127.0.0.1`、同源限制、TrustedHost、CSP、媒体路径校验和敏感信息保护。
7. Cookie、密码、Local Storage 和 API Key 明文不得进入 Web 响应、SQLite、日志、测试快照或 Agent 输出。
8. 自动测试只能使用临时 Vault、模拟数据和离线页面。不要访问、读取、扫描、下载或导入我的真实抖音收藏和博主数据。
9. 不安装、不卸载、不重启我当前使用的 LaunchAgent/Web/Worker 服务，除非我之后明确要求。
10. 不静默切换分析模式，不绕过长视频、AI 消耗、人工复核、维护、重建和删除确认。

工作方式：

- 按执行方案的阶段 0 到阶段 5 推进。
- 每个阶段先给出简短的实现范围和预计修改文件，再开始编码。
- 每个阶段完成后运行相关测试并报告结果，然后继续下一阶段；不要只停在方案或脚手架。
- 优先完成一个真正可用的纵向闭环，再扩展剩余页面。
- 遇到既有未提交改动时合并兼容，不覆盖用户内容。
- 若发现执行方案与实际代码冲突，以实际代码和测试为依据，说明差异并采用最小兼容调整。
- 不要为了减少工作量删除现有能力、测试或安全校验。

架构要求：

- 统一用户阶段：准备、读取来源、下载内容、本地提取、AI 整理、写入知识库、完成。
- 统一返回 `state/state_label`、`stage/stage_label`、`progress`、`message_for_user`、`next_action`、`retryable`、`requires_user_action` 和 `updated_at` 等必要状态。
- 状态转换、确认门槛、幂等和删除边界由服务端强制，不只依赖前端按钮是否可见。
- 大清单使用服务端分页；全选和排除必须作用于整份清单而非当前页。
- 页面关闭后从持久状态恢复。优先使用 SSE 更新，并提供合理的断线降级。
- 原生 JS 按页面拆分模块，共享 API、SSE、dialog、toast、时间和状态映射工具。
- 保证直接 URL、刷新、前进后退、键盘焦点、320px、亮暗主题和减少动画可用。

验证要求：

- 为新增服务端状态门槛、API、数据库迁移和 UI 行为补充自动化测试。
- 覆盖授权成功/失败/过期/账号变化、两条授权通道隔离、父子任务、跨页选择、重复确认、失败恢复、Gateway 等待、删除隔离和危险操作未确认不执行。
- 运行仓库适用的完整检查，至少包括：
  `uv run ruff check .`
  `uv run pytest`
  `node --check src/douyin_wiki/webapp/static/*.js`
- 如果现有环境导致部分检查无法执行，明确记录命令、错误和未验证范围，不得声称通过。

最终交付时请提供：

1. 实现结果摘要。
2. 关键用户流程说明。
3. 修改文件清单。
4. 数据库/API 兼容说明。
5. 测试命令和结果。
6. 尚未执行的真实抖音验证边界。
7. 已知限制和建议的下一步。

只有在 `docs/design/web-primary-operation-plan.md` 的“完成定义”和“总体验收场景”全部满足后，才能宣布整个任务完成。如果单轮无法安全完成全部阶段，请保持代码在测试通过的可用状态，并明确报告已完成阶段、剩余阶段和具体阻塞，不要模糊地宣称已完成。
```
