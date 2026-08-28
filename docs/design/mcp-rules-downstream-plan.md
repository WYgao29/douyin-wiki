# 抖库 MCP 规则下沉设计备忘

> 状态：暂缓实施
>
> 记录日期：2026-08-27
>
> 适用范围：`src/douyin_wiki/mcp_server.py` 及 Gateway Agent 接入层
>
> 启动条件：产品流程和 MCP 公共接口稳定后，由后续版本重新评审并明确排期

## 1. 文件目的

本文件记录如何把当前依赖 Hermes、OpenClaw、Claude、Codex 等外部 Agent 系统提示词的操作规则，下沉到抖库 MCP Server 本身。

目标是让支持 MCP 的外部 Agent 只需配置抖库 MCP Server，即可通过工具描述、结构化状态机和服务端校验完成绝大多数操作，不再要求用户粘贴一大段专用系统提示词。

本文仅作为后期版本设计依据。当前版本不修改 MCP 工具、任务状态、Gateway 流程或兼容接口。

## 2. 当前实现基础

现有实现已经完成部分规则下沉：

- `FastMCP` 初始化时提供全局 `instructions`。
- 每个 MCP 工具有中文 docstring，用作工具说明。
- `get_analysis_context` 返回结构化 `analysis_schema`。
- SQLite 保存持久任务事件，支持 Agent 重启后继续处理。
- `gateway_context` 保存原会话路由。
- 服务端已经执行部分状态、人工复核、专题来源范围和博主选择校验。
- 工具返回中文显示标签，同时保留内部状态码供程序判断。

当前不足：

- 外部 Agent 仍需记住多个工具之间的调用顺序。
- 多个输入和输出使用 `dict[str, Any]`，Schema 对 Agent 的约束不够明确。
- 工具返回结果没有统一的“下一步动作”结构。
- 一些安全和确认规则主要依赖自然语言说明。
- 普通 Agent 同时看到较多底层工具，容易选错工具或跳过阶段。
- STDIO MCP 无法在没有活跃请求时主动唤醒外部 Agent，异步事件仍需要 Gateway 适配器或轮询。

## 3. 设计原则

后续实施遵循以下原则：

1. 重要规则不能只写在提示词中，必须由服务端状态机和参数校验保证。
2. Agent 不需要记忆流程，只需读取工具结果中的 `next_action`。
3. 用户确认、权限授权和不可逆写入必须有服务端门槛。
4. 工具调用必须幂等，可识别重复、过期和乱序提交。
5. 面向用户的状态、时间和错误信息继续使用中文及北京时间。
6. 保留 Provider、Gateway 和 Local 三种分析模式。
7. 新接口稳定后再废弃旧接口，并提供明确兼容周期。
8. 不把 Cookie、API Key、原视频或原图暴露给外部 Agent。

## 4. 目标交互模型

### 4.1 统一工作流返回值

所有会启动或推进流程的工具统一返回 `WorkflowResult`：

```json
{
  "workflow": "capture_douyin_v1",
  "job_id": "job_xxx",
  "state": "awaiting_agent",
  "state_label": "待 AI 处理",
  "message_for_user": "本地转录已经完成，等待内容校正",
  "next_action": {
    "id": "action_42",
    "type": "agent_generate",
    "tool": "complete_job_action",
    "reason": "需要校正逐字稿",
    "requires_user_confirmation": false,
    "arguments": {
      "action_id": "action_42"
    },
    "instruction": "只修正明显识别错误，不得摘要、删减或补写",
    "input": {},
    "output_schema": {}
  }
}
```

建议的 `next_action.type`：

- `wait`：等待本地 Worker 完成阶段任务。
- `agent_generate`：要求当前 Agent 按 Schema 生成结果。
- `ask_user`：必须向用户提问或确认。
- `authorize`：要求用户在本机完成浏览器或系统授权。
- `call_tool`：可以直接调用指定的后续工具。
- `notify_user`：把阶段结果发送到原会话。
- `done`：流程已经完成。
- `failed`：流程终止，并返回可重试信息。

### 4.2 收敛为高层工作流工具

普通外部 Agent 默认使用：

```text
capture_douyin
capture_douyin_creator
get_next_action
complete_job_action
search_knowledge
get_entry
create_topic
search_topic
```

现有底层工具继续作为内部实现或高级接口：

```text
get_analysis_context
submit_transcript_correction
submit_gateway_analysis
resolve_review
approve_job
acknowledge_job_event
```

后期可考虑提供两个入口：

- `douyin-wiki-mcp`：默认、安全、高层工具集。
- `douyin-wiki-mcp-admin`：调试、维护和兼容工具集。

### 4.3 Agent 的统一执行循环

外部 Agent 只需要遵守通用循环：

```text
调用入口工具
→ 读取 next_action
→ 执行指定动作
→ 调用 complete_job_action
→ 再读取 next_action
→ 直到 done 或 failed
```

业务规则、顺序和输出 Schema 均由 MCP 返回，不再由外部系统提示词长期保存。

## 5. 强类型接口

把以下松散参数逐步替换为 Pydantic 模型：

- `inspirations: list[dict[str, Any]]`
- `gateway_context: dict[str, Any]`
- `analysis: dict[str, Any]`
- 博主作品选择数据
- 人工复核结果
- 提醒确认数据

每个字段至少提供：

- 中文标题和描述。
- 必填或可选状态。
- 枚举值及含义。
- 示例值。
- 长度和格式限制。
- 是否需要用户确认。
- 是否允许 Agent 推断。

工具输出定义明确的 `outputSchema`，避免 Agent 根据自然语言猜测返回格式。

## 6. 服务端强制规则

以下规则必须在服务端执行，不能只依赖 Agent 提示词：

- 灵感和原始证据只能追加，不得被 AI 覆盖或改写。
- 图文作品不能提交视频逐字稿校正。
- 视频 v2 分析缺少章节时间轴时拒绝入库。
- 博主作品仍存在“待决定”时拒绝确认导入。
- 未经用户确认时拒绝长视频分析、提醒创建和专题笔记保存。
- 模糊时间未解析为绝对时间时拒绝创建提醒。
- 专题搜索只能使用当前启用来源，禁止回退全库。
- 过期 action、错误阶段提交和乱序调用必须被拒绝。
- 同一个 action 重复提交必须返回同一结果，不能重复产生副作用。
- 已完成任务不得因重复事件再次通知或重复入库。

建议统一错误结构：

```json
{
  "error_code": "USER_CONFIRMATION_REQUIRED",
  "error_label": "需要用户确认",
  "message_for_user": "该视频超过 30 分钟，是否继续分析？",
  "retryable": true,
  "next_action": {
    "type": "ask_user",
    "confirmation_key": "approve_long_video"
  }
}
```

## 7. 用户确认与授权

在客户端支持时，使用 MCP Elicitation 请求结构化确认：

- 长视频是否继续分析。
- 是否接受不确定的 ASR 或 OCR 内容。
- 模糊提醒时间的澄清。
- 博主作品批量导入的最终确认。
- 是否保存专题笔记。

不支持 Elicitation 的客户端使用兼容路径：

```json
{
  "next_action": {
    "type": "ask_user",
    "question": "……",
    "expected_response_schema": {}
  }
}
```

Cookie、API Key 等敏感信息不得通过普通表单向 Agent 收集。抖音登录继续由本机授权流程完成，MCP 只返回授权范围、命令和状态。

## 8. MCP Instructions、Prompts 与 Resources

三者职责分离：

### Server Instructions

保留简短、稳定的全局说明，仅包含：

- 抖库的用途。
- 高层执行循环。
- 用户确认原则。
- 不得暴露敏感数据。
- 中文用户状态要求。

不得继续在全局 instructions 中堆叠所有阶段细节。

### Prompts

作为用户主动选择的快捷入口，可提供：

- `保存抖音作品`
- `保存抖音博主`
- `检索抖库`
- `创建研究专题`

Prompt 不能承担安全校验，因为客户端可能不加载或用户可能不选择。

### Resources

可提供只读资源：

- `douku://guide`
- `douku://capabilities`
- `douku://job/{job_id}`
- `douku://entry/{entry_id}`
- `douku://topic/{topic_id}`

资源只提供上下文，不代替工作流工具。

## 9. 异步事件边界

STDIO MCP Server 不能在没有活跃客户端请求时主动唤醒任意 Agent。因此不能只依靠 MCP Server 完成 Gateway 模式的异步闭环。

后续版本保留两种方案：

### Provider 模式

外部 Agent 只负责提交、查询和向用户展示结果；校正与分析由抖库配置的模型完成。这是最接近零配置的方式。

### Gateway 模式

增加 Hermes、OpenClaw 等 Gateway Adapter：

```text
本地 Worker 产生事件
→ Adapter 读取事件
→ 唤醒或调用 Gateway Agent
→ Agent 执行 next_action
→ Adapter 提交结果并确认事件
```

Adapter 负责：

- 事件轮询或 Gateway 调度。
- 原会话路由。
- action 幂等和确认。
- 阶段消息降噪。
- 批量任务汇总。
- Gateway 重启恢复。

## 10. 分阶段实施建议

### P0：确定性工作流

- 新增 `WorkflowResult`、`NextAction` 和统一错误模型。
- 新增 `get_next_action`、`complete_job_action`。
- 为 action 增加 ID、版本、有效阶段和幂等键。
- 将关键业务限制改为服务端强制。
- 保留所有旧工具以兼容当前 Gateway。

### P1：接口收敛

- MCP 输入、输出替换为强类型模型。
- 补齐参数说明、枚举、示例和输出 Schema。
- 默认只暴露高层工具。
- 管理和兼容工具移动到高级入口。
- 标记旧工具的弃用版本和迁移方式。

### P2：标准 MCP 交互

- 增加可选 Elicitation。
- 增加用户快捷 Prompts。
- 增加只读 Resources。
- 根据客户端能力自动选择 Elicitation 或 `ask_user` 兼容路径。

### P3：Gateway Adapter

- 提供 Hermes Adapter。
- 提供 OpenClaw Adapter。
- 验证断线恢复、重复事件和批量任务降噪。
- 再评估是否需要受鉴权的本地 HTTP MCP 传输。

## 11. 测试与验收

后期实施至少覆盖：

- 不添加任何抖库专用系统提示词时，Agent 能从 MCP 元数据完成单作品入库。
- 视频和图文能选择正确的不同流程。
- Agent 无法跳过人工复核、长视频确认或提醒确认。
- 错误阶段、重复和过期 action 不会造成副作用。
- 博主清单未全部决定时无法确认导入。
- 专题问答不会引用专题外文章。
- Gateway 重启后能继续未完成 action。
- Provider 模式不依赖外部 Agent 完成分析。
- 不支持 Prompts、Resources 或 Elicitation 的旧客户端仍可使用核心工具。
- Hermes、Claude、Codex 和 OpenClaw 至少各完成一轮兼容验证。
- 用户回复中不出现英文内部状态或非北京时间。

## 12. 后期评审问题

正式启动前需要重新确认：

1. 当前产品流程和任务状态是否已经稳定。
2. 哪些工具属于公共接口，哪些只供内部或调试使用。
3. Gateway 模式是否仍需要外部 Agent 承担逐字稿校正。
4. 是否优先推广 Provider 模式作为默认开箱体验。
5. Hermes、OpenClaw 等目标客户端当时支持的 MCP 协议版本。
6. Elicitation、Sampling、订阅和 HTTP 传输的实际客户端兼容性。
7. 是否允许 MCP Server 通过客户端 Sampling 使用 Gateway 模型。
8. 旧 MCP 接口需要保留几个版本。

## 13. 当前结论

本方案暂缓实施。当前版本继续沿用现有 MCP 工具、Server Instructions、Gateway 事件队列和接入文档。

后期版本的最终目标是：用户只需添加抖库 MCP 配置，外部 Agent 即可根据 MCP 返回的结构化动作完成流程；外部系统提示词只作为可选增强，不再是正确运行的必要条件。
