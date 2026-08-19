# Douyin Wiki

一个面向 AI Agent 的 macOS 本地抖音知识库。OpenClaw、Hermes 等 Gateway Agent 接收
用户的抖音分享文本和灵感，本地 Worker 处理媒体，Gateway 使用当前会话模型校正与分析，
最后生成可追溯的 Obsidian Markdown。

## v2 能力

- 从整段抖音分享文案提取短链，自动识别视频或静态图文作品。
- 使用浏览器 fresh cookies 调用 `yt-dlp` 下载视频。
- 使用独立 Playwright 浏览器会话采集图文正文和原分辨率图片，不接触日常 Chrome Profile。
- 通过 `ffmpeg`、MLX Whisper/Whisper CLI 和 macOS Vision 完成本地转录与 OCR。
- 默认由 OpenClaw/Hermes 当前会话模型校正与分析文字；原视频不上传给模型。
- 本地 Worker 在转录后暂停，通过持久任务事件把校正、人工确认和完成结果交回原会话。
- 可选切换为后台 OpenAI-compatible provider，或无 token 的本地降级模式。
- 低置信片段暂停等待人工确认，长于 30 分钟的视频在消耗 AI token 前等待确认。
- 生成不可变 `raw/` 记录、`wiki/sources/` 一屏精华页、隐藏机器侧车以及概念/实体 wikilink。
- 自动分类教程、解释、观点、推荐、事件、案例、清单等内容并选择对应知识卡片。
- 优先保存抖音单独设置的视频封面，并在 `wiki/sources/` 主资料页中直接展示；
  没有独立封面时回退到原始封面或视频关键帧。
- 使用 SQLite FTS5 + 本地 Embedding 返回带原作品链接，以及时间戳或图片编号的证据包。
- 优先索引带上下文、时间戳、原文和来源类型的 `knowledge_atoms`。
- 用户确认后创建 macOS 提醒事项。
- 每周标记过期内容、检查孤立页面，并把到期媒体移入系统废纸篓。

## 安装

```bash
uv sync --extra dev
uv run douyin-wiki init
uv run douyin-wiki doctor
```

首次执行 `init` 时会启动引导：

1. 选择“新建独立 Vault（推荐）”或“使用已有 Obsidian Vault”。
2. 新建时选择上级目录和 Vault 名称；使用已有 Vault 时填写它的根目录。
3. 系统只创建缺失的目录与模板，不覆盖已有笔记或 Obsidian 配置。
4. 系统初始化本地 Git 和 SQLite，并提示在 Obsidian 中打开该目录。
5. `doctor` 会检查该目录是否已经登记为 Obsidian Vault，并给出具体操作提示。

自动化或无人值守安装可以跳过交互：

```bash
uv run douyin-wiki init --vault /absolute/path/to/Douyin-Wiki
```

默认建议位置：

- 配置：`~/Library/Application Support/douyin-wiki/config.toml`
- Vault：`~/Documents/Obsidian/Douyin-Wiki`
- 状态库：`<Vault>/.douyin-wiki/state.sqlite3`
- 图文专用浏览器：`~/Library/Application Support/douyin-wiki/browser-profile/`

初始化后会注入 `.obsidian/`、`raw/`、`wiki/`、`index.md`、`log.md`、`AGENTS.md`、
`.gitignore` 和 `.douyin-wiki/`。如果目标目录属于已有 Vault，原文件保持不变。

本地中文语义 Embedding 和 Apple Silicon Whisper 是可选的大体积依赖：

```bash
uv sync --extra dev --extra embeddings --extra mlx
```

未安装时会分别降级到字符 n-gram 向量与现有 `whisper` CLI。

## 分析模式

默认配置是 `gateway`：本项目不单独保存模型 API key，AI token 由 OpenClaw/Hermes 中当前
选用的模型消耗。

```toml
analysis_mode = "gateway"
```

三个模式分别是：

- `gateway`（默认）：本地 Worker 做下载、ASR、OCR 和入库；Gateway Agent 做校正与分析。
- `provider`：后台 Worker 直接调用单独配置的 OpenAI-compatible 模型。
- `local`：不调用模型，使用本地启发式降级分析。

`provider` 是严格模式：模型、API key 或端点不可用时任务会明确失败并可重试，不会静默写入
本地启发式结果。OCR 运行异常会写入任务警告，不会被误报为“画面没有文字”。

切换模式：

```bash
uv run douyin-wiki configure-analysis-mode gateway
```

只有选择 `provider` 时才需要配置后台模型：

推荐用初始化命令写入模型配置，并把密钥保存到 macOS Keychain：

```bash
uv run douyin-wiki configure-model \
  --model YOUR_MODEL \
  --base-url https://api.openai.com/v1
```

`configure-model` 会同时切换到 `provider` 模式。命令会交互式读取密钥，不会把密钥写入
配置、Vault、SQLite 或 Git；已安装后台服务时会自动重启 worker。也可以手工编辑配置：

```toml
[llm]
enabled = true
base_url = "https://api.openai.com/v1"
model = "YOUR_MODEL"
api_key_env = "DOUYIN_WIKI_LLM_API_KEY"
```

环境变量会优先于 Keychain，适合 CI 或临时会话：

```bash
export DOUYIN_WIKI_LLM_API_KEY="..."
```

`gateway` 和 `local` 模式不要求该模型配置。

## CLI 工作流

提交任务：

```bash
uv run douyin-wiki capture \
  '6.43 复制打开抖音 https://v.douyin.com/uvHsRpXIn8s/' \
  --inspiration '研究离职后如何筛选财经信息源'
```

视频和图文使用同一个 `capture` 命令。视频 Cookie 失效、图文页面要求登录或出现验证码时，
任务统一进入 `needs_auth`。先查看两套本地认证状态；传入一个视频 URL 时，视频检查会让
`yt-dlp` 执行只读模拟访问，不下载媒体：

```bash
uv run douyin-wiki auth status
uv run douyin-wiki auth status --video-url 'https://www.douyin.com/video/作品ID'
```

视频使用配置中的日常浏览器 Cookie。以下命令只打开对应浏览器，不读取、复制或输出 Cookie；
登录后再次运行 `auth status --video-url`，再重试原任务：

```bash
uv run douyin-wiki auth video
uv run douyin-wiki jobs retry JOB_ID
```

图文使用独立 Playwright Profile；完成一次专用浏览器登录后重试原任务：

```bash
uv run douyin-wiki auth douyin
uv run douyin-wiki jobs retry JOB_ID
```

`auth status` 只返回状态、Cookie 来源和修复动作，从不返回 Cookie 值。没有指定视频 URL 时，
视频检查只读 Chromium Cookie 数据库中的域名、Cookie 名和过期时间，不解密 Cookie 值；
最终服务器可用性仍会在实际下载时验证。专用浏览器目录与用户日常 Chrome Profile 完全分离。
静态图文只执行图片下载和逐图 Vision OCR，不调用 yt-dlp、ffmpeg、Whisper 或逐字稿校正；
背景音乐只保存曲名和作者元数据。

处理和查看：

```bash
uv run douyin-wiki worker run --once
uv run douyin-wiki jobs get JOB_ID
uv run douyin-wiki jobs events
uv run douyin-wiki jobs approve JOB_ID
uv run douyin-wiki jobs retry JOB_ID
uv run douyin-wiki review resolve JOB_ID --resolution 'asr-3=修正后的文字'
uv run douyin-wiki search '财经媒体筛选'
```

任务状态：

```text
queued → resolving → downloading → transcribing → awaiting_agent_analysis（校正）
                                                   ↘ needs_review
                                      awaiting_agent_analysis（分析）→ analyzing → completed
                     ↘ waiting_confirmation                    ↘ completed_with_warnings / failed
```

图文任务状态：

```text
queued → resolving → downloading → extracting → awaiting_agent_analysis（分析）→ completed
                                 ↘ needs_review
                     ↘ needs_auth                                      ↘ failed
```

Gateway 模式的手工调试命令：

```bash
uv run douyin-wiki gateway context JOB_ID
uv run douyin-wiki gateway submit-correction JOB_ID correction.json \
  --producer hermes --model MODEL_NAME
uv run douyin-wiki gateway submit-analysis JOB_ID analysis.json \
  --producer hermes --model MODEL_NAME
```

已入库条目也可以接受结构化重分析，且不会改写用户灵感：

```bash
uv run douyin-wiki entry submit-analysis ENTRY_ID analysis.json \
  --producer codex \
  --model MODEL_NAME
```

复用已有校正逐字稿和 OCR 生成 v2 文档，不会重新下载或转录：

```bash
uv run douyin-wiki entry reanalyze ENTRY_ID
uv run douyin-wiki entry reanalyze-all
# 已是 v2 时默认跳过；需要强制重跑时加 --force
```

Gateway 模式下命令会进入 `awaiting_agent_analysis` 的分析阶段；Provider 模式由 Worker
调用已配置模型。旧 `AnalysisResult` 输入会自动升级，迁移期间仍可读取 v1 数据。

SQLite 是可重建投影。预览或执行 Markdown → SQLite 重建：

```bash
uv run douyin-wiki database rebuild
uv run douyin-wiki database rebuild --apply
```

入库顺序为先原子写入 Vault 文档，再用单个 SQLite 事务提交条目、检索块、关系和提醒。Worker
使用可续期租约；进程异常退出后，只有过期租约对应的处理中任务会自动回到队列。

## MCP

启动 STDIO server：

```bash
uv run douyin-wiki-mcp
```

Codex 配置示例：

```toml
[mcp_servers.douyin-wiki]
command = "/opt/homebrew/bin/uv"
args = ["--directory", "/Users/weisengao/Documents/ChatGPT/douyin-wiki", "run", "douyin-wiki-mcp"]
tool_timeout_sec = 60
```

若不使用 Keychain，可额外设置 `env_vars = ["DOUYIN_WIKI_LLM_API_KEY"]`。

采集是异步任务，因此 MCP 调用不会被作品处理时长阻塞。Gateway 应传入原会话的
`gateway_context`，使用 `list_job_events` 接收持久事件，并在成功处理或投递后调用
`acknowledge_job_event`。没有路由上下文的 CLI 任务不会产生投递事件；同一任务的新状态会淘汰
尚未处理的旧状态，防止 Gateway 恢复后执行过时动作。完整接入配置和 Agent 工具顺序见
[Gateway Agent 接入指南](docs/gateway-agents.md)。

## 后台服务

```bash
uv run douyin-wiki service install
```

这会安装两个本地 LaunchAgent：常驻 worker，以及每周日 03:00 的维护任务。卸载：

```bash
uv run douyin-wiki service uninstall
```

## Vault 数据边界

- `raw/`：分享原文、原始/校正逐字稿、OCR、来源元数据；首次写入后不覆盖。
- `raw/assets/<video_id>/`：原视频与临时关键帧；默认 30 天，可设 `keep` 或 `discard`。
- `raw/covers/<video_id>.*`：Markdown 使用的视频封面；不随临时视频清理。
- `raw/images/<作品ID>/`：图文原图，按 `001`、`002` 顺序永久保留；不进入 Git，也不参与
  30 天媒体清理。
- `wiki/sources/`：封面/首图、灵感、一句话、核心收获、自适应卡片和定位证据组成的一屏资料页。
- `wiki/.data/sources/<video_id>.md`：完整分析、知识原子、ASR/OCR 证据和模型溯源；由 Git
  管理，因为目录以 `.` 开头而默认不显示在 Obsidian 文件列表。
- `wiki/concepts/`、`wiki/entities/`、`wiki/syntheses/`：跨条目知识。
- `index.md` 和 `log.md`：内容地图与只追加日志。
- `.douyin-wiki/`：任务、索引、向量和临时工作目录；可从 Markdown 重建，不进入 Git。

Vault 会初始化为独立本地 Git 仓库。程序每次只暂存本次生成的 Markdown，不自动推送远端。

## 开发验证

```bash
uv run ruff check .
uv run pytest
```

默认测试不访问网络、不读取浏览器 cookie、不创建系统提醒。真实抖音验证使用 `live` marker，需显式运行。

## v2 边界

仅支持本地 macOS 单用户和抖音来源。图文第一版支持静态单图和多图，检测到 Live Photo 会
明确报错而不会静默丢弃动态内容。不包含移动端分享菜单、Web/REST 服务、SaaS、多用户权限、
远程同步或自动 Git 推送。抖音页面与 cookie 规则可能变化，下载错误会保留稳定错误码和原始诊断信息。
