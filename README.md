# 抖库

抖库是一个面向 AI Agent 的 macOS 本地抖音知识库。OpenClaw、Hermes 等 Gateway Agent 接收
用户的抖音分享文本和灵感，本地 Worker 处理媒体，Gateway 使用当前会话模型校正与分析，
最后生成可追溯的 Obsidian Markdown。

为兼容已经安装的环境，命令名、Python 包名、MCP 配置标识和本地状态目录继续使用
`douyin-wiki`；它们是稳定的技术标识，项目展示名称统一为“抖库”。

## v2 能力

- 从整段抖音分享文案提取短链，自动识别视频或静态图文作品。
- 从博主主页或任意单条作品反查博主，先清点全部公开作品，再由用户选择后批量采集。
- 博主同步只在用户手动触发时执行；不会创建每日或定时主页同步任务。
- 博主作品按“待入库、已入库、未入库”分区；所有用户可见日期时间统一使用北京时间。
- 使用浏览器 fresh cookies 调用 `yt-dlp` 下载视频。
- 使用独立 Playwright 浏览器会话采集图文正文和原分辨率图片，不接触日常 Chrome Profile。
- 通过 `ffmpeg`、MLX Whisper/Whisper CLI 和 macOS Vision 完成本地转录与 OCR。
- 默认由 OpenClaw/Hermes 当前会话模型校正与分析文字；原视频不上传给模型。
- 本地 Worker 在转录后暂停，通过持久任务事件把校正、人工确认和完成结果交回原会话。
- 可选切换为后台 OpenAI-compatible provider，或无 token 的本地降级模式。
- 低置信片段暂停等待人工确认，长于 30 分钟的视频在消耗 AI token 前等待确认。
- 生成不可变 `raw/` 记录、`wiki/sources/` 一屏精华页、隐藏机器侧车以及概念/实体 wikilink。
- 自动分类教程、解释、观点、推荐、事件、案例、清单等内容并选择对应知识卡片。
- 视频按内容展开顺序生成“时间轴图解”，以章节起点、摘要、要点和可选对比表完整整理内容；
  不再输出零散的“关键片段”列表。
- 优先保存抖音单独设置的视频封面，并在 `wiki/sources/` 主资料页中直接展示；
  没有独立封面时回退到原始封面或视频关键帧。
- 使用 SQLite FTS5 + 本地 Embedding 返回带原作品链接，以及时间戳或图片编号的证据包。
- 优先索引带上下文、时间戳、原文和来源类型的 `knowledge_atoms`。
- 用户确认后创建 macOS 提醒事项。
- 每周标记过期内容、检查孤立页面，并把到期媒体移入系统废纸篓。
- Web v0.1.7 动态读取 Vault，支持提交抖音链接或分享文案、收藏并永久保留视频，以及封面资料库、文章页和带证据引用的本地 AI 对话。

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
uv run douyin-wiki init --vault /absolute/path/to/抖库
```

默认建议位置：

- 配置：`~/Library/Application Support/douyin-wiki/config.toml`
- Vault：`~/Documents/Obsidian/抖库`（已有 Vault 不自动改名或移动）
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

`configure-model` 会同时切换到 `provider` 模式。云端接口会交互式读取密钥；指向
`localhost`、`127.0.0.1` 或 `::1` 的 LM Studio/Ollama 兼容接口不要求密钥。密钥不会写入
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
任务统一进入“需要登录授权”。本机 Worker 会先保存并解锁任务，再显示 macOS 系统弹框；选择
“打开浏览器授权”后，抖库会打开对应浏览器、验证授权状态，并在验证成功后自动重试同一授权通道中仍处于
“需要登录授权”的任务。选择“稍后处理”、等待超时或浏览器启动失败不会丢失任务，下面的手工命令始终可用。

状态检查同时覆盖视频、图文和博主主页；传入一个视频 URL 时，视频检查会让 `yt-dlp` 执行只读模拟访问，
不下载媒体：

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

自动授权引导默认启用，可在配置中调整最长等待时间和轮询间隔，或完全关闭：

```toml
[auth_guidance]
enabled = true
timeout_seconds = 600
poll_seconds = 5
```

设为 `enabled = false` 只关闭系统弹框，不会禁用 `auth video`、`auth douyin` 或任务手工重试。
选择“稍后处理”只保留任务暂停状态，不会在同一次失败中循环弹框。
自动流程不会读取、复制或显示 Cookie 值、密码和 Local Storage。视频授权使用配置中的日常浏览器；
图文与博主授权使用抖库专用 Playwright Profile。同一授权通道同时只显示一个引导窗口。

### 博主批量采集

主页链接和该博主任意视频/图文分享文本都可作为入口。清点阶段只读取作品信息和临时封面，
不会下载作品、执行 ASR/OCR 或调用 AI：

```bash
uv run douyin-wiki creator add \
  '8.92 复制打开抖音 https://v.douyin.com/rrcucI9W-e8/' \
  --inspiration '关注企业 AI 部署方案'
uv run douyin-wiki worker run --once
uv run douyin-wiki creator inventory JOB_ID
```

`inventory` 默认一次返回全部作品，确保用户在一次回复中看完整清单。只有清单特别大且调用方
主动要求时，才使用 `--page` 和 `--limit` 兼容分页读取。

清单编号在本次任务中固定。新发现作品先显示为“待入库”；保存决定后变为“已选入库”或“未入库”，
全部完成决定后才能确认：

```bash
uv run douyin-wiki creator select JOB_ID --include 1-10,15
uv run douyin-wiki creator select JOB_ID --exclude 11-14
uv run douyin-wiki creator confirm JOB_ID
```

确认后才会为选中的作品建立普通采集子任务。之前跳过的作品可显式重新导入：

```bash
uv run douyin-wiki creator import CREATOR_ID --work-id WORK_ID
```

同步没有计划任务，只有以下命令会重新访问博主主页；没有新作品时直接完成：

```bash
uv run douyin-wiki creator list
uv run douyin-wiki creator show CREATOR_ID
uv run douyin-wiki creator works CREATOR_ID
uv run douyin-wiki creator sync CREATOR_ID
```

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
待处理 → 正在解析 → 正在下载 → 正在转录 → 待 AI 处理（校正）
                                             ↘ 需要人工复核
                                待 AI 处理（分析）→ 正在分析 → 已完成
                 ↘ 等待用户确认                         ↘ 已完成（有提示）/ 失败
```

图文任务状态：

```text
待处理 → 正在解析 → 正在下载 → 正在提取内容 → 待 AI 处理（分析）→ 已完成
                                      ↘ 需要人工复核
                         ↘ 需要登录授权                         ↘ 失败
```

博主清点与批量任务状态：

```text
待处理 → 正在解析 → 正在清点作品 → 待选择作品 → 正在创建采集任务 → 正在处理所选作品 → 已完成
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

Gateway 模式下命令会进入“待 AI 处理”的分析阶段；Provider 模式由 Worker
调用已配置模型。旧 `AnalysisResult` 输入会自动升级，迁移期间仍可读取 v1 数据。

SQLite 是可重建投影。预览或执行 Markdown → SQLite 重建：

```bash
uv run douyin-wiki database rebuild
uv run douyin-wiki database rebuild --apply
```

入库顺序为先原子写入 Vault 文档，再用单个 SQLite 事务提交条目、检索块、关系和提醒。Worker
使用可续期租约；进程异常退出后，只有过期租约对应的处理中任务会自动回到队列。

## 专题研究

专题用于在用户选定的文章范围内进行研究与决策，不会在缺少证据时回退到全库。可以通过 Web
首页多选文章创建，也可以使用 CLI：

```bash
uv run douyin-wiki topic create 'AI 产业研究' \
  --entry-id dy-作品ID1 \
  --entry-id dy-作品ID2 \
  --goal '比较不同来源的共识、分歧和决策依据'
uv run douyin-wiki topic list
uv run douyin-wiki topic show TOPIC_ID
uv run douyin-wiki topic search TOPIC_ID '产业链判断'
uv run douyin-wiki topic generate TOPIC_ID --kind decision_brief
```

专题成果支持总览、对比表、证据地图、共识与分歧、决策简报和 FAQ。成果保存在
`topics/<topic_id>/artifacts/`；来源发生变化后旧成果会标记为“需要更新”，但不会自动重新生成或
消耗 token。专题笔记只有在用户明确确认后才会逐字保存。

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

维护任务只处理知识过期、孤立页面和媒体保留策略，不会访问或同步博主主页。
常驻 Worker 会检测本地程序文件是否已更新：停止领取新任务，等待正在处理的任务结束后退出，
再由 LaunchAgent 自动启动新进程，避免更新前的旧代码处理新任务。

## Web v0.1.7

Web 页面动态扫描 `wiki/sources/` 和 `creators/*/sources/`，不为每篇资料生成或维护单独的
HTML 文件。新文章入库或已有文章更新后，文件监听器会刷新资料库；`raw/`、`.data/`、日志和
机器侧车不会显示。

资料卡片和文章页都可收藏。收藏视频会切换为永久保留；如果本地媒体此前已被维护任务清理，
后台只重新下载媒体，不会再次转录、分析或消耗 AI token。取消收藏后，视频重新采用配置的临时
保留期限；图文资料仍保持永久媒体策略。

前台调试运行：

```bash
uv run douyin-wiki web run
```

默认地址是 [http://127.0.0.1:8765](http://127.0.0.1:8765)。安装为登录后自动运行的
macOS 本地服务：

```bash
uv run douyin-wiki web install
uv run douyin-wiki web status
uv run douyin-wiki web open
# 只移除常驻服务，不删除 Vault 或本地聊天记录
uv run douyin-wiki web uninstall
```

可在配置中调整端口，但主机固定为 `127.0.0.1`：

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8765
```

浏览 Markdown、封面和文章不消耗 token。右侧 AI 对话复用 `[llm]` 中的 OpenAI-compatible
模型与 Keychain 密钥；只有发送问题才调用模型。对话只发送有长度上限的文字上下文和检索证据，
不发送视频、图片、Cookie、隐藏侧车或密钥。聊天历史只保存在 `.douyin-wiki/state.sqlite3`，
可在页面中手动删除。网页写入能力包括提交单独的抖音链接或整段分享文案到后台采集队列、
用户确认后逐字追加灵感，以及将整条资料及其媒体移入抖库废纸篓；废纸篓支持恢复和再次确认
后彻底删除。它不能触发博主同步、提醒或维护。

模型尚未配置时，可在网页左侧打开“模型设置”，或直接访问
[http://127.0.0.1:8765/settings/model](http://127.0.0.1:8765/settings/model)。页面支持填写
OpenAI-compatible 接口地址、模型名称和 API Key：接口与模型写入本地配置，API Key 只写入
macOS Keychain且永不回显；本机 LM Studio/Ollama 可不填密钥。更换云端接口时必须输入新服务
对应的密钥，系统不会把旧服务密钥发送给新接口。保存本身不调用模型；“测试连接”会发出一条极短请求并显示 token
用量。网页配置不会切换现有 `gateway`、`provider` 或 `local` 分析模式；如果当前已经是
`provider`，后台 Worker 下次重启后也会复用这份模型配置。

## Vault 数据边界

- `raw/`：分享原文、原始/校正逐字稿、OCR、来源元数据；首次写入后不覆盖。
- `raw/assets/<video_id>/`：原视频与临时关键帧；默认保留 30 天，也可设为永久保留或处理后清理。
- `raw/covers/<video_id>.*`：Markdown 使用的视频封面；不随临时视频清理。
- `raw/images/<作品ID>/`：图文原图，按 `001`、`002` 顺序永久保留；不进入 Git，也不参与
  30 天媒体清理。
- `wiki/sources/`：封面/首图、灵感、一句话、核心收获、自适应卡片和定位证据组成的一屏资料页。
- `wiki/.data/sources/<video_id>.md`：完整分析、知识原子、ASR/OCR 证据和模型溯源；由 Git
  管理，因为目录以 `.` 开头而默认不显示在 Obsidian 文件列表。
- `wiki/concepts/`、`wiki/entities/`、`wiki/syntheses/`：跨条目知识。
- `creators/<博主名_短ID>/`：博主批量采集的自包含目录，内含 `index.md`、`log.md`、
  `sources/`、`raw/`、概念/实体目录和隐藏 `.data/`；不会与旧资料目录混放。
- 已经存在于 `wiki/sources/` 的作品保持原位，博主 `index.md` 只链接它，不搬迁或复制。
- `index.md` 和 `log.md`：内容地图与只追加日志。
- `.douyin-wiki/`：任务、索引、向量和临时工作目录；可从 Markdown 重建，不进入 Git。

Vault 会初始化为独立本地 Git 仓库。程序每次只暂存本次生成的 Markdown，不自动推送远端。

## 开发验证

```bash
uv run ruff check .
uv run pytest
```

默认测试不访问网络、不读取浏览器 cookie、不创建系统提醒。真实抖音验证使用 `live` marker，需显式运行。

## 本地发行包

构建 wheel 和源码包：

```bash
uv build
```

构建结果写入 `dist/`。可以在新的 Python 3.12 虚拟环境中直接安装 wheel：

```bash
python3.12 -m venv /tmp/douku-release-check
/tmp/douku-release-check/bin/pip install dist/douyin_wiki-0.1.5-py3-none-any.whl
/tmp/douku-release-check/bin/douyin-wiki --help
```

发行包不会包含 Vault、SQLite、Cookie、媒体、模型密钥或本机配置。完整媒体处理仍要求 macOS
以及系统中的 `yt-dlp`、`ffmpeg`、Whisper、Swift Vision 和浏览器授权。

## 当前边界

仅支持本地 macOS 单用户和抖音来源。图文第一版支持静态单图和多图，检测到 Live Photo 会
明确报错而不会静默丢弃动态内容。Web v0.1.5 仅绑定本机回环地址，内部接口不作为远程公共 API。
不包含移动端分享菜单、公网或局域网服务、SaaS、多用户权限、远程同步或自动 Git 推送。
抖音页面与 cookie 规则可能变化，下载错误会保留稳定错误码和原始诊断信息。

## 许可证

本项目采用 [MIT License](LICENSE) 开源。
