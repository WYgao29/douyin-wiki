# 抖库画面检索（Indexed sidecar）方案说明

> 状态：v1.14 已吸收续审。非法命中丢弃；未映射只留给合法路径找不到条目。第 1 期不承诺识别「zvec 被单独删除」。尚未编码。
>
> 版本：1.14
>
> 日期：2026-09-22
>
> 仓库：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
>
> 关联外部项目：Indexed 仅作能力样板与开发期 sidecar。抖库**最终版包含画面检索**；Indexed git 仓库开发结束后删除，不作为永久源码依赖。
>
> 目标读者：审核 Agent、实现 Agent、开发者

## 1. 文件目的

本文件说明如何把「用自然语言找画面」做成抖库的正式能力。Indexed 用来对照实现思路（多模态向量、按时间切段、本机检索），不是抖库要长期依赖的那个 GitHub 项目。

**画面识别在本文中的含义：** 对已入库视频/图片做多模态 embedding，用自然语言找回片段或图片（例如「有白板的画面」）。**不是**再做一套 OCR——抖库已有 macOS Vision OCR。也不是对话大模型看视频。

完成后，抖库 Web 有「画面检索」入口：命中时间码或图片序号，并打开资料页（第一期不承诺页内视频 seek）。采集、ASR、OCR、笔记、FTS5、专题、对话仍由抖库负责。Indexed 源码删除后，该入口仍应能工作——靠抖库自己管理的运行时，而不是 `~/indexed` 仓库。

审核重点：架构边界是否正确、是否违反抖库现有约束、分期是否可落地、失败与显存风险是否写清。

## 2. 背景与本机现状

### 2.1 抖库

抖库是面向 AI Agent 的 macOS 本地抖音知识库。日常入口是本机 Web（`127.0.0.1:8765`），技术栈为 FastAPI + Jinja2 + 原生 HTML/CSS/JS。媒体落在 Vault：

- 视频：`{vault}/raw/assets/<作品ID>/original.*`
- 图文原图：`{vault}/raw/images/<作品ID>/`
- 博主资料：`{vault}/creators/<博主>/raw/assets/` 与对应 `raw/images/`
- 知识页：`wiki/sources/`（以及博主自包含目录）
- 检索：SQLite FTS5 + 文本 embedding（sentence-transformers 或字符 n-gram 降级）

现有顶栏搜索只搜知识库文字，不搜画面。

### 2.2 Indexed

Indexed 是本机多模态检索的开源样板（Apache-2.0）。用户用它表达目标，并已明确：

1. **抖库最终版要有画面检索**（产品功能，默认仍可关闭，但是正式能力）。
2. **Indexed 这个项目/源码仓库最终不保留**，开发结束后删除。
3. 因此不能把「删掉 `~/indexed` 之后功能一起消失」写成预期。

分层：

| 阶段 | Indexed git（`~/indexed`） | 画面检索在抖库 |
| --- | --- | --- |
| 开发第 1–3 期 | 本机 sidecar，证明 UI、映射、outbox | 代码留在抖库，对 HTTP 适配器编程 |
| 产品化期（第 1–3 期之后、删源码之前） | 从已构建产物抽出运行时，放到抖库管理的目录 | 抖库启动该运行时；配置不再指向 `~/indexed` |
| 最终版 | 源码仓库可删 | 功能仍在；依赖抖库自己的 runtime 目录 + 模型包 |

禁止把 Indexed **源码树**做成 git submodule 或打进 wheel。允许在产品化期复制**已构建**的最小运行时（Apple helper、必要的 Node 控制面、模型路径），所有权归抖库。适配器模块留在抖库，不要写成「拆功能时连 UI 一起删」。

开发机对照（2026-09-20，M1 Max），不是最终安装路径：

- 源码：`/Users/weisengao/indexed`
- 数据目录：`INDEXED_HOME=/Users/weisengao/.indexed`
- 启动：`/Users/weisengao/indexed/serve.sh` → `http://127.0.0.1:18767/`
- 模型包：`~/.indexed/models/WeMM-Embedding-2B-Apple-Q8-G64`（约 2.50 GiB）
- 执行模式 B：Core ML 视觉（ANE）+ MLX 语言（GPU）
- `embedding validate --full` 状态 `valid`；加载约 70 秒，语言侧约 2GB
- CLI 入口：`~/.local/bin/indexed`（自动设置 `INDEXED_HOME`）
- 当前**没有**开机自启；进程关闭后 18767 为空
- Chrome 扩展未安装，本方案也不安装

Indexed 产品里还有 YouTube/B 站网页采集。抖库不需要这条路。

### 2.3 名称对照（避免审核时混淆）

| 说法 | 含义 |
| --- | --- |
| WeMM-Embedding-2B | 腾讯多模态 embedding 模型 |
| WeMM-Embedding-2B-Apple-Q8-G64 | 给 Apple 后端用的转换包（MLX Q8 语言 + Core ML 视觉） |
| Apple embedding / `apple-native` | Indexed 在 Mac 上跑该模型的方式，不是另一个模型 |
| zvec | Indexed 的本地向量库，与抖库 SQLite 向量列不是同一空间 |
| 画面检索 / 画面识别 | 抖库里用自然语言找画面的产品能力（最终版保留） |

## 3. 决策

采用 **开发期 Indexed sidecar + 抖库自有 UI；产品化后由抖库托管同一类运行时**。用户始终只看见抖库。

```text
用户 ── 抖库 Web :8765 ── FastAPI 适配器 ── HTTP ── Indexed :18767
                              │                      │
                              │                      ├─ Swift WeMM helper
                              │                      └─ zvec（抖库专用 INDEXED_HOME，不复用 ~/.indexed）
                              ├─ SQLite FTS5 / 文本 embedding（不变）
                              └─ Vault 媒体文件（Indexed 只读扫描）
```

拒绝的备选：

| 备选 | 拒绝原因 |
| --- | --- |
| 把 Indexed 的 Swift/Node 改写成 Python | 工作量过大，且要重做 Apple embedding |
| iframe 整站 Indexed Dashboard | 会露出「加任意文件夹 / 配模型」，和抖库设置、设计系统冲突；Dashboard 的 `/api` 写死在同源根路径 |
| 用 WeMM 替换现有 `search.py` | 知识原子、专题证据包、Gateway 引用仍依赖 FTS5 + 文本块 |
| 把 zvec 写入 `state.sqlite3` | 维度（2048 vs 384/模型维）和 `embedding_space` 都不同，混用会搜乱 |
| Chrome 扩展采集 B 站/YouTube | 抖库已经用 yt-dlp / Playwright 把媒体落到 Vault |
| 登记整个 Vault 给 Indexed | Indexed 会索引 `.md` 等文档扩展名，污染画面检索并浪费算力 |

许可证：Indexed Apache-2.0，抖库 MIT，可在抖库中调用其本机 HTTP/CLI，不需要 vendoring 源码。

## 4. 执行约束

### 4.1 必须遵守

- 实现工作发生在抖库仓库的**独立分支或 worktree**。未经用户确认不得改 `/Users/weisengao/indexed` 源码。第 1 期不依赖给 Indexed 加 `--kinds` / `scan:false` HTTP。
- 保留 FastAPI、Jinja2、原生 HTML/CSS/JS；不引入 React/Vue/Svelte/Tailwind/CDN/前端构建链。
- Web 业务走 Python 服务层，**不在请求处理里拼接 shell 调用 `indexed` CLI**。运行时只用本机 HTTP。
- **登记根目录的唯一正式方式：** 直接写入 `{INDEXED_HOME}/config/config.json`（Indexed `configPath()`：home 非空时是 `path.join(home, "config", "config.json")`，不是 home 根下的 `config.json`）。内容含完整 profile + `roots` + 逐根 `rootKinds` + `autoScan: false`。禁止 CLI `assets add`、禁止 HTTP `POST /api/assets/libraries`。
- 身份与就绪信号以 **§4.3** 为准：pin 只用 `GET /api/assets/model-info`；helper 看 `GET /api/embedding-backend` 的 `state === "ready"`；home 看 `GET /api/config`；画面库看 `GET /api/assets/libraries`。`GET /api/status` **会返回** embedding 字段和 `visualReachable`，适配器**不得**用来 pin，也**不得**把 `visualReachable` 当成画面 asset index 可用。
- 第 1 期 config **只有** `visual-index init` 可写。Web 启动只读检查。sidecar 运行中禁止自动停/重启。第 1 期不新增 creator root。`visual_search.base_url` 必须与写入的 `server.host:server.port` 一致，以 Indexed config 为准，抖库字段只能等于它或从它派生。
- 第 1 期 `visual_search.base_url` **只接受** 字面量 host `127.0.0.1`、scheme `http`、无用户名密码、path 为空或 `/`、无 query/fragment。`localhost` 与 `::1` 视为未启用。比较规则见 §5.1，不是整段 URL 字符串相等。
- Web 只绑定 `127.0.0.1`。不放宽 TrustedHost / CSP / 媒体路径校验。
- 不得把 Indexed 的媒体流路径变成「任意本地文件读取」。第一期不反代 `/api/assets/stream`，不新增视频文件 HTTP。
- 不展示 Cookie、密码、API Key、Indexed 配置里的密钥。
- 不替换、不混写现有 SQLite 文本 embedding。画面向量只存在抖库专用 INDEXED_HOME 的 zvec。
- **登记协议写死 kinds**，禁止依赖「映射时再过滤」：
  - `raw/assets` 与 `creators/*/raw/assets` → `kinds=["video"]`（只吃 `original.*` 视频，不索引 `frames/*.jpg`、`cover.*`、`*.json`）
  - `raw/images` 与 `creators/*/raw/images` → `kinds=["image"]`
- 禁止登记 Vault 根、`wiki/`、`.obsidian/`、`.douyin-wiki/`、`raw/`（会扫到 markdown 与 frames）。
- 抖库使用**独立 INDEXED_HOME**（默认 `{vault}/.douyin-wiki/indexed-home`，即 `config.state_dir / "indexed-home"`），不复用用户 `~/.indexed`。启动时 **校验** 该 home（只读），不在 Web 启动时 reconcile/写盘。
- 该 home 必须 `autoScan=false`。第 1 期不扫描。日后扫描只由抖库发起且 `background=true`。
- 启动 sidecar 时 **禁止** 环境变量 `INDEXED_CONFIG`、`INDEXED_DATA_DIR`。`storage.path` 必须为空或位于 `{INDEXED_HOME}/data/zvec`。identity 文件**固定**在 `{INDEXED_HOME}/embedding-identity.json`，与 zvec 目录分离。第 1 期测试覆盖 `INDEXED_CONFIG` 与 `INDEXED_DATA_DIR` 均导致 init/doctor 失败。
- 未经用户确认，不安装 LaunchAgent，不默认开机自启。全量扫描不走 init，走未来单独的 `visual-index backfill --dry-run` / `backfill`（第 3 期，未批准）。
- 第 1 期仍可不改 schema。第 3 期 outbox 未批准前不得实现排水。
- 默认关闭时未安装运行时，doctor 与 Web 不得失败。

### 4.2 不属于本方案

- 公网 / 局域网 Indexed 或抖库。
- 修改 Indexed 的 Apple 模型、执行模式、zvec 格式。
- 扫描版 PDF OCR、YouTube/B 站网页记忆。
- 用画面检索结果自动改写 wiki 笔记。
- 把 WeMM 当作对话模型。
- 第一期导出 ASR 为 `.srt` sidecar（可列为后续，见 §12）。
- 第一期把 Indexed Dashboard 的「素材与扫描 / 模型与数据库」搬进抖库设置页。
- 第一期补 HTML5 `<video>` seek（见 §5.2）。
- 把 Indexed **源码仓库**做成 git submodule 或打进 wheel。
- 开发第 1–3 期就复制整棵 Indexed 树。产品化期只抽已构建最小运行时，清单另写，不在第 1 期做。

### 4.3 第 1 期 HTTP 合同（唯一正文）

字段名以 Indexed git **`c2a9d92`** 的源码为准，不是「以后磁盘上的当前树」。核对过的文件：`apps/server/src/index.ts`、`packages/config/src/index.ts`、`packages/core/src/library.ts`、`packages/core/src/asset-library.ts`、`packages/core/src/search-assets.ts`、`packages/clients/src/local-vectors.ts`、`packages/clients/src/embedding-inputs.ts`。这几份在 `c2a9d92` 与工作树一致。工作树另有未提交的 `package.json` 与未跟踪的 `serve.sh`，**不是**本 HTTP 合同。其他章节只引用本小节，不得另写一套端点或源码里不存在的状态名（例如 `loading`）。

第 1 期客户端只调用下列六个。全部走 loopback HTTP，可注入 `httpx.MockTransport`。

| 方法 | 路径 | 用途 | 不用来 |
| --- | --- | --- | --- |
| GET | `/api/embedding-backend` | helper 是否 ready | pin identity |
| GET | `/api/config` | home handshake | pin identity |
| GET | `/api/status` | 进程能应答；看 `storage` | pin；**不看** `visualReachable` / `transcriptReachable` / embedding* |
| GET | `/api/assets/model-info` | pin：`model` / `dimension` / `embeddingSpace` | helper 是否在跑 |
| GET | `/api/assets/libraries` | 画面 asset library：roots / kinds / `autoScan` / `assetCount` | 旧版 visual index |
| POST | `/api/assets/search` | 查询 | 把 `path` 交给前端 |

**禁用（第 1 期不得封装）：** `POST /api/assets/libraries`（HTTP 虽可带 `kinds`，但省略 `scan` 就会 `queueScan`）、`POST .../libraries/scan`、`.../stop`、`POST /api/assets/prune`、`GET /api/assets/stream`、`GET /api/assets/preview`、CLI `assets add` / `scan`。

#### `/api/embedding-backend`

`ServerEmbeddingRuntime.status()`。无 Apple 后端时 `{ "managed": false, "state": "remote" }`，视为未就绪。

有后端时 `state` 为 `stopped` | `starting` | `ready` | `restarting` | `failed`（源码无 `loading`）。**就绪当且仅当 `state === "ready"`。** `starting` / `restarting` → 产品文案「启动中」。`stopped` / `failed` / `remote` → 不可用。

`executionMode` / `mode` / `visionCompute` / `dimension` 来自本次 `reconcile()` 构造 helper 时的 `this.options`；`model` / `embeddingSpace` 来自 ready runtime。这是**已加载子集**，用来和常量表比对。它不代替 `model-info` 做 embedding identity，也不包含 `privateANE` 或 `video`。磁盘 config 后来改回去，这里仍可能是旧进程的值。

#### `/api/config`

`publicConfig()` 会克隆整份 config，并加上 `configPath`、`indexedHome`、每个 profile 的 `resolvedStoragePath` / `resolvedSpaceId`。密钥被清空。它**不**返回 source revision、build id 或 runtime digest。`c2a9d92` 的 HTTP 没有这个字段。第 1 期不改 Indexed，因此**不能**用握手证明正在跑的 `dist/server/index.js` 来自该 commit。`bin/indexed` 执行的是构建产物，不是 git 对象。

`publicConfig()` 每次 `loadConfig()` 读磁盘，不是 helper 进程里已经加载的那份。`reconcile()` 只在启动或显式重启时用当时的 profile 构造 `AppleEmbeddingBackend`。helper 起来之后改磁盘，再改回标准值，这次握手、`ready` 和 `model-info` 都可以过，helper 仍是旧的 `privateANE`。第 1 期不改 Indexed，**接受这项限制**。不得把磁盘校验写成运行时身份已闭合。

配置、模型包或 helper 路径一旦变更：先停 sidecar，再手工启动，然后才允许 `identity-pin`。pin 不代重启。已 pin 之后磁盘与 identity 不一致 → doctor 失败、搜索拒绝。磁盘与 identity 一致、helper 却是更早一次启动留下的参数：第 1 期发现不了，靠上面的重启规则，不靠 HTTP。

磁盘校验用下面这些**完整路径**。缺字段就拒绝，禁止把缺的 `spaceId` 当成 `""`。`library` 在顶层，不在 profile 里。夹具必须按这个形状，不得把 `spaceId` 放进 `embedding`。

- 顶层 `indexedHome`、`configPath`、`activeProfile`
- 顶层 `library.roots`、`library.rootKinds`、`library.autoScan`
- `profiles[activeProfile].spaceId`
- `profiles[activeProfile].resolvedStoragePath`（响应里后加的；该路径当时的 `lstat` 不是 symlink）
- `profiles[activeProfile].resolvedSpaceId` 等于 `wemm-embedding-2b-apple-2048-2048-wemm-indexed-v1`
- `profiles[activeProfile].embedding.model` / `dimension` / `inputStyle`
- `profiles[activeProfile].embedding.native.binary` / `modelPackage` / `executionMode` / `mode` / `visionCompute` / 整个 `privateANE`
- `profiles[activeProfile].video.chunkSeconds` / `maxChunkSeconds` / `fps` / `width` / `maxSegments`
- `profiles[activeProfile].storage.assetIndex`

路径和 library 做直接比较，不进 `embedding_profile_digest`。digest 只含 §6.4 常量表。

`resolvedStoragePath` 必须位于 `{INDEXED_HOME}/data/zvec`（`storage.path` 为空时 Indexed 默认；也等于 `resolveLocalStoragePath`）。`INDEXED_DATA_DIR` 会改写该解析结果，故第 1 期禁止该环境变量。

#### `/api/status`

`packages/core/src/library.ts` `status()`。`visualReachable` / `transcriptReachable` 探测的是 **legacy** `ossVisualIndex` / `ossTranscriptIndex`（默认 `video-visual` / `video-transcript`），**不是** `assetIndex = "library-assets"`。`visualReachable=true` 不表示画面素材库可用。适配器只把本次 HTTP 成功 + `storage` 当作「进程与存储层应答」；画面库看 `/api/assets/libraries`。

**副作用：** `status()` → `localVectorStatus()` 会 `mkdirSync` zvec 目录（`local-vectors.ts`）。因此「目录已存在」不等于「已有向量」。空库判定见 §6.4，**禁止**用本次 status 调用是否建出目录来决定能不能 pin。

抖库「只读」指：不写 Indexed config、不写 identity、不写 zvec 内容、不停启 sidecar。doctor 可以调用 `/api/status`；Indexed 因此建出空目录，算 Indexed 副作用，不算抖库写盘。不得把这个空目录当成已有向量，也不得因此 pin。

#### `/api/assets/model-info`

`{ model, maxModelLen, dimension, embeddingSpace }`。`dimension` 来自配置，其余可来自 helper discover。pin 用这三项（外加抖库侧 digest 与 execution_mode）。

#### `/api/assets/libraries`

`{ libraries: [{ id, path, name, kinds, assetCount, lastScanAt, state, job }], autoScan, scanIntervalSeconds, maxAssetsPerScan, maxFilesPerLibrary }`。doctor：`autoScan === false`；roots/kinds 与 init 写入一致。`assetCount === 0` 在第 1 期合法（尚未扫描）。

#### `POST /api/assets/search`

请求必须显式：

```json
{
  "query": "…",
  "kind": "video",
  "includeMissing": false,
  "includeLegacy": false
}
```

`includeLegacy` 在 Indexed 里默认是 true（会搜只读 `documentIndex`）。`kind: "video"` 时服务端仍会合并 Chrome 扩展网页记忆（`source: "web"`）。适配器必须：

- 丢弃 `source !== "local"`
- 丢弃 `legacy === true`
- 不把 `path` / `openUrl` 绝对路径交给前端
- video / image 分两次查询再按 score 降序合并

命中先按 §6.1 / §6.2 / §10 过滤，再决定是否返回：

- 丢弃，不进结果：`source !== "local"`、`legacy === true`、root 外路径、不是 `original.*` 也不是编号图（含 `foo.mp4`、`frames/`、`cover.*`）。打日志，不给前端 label。
- `unmapped: true`：路径落在已配置 root 内，且布局是合法的 `original.*` 或编号图，但数据库没有对应条目。只给脱敏 `label`，无 `entry_id`、无 path。
- 已映射：合法布局且 `video_id` 精确匹配。

## 5. 用户可见行为

### 5.1 开关默认关闭

配置新增：

```toml
[visual_search]
enabled = false
base_url = "http://127.0.0.1:18767"
timeout_seconds = 60
indexed_home = ""          # 空则 {vault}/.douyin-wiki/indexed-home
helper_binary = ""         # indexed-apple-embedding 绝对路径，init 注入
model_package = ""         # WeMM 模型包绝对路径，init 注入
```

第 1 期 **不** 把 `model` / `dimension` / `execution_mode` 放进抖库 TOML，也不从模型包或 helper 自动发现。profile drift 的期望值是下列写死常量（与 Indexed `packages/contracts` / `DEFAULT_CONFIG` 一致）：

| 字段 | 第 1 期值 | 源码依据 |
| --- | --- | --- |
| `model` | `wemm-embedding-2b-apple-2048` | `appleEmbeddingModel(2048)` |
| `dimension` | `2048` | `APPLE_EMBEDDING_DEFAULT_DIMENSION` |
| `execution_mode` | `b` | `DEFAULT_CONFIG` 的 `native.executionMode`；本机已验证 mode B |
| `inputStyle` | `wemm` | apple-native `normalizeConfig` |
| `asset_index` | `library-assets` | `DEFAULT_CONFIG.storage.assetIndex` |
| `activeProfile` | `default` | 第 1 期写死 |

`helper_binary` 与 `model_package` 仍来自抖库配置。init / doctor 比较的不是上表六行，而是 **§6.4 `embedding_profile_digest` 的全部输入**（含 `inputStyle`、`spaceId=""`、`privateANE`、`video` 切片、corpus input version、`assetIndex`、zvec schema）外加这两条路径的 digest。任一不一致 → 拒绝，要求 `--reconfigure-embedding`。换执行模式、维度、切片或 ANE 参数是后续版本，不在第 1 期做。

`base_url` 比较：解析 URL 后 `hostname == "127.0.0.1"` 且 `port` 整数等于即将写入的 `server.port`（默认 18767）。`http://localhost:18767` 与 `http://[::1]:18767` 视为未启用，即使它们也是回环。

`enabled = false` 时：侧栏不显示「画面检索」，Worker 不通知 Indexed，doctor 报告「未启用」。抖库其余功能与现在一致。

### 5.2 启用后

1. `uv run douyin-wiki visual-index init --dry-run` 打印将写入 `{INDEXED_HOME}/config/config.json` 的内容，不写盘、不扫描。确认后 `init`：只写该 Indexed 配置文件。sidecar 必须未在跑。
2. **不**修改抖库主配置（`~/Library/Application Support/douyin-wiki/config.toml`）。`helper_binary` / `model_package` / `base_url` 由用户事先写在抖库配置里；init 只校验 `base_url` 与即将写入的 `server.host:port` 一致，不一致则失败，不「设成」。
3. 用户手工启动 sidecar（`INDEXED_HOME` 指向专用 home，且未设置 `INDEXED_CONFIG` / `INDEXED_DATA_DIR`）。
4. `uv run douyin-wiki visual-index identity-pin`：sidecar 必须在跑且 `embedding-backend` ready；仅当 identity 文件不存在且 zvec 为空时原子创建。详见 §6.4。
5. 第 1 期无画面检索页、无存量索引。

### 5.3 Indexed 专用 config 的完整内容（第 1 期必须）

空 `INDEXED_HOME` 上 Indexed 默认 profile 是 `provider: remote`、空 model。只写 roots 不够，`model-info` 会失败或指错模型。

**init 策略：**

- config 不存在：创建完整 apple-native 文件。
- config 已存在且 profile 已是合法 apple-native：只 merge `library.roots` / `rootKinds` / `autoScan`。**但必须先比对** 有效 profile 与 §6.4 常量表（`model` / `dimension` / `inputStyle` / `execution_mode` / `privateANE` / `video` / `spaceId` / `assetIndex` / `activeProfile`）以及抖库配置里的 `helper_binary` / `model_package`；任一不一致 → 拒绝，要求 `--reconfigure-embedding`。不得静默沿用旧路径。不得从抖库 TOML 读取这些 embedding 字段（第 1 期没有）。
- config 已存在但是 Indexed 默认 remote / 空 model（`loadConfig({create:true})` 会写出这种）：**拒绝**，要求 `--reconfigure-embedding`。不得静默保留不可用 profile。
- merge 禁止覆盖 embedding/storage，除非 `--reconfigure-embedding`。
- schema `version` 必须为 1。
- 第 1 期测试：写入路径 = `configPath()`；设置了 `INDEXED_CONFIG` 或 `INDEXED_DATA_DIR` 则失败；已有 remote 空 model 则失败；合法 apple-native 但 helper 路径已漂则失败。

创建时的完整骨架（字段名以 Indexed `DEFAULT_CONFIG` 为准）：

```json
{
  "version": 1,
  "activeProfile": "default",
  "server": { "host": "127.0.0.1", "port": 18767 },
  "library": {
    "roots": ["…/raw/assets", "…/raw/images"],
    "rootKinds": {
      "…/raw/assets": ["video"],
      "…/raw/images": ["image"]
    },
    "autoScan": false,
    "scanIntervalSeconds": 900,
    "maxAssetsPerScan": 400,
    "maxFilesPerLibrary": 100000,
    "kinds": ["video", "image"]
  },
  "profiles": {
    "default": {
      "label": "douku-visual",
      "spaceId": "",
      "embedding": {
        "provider": "apple-native",
        "baseUrl": "",
        "apiKey": "",
        "model": "wemm-embedding-2b-apple-2048",
        "dimension": 2048,
        "inputStyle": "wemm",
        "native": {
          "binary": "<从抖库 visual_search.helper_binary 注入>",
          "modelPackage": "<从 visual_search.model_package 注入>",
          "coreMLCache": "",
          "executionMode": "b",
          "mode": "fast",
          "visionCompute": "ane",
          "privateANE": {
            "videoDownProjection": "q8",
            "videoPipeline": 2,
            "sequenceLength": 2112,
            "mlpFraction": 0.75,
            "mlpVariant": 8,
            "mlpMaxLayers": 24,
            "recurrenceProfile": "",
            "recurrenceBlockSize": 8,
            "recurrenceLayerSlots": [0],
            "recurrenceQueryScale": 4096,
            "recurrenceMaxTokens": 8192,
            "recurrenceIODtype": "fp16",
            "recurrenceVerifyReference": false
          }
        }
      },
      "video": {
        "chunkSeconds": 30,
        "maxChunkSeconds": 60,
        "fps": 2,
        "width": 1280,
        "maxSegments": 240
      },
      "storage": {
        "provider": "local",
        "path": "",
        "assetIndex": "library-assets"
      }
    }
  }
}
```

上面的 JSON 就是第 1 期要写入的骨架，含 `video` 与 `native.privateANE`。`test_init_writes_complete_apple_native_profile` 必须断言这些键和值，不得只断言 model/dimension。缺 `helper_binary` 或 `model_package` 则 init 失败，不得写下半截 remote profile。`dry-run` 打印将写入的 JSON（可打码绝对路径前缀以外的结构），不写盘。

### 5.4 config 变更与常驻进程（第 1 期）

第 1 期 **ownership 写死**：

| 角色 | 允许 |
| --- | --- |
| `visual-index init` / `init --dry-run` | 唯一写 Indexed config 的一方。sidecar 必须未在跑。原子写 `{INDEXED_HOME}/config/config.json`。**不改抖库主配置。** |
| `visual-index identity-pin` | 唯一写 `embedding-identity.json` 的一方。sidecar 必须在跑、helper ready，且 `GET /api/config` 证明进程属于当前 INDEXED_HOME。Web/doctor/search **不得**隐式 pin。 |
| Web 启动 / doctor | **只读**。不得 merge、不得停/启 sidecar、不得写 identity。发现有效 profile 与 §6.4 常量表或 `helper_binary` / `model_package` digest 漂移 → 失败，提示 `--reconfigure-embedding`。 |
| 手工 sidecar | 用户启动。第 1 期不托管、不重启。 |
| creator 新 root | 第 1 期不支持。 |

`base_url` 是用户配置。init 校验它与即将写入的 `server.host:port` 一致，不一致则失败，不回写抖库配置。helper/model 路径同样来自抖库配置，init 把它们抄进 Indexed config，不反向改抖库。

第 3 期才允许 supervisor 停机改 config 再启动。第 1 期不做热 reload。

## 6. 目录与身份映射

### 6.1 允许登记的 library roots

**唯一登记方式：** 写入 `{INDEXED_HOME}/config/config.json`。

Indexed CLI `assets add` 没有 `--kinds`；即使 `--no-scan` 也只 `addLibrary(..., {scan:false})`，kinds 落到全局默认（video+image+document）。HTTP `POST /api/assets/libraries` 虽可带 `kinds`，但省略 `scan` 就会 `queueScan`。这两条路第 1 期都禁用。

`kinds=["video"]` 只按扩展名过滤，walker 会递归收下该根下所有视频，**不限制文件名必须是 `original.*`**。因此：

- 抖库写入这些目录时，视频只允许 `original.*`（现有采集已如此）。不得再放 `preview.mp4` / `draft.mov`。
- 抖库映射只认 `original.*` 与编号图。其它视频若被 Indexed 编进向量，UI 不展示（仍可能浪费算力）。
- 第 1 期测试：盘点计划在 `kinds=video` 下不含 `frames/*.jpg`、`cover.jpg`；映射排除 `foo.mp4`。不在第 1 期改 Indexed walker。

**realpath preflight（公共函数，不是只在 init 做一次）：** `walkLibrary` 对配置的 root 调 `readdirSync`，root 本身是 symlink 时会跟出去扫。子项 symlink 不会跟（`Dirent.isFile()` / `isDirectory()` 对 symlink 为 false）。init 之后把 root 换成 symlink，下次 scan 仍会跟出去。zvec 目录在 pin 之后换成 symlink，identity 里的字符串也不会变。

同一套检查用于：`init` 写入前、`identity-pin` 锁内、doctor、搜索，以及第 3 期每次 scan 之前。第 1 期没有 scan，但函数必须存在，doctor 与搜索必须调用。

每个 library root，以及解析后的 zvec 路径（若已存在），必须同时满足：

- `lstat` 是目录，不是 symlink；zvec 路径不存在时允许（空库）
- 路径等于它的 `realpath`（中间每一段都不是 symlink）
- root 的 `realpath` 位于 Vault 的 `realpath` 之内；zvec 的 `realpath` 位于 `{INDEXED_HOME}/data/zvec`

指向 Vault 外、或即使目标仍在 Vault 内的 symlink，一律拒绝。init 不把 symlink 路径写进 config。事后被替换 → doctor 失败，搜索拒绝，不自动改 config。

新 creator root：**第 3 期**才允许合并进 config 再写盘。第 1 期 init 只登记当时已存在的 `raw/assets` 与 `raw/images`（及当时已有的 creator 目录）。之后新建的 creator 目录不自动加入，doctor 发现缺根只失败。

### 6.2 path → 抖库条目

只认：

```text
视频：<assets_root>/<work_id>/original.<video_ext>
图文：<images_root>/<work_id>/<NNN>.<image_ext>
```

`foo.mp4`、`frames/`、`cover.*`、metadata、root 外路径一律不映射。`work_id` 等于该层目录名，并用数据库 `video_id` 精确匹配。

### 6.3 专用 INDEXED_HOME 与启动校验

默认 `{vault}/.douyin-wiki/indexed-home`。配置文件是 `{INDEXED_HOME}/config/config.json`。

第 1 期 Web/doctor **只读**该文件，不 reconcile、不写盘。缺根、autoScan 被改回 true、出现无关根、进程环境带 `INDEXED_CONFIG` 或 `INDEXED_DATA_DIR` → doctor 失败，提示跑 init（须先停 sidecar）。

端点语义见 **§4.3**。此处只规定 doctor 用法：sidecar 在跑时，home / `configPath` / `resolvedStoragePath` / `assetIndex` 对不上 → 失败。`visualReachable` 即使为 true 也不当作画面库就绪。

Vault 路径变更：禁止只改 roots 继续用旧 zvec。必须换新 `indexed-home` 或显式 reset。第 3 期再做 remove-root + prune；第 1 期 doctor 遇到 stale creator root 只失败，不自动删。

### 6.4 embedding identity 持久化

**位置选定：identity 固定在 home 根，与 zvec 分离。**

| 东西 | 路径 | 谁拥有 |
| --- | --- | --- |
| identity | `{INDEXED_HOME}/embedding-identity.json` | 抖库 `identity-pin` |
| digest 缓存 | `{INDEXED_HOME}/artifact-digest.cache.json` | 抖库（性能优化，非完整性保证） |
| zvec | `{INDEXED_HOME}/data/zvec`（`storage.path` 空时的 Indexed 默认；也等于 `/api/config` 的 `resolvedStoragePath`） | Indexed |
| Indexed config | `{INDEXED_HOME}/config/config.json` | 抖库 init |

「zvec 是否为空」看 **解析后的 `resolvedStoragePath` 的文件系统**，不是 identity 所在目录，也不是「目录在不在」。`GET /api/status` 会先把空目录建出来，所以 identity-pin **不得**调用 `localVectorStatus`，也不得把「目录已存在」当成有数据。只读目录项，不创建目录。

允许 pin 的只有这三种，其余一律拒绝：

| 看到的 | 判定 |
| --- | --- |
| 路径不存在 | 空 |
| 真实目录，且里面没有任何条目 | 空（含 status 刚 `mkdir` 出来的空目录） |
| 真实目录，唯一条目是 `.indexed-zvec.json`，且 JSON 为 `{"format":"indexed-zvec","version":2,"collections":{}}` | 空 manifest |

拒绝 pin：

| 看到的 | 原因 |
| --- | --- |
| 不是真实目录（文件、symlink、损坏路径） | fail closed |
| 任一文件名以 `.lance` 结尾 | legacy LanceDB（`readManifest` 会抛，不能当 zvec） |
| manifest 读不出、`format` 不是 `indexed-zvec`、`version` 不是 2、`collections` 不是对象 | 损坏或未知格式 |
| `collections` 非空（含 0 行 collection；不打开 zvec 数行） | 已有集合，不是空库 |
| 除 manifest 外还有任何条目（子目录、`.DS_Store`、未知文件） | 未知内容 |

第 1 期 pin 之后，identity 存在而 zvec 不存在或仍是空目录，**不是损坏**。这和「尚未建库」无法区分：identity 只记路径和逻辑 collection，不记「库已经建过」。`/api/status` 还可能把缺的目录重新建成空目录。doctor / 搜索把这种情况说成「尚未建立画面索引（第 3 期）」，不 fail closed。

「zvec 建库之后被单独删除」要到第 3 期才能承诺识别。批准 scan 前必须先有持久化的建库标记或库实例标识；有了标记再发现 zvec 缺失或被换成空目录，才 fail closed。第 1 期不得把「identity 在、zvec 不在」写成已能检测单独删除。

reset（第 1 期不做命令，只定义）：要重来就同时删 zvec 目录、identity 和 digest 缓存。只删 identity、留下非空 zvec，仍是损坏，fail closed。备份时这两棵树都要带。

字段：`contract_revision`, `embedding_profile_digest`, `model`, `dimension`, `embeddingSpace`, `execution_mode`, `model_package_digest`, `helper_digest`, `resolved_zvec_root`, `asset_index`, `zvec_index_schema_version`, `zvec_storage_format_version`, `collection_identity`, `pinned_at`。

`contract_revision` 固定 `c2a9d92`。它只表示这份适配器按该 commit 的 HTTP 写的，**不是**运行中 bundle 的证明。`publicConfig()` 不返回 revision。第 1 期不改 Indexed 去加 build id。`helper_digest` 只覆盖 helper 那一个二进制，**不**覆盖 Indexed server bundle、Node、`@zvec/zvec`、metallib。那些字节的 `runtime_digest` 仍属于产品化期。要证明 18767 上的进程就是 `c2a9d92`，必须先批准改 Indexed；未批准前不得把 identity 写成已验证。

**`embedding_profile_digest`：** sha256(canonical JSON)。canonical = 键排序、无空白、UTF-8。输入**正好**是下面这张常量表，再加 `embedding_space`。不含绝对路径，不含 `binary` / `modelPackage` / `roots` / `rootKinds` / `autoScan`。那些另比：路径字符串直接比较；helper 与模型包用各自的内容 digest。

`embedding_space` 必须等于 `wemm-embedding-2b-apple-2048-2048-wemm-indexed-v1`，并与 `model-info.embeddingSpace` 相同。对不上就拒绝 pin。

固定样例（测试必须断言这个字符串和 hash，不得另写一套键）：

```text
{"asset_index":"library-assets","contract_revision":"c2a9d92","corpus_input_version":{"image":"wemm-media-user-v1:apple-prompt-v2","video":"wemm-media-user-v1:apple-prompt-v2"},"dimension":2048,"embedding_space":"wemm-embedding-2b-apple-2048-2048-wemm-indexed-v1","execution_mode":"b","input_style":"wemm","model":"wemm-embedding-2b-apple-2048","native_mode":"fast","private_ane":{"mlpFraction":0.75,"mlpMaxLayers":24,"mlpVariant":8,"recurrenceBlockSize":8,"recurrenceIODtype":"fp16","recurrenceLayerSlots":[0],"recurrenceMaxTokens":8192,"recurrenceProfile":"","recurrenceQueryScale":4096,"recurrenceVerifyReference":false,"sequenceLength":2112,"videoDownProjection":"q8","videoPipeline":2},"space_id":"","video":{"chunkSeconds":30,"fps":2,"maxChunkSeconds":60,"maxSegments":240,"width":1280},"vision_compute":"ane","zvec_index_schema_version":2,"zvec_storage_format_version":2}
```

sha256：`46afadbf61da87b7cc2e5a88bc5088925ddb7283e7c8119ae9e47434f20d9b43`

| 键 | 第 1 期值 |
| --- | --- |
| `contract_revision` | `c2a9d92`（适配器合同，不是进程证明） |
| `model` | `wemm-embedding-2b-apple-2048` |
| `dimension` | `2048` |
| `input_style` | `wemm` |
| `space_id` | `""` |
| `execution_mode` | `b` |
| `native_mode` | `fast` |
| `vision_compute` | `ane` |
| `private_ane` | `videoDownProjection=q8`, `videoPipeline=2`, `sequenceLength=2112`, `mlpFraction=0.75`, `mlpVariant=8`, `mlpMaxLayers=24`, `recurrenceProfile=""`, `recurrenceBlockSize=8`, `recurrenceLayerSlots=[0]`, `recurrenceQueryScale=4096`, `recurrenceMaxTokens=8192`, `recurrenceIODtype=fp16`, `recurrenceVerifyReference=false` |
| `video` | `chunkSeconds=30`, `maxChunkSeconds=60`, `fps=2`, `width=1280`, `maxSegments=240` |
| `corpus_input_version.video` / `.image` | `wemm-media-user-v1:apple-prompt-v2` |
| `asset_index` | `library-assets` |
| `zvec_index_schema_version` | `2`（源码常量 `INDEX_SCHEMA_VERSION`，HTTP 不另报磁盘值） |
| `zvec_storage_format_version` | `2`（空库时写入此期望值；若已有空 manifest，必须读盘为 2） |

改切片、`privateANE`、`inputStyle` 或 corpus version 都会改变 digest。旧 zvec 不得继续当兼容数据。超时、队列长度不进 digest。

`collection_identity` = `assetIndex` + `embeddingSpace` + `resolved_zvec_root` 的规范化三元组（Indexed 本地 collection 名由 indexName + embeddingSpace 派生，见 `localVectorCollectionName`）。任一变化视为不兼容，即使 embedding digest 相同。

**digest 规则：**

- `helper_digest`：helper 二进制单文件 sha256。不含 server bundle / Node / zvec / metallib。
- `model_package_digest`：模型目录的规范化 manifest hash。manifest = 相对路径（posix、排序）+ 每个常规文件的 size 与 sha256。
- **计入：** 常规文件。
- **symlink：** 包内出现任何 symlink（含指向包内）→ identity-pin 与 doctor **拒绝**。不记录「链接 → 目标路径」来冒充内容 hash。本机当前模型包没有 symlink，这条不会误伤它。
- **不计入：** 权限、mtime；目录名 `__pycache__`、`.tmp`、`lost+found`；后缀 `.tmp` / `.cache` / `.DS_Store`；包内已有的 digest 缓存文件。
- **缓存只是性能优化，不是完整性保证。** key 为 `(path, size, mtime_ns, inode)`。size+mtime 被复原但内容被换时，缓存可能仍命中旧 digest。因此完整性保证只存在于：
  - `identity-pin`：**每次强制全量重算** helper 与整个 package manifest，不读缓存。
  - `doctor`：同样**强制全量重算** helper 与整个 package manifest，不读缓存。不提供 `doctor --deep` 第二档——普通 doctor 就是完整性检查。
  - Web 启动 / 搜索路径：可用未失效缓存作可用性检查；对不上 identity 仍 fail closed。这不是完整性保证。
- 不得在每次 Web 启动全量 hash 2.5GiB。

**写入入口只有 `visual-index identity-pin`。顺序写死：先拿锁，锁内重查，临时文件写完后用 `os.link` 发布。** 先看文件再拿锁会让两个进程都看到「不存在且为空」。macOS 上 `rename` 会覆盖已有文件，不能代替不可覆盖。禁止对最终路径 `O_EXCL` 打开后再写入。

1. `fcntl.flock(LOCK_EX | LOCK_NB)` 锁 `{INDEXED_HOME}/identity.lock`。拿不到 → 立即失败。禁止把锁文件本身当 `O_EXCL` 互斥文件。
2. **仍持有锁**时重查，不得使用进锁前的结果：
   - identity 文件已存在 → 拒绝，不覆盖
   - zvec 按 §6.4 空库表判定；非空 → 拒绝。禁止用「目录存在」或 `localVectorStatus`
   - §6.1 preflight：roots 与 zvec 路径不是 symlink
   - sidecar 在跑；`GET /api/embedding-backend` 的 `state === "ready"`；`GET /api/assets/model-info` 成功
   - `GET /api/config` 的磁盘字段按 §4.3 完整路径与常量表、roots、路径直接比较。这不是 helper 已加载证明。`GET /api/embedding-backend` 的已加载子集（`executionMode` / `mode` / `visionCompute` / `dimension` / `model` / `embeddingSpace`）也必须与常量表一致。18767 是别的 home → 拒绝
3. 在同一目录写临时文件 `embedding-identity.json.{pid}.tmp`，写完整 JSON，fsync 文件。禁止对最终路径 `open` 后再写入（崩溃会留下空文件或半截 JSON，之后 pin 因「已存在」永远拒绝）。
4. `os.link(临时文件, embedding-identity.json)` 发布。`link` 不覆盖；`EEXIST` → 拒绝，删掉临时文件。然后 fsync 父目录，再删临时文件。禁止 `rename` 盖到已有 identity。
5. 释放 flock。进程崩溃由内核释放锁；锁释放**不会**补完半截最终文件，所以最终文件只能由 `link` 出现。
6. Web、doctor、search **禁止**隐式 pin。identity 存在但不是完整 JSON → fail closed，不覆盖。

之后每次就绪只读比对 identity 全部字段（含 zvec 绑定）。不一致则拒绝搜索。更新 identity 只允许用户确认重建之后（第 1 期不做重建命令）。

`enabled=false` 只停止抖库调用；专用 home 的 autoScan 仍为 false，因此不会自己扫。不在关闭开关时 purge 向量。

## 7. 模块设计

建议新增，不把逻辑塞进 `webapp/app.py` 神文件：

| 模块 | 职责 |
| --- | --- |
| `src/douyin_wiki/config.py` | `VisualSearchSettings` |
| `src/douyin_wiki/adapters/indexed.py` | 第 1 期只封装 §4.3 六个：`embedding-backend`、`config`、`status`、`model-info`、`libraries`、`search`。不得少，不得多。第 3 期才加 scan / prune |
| `src/douyin_wiki/visual_index.py` | 目录登记策略、path 映射、启用校验、与 Worker 的队列接口 |
| `src/douyin_wiki/webapp/` 新路由 | `/visual` 页面数据、`/api/visual/search`、健康检查；可选流媒体反代 |
| `webapp/static/visual-search.js` | 仅该页的前端 |
| `webapp/templates/app.html` | 侧栏一项 + `#visual-view` |
| `tests/test_visual_index.py` | 纯函数映射、配置默认、启用开关（第 1 期竖切） |
| `tests/test_indexed_adapter.py` | `httpx.MockTransport` 合同测试（第 1 期竖切） |
| `tests/test_visual_web.py` | TestClient：侧栏、搜索 API、离线态（第 2 期竖切） |
| `tests/test_visual_worker.py` | 落盘通知、失败不翻盘、prune（第 3 期竖切） |

Worker **不得**在下载刚写入 `raw/assets` 时入队。必须在最终路径确定之后。`RetentionPolicy.DISCARD` 的媒体**不进入画面索引**；若已入队，清理时改为 prune，不得对已删文件 scan。

第 3 期 outbox **尚未批准实现**。当前草案不够：`POST .../scan` 只返回 `queued`。补设计前不得写排水循环。需要至少：按 root 聚合、job 完成轮询、lease、幂等键、scan/prune 区分、退避、崩溃恢复。见 §9.2。

第 1 期只准备映射、写 config、HTTP 客户端（**§4.3 六个端点**）。不发 scan。

Web 请求路径：浏览器 → 抖库 `/api/visual/search` → 适配器。

抖库搜索响应（第 1 期适配器即按此形状，不把本机绝对路径交给前端）：

```json
{
  "ok": true,
  "query": "有白板的片段",
  "offline": false,
  "hits": [
    {
      "entry_id": "dy-…",
      "kind": "video",
      "start_seconds": 12,
      "end_seconds": 22,
      "image_index": null,
      "score": 0.42,
      "score_kind": "ranking",
      "title": "…",
      "cover_available": true,
      "unmapped": false
    }
  ]
}
```

规则：`score` 只是排序分，不是置信度。video / image 分开查再按 score 降序合并；一类失败时仍返回另一类并在顶层带 `partial_error`。非法命中直接丢弃，不出现在 `hits`。`unmapped: true` 只用于合法 `original.*` / 编号图找不到条目：不带 `entry_id`、不带绝对 path，只给脱敏 `label`（作品 ID 或文件名）。

Indexed HTTP 合同见 **§4.3**（第 1 期六个端点；第 3 期才加 scan/stop/prune）。搜索请求必须带 `includeMissing: false` 与 `includeLegacy: false`；丢弃 `source !== "local"` 与 `legacy === true`。

## 8. 与现有检索的关系

| 通道 | 引擎 | 问法 | 证据 |
| --- | --- | --- | --- |
| 顶栏 / `search_knowledge` / MCP | FTS5 + 文本向量 | 原话、概念、灵感 | 时间戳或图片编号 + wiki |
| 画面检索 | WeMM + zvec | 画面长什么样 | 时间码 + 本地媒体 + 尽量链到资料页 |

MCP 第一期**不**新增 `search_visual`，除非审核认为 Agent 必须同步具备。建议第二期再加，以免 Gateway 把画面命中误当成知识原子。

Chat 右侧对话第一期也不自动混入画面命中，避免把 WeMM 分数当成「原文证据」。

## 9. 生命周期

| 事件 | 画面索引动作 |
| --- | --- |
| 下载写入临时 `raw/assets` | **不入队** |
| 最终路径确定且 retention ≠ DISCARD | 第 3 期才入队（本期不实现） |
| `RetentionPolicy.DISCARD` | 永不 scan；已入队则改 prune |
| 采集失败、未落盘 | 不入队 |
| Indexed 离线 | outbox 保留，启动时重放 |
| 临时媒体清理 / 废纸篓 / 彻底删除 | outbox `prune`；Indexed prune 只删索引行 |
| 从废纸篓恢复且文件回来 | 再入队 scan |
| 收藏导致重新下载 | 最终路径确定后再入队 |
| `enabled=false` | 停止排水；autoScan 保持 false；不 purge |

### 9.1 GPU 硬策略（第 3 期才实现，第 1 期不扫）

禁止「先看槽空闲再发 HTTP」。Whisper/OCR 与 visual scan 争同一把锁。

**锁必须持有到 Indexed scan job 完成（成功、失败或超时），不能在 HTTP `queued` 返回后释放。** 因此第 3 期调度器必须包含 job 轮询；只做到 enqueue 无法保证 GPU 不重叠。第 1 期不扫，不实现这把锁。

禁止 CLI `assets scan`（无 background，并发 2）。

### 9.2 Outbox（第 3 期设计未批准）

v1.4 的「每文件一条 pending_scan」不够。`scan` HTTP 只表示 queued。批准第 3 期前必须补：

- 按 `library_root` 聚合，合并同一根上的重复事件
- 轮询 libraries job：完成、errors、崩溃后恢复
- `operation`: scan | prune
- `idempotency_key`、`job_id`、`entry_id`
- `next_attempt_at`、`claimed_by`、`lease_expires_at`

未通过该补设计前，不得实现排水循环。另外，批准 scan 前还要有持久化建库标记或库实例标识，否则无法区分「尚未建库」和「zvec 被单独删除」。没有这个标记，不得把单独删除 zvec 写成可检测。

## 10. 失败模式

| 情况 | 产品行为 |
| --- | --- |
| Indexed 未启动 | 画面页说明如何启动；采集任务完成不失败，可带非致命警告 |
| 模型仍在加载（约 70s，`state` 为 `starting` / `restarting`） | 健康检查显示「启动中」，搜索按钮不可用或排队 |
| 扫描中 | 任务中心或画面页显示「画面索引进行中」，禁止当成卡死 |
| 413 / 超长 | 单条媒体失败并记录，不重试死循环（Indexed 扩展侧有过无限重试 bug，抖库适配器必须设最大次数） |
| GPU 争用导致 helper 重启 | 适配器尊重 Indexed 的 autoRestart；抖库侧指数退避 |
| path 落在媒体根外 | 拒绝映射，当错误命中丢弃并打日志 |
| `enabled=true` 但从未登记目录 | doctor 失败；画面页提示需要初始化（一次性运维，可用 CLI） |

采集主状态机（待处理 → 下载 → 转录 → AI → 完成）**不**因为画面索引失败而进入失败。画面索引是附加通道。

## 11. 安全

- 抖库继续只绑 `127.0.0.1`。Indexed 亦只绑回环。
- 第一期不反代 `/api/assets/stream` 或 preview。
- 点击结果只打开抖库文章页；预览用现有图片 `/media/`（封面或关键帧）。正文显示命中时间文本。
- 页内视频 seek 列为第 4 期单独确认（需受控视频 URL + 播放器 + 安全审核，并放宽 `safe_media_path` 的专项评审）。

## 12. 分期与验收

### 第 0 期 — 本文审核

- 本文件经用户指定的其他 Agent 审核。
- 不写业务代码。

### 第 1 期 — 适配器 + init + 合同（无新页面）

编码范围：config、`visual-index init`（只写 Indexed config）、`visual-index identity-pin`、映射、HTTP 客户端（**§4.3 六个端点**）、doctor 只读校验、测试。**不发 scan，不写 outbox，不加 scan/prune 客户端，不改抖库主配置。**

验收：

- `enabled=false` 时零 HTTP。
- `localhost` / `::1` / 非 `127.0.0.1` 的 base_url 视为未启用。
- init 只写 Indexed config：含逐根 rootKinds 与 autoScan=false；不调用 assets add；不改抖库主配置；`base_url` 与 `server.host:port` 不一致则失败。
- 已有 remote/空 model 的 config：init 拒绝，除非 `--reconfigure-embedding`。
- 环境存在 `INDEXED_CONFIG` 或 `INDEXED_DATA_DIR`：init/doctor 失败。
- `kinds=video` 盘点不含 frames/cover；映射拒绝 `foo.mp4`。
- embedding-backend 夹具测 `state === "ready"`；status 测 server/storage 可达（字段可含 embedding 与 `visualReachable`，都不用来 pin 或判断画面库）；libraries 测 asset index；model-info 测 identity。
- sidecar 在跑时 init 拒绝。
- identity 只由 `identity-pin` 写入；须先 `/api/config` handshake；18767 是其他 home、`resolvedStoragePath` 在 home 外、或 `assetIndex` 不等于 `library-assets` 则拒绝；并发两个 pin 只有一个成功；有 zvec 无 identity 则 fail closed；崩溃后 flock 不残留。
- doctor/init 发现 helper/model 路径或 §6.4 常量表（含 video / privateANE / inputStyle）漂移则失败。
- identity 含 `embedding_profile_digest` 与 `contract_revision=c2a9d92`（不是进程证明）；改 `video.fps` 或 `privateANE` 后旧 identity 拒绝搜索。
- 两个 pin 并发：先拿 flock，锁内重查；`rename` 不得覆盖已有 identity。
- live `/api/config` 的 privateANE / video / roots 与常量表不一致则拒绝 pin。
- init 之后把 root 换成 symlink：doctor 与搜索拒绝。
- 空库表：缺目录、空目录、空 manifest 可以 pin；`.lance`、损坏 manifest、非空 collections、未知文件拒绝。status 建出的空目录仍可 pin。
- symlink root（含指向 Vault 内）拒绝登记；模型包内 symlink 拒绝 pin。
- doctor 全量重算 helper 与 package manifest（不读 digest 缓存）。
- 搜索适配器 POST 含 `includeLegacy: false`；丢弃 web/legacy 命中；不向调用方返回绝对路径。
- **第 1 期不建立已有媒体索引。**

第 1 期不 backfill。doctor 在 enabled 且 identity 已 pin、但 libraries 扫描计数为 0 时，说明「尚未建立画面索引（第 3 期）」。

开发期 sidecar 由用户手工启动。产品化期再定：Web / Worker / 独立 supervisor / LaunchAgent 谁负责起停、崩溃重启、端口冲突。第 1 期不定 ownership。Worker 是长期循环，适合作为日后协调者候选人，但不在第 1 期实现。

### 第 2 期 — Web「画面检索」页

编码范围：侧栏、`#visual-view`、`/api/visual/search`、空态/离线态、资料页命中时间文案。

验收：

- 顶栏文字搜索结果不变。
- 画面页离线有说明；在线能展示夹具命中时间，**页面不得出现 `<video>` 或 seek**。
- 能从精确布局 path 解析到临时 Vault 的 entry。
- 无 Indexed Dashboard iframe，无第二套模型设置页。

### 第 3 期 — 暂不批准

补齐 §9.2（按 root 队列、完成判定、lease、幂等）以及同一把 GPU 调度锁之前，不得编码排水循环、scan、prune Worker。

### 第 4 期 — 可选，需单独确认

- 系统设置开关、显存说明、全量索引进度条（必须确认）。
- 可选 LaunchAgent（默认关）。
- MCP `search_visual`。
- ASR → `.srt` sidecar。
- 受控视频 URL + 页内播放与 `currentTime` 定位（需安全评审）。
- live 评估：延迟、显存峰值、无关查询分数阈值。不做 CI 门禁。

### 产品化期 — 暂不批准

第 1–3 期完成且补齐下列清单之前，不得抽取 runtime、不得删除 Indexed git、不得宣称最终版已具备画面检索：

- clean build 与 artifact hash（pinned `c2a9d92` 工作树现有未提交 `package.json` / `serve.sh`，不能当可复现构建）
- Node 20、`@zvec/zvec`、Apple helper、metallib、模型包、native 依赖清单
- 签名/公证（若分发二进制）
- LICENSE / NOTICE
- `runtime_digest` 替代 git commit
- zvec 升级与不兼容时的换空目录策略

仅复制 `dist/server/index.js` 不足。

## 13. 实现时建议改动的文件

仅指导，审核可增删：

```text
src/douyin_wiki/config.py
src/douyin_wiki/adapters/indexed.py          # 新建
src/douyin_wiki/visual_index.py               # 新建
src/douyin_wiki/setup.py                      # doctor
src/douyin_wiki/worker.py 或 service.py       # 落盘钩子（第 3 期）
src/douyin_wiki/webapp/app.py                 # 路由（第 2 期，保持瘦）
src/douyin_wiki/webapp/visual_api.py          # 新建，第 2 期
src/douyin_wiki/webapp/templates/app.html
src/douyin_wiki/webapp/static/app.js          # 仅导航接线
src/douyin_wiki/webapp/static/visual-search.js
src/douyin_wiki/webapp/static/app.css         # 复用现有卡片样式，少加新皮肤
src/douyin_wiki/cli.py                        # visual-index init / identity-pin
src/douyin_wiki/database.py                   # 第 3 期 outbox 迁移
tests/test_visual_index.py
tests/test_indexed_adapter.py
tests/test_visual_web.py                      # 第 2 期
tests/test_visual_worker.py                   # 第 3 期
tests/fixtures/indexed_assets_search.json
tests/fixtures/indexed_status.json
tests/fixtures/indexed_embedding_backend.json
tests/fixtures/indexed_model_info.json
tests/fixtures/indexed_libraries.json
tests/fixtures/indexed_config.json
docs/README.md
README.md                                     # 用户文档，第 2 期以后
```

不修改：`adapters/embeddings.py` 的文本向量空间、`search.py` 的 FTS 融合公式、Vault markdown 格式。

## 14. 已锁定的决策（2026-09-20 用户）

1. 第一期不补 `<video>` seek。资料页只展示命中时间文本。**接受。**
2. 独立 INDEXED_HOME：`{vault}/.douyin-wiki/indexed-home`，不复用 `~/.indexed`。**接受。**（试验拆除时删该目录即可，不碰用户 `~/.indexed`。）
3. init 写 `{INDEXED_HOME}/config/config.json`。**接受。**
4. GPU：第 1 期不扫。第 3 期必须与 Whisper **同一把锁**，禁止 check-then-send。OS 级锁仍不做。
5. Recall@K 不进 CI。**接受。**
6. 画面检索是抖库最终能力；Indexed git 可删。**产品化期暂不批准**，清单见 §12。

2026-09-21 第五次复审：identity-pin 必须 `/api/config` handshake；identity 与 zvec 分离；profile drift 拒绝；digest 缓存非完整性保证；identity 绑定 zvec/collection。

2026-09-22 第 1 期合同闭合：

7. `model` / `dimension` / `execution_mode` 第 1 期是 §5.1 写死常量，不进 TOML、不从 package 探测。
8. `identity.lock` 用 `fcntl.flock`，不用 O_EXCL 锁文件。
9. 画面库就绪看 `/api/assets/libraries`，不看 `visualReachable`。
10. doctor 与 identity-pin 都全量重算 package manifest；Web/搜索缓存不是完整性保证。
11. 第 1 期 base_url 只接受 `127.0.0.1`。
12. HTTP 合同只在 §4.3 定义，钉在 commit `c2a9d92`。

2026-09-22 续审（v1.11，v1.10「已闭合」作废后补上）：

13. identity 绑定完整有效 profile（`embedding_profile_digest`），不只 model/dimension/execution_mode。
14. zvec 空库按 §6.4 表判定；status 的 mkdir 不是「已有数据」。
15. library root 必须 realpath 且不是 symlink；模型包内 symlink 拒绝。
16. HTTP 合同钉 `c2a9d92` 的字段形状。`contract_revision` 不是运行中 bundle 的证明。

2026-09-22 续审（v1.12）：

17. identity-pin 先 flock，锁内重查。最终文件用同目录临时文件 fsync 后 `os.link` 发布，禁止对最终路径先打开再写，禁止 `rename` 覆盖。
18. `/api/config` 是磁盘校验，不是 helper 已加载证明。改配置后必须手工重启 sidecar 再 pin。已加载子集只看 `/api/embedding-backend` 里实际返回的字段。
19. realpath / 非 symlink 是公共 preflight：init、pin、doctor、搜索，以及第 3 期 scan 前。不只 init 一次。
20. 骨架 JSON 含 `video` 与 `privateANE`。`/api/status` 建空目录是 Indexed 副作用，不算抖库写盘。

2026-09-22 续审（v1.13）：

21. 磁盘配置与已加载参数分开。第 1 期接受：helper 用旧 `privateANE` 启动后，磁盘改回标准值，HTTP 发现不了。
22. `spaceId` 在 `profiles[activeProfile]`，不在 `embedding`。`library` 在顶层。
23. `embedding_profile_digest` 只含常量表加 `embedding_space`。路径和 library 直接比较，不进 digest。固定样例 hash 见 §6.4。

2026-09-22 续审（v1.14）：

24. 非法命中丢弃。`unmapped` 只留给 root 内的合法 `original.*` / 编号图且数据库没有条目。
25. 第 1 期允许「已 pin、尚未建库」。识别 zvec 被单独删除是第 3 期前置，不是第 1 期行为。

## 15. 如何开工

第 1 期：用户说「开工」后按 §16 TDD。范围=init、identity-pin、映射、HTTP 合同、doctor 只读。不 scan、不 outbox、不托管、不改抖库主配置。

## 16. TDD 执行规范（1.1 审核结论）

本仓库已有约定：默认测试不访问网络、不读 Cookie；HTTP 用 `httpx.MockTransport`（见 `tests/test_share.py`）；Web 用 `TestClient` + 临时 Vault（见 `tests/test_web.py`）；真机用 `pytest.mark.live`（见 `tests/test_live_smoke.py`）。画面检索必须沿用，不得另起一套「连上本机 18767 才算测过」。

### 16.1 铁律

```text
没有先失败的测试，就没有生产代码。
```

每一条行为：先写一个测试 → 跑到失败（缺功能，不是 typo）→ 写刚好能过的代码 → 再跑该测试与全量 `uv run pytest`。重构只在绿之后，且不添加行为。

禁止：

- 先写 `adapters/indexed.py` / `visual_index.py` 再补测试。
- 一次写完 `test_visual_index.py` 里所有用例再实现（横切）。那会在接口未定前锁死测试，正是 TDD 技能里的 horizontal slice。
- 测 mock 自己而不是测抖库行为（例如 assert `transport` 被调用了 3 次却不 assert 返回给 UI 的结构）。
- 默认测试套件启动 Indexed、加载 WeMM、扫描用户 Vault。
- 用现有 `search.py` 顶栏搜索冒充画面检索测试（会立刻绿，证明测错了对象）。

允许 mock 的唯一理由：Indexed 是外部进程。客户端必须可注入 `httpx.MockTransport`（或等价 transport），与 `DouyinShareResolver(transport=...)` 同一手法。path 映射、开关、root 校验是纯函数，用 `tmp_path` 真文件系统，不 mock。

配置文件本身可后写；`VisualSearchSettings` 的默认值与 `enabled=false` 的行为必须先有失败测试。

### 16.2 竖切顺序（一次一条行为）

第 1 期按下列顺序 RED→GREEN，不要跳号一次做完：

1. `test_visual_search_defaults_to_disabled`  
   新配置缺省 `enabled is False`，`base_url` 为回环 18767。
2. `test_disabled_visual_index_does_not_call_indexed`  
   `notify_media_ready` 在关闭时零 HTTP。
3. `test_original_video_layout_maps_work_id`  
   仅 `<root>/<work_id>/original.mp4` 映射成功。
4. `test_creator_original_video_layout_maps_work_id`  
   `creators/<name>/raw/assets/<work_id>/original.mp4`。
5. `test_numbered_image_layout_maps_index`  
   `<root>/<work_id>/001.webp` → index 1。
6. `test_frames_cover_and_json_are_not_mapped`  
   frames/cover/info.json 拒绝。
6b. `test_video_kind_inventory_excludes_frames_and_cover`  
   临时目录同时放 original.mp4、frames/0001.jpg、cover.jpg；kinds=video 的计划扫描集合只有视频。
6c. `test_non_loopback_base_url_is_treated_as_disabled`
6d. `test_localhost_base_url_is_treated_as_disabled`
7. `test_unmapped_hit_keeps_label_without_path`  
   合法 `original.mp4` 找不到条目时返回脱敏 label，不返回绝对路径。
7b. `test_illegal_hit_is_dropped`  
   `foo.mp4`、`frames/`、`cover.*`、root 外路径不进 `hits`，也不当 unmapped。
8. `test_search_uses_injected_http_transport`  
   MockTransport 返回夹具 JSON；适配器 POST 含 `query`/`kind`、`includeMissing: false`、`includeLegacy: false`，并把映射后的 entry 放进 hits。
8b. `test_web_and_legacy_hits_are_dropped`
9. `test_search_when_indexed_is_down_returns_offline_status`  
   连接失败不抛未捕获异常，返回可展示的离线结构。
10. `test_init_writes_indexed_config_under_config_dir`  
    写入 `{INDEXED_HOME}/config/config.json`，不是 home 根下 `config.json`。
11. `test_doctor_reports_visual_search_disabled`  
    未启用时 doctor 明确「未启用」，不是 Indexed 故障。
11b. `test_doctor_succeeds_when_indexed_binary_is_missing_and_feature_disabled`  
    PATH 无 `indexed`、未设置源码路径时，默认关闭下 doctor/Web 仍成功。
12. `test_vault_root_is_not_an_allowed_library`  
    登记策略拒绝 Vault 根与 `wiki/`。
13. `test_init_dry_run_lists_roots_and_kinds_without_scanning`
13b. `test_init_writes_root_kinds_and_autoscan_false_without_assets_add`
13c. `test_init_refuses_when_INDEXED_CONFIG_is_set`
13d. `test_init_refuses_when_INDEXED_DATA_DIR_is_set`
13e. `test_init_refuses_existing_remote_empty_model_without_reconfigure`
13f. `test_init_refuses_when_helper_path_drifted`
14. `test_model_info_fixture_records_embedding_space`
14b. `test_status_is_not_used_as_embedding_identity`
14c. `test_embedding_backend_fixture_reports_helper_state`
14d. `test_visual_reachable_is_not_asset_index_ready`
15. `test_foo_mp4_is_not_mapped_as_work_original`
16. `test_init_writes_complete_apple_native_profile`  
    断言写入的 JSON 含 `video` 与 `native.privateANE` 的常量表值，不只 model/dimension。
17. `test_init_refuses_when_sidecar_is_running`
18. `test_identity_pin_writes_file_when_zvec_empty`
18b. `test_identity_pin_refuses_when_sidecar_is_stopped`
18c. `test_concurrent_identity_pin_only_one_succeeds`
18d. `test_identity_pin_refuses_when_sidecar_home_mismatches`
18e. `test_identity_records_resolved_zvec_and_asset_index`
18f. `test_identity_pin_refuses_when_resolved_storage_path_outside_home`
18g. `test_identity_pin_refuses_when_asset_index_mismatches`
18h. `test_identity_pin_allows_empty_dir_created_without_manifest`
18i. `test_identity_pin_allows_empty_zvec_manifest`
18j. `test_identity_pin_refuses_lance_corrupt_manifest_and_unknown_files`
18k. `test_video_fps_drift_changes_profile_digest`
18l. `test_symlink_library_root_is_refused`
18m. `test_model_package_symlink_is_refused`
18n. `test_identity_pin_rechecks_inside_flock`
18o. `test_identity_rename_does_not_overwrite`
18p. `test_live_config_profile_mismatch_refuses_pin`
18q. `test_doctor_refuses_root_replaced_by_symlink`
18r. `test_config_space_id_is_on_profile_not_embedding`
18s. `test_identity_publish_links_complete_file`
18t. `test_profile_digest_matches_fixed_fixture`
19. `test_identity_mismatch_rejects_search`
20. `test_missing_identity_with_existing_vectors_fails_closed`
20b. `test_pinned_without_zvec_is_not_yet_built`
21. `test_search_hits_omit_absolute_paths`

第 1 期**不要**在 CI 里对样例 mp4 打真实 `scan`。§12 第 1 期「样例 mp4」改为：临时 Vault 里放 `original.mp4` 空文件/夹具字节，只测登记路径与 HTTP 合同；真扫标 `live`。

第 2 期竖切（各一条 RED→GREEN）：

1. `enabled=false` 时 HTML 不含「画面检索」导航。
2. `enabled=true` 且 Indexed 离线时，画面页有恢复说明，资料库 API 仍 200。
3. `POST /api/visual/search` 经 MockTransport 返回已映射的 entry 与命中时间文本。
4. 画面页 HTML 不含 `<video>`。
5. 顶栏知识搜索响应形状与现网测试一致（回归）。
6. 页面不含 Indexed Dashboard iframe、不含第二套模型设置。

第 3 期竖切：在 §9.2 批准前不要写。`test_scan_retries_are_bounded` 属于第 3 期。

### 16.3 夹具与真机

- 把 `POST /api/assets/search`、`GET /api/assets/model-info`、`GET /api/status`、`GET /api/embedding-backend`、`GET /api/assets/libraries`、`GET /api/config` 分六份夹具。禁止用 status 夹具冒充 embeddingSpace。config 夹具必须含顶层 `indexedHome` / `configPath` / `activeProfile`，以及 `profiles.default.resolvedStoragePath` 与 `profiles.default.storage.assetIndex`（后两项不在顶层）。embedding-backend 夹具的 `state` 必须是源码枚举值之一，不得写 `loading`。
- 真机 WeMM：`@pytest.mark.live` 且 `DOUYIN_WIKI_RUN_LIVE_INDEXED=1`。live 必须用临时 `INDEXED_HOME`，禁止写用户 `~/.indexed/data/zvec`。
- 前端不单独上 Jest。行为通过 `TestClient` 断言 HTML/JSON。
- Recall@K / 延迟 / 显存不是默认 pytest 门禁。

### 16.4 完成定义（每一竖切）

- [ ] 测试名描述行为，不含糊的 `test_works`
- [ ] 亲眼看到该测试因功能缺失而失败
- [ ] 最少量生产代码后该测试转绿
- [ ] `uv run pytest` 全绿，无新增警告
- [ ] 未把用户 Vault 或本机 18767 写进默认测试

不能勾完就不是 TDD，生产代码作废重来。

## 17. Indexed 源码拆除（不是拆除抖库功能）

删除 `~/indexed` 之前必须已完成产品化期。届时：

1. 确认抖库 runtime 目录可独立 `serve`，doctor 指向它。
2. 停掉旧进程。
3. 删除 `/Users/weisengao/indexed` 与可选的 `~/.local/bin/indexed`。
4. **保留** 抖库画面检索模块、outbox、`{vault}/.douyin-wiki/indexed-home`、模型包。
5. `uv run pytest` 全绿；开启画面检索时不再需要 Indexed git。

未产品化就删 Indexed git，画面检索会停。那是操作顺序错误，不是产品设计。

不要用这份清单删除 `visual_index.py` 或侧栏入口。

## 修订记录

| 日期 | 版本 | 说明 |
| --- | --- | --- |
| 2026-09-20 | 1.0 | 初稿，待审核 |
| 2026-09-20 | 1.1 | TDD 审核：补 §16 竖切顺序、禁止横切测例、默认测试禁止真 WeMM；收紧第 1 期验收 |
| 2026-09-20 | 1.2 | 吸收外部审核：强制 rootKinds、专用 INDEXED_HOME 与 autoScan=false、最终路径 outbox、取消第一期视频 seek、init --dry-run、loopback URL、Pinned commit c2a9d92 |
| 2026-09-20 | 1.3 | 用户拍板 1–5；当时误写成「功能随 Indexed 拆除」 |
| 2026-09-20 | 1.4 | 更正产品目标：画面检索是抖库最终能力；Indexed 只是样板 |
| 2026-09-21 | 1.5 | 复审：写 INDEXED_HOME config；拆 status/model-info；第 3 期与产品化不批准 |
| 2026-09-21 | 1.6 | 完整 apple-native config、停机写盘、identity、检索 schema |
| 2026-09-21 | 1.7 | P0：写入 `{INDEXED_HOME}/config/config.json`；status 三分；第 1 期仅 init 写盘 |
| 2026-09-21 | 1.8 | P0：`visual-index identity-pin`；init 不改抖库配置；禁 INDEXED_CONFIG/DATA_DIR；manifest digest + 缓存；remote 空 profile 拒绝 merge |
| 2026-09-21 | 1.9 | home handshake（`/api/config`）；identity 与 zvec 分离；profile drift；digest 缓存非完整性保证；identity 绑定 zvec/collection |
| 2026-09-22 | 1.10 | 第 1 期合同闭合：§5.1 常量来源；flock；不看 visualReachable；doctor 全量 manifest；只接受 127.0.0.1；HTTP 合同单点 §4.3 |
| 2026-09-22 | 1.11 | 续审：完整 profile digest；zvec 空库表；realpath；合同钉 c2a9d92。v1.10「已闭合」作废 |
| 2026-09-22 | 1.12 | 锁内重查与 O_EXCL；live profile digest；事后 preflight；骨架补 video/privateANE。bundle 修订仍不可由 HTTP 证明 |
| 2026-09-22 | 1.13 | 磁盘校验≠已加载证明；spaceId 路径；link 发布 identity；digest 与路径比较拆开 |
| 2026-09-22 | 1.14 | 非法命中丢弃；unmapped 只留给合法路径无条目；第 1 期不识别单独删 zvec |
