# 抖库 MCP 双模式仿真测试报告

- **测试时间**：2026-09-27 18:55–19:11（CST / Asia/Shanghai）
- **测试视频**：`modal_id=7689429622975314067` → 规范化为 `https://www.douyin.com/video/7689429622975314067`
- **实测时长**：约 **41.9 秒**（metadata `duration_seconds=41.888`）
- **机位**：本机 Mac（machineId `78c6c145-5c87-45ae-a4e0-35e6516c0d67`）
- **项目根**：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- **报告路径**：`docs/mcp-dual-mode-sim-report.md`
- **证据副本**：`docs/_mcp-sim-evidence-20260927/`

---

## 1. 环境快照（测试前）

| 项 | 值 |
|---|---|
| 正式配置 | `~/Library/Application Support/douyin-wiki/config.toml` |
| `analysis_mode` | **`provider`**（已是本机 LLM 模式） |
| Vault | `/Users/weisengao/Documents/Obsidian/抖库` |
| DB | `…/抖库/.douyin-wiki/state.sqlite3` |
| LLM | `http://127.0.0.1:8000/v1` / `Qwen3.6-35B-A3B-4bit` / `response_format=json_schema` / `max_output_tokens=8192` |
| ASR/OCR | `auto` / `auto`（实测落成 SenseVoice + RapidOCR） |
| Cookie/Profile | 专用 Playwright profile 可用；`auth status` 视频 Cookie 未过期 |
| 常驻进程 | LaunchAgent `worker`(pid 39876)、`web`(8765)、若干 `douyin-wiki-mcp` |
| oMLX | `/v1/models` 需 Keychain 中 `DOUYIN_WIKI_OMLX_API_KEY`（长度 12）才返回模型列表 |

**备份**：`.test-backup-20260927/config.toml`（与正式配置一致）。  
**隔离策略**：全程使用独立测试 Vault + 独立 `--config-path`，**未改写**正式 `config.toml`，也未污染正式 Obsidian 库。

测试配置要点：

- Provider：`.test-sim-20260927/config-provider.toml`（`analysis_mode=provider`，vault→测试库）
- Gateway：`.test-sim-20260927/config-gateway.toml`（`analysis_mode=gateway`，同一测试库）

---

## 2. 方法说明（真实 MCP vs 等价路径）

本 Agent **未**挂接 `douyin-wiki-mcp` 连接器，因此采用：

> **MCP 等价路径**：直接调用 `DouyinWikiService` 中与 `src/douyin_wiki/mcp_server.py` 工具相同的方法（`capture_douyin` / `get_job` / `get_analysis_context` / `submit_transcript_correction` / `submit_gateway_analysis` / `resolve_review`），以及官方 CLI `douyin-wiki gateway submit-correction|submit-analysis`（内部同样走 Service）。

MCP 入口定义见：

- 模块：`src/douyin_wiki/mcp_server.py`（`douyin-wiki-mcp` → `mcp_server:main`，stdio）
- 关键采集/分析工具：`capture_douyin`、`get_job`、`get_analysis_context`、`submit_transcript_correction`、`submit_gateway_analysis`、`resolve_review`、`list_job_events`、`acknowledge_job_event`

Worker：对测试库使用 `douyin-wiki worker run --config-path <test-config>`；正式 LaunchAgent worker **保持运行且未改配置**。

---

## 3. Mode A — MCP + 本机 LLM（`analysis_mode=provider`）

### 3.1 时间线

| 本地时间 (CST) | 事件 |
|---|---|
| 18:56:42 | MCP 等价 `capture` 提交；job `82b2e1d9ba044932a3c31b48b5addb8f` → `queued` |
| 18:56:59–19:01:05 | **一直停在 `queued`**（~246s）；判定 **MEDIA_STALL**（短视频却无媒体进展） |
| 根因 | 首次 `worker --forever`（无 `PYTHONUNBUFFERED`）**静默退出**，日志文件 0 字节，任务无人认领 |
| 19:01:11 | 改用 `worker run --once` 后立刻认领；下载/ASR/OCR 很快完成 |
| ~19:01:25–31 | SenseVoice + RapidOCR 加载并完成（worker-once 日志） |
| 19:01:43 | `POST http://127.0.0.1:8000/v1/chat/completions` → **HTTP 200** |
| 19:03:01–19:05:10 | 状态长期显示 `transcribing@0.62`（heartbeat），实际已在 LLM **correction** 阶段（`analysis_progress.phase=correction`） |
| 19:05:10 | **`failed` / `model_output_limit` /「模型输出达到 token 上限」** |

证据：

- `docs/_mcp-sim-evidence-20260927/mode-a.log`
- `docs/_mcp-sim-evidence-20260927/mode-a-summary.json`
- `docs/_mcp-sim-evidence-20260927/mode-a-final.json`（artifacts 含 `transcript_raw`/`ocr`/`video_path`，无 entry）

媒体链路：**成功**（SenseVoice + RapidOCR，视频 ~42s）。  
分析链路：**失败**于本机 Qwen `max_output_tokens=8192` 截断（`adapters/llm.py` 抛 `ModelOutputError` / `model_output_limit`）。

### 3.2 Mode A 发现

1. **短视频媒体本身很快**；真正拖垮体验的是「worker 没起来还显示 queued」。
2. Provider 模式下状态机在 LLM 阶段仍可能长时间停在 `transcribing@0.62`，与真实阶段（correction）不一致 → 监控/UX 误导。
3. `model_output_limit` 在短视频上仍可复现（校正 JSON 过大或模型啰嗦），需抬高 `max_output_tokens`、压缩 prompt，或对截断做重试/降级。

---

## 4. Mode B — MCP + Gateway（`analysis_mode=gateway`）

### 4.1 时间线

| 本地时间 (CST) | 事件 |
|---|---|
| 19:05:46 | 同 URL 再次 capture；job `b22c3a306c934dbd86ced0bfa46eb6ad`，带 `gateway_context={gateway:sim-test,…}` |
| 19:05:51 | `downloading` |
| 19:05:59 | `transcribing` |
| 19:06:15 | **`awaiting_agent_analysis@0.62`**（媒体结束，约 **24s**） |
| 19:06+ | `get_analysis_context` 成功：`phase=transcript_correction`，含 `transcript_raw`/`ocr`/`analysis_schema`/`gateway_context` |
| 首次 `submit_transcript_correction`（手写等价脚本） | **失败**：向 Service 传入了 `dict` 而非 `TranscriptCorrection`（MCP 层会 `model_validate`，裸 Service 不会） |
| 首次 `submit_gateway_analysis` | 正确拒绝：`JobStateError: 请先提交逐字稿校正` |
| 19:10 左右 | 改用官方 CLI `gateway submit-correction` → 成功（仍「待 AI 处理」，`phase→analysis`，progress 0.68）；未进入 `needs_review` |
| 随后 | CLI `gateway submit-analysis` 提交最小合法 stub → 任务 **requeue** 为 `queued@0.68` |
| 19:10:26–19:10:36 | `analyzing` → **`completed`**；写入 entry `dy-7689429622975314067` |

证据：`docs/_mcp-sim-evidence-20260927/mode-b.log`、`mode-b-final.json`、`mode-b-capture.json`。

### 4.2 Mode B 发现

1. Gateway 停点行为符合设计：媒体后进入 `awaiting_agent_analysis`，`result.next_tool=get_analysis_context`。
2. Agent 闭环 **可在无真实 OpenClaw/Hermes 的情况下** 用 MCP 等价/CLI 完成：校正 →（可选复核）→ 分析提交 → worker 确定性入库。
3. 文档 (`docs/gateway-agents.md`) 与代码一致要求先 `submit_transcript_correction`；等价调用若跳过 MCP 的 pydantic 包装会踩坑。
4. `analysis_schema` 对 Agent 不友好处：`RecommendationCard.criteria` 必须是 **list**；`OtherCard` 字段是 `notes` 而非直觉上的 `summary`。证据校验会拒绝无法回溯的 claims/atoms——stub 需清空这些字段。
5. CLI/`get_job` 对 Agent 返回 **中文 label**（如「待 AI 处理」「已完成」），内部状态码在 DB 仍是英文；混用轮询时容易写错判断。

---

## 5. 对比与污染

| 维度 | Mode A (provider) | Mode B (gateway) |
|---|---|---|
| 媒体 | 成功（worker 起来后秒级～数十秒） | 成功（~24s 到 awaiting） |
| 分析 | 本机 LLM **失败** `model_output_limit` | Agent stub **成功入库** |
| 终态 | `failed`，无 entry | `completed`，entry `dy-7689429622975314067` |
| 双次采集 | 同一 `video_id` 两 job；A 失败无库，B 成功写库；资产路径按 video_id 复用，未见正式库污染 | 同左 |

正式 Vault 查询该 `video_id`：**无**对应 entries/jobs（测试仅写隔离库）。

---

## 6. 问题分级

### Blocker（阻塞主路径）

1. **Provider：本机 LLM 校正阶段 `model_output_limit`**  
   - 证据：job `82b2e1d9…` / `error_code=model_output_limit`  
   - 影响：MCP+本机 LLM 主路径无法闭环  
   - 建议：提高 `max_output_tokens`、收紧 correction schema/prompt、截断重试；或短视频走单次 JSON。

### Major（重大）

2. **测试/侧车 `worker --forever` 易静默失败**（无日志、任务永久 `queued`）  
   - 短视频 >4min 仍 `queued` 即应视为故障。  
   - 建议：Launch/文档强制 `PYTHONUNBUFFERED=1`；启动后心跳/自检；queued 超时告警。

3. **Provider 进度状态滞后**（已进入 LLM correction 仍显示 `transcribing@0.62`）  
   - 监控与 Agent 轮询易误判。

4. **MCP 工具层 vs Service 直调的类型契约**  
   - MCP 会 `TranscriptCorrection.model_validate`；直接调 Service 传 dict 会 `AttributeError`。  
   - 对「等价路径/脚本集成」文档需写明必须构造模型对象，或 Service 接受 dict。

### Nit（小问题）

5. `get_analysis_context` / CLI 中英混用（label vs 内部码）增加 Agent 解析成本。  
6. Schema 字段命名反直觉（`criteria: list`、`OtherCard.notes`）。  
7. `funasr` 日志噪声：`Loading remote code failed: model, No module named 'model'`（仍成功跑完）。  
8. 手写 stub 时 `RecommendationCard` 易踩类型错误（已用 `other` 卡绕过）。

---

## 7. 清理与配置恢复确认

| 动作 | 结果 |
|---|---|
| 停止测试 worker（provider/gateway/`--once`） | 已停止 |
| 删除隔离环境 `.test-sim-20260927/`（含 2 jobs、1 entry、~4.1MB 媒体） | **已删除** |
| 正式 `config.toml` | **全程未改动**；与 `.test-backup-20260927/config.toml` diff 一致 |
| 正式 Vault / 该 video_id | **无测试残留** |
| 正式 LaunchAgent worker (39876) | **仍在运行** |
| 备份保留 | `.test-backup-20260927/config.toml`（验证后可手动删） |
| 证据保留 | `docs/_mcp-sim-evidence-20260927/`（日志与 job JSON） |
| git | **未 commit / 未 push** |

---

## 8. 需用户侧关注的事项

1. **Provider 模式当前无法稳定分析该片**：oMLX/Qwen 触发 `model_output_limit`（非 Cookie、非下载问题）。若要坚持本机 LLM，请调高 `max_output_tokens` 或换模型/关 thinking 相关开销后重试。  
2. Cookie/下载链路本次 **正常**；无需重新 `auth`。  
3. oMLX 服务在线且 Keychain 密钥可用。  
4. 若要用真实 Gateway Agent（OpenClaw/Hermes）替代 stub：工具序列已验证可行——`capture_douyin` → 等到「待 AI 处理」→ `get_analysis_context` → `submit_transcript_correction` → `submit_gateway_analysis`。

---

## 9. 成功标准对照

- [x] 两模式均真实跑过，有 job/日志/文件证据  
- [x] 原设置恢复并校验（实际未改正式配置）  
- [x] 测试数据已删除（隔离库整目录移除）  
- [x] 中文报告落盘本路径  
