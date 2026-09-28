# 抖库（douyin-wiki）整仓代码审查

- **审查角色**：审码
- **日期**：2026-09-28（Asia/Shanghai / CST）
- **机位**：用户 Mac `78c6c145-5c87-45ae-a4e0-35e6516c0d67`
- **项目根**：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- **范围**：当前工作区整树 + 相对 `origin/main` / `HEAD` 的近期变更；重点看变更热路径与系统性风险（`service*`、MCP、gateway、ASR/OCR、worker、auth/cookies、vault）
- **约束**：只读审查；未改代码、未跑测试、未 push

---

## 仓库状态（简要）

| 项 | 状态 |
|---|---|
| 分支 | `main` @ `5ff63fc` |
| 相对 `origin/main` | **超前 11 个已提交**（未 push）：CDN/Playwright 下载、oMLX、cookie/schema、CSRF/symlink、长视频可恢复分析等 |
| 工作区 | **大量未提交修改 + 未跟踪文件**（含 service 瘦身拆分、SenseVoice/RapidOCR、校正分片重写、文档与隔离测试残留） |
| 正式 LaunchAgent | `com.local.douyin-wiki.worker` / `web` / `maintenance` 仍指向本机 `.venv`；worker **无** `PYTHONUNBUFFERED` |

**已提交但未 push（摘要）**：`abae84b`…`5ff63fc`（媒体 CDN 绑定、cookie 优先 Playwright profile、本地写请求 Origin/Referer、vault 拒绝内部 symlink、长视频 analysis checkpoint 等）。

**未提交核心（摘要）**：

- `service.py` 由单体削到约 **611** 行，逻辑迁入 `service_{capture,analysis,import,entries,trash,maintenance}.py`（方法集合与 `HEAD` AST 核对：**121=121，无缺失**）
- 新增 `adapters/media_models.py`：`SenseVoice` + `FSMN-VAD` / `RapidOCR PP-OCRv6 small`，`SelectedTranscriber` / `SelectedOCR`
- `adapters/llm.py`：校正路径预分块、单段文本切分、局部 ID、有界 `ModelLimitError` 重试、更完整的 `finish_reason=length` 诊断
- `operation.py` / `jobs.js`：校正阶段与 `waiting_for_resource` 文案；媒体 provenance 展示
- `share.py`：解析 `modal_id`
- 根目录残留：`.test-backup-20260927/`、`.test-r1-20260927/`、`7686768044248452390/`、`docs/_mcp-sim-evidence-20260927/`（勿误提交）

依据：`docs/service-slim-execution.md`、`docs/mcp-dual-mode-sim-report.md`、`docs/mcp-gateway-formal-smoke.md` 与当前文件内容。

---

## Blocker

### B1. Provider 校正曾因 `model_output_limit` 失败；缓解已写但需以「提交边界」对待

**现象（已证实于 2026-09-27 仿真）**：短视频（~42s）在 provider 模式校正阶段失败，`error_code=model_output_limit`（见 `docs/mcp-dual-mode-sim-report.md`）。

**当前工作区缓解**（未提交，`adapters/llm.py`）：

- 校正前按 `max_chars` 预切文本块（约 `max_output_tokens//4`，封顶 1600）
- `_budgeted_chunks(..., max_text_chars=...)`
- 单块遇 `ModelLimitError` 时二分 chunk；单 piece 再 `split_text`；有界 `max_limit_splits`
- 请求侧使用局部 `0..n-1` ID；校验完整；checkpoint 含 `max_output_tokens` / thinking / scope
- `finish_reason=length` 附带 `content_chars` / `reasoning_chars` / usage

**隔离回归**（`docs/service-slim-execution.md`）：同片校正 5/5 子块成功，停在 `needs_review`（ASR 疑点），**尚未走完分析入库**。

**仍算 Blocker 的原因**：

1. 缓解与 service 瘦身、ASR/OCR 等捆在**未提交工作区**；正式 LaunchAgent 若未装上该树，线上仍是旧校正逻辑。
2. 当 piece 短于 `min_chars*2`（24）仍触发输出截断时，`run_chunk` 会原样重抛（`llm.py` 约 850–860 行），**无进一步降级**（关 thinking 已在正式配置 `enable_thinking=false`，但啰嗦模型/schema 仍可能打满 8192）。
3. 未在本审查中复跑 live；提交/发布前应再跑该 `modal_id` 全链路到 `completed`。

**建议**：把 R1 校正改动与 ASR/OCR、service 拆分**拆开提交**；发布前用正式 LaunchAgent worker 复测 provider 全链路；对不可再切的最小块考虑降级（缩小 OCR、临时抬 `max_tokens`、或明确失败文案指向调参）。

---

## Major

### M1. SenseVoice 常产出「少段、长段」；`analyze` 仍不能按文本切段

`SenseVoiceTranscriber` 按 FSMN-VAD span 出段（`media_models.py` 约 87–124 行），隔离样本整片仅 **1 段 / 242 字**。

校正路径已能对单段文本再切；**分析路径** `analyze()`（`llm.py` 约 931+）的 `ModelLimitError` 处理只按 **segment 列表二分**，单段不 fit 直接 `ModelContextError`（`_budgeted_chunks` 约 466–458 行），输出超限也不能像校正那样 `split_text`。

ASR 默认切到 SenseVoice（新装 `asr_provider=auto`）后，长独白/少 VAD 边界视频更容易在 **analysis** 而非 correction 爆限。

**建议**：分析侧复用校正的文本预分块，或强制 VAD/ASR 最大段长；至少对单段超预算做与 correction 对称的切分。

### M2. SenseVoice 无置信度 → 含数字/专有词即进人工复核

`detect_review_issues(..., unknown_confidence=True)`（`review.py` 约 28–45 行）在 `confidence`/`avg_logprob` 皆空且命中 `MATERIAL_PATTERN`（`\d|元|块|折|%|[A-Za-z]{2,}`）时建疑点。  
`service_capture.py` 在 `media_provenance.asr.provider == "sensevoice"` 时打开该开关（约 517–528 行）。

隔离回归已因此停在 `needs_review@0.66`（一条 ASR 疑点）。对口播价格/型号类内容可能**高频拦在复核门**，provider「全自动入库」体验变差。

**建议**：收紧规则（仅多位数/货币/连续英文）、抽样阈值、或 SenseVoice 侧尝试映射内部 score；文档写明默认会多复核。

### M3. Gateway 事件桥依赖 Agent 侧轮询；本仓未装自动唤醒

能力已具备：`list_job_events` / `acknowledge_job_event`（`mcp_server.py`、`service.py`、`database.py`）、`gateway monitor-events`（`cli.py`）、文档契约（`docs/gateway-agents.md`）。  
正式烟雾：LaunchAgent + 事件可读（`docs/mcp-gateway-formal-smoke.md`）。

执行记录明确：**未安装 Hermes/OpenClaw 专用调度，真实自动唤醒未验收**（`docs/service-slim-execution.md` R3）。无 Agent cron/桥时，任务会停在 `awaiting_agent_analysis`。

**建议**：产品层把「需自建 monitor-events 轮询」标成 Gateway 必装步骤；或提供一键 LaunchAgent 模板。

### M4. Worker 日志无 `PYTHONUNBUFFERED`，异常退出时易「假静默」

仿真中 sidecar `--forever` 曾静默退出、日志 0 字节，任务长期 `queued`（`mcp-dual-mode-sim-report.md`）。  
正式 plist 将 stdout/stderr 重定向到文件，但 **EnvironmentVariables 无 `PYTHONUNBUFFERED=1`**，Python 块缓冲下崩溃/早退可能长时间看不到输出。

**建议**：LaunchAgent 增加 `PYTHONUNBUFFERED=1`（或 `python -u`）；Web/CLI 对长时间 `queued` 提示检查 worker heartbeat。

### M5. `trust_remote_code=True` 加载 SenseVoice

`media_models.py` 约 61–66 行对 FunASR `AutoModel` 开启 `trust_remote_code=True`。模型来自 ModelScope 远端代码执行面，属供应链风险。

**建议**：钉版本/校验哈希、优先无 remote code 的加载方式、文档警告仅信任固定 revision。

### M6. 工作区「瘦身 + 多特性」未分提交，回归与回滚成本高

`docs/service-slim-execution.md` 已说明：为避免与既有未提交 ASR/OCR 等混提，**未做**计划中的切片提交。当前一次 `git add` 会混入：

- service 六模块拆分
- ASR/OCR extras 与 selector
- LLM 校正大改
- 文档/测试残留目录

方法集合已对齐，但审查/bisect/回滚困难；根目录测试残留若被加入版本库会污染仓库。

**建议**：按「ASR/OCR → 校正 R1 → UX R2 → service 拆分 → 文档」分提交；`.gitignore` 或清理 `.test-*`、`docs/_mcp-sim-evidence-*`、数字目录实验笔记。

### M7. 本地 CSRF 仍允许「无 Origin/Referer」的写请求

`webapp/app.py` `_local_write_origin_ok`（约 404–426 行）：有 Origin/Referer 则必须匹配 `Host`；**两者皆无则放行**（方便 curl）。绑定 localhost 时风险有限，但任意能打到本机端口的进程可写操作 API。

**建议**：维持现状则在文档标明「本机信任边界」；若 Web 有会话 cookie，考虑双重提交 token。

---

## Nit

### N1. 从 `review` 导入私有 `_deduplicate_issues`

`service_capture.py` / `service_analysis.py` 依赖 `review._deduplicate_issues`。可改为公开 `deduplicate_review_issues`。

### N2. `SelectedTranscriber` / `SelectedOCR` 在构造时即实例化双后端

即使配置为 `whisper`/`vision`，仍构造 SenseVoice/RapidOCR 对象（懒加载模型，可接受）。可按 provider 延迟构造，减噪声。

### N3. RapidOCR 与 VisionOCR 均把帧元组第一元写入 `timestamp_ms`

与 `extract_frames` 返回的毫秒时间戳一致（沿用旧约定），命名易误解；可在类型/注释标明「第一元即 timestamp_ms」。

### N4. `media_models` → `media` 重依赖

`media_models.py` import `VisionOCR`/`WhisperTranscriber` 会拉起整份 `media.py`。长期可把 Whisper/Vision 抽到更小模块以免循环与加载变重。

### N5. 配置升级与默认值分叉合理但需文档

`load_config` 对已有文件 `setdefault(asr_provider=whisper, ocr_provider=vision)`（`config.py` 约 190–194 行）；**新** `AppConfig`/`MediaSettings` 默认 `auto`。行为正确，README/CHANGELOG 需写清，避免「重装变 SenseVoice、升级仍 Whisper」的困惑。

### N6. MCP/CLI 状态中英混用

仿真已记：展示层中文 label、内部英文 status。Agent 轮询应用 `state` 字段而非 `state_label`（文档可加粗）。

### N7. 正式 worker plist 未暴露 ASR/OCR extra 安装状态

`pyproject.toml` 新增 optional `asr` / `ocr`。`auto` 回退 Whisper/Vision 可用，但用户以为已上 SenseVoice 时可能静默回退（有 `fallback_reason` provenance，Web 已展示——好）。doctor 可显式检查 extra。

---

## 热路径速览（做得好的地方）

1. **CDN 绑定**：`expected_work_id` + anchor/query media id 冲突惩罚、无 anchor 不轻信页面 CDN、静音 CDN 回退 yt-dlp（`media.py` 未提交 diff）——对准错片/广告轨。
2. **Cookie**：优先可用的 Playwright profile；有未过期 cookie 时不把 yt-dlp 失败误判成要重新登录。
3. **Vault**：写/扫描拒绝 vault 内 symlink，与删除策略对齐（`vault.py` `_reject_vault_symlinks`）。
4. **长视频分析**：checkpoint / 可恢复、证据校验（已提交 `5ff63fc`）。
5. **校正 UX**：进入校正前切到 `ANALYZING` + `waiting_for_resource`；Web 终态不再误显示进行中校正（`operation.py` / `jobs.js`）。
6. **Service 瘦身**：方法无丢失；职责文件边界清晰，利于后续评审。
7. **Secrets**：Keychain + `hmac.compare_digest` 回读校验（`secrets.py`）。

---

## 风险矩阵（提交/发布优先级）

| 优先级 | ID | 主题 | 状态 |
|---|---|---|---|
| P0 | B1 | Provider `model_output_limit` / 最小块仍可能失败 | 工作区已大幅缓解；需分提交 + live 复验全链路 |
| P1 | M1 | SenseVoice 长单段 × analyze 无文本切分 | 未修 |
| P1 | M2 | 无置信度 → 复核洪泛 | 行为有意；产品需校准 |
| P1 | M3 | Gateway 无自动事件桥 | 契约有、安装无 |
| P1 | M4 | Worker 日志缓冲 | 未修 |
| P1 | M5 | `trust_remote_code` | 未修 |
| P1 | M6 | 未分提交 / 测试残留 | 流程 |
| P2 | M7 / N* | CSRF 本机信任、API 整洁度、文档 | 可迭代 |

---

## 结论

当前树相对 `origin/main` 是两层变化：**11 个已提交媒体/安全/可恢复分析修复**，外加**更大块未提交的 ASR/OCR + 校正韧性 + service 拆分**。  
已知仿真 blocker（短视频 `model_output_limit`）在工作区校正路径上已有实质修复并有隔离成功证据，但 **analyze 与 SenseVoice 长段组合、复核门、Gateway 自动唤醒、worker 日志、信任远端代码** 仍是发布前应处理的 Major。  
**不要**把 `.test-*` 与证据目录打进业务提交；建议按主题拆 commit 后再考虑 push。

