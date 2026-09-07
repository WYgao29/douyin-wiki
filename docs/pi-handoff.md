# 抖库 → Pi Agent 交接说明

这份文档用于把“抖库”交给 Pi Agent 操作知识库。目标是让 Codex 和 Pi 使用同一套本地知识库，但分工不同：

- Codex：维护源代码、测试、版本迭代和 GitHub 同步。
- Pi：作为知识库 Gateway Agent，接收用户请求并通过 MCP 操作抖库。
- 抖库 Worker：负责本地下载、转写、OCR、任务队列、Markdown 和 SQLite 索引。
- OMLX：提供本机 OpenAI-compatible 模型接口。

不要把抖库切换到 `provider` 模式。当前应保持 `analysis_mode = "gateway"`，这样视频的逐字稿校正和内容分析会回到 Pi Agent，而不是让 Worker 绕过 Agent 直接调用模型。

## 当前本机环境

- 项目目录：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- Pi：已安装，当前版本为 `0.84.4`
- OMLX 地址：`http://127.0.0.1:8000/v1`
- OMLX 模型：`Qwen3.6-35B-A3B-4bit`
- 抖库 Web：`http://127.0.0.1:8765`
- 抖库用户配置：`~/Library/Application Support/douyin-wiki/config.toml`
- Vault：`~/Documents/Obsidian/抖库`
- SQLite：`~/Documents/Obsidian/抖库/.douyin-wiki/state.sqlite3`

本地配置、Vault、SQLite 和 API Key 不属于 Git 仓库，不得复制到 GitHub。OMLX Key 已保存到 macOS Keychain，服务名为 `douyin-wiki`，账号名为 `OMLX_API_KEY`。

## 第一次配置 Pi

以下命令在普通 macOS Terminal 中执行，并且都在项目目录下完成。

### 1. 安全配置 OMLX 密钥

打开 Pi 的模型配置：

```bash
nano ~/.pi/agent/models.json
```

在 OMLX provider 中，`apiKey` 必须使用 Keychain 命令，不要填写明文密钥：

```json
"apiKey": "!security find-generic-password -s 'douyin-wiki' -a 'OMLX_API_KEY' -w"
```

Pi 支持通过命令动态读取 `apiKey`，这样密钥不会写入 Pi 配置文件。验证时不要打印密钥：

```bash
if security find-generic-password -s douyin-wiki -a OMLX_API_KEY -w >/dev/null 2>&1; then
  echo "OMLX Keychain 已配置"
else
  echo "OMLX Keychain 未配置"
fi
```

验证模型：

```bash
pi --list-models omlx
pi -p --provider omlx --model Qwen3.6-35B-A3B-4bit "只回复：OMLX连接成功"
```

### 2. 安装 Pi 的 MCP 扩展

```bash
pi install npm:pi-mcp-extension
pi list
```

### 3. 配置抖库 MCP

创建项目级配置：

```bash
cd /Users/weisengao/Documents/ChatGPT/douyin-wiki
mkdir -p .pi
nano .pi/mcp.json
```

写入以下内容：

```json
{
  "settings": {
    "toolPrefix": "mcp",
    "requestTimeoutMs": 120000,
    "maxRetries": 3
  },
  "mcpServers": {
    "douyin-wiki": {
      "transport": "stdio",
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "--directory",
        "/Users/weisengao/Documents/ChatGPT/douyin-wiki",
        "run",
        "douyin-wiki-mcp"
      ],
      "lifecycle": "eager"
    }
  }
}
```

这个文件不含密钥。若不希望它出现在 Git 状态中，把 `.pi/` 加入项目的 `.git/info/exclude`，不要把本地密钥写入 `.pi/mcp.json`。

### 4. 启动 Pi

```bash
cd /Users/weisengao/Documents/ChatGPT/douyin-wiki
pi --provider omlx --model Qwen3.6-35B-A3B-4bit
```

进入 Pi 后先执行：

```text
/mcp
```

确认 `douyin-wiki` MCP 已连接。然后发送：

```text
你现在是抖库的主 Gateway Agent。请先读取 docs/gateway-agents.md，检查 MCP 连接和 git 状态。

你的职责是操作本地知识库，不是维护源代码。不要修改 src/、tests/ 或配置代码，不要执行 git commit、git push、git reset，也不要读取或输出任何 API Key。

当用户提供抖音链接时，使用 douyin-wiki MCP：保留用户原始灵感，调用 capture_douyin，传递当前 gateway_context，立即返回 job_id；随后通过 list_job_events 跟进任务。

视频进入“待 AI 处理”后，按 docs/gateway-agents.md 的顺序执行逐字稿校正、人工复核和结构化分析；每个事件处理成功后调用 acknowledge_job_event。

查询知识库优先使用 search_knowledge 和 get_entry。博主批量采集必须先展示作品清单并保存用户选择。提醒、长视频继续处理和不确定内容都必须先获得用户明确确认。
```

## Pi 的工作边界

Pi 可以：

- 通过 `capture_douyin`、`search_knowledge`、`get_entry` 等 MCP 工具操作知识库；
- 跟踪异步任务、校正逐字稿、提交结构化分析；
- 处理人工复核、登录授权和长视频确认流程；
- 读写知识库中由 MCP 工具负责维护的 Markdown、索引和任务状态。

Pi 不可以：

- 直接编辑 SQLite 或 Vault 内部文件来绕过 MCP；
- 修改抖库源代码、测试和部署配置；
- 执行 `git commit`、`git push`、强制推送或破坏性 Git 操作；
- 获取、打印、复制或提交任何 API Key、Cookie 或本地私密数据；
- 自动同步博主主页或自动创建提醒。

## 任务处理顺序

抖库的完整 Gateway 流程定义在 [docs/gateway-agents.md](gateway-agents.md)。Pi 必须遵守以下顺序：

1. `capture_douyin`：提交链接、灵感和 `gateway_context`。
2. 立即向用户返回 `job_id`，不要同步等待下载完成。
3. `list_job_events`：读取未确认事件。
4. 视频先 `get_analysis_context`，再 `submit_transcript_correction`。
5. 需要人工复核时，先展示疑点，等待用户确认后调用 `resolve_review`。
6. 进入内容分析后调用 `submit_gateway_analysis`。
7. 成功处理后调用 `acknowledge_job_event`。
8. 查询资料时保留原始链接、视频时间戳和图文图片编号。

## 与 Codex 共用知识库

Codex 和 Pi 不会自动共享聊天上下文，但可以共享同一份数据。只要两者都连接到本项目的 `douyin-wiki-mcp`，并使用上面相同的用户配置、Vault 和 SQLite，它们操作的就是同一个知识库：

```text
Codex ─┐
       ├─ douyin-wiki MCP ─ 本地 Worker / Vault / SQLite
Pi ────┘
```

Codex 负责代码和 GitHub；Pi 负责知识库日常操作。一个 Agent 新增或修改的资料，另一个 Agent 可以通过 MCP 查询到。

如果 Pi 需要长期跟进异步事件，可以使用 tmux 保持会话：

```bash
tmux new -s douyin-pi
cd /Users/weisengao/Documents/ChatGPT/douyin-wiki
pi --provider omlx --model Qwen3.6-35B-A3B-4bit
```

分离会话使用 `Ctrl+B` 后按 `D`，重新进入：

```bash
tmux attach -t douyin-pi
```

Pi 关闭后不会自动成为后台 Gateway；完全无人值守还需要额外的定时唤醒或消息网关。抖库提供的事件监控命令是：

```bash
uv run douyin-wiki gateway monitor-events
```

## 安全与 Git 边界

可以提交到 GitHub：

- 源代码、测试和公开文档；
- 本交接文档；
- 不包含密钥的配置模板。

不要提交到 GitHub：

- `~/Library/Application Support/douyin-wiki/config.toml`；
- `~/.omlx/settings.json`；
- `~/.pi/agent/models.json`；
- `.pi/mcp.json`（如果只想在本机使用）；
- Vault、SQLite、Cookie 和 Keychain 内容。
