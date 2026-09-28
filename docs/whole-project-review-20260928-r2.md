# 抖库（douyin-wiki）整仓复审（R2）

- **审查角色**：审码
- **日期**：2026-09-28（Asia/Shanghai / CST）
- **机位**：用户 Mac `78c6c145-5c87-45ae-a4e0-35e6516c0d67`
- **项目根**：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- **对照**：[`docs/whole-project-review-20260928.md`](whole-project-review-20260928.md)（R1）中的 Blocker/Major：B1、M1–M7
- **范围**：当前工作区整树 + `git status` / `diff` / `log`；重点复核 R1 项是否关闭，并扫描新风险
- **约束**：只读审查；未改业务代码、未跑测试、未 push（本文件为审查交付物）

---

## 仓库状态（相对 R1）

| 项 | 状态 |
|---|---|
| 分支 | `main` @ `5ff63fc`（与 R1 相同） |
| 相对 `origin/main` | 仍超前 **11** 个已提交、未 push |
| 工作区 | 仍大量未提交修改 + 未跟踪文件；相对 R1 又叠了 **M1 analyze 预分块、M2 无置信度复核策略、M4 `PYTHONUNBUFFERED` 安装器、文档/测试** |
| 包加载 | `.venv` 指向 `src/douyin_wiki/...`（可编辑安装）；正式 worker **已加载**工作区 `llm.py` 中的校正/分析缓解 |
| 正式 LaunchAgent | worker/web/maintenance 仍在；**已装** worker plist **仍无** `PYTHONUNBUFFERED`（安装器源码已改，需重装才写入） |

**未提交核心增量（相对 R1 报告时）**：

- `adapters/llm.py`：`analyze()` 对称校正的文本预切 / `split_text` / 有界 `ModelLimitError` 缩小（约 931–1126 行）
- `review.py`：缺置信度不再单独建疑点；新增 `transcript_confidence_info`
- `setup.py`：worker/maintenance 安装环境加 `PYTHONUNBUFFERED=1`
- `docs/gateway-agents.md`：事件桥安装清单与伪代码
- `tests/test_transcript_quality.py`、`test_service.py`（长段 analyze）、`test_interfaces.py`（plist 断言）等
- 执行记录：`docs/service-slim-execution.md` 已记 M1/M4 与置信度修复（文档自称离线 **379 passed**；本轮未复跑）

方法集合：HEAD `DouyinWikiService` **121** 方法 ↔ 工作区 mixin 并集 **121**，名称无增无缺。

---

## R1 项逐条复核

### B1. Provider `model_output_limit` / 校正最小块 — **部分关闭（技术主因已缓解；发布残留）**

| 维度 | 结论 | 证据 |
|---|---|---|
| 校正预切 + 超限二分/`split_text` | **已修（工作区）** | `llm.py` 726–778、815–873：`max_chars`、`_budgeted_chunks(..., max_text_chars=...)`、`run_chunk` 有界缩小 |
| 隔离校正成功 | **仍成立** | `docs/service-slim-execution.md`：同片校正 5/5 子块 |
| 正式进程是否吃到代码 | **是**（可编辑安装） | `.venv` 加载 `src/.../adapters/llm.py`，内含 `max_text_chars` |
| 最小块仍超限 | **仍开** | `llm.py` 851–860：`split_text` 为 `None` 时原样重抛，无关 thinking / 抬 token / 缩 OCR 等降级 |
| 全链路到 `completed` | **仍未验收** | 隔离 job 曾停在 `needs_review`（旧 ASR 疑点）；M2 修好后**未**再跑「resolve → analyze → 入库」live |

**判定**：不再是「短视频校正必炸」的硬 Blocker；降为 **发布前 Major 残留**——最小块路径与 live 全链路仍缺闭环。建议：分提交后用正式 worker 跑通 provider 至 `completed`；最小块失败文案指向调参。

---

### M1. SenseVoice 长单段 × `analyze` 无文本切分 — **已关闭（工作区）**

R1 时 `analyze()` 只按 segment 列表二分，单段不 fit 直接 `ModelContextError`。

**现况**：

- 预切：`llm.py` 952–1021（`max_text_chars`、`split_text`、`add_piece`）
- 超限：`llm.py` 1058–1119（列表二分 + 单段 `split_text`，`max_limit_splits=256`）
- 粗粒度时间：`source_segment_id` + 共用原段 `start_ms`/`end_ms`；提示禁止按字推细时戳（945–950）
- 测试：`tests/test_service.py` `test_analysis_presplits_one_long_segment_and_preserves_coarse_citations`（约 1639–1703）
- 隔离：同片 1 段/242 字真实 `analyze` 约 43s 过证据校验（`service-slim-execution.md`）；该短输入未触发预切

**残留（不回升 Major）**：与校正相同的最小块失败；多分片 merge 在「两份 partial 仍超预算」时 fail-closed（`llm.py` 1212–1215）——有意，长独白+高分片数时需留意。

---

### M2. 无置信度 → 含数字即进复核 — **已关闭（工作区）**

| 点 | 证据 |
|---|---|
| 规则 | `review.py` 21–42：`unknown_confidence` **形参保留但函数体未使用**；仅 `low_confidence`（真实分数）×（材料词或极低分）建疑点 |
| 说明 | `transcript_confidence_info`（61–83）：缺分 → `confidence_status=unavailable`，文案明确不单独触发复核 |
| 调用 | `service_capture.py` 395、531：写入 provenance + `detect_review_issues(transcript_raw)`（不再传制造洪泛的开关） |
| 测试 | `tests/test_transcript_quality.py`；`test_media_models.py` `test_unknown_sensevoice_confidence_is_not_a_review_reason` |
| 文档 | README / `gateway-agents.md` / CHANGELOG 已写清 |

历史隔离 job 仍带旧疑点（有意不自动清除）。**新任务**不应再因「SenseVoice 无分 + 数字」 alone 卡在 `needs_review`。

---

### M3. Gateway 事件桥无自动唤醒 — **部分缓解（文档）；产品缺口仍开**

- 能力仍在：`list_job_events` / `acknowledge_job_event` / `gateway monitor-events`
- **新增**：`docs/gateway-agents.md` 明确「写入事件不启动 Agent」、安装四步清单、伪代码、失败不先 ack、避免只靠 `after_event_id` 高水位
- **仍缺**：本仓仍无 Hermes/OpenClaw 专用调度或一键 LaunchAgent；真实自动唤醒**未验收**（执行记录 R3）

判定：**文档 Major → 产品 Major 仍开**（契约更清晰，安装仍靠用户/Agent）。

---

### M4. Worker 日志无 `PYTHONUNBUFFERED` — **部分关闭**

| 层 | 状态 | 证据 |
|---|---|---|
| 安装器源码 | **已修** | `setup.py` 547–551：`common_env` 含 `"PYTHONUNBUFFERED": "1"` |
| 单元测试 | **已有** | `tests/test_interfaces.py` `test_launch_agent_has_homebrew_path_and_unbuffered_logs` |
| 本机已装 plist | **未生效** | `~/Library/LaunchAgents/com.local.douyin-wiki.worker.plist` 的 `EnvironmentVariables` **无**该键 |
| Web 安装器 | **未对齐** | `WebLaunchAgentInstaller.install`（约 642–645）仍只有 `DOUYIN_WIKI_CONFIG`/`PATH` |

判定：**代码侧部分关闭**；需 `service install`（或等价重装）后才算运行时关闭。Web 侧不对称见新 Nit。

---

### M5. `trust_remote_code=True` — **仍开**

`adapters/media_models.py` 61–66：FunASR `AutoModel(..., trust_remote_code=True)`。无钉 revision/哈希、无文档强制警告升级路径。供应链面不变。

---

### M6. 未分提交 / 测试残留 — **仍开**

- 工作区仍混：service 六模块、ASR/OCR、校正 R1、analyze M1、置信度 M2、UX、文档、锁文件
- 未跟踪残留仍在：`.test-backup-20260927/`、`.test-r1-20260927/`（约 4.7M）、`7686768044248452390/`、`docs/_mcp-sim-evidence-20260927/`
- `.gitignore` 仅有 `.test-env/`，**未**覆盖上述目录
- 执行记录仍建议按主题拆 commit；本轮亦未提交

---

### M7. 本地 CSRF 允许无 Origin/Referer — **仍开（有意）**

`webapp/app.py` 403–424：有 Origin/Referer 则必须匹配 `Host`；**两者皆无则 `return True`**。`TrustedHostMiddleware` 限 `127.0.0.1`/`localhost`（约 375）。绑定本机时风险有限；任意能打本机端口的进程仍可写 API。无双重提交 token。

---

## 新发现（相对 R1）

### Major（新）

本轮扫描**未**发现高于既有仍开项的新 Blocker。媒体 CDN 绑定（`media_id` query、`bit_rate_audio` 优先于 `music.play_url`、锚定冲突罚分）属加固，见热路径。

### Nit（新）

#### N8. Web LaunchAgent 安装器未设 `PYTHONUNBUFFERED`

Worker/maintenance 已加（`setup.py` 550），Web 安装路径（642–645）遗漏。Web 同样把 stdout 重定向到文件，崩溃时同样可能「假静默」。

#### N9. `unknown_confidence` 死参数

`review.py` 22：关键字仍在，文档称兼容；函数体完全忽略。易误导后续调用方「再开也能洪泛」。可删参或实现为显式 no-op 注释 + 弃用。

#### N10. Analyze 递归缩小时外层进度可能停滞

`analyze` 进度按初始 `chunks` 下标前进（1128–1132）；`analyze_chunk` 内部再切不增加 `completed` 分子。长段连炸时 UI 可能长时间停在同一格（正确性无影响）。

---

## R1 Nit 简表

| ID | 主题 | R2 |
|---|---|---|
| N1 | `_deduplicate_issues` 私有导入 | **仍开**（`service_capture.py` 32、`service_analysis.py` 26） |
| N2 | Selector 构造即双后端 | **仍开**（`media_models.py` 206–241） |
| N3 | OCR `timestamp_ms=source_index` | **仍开**（约定未变；Vision 路径 `media.py` 1767） |
| N4 | `media_models`→`media` 重依赖 | **仍开** |
| N5 | 升级默认 whisper vs 新装 auto | **文档已补**（README）；行为仍分叉合理 |
| N6 | 状态中英混用 | **仍开**（文档已强调用 `state`） |
| N7 | doctor 未查 asr/ocr extra | **仍开** |

---

## 热路径速览（本轮仍成立 / 增强）

1. **CDN**：query `media_id` 进身份键；锚点同 path 异 media id → -1000；视频轨 `bit_rate_audio` 优先，避免配乐顶替口播（`media.py` 工作区 diff）。
2. **校正 + 分析**预算对称预切（工作区）。
3. **置信度语义**与 Web/MCP provenance 展示（`jobs.js` / `web_operation.py`）。
4. **Service 瘦身**方法 121=121。
5. **Gateway 文档**安装契约显著补强。

---

## 风险矩阵（R2）

| 优先级 | ID | 主题 | R2 状态 |
|---|---|---|---|
| P1 | B1 残留 | 最小块超限 + live 全链路未闭环 | 部分关闭；降出硬 Blocker |
| ~~P1~~ | ~~M1~~ | analyze 长单段 | **关闭** |
| ~~P1~~ | ~~M2~~ | 无置信度复核洪泛 | **关闭** |
| P1 | M3 | Gateway 自动唤醒未装 | 文档部分缓解；产品仍开 |
| P1 | M4 | PYTHONUNBUFFERED | 源码关闭；**已装 plist 未刷新** |
| P1 | M5 | `trust_remote_code` | 仍开 |
| P1 | M6 | 混提 / 测试残留 | 仍开 |
| P2 | M7 | CSRF 无头放行 | 仍开（本机信任） |
| P2 | N8–N10 / 旧 N* | Web unbuffered、死参、进度、API 整洁 | 可迭代 |

---

## 结论

用户称「改了，再次整体 review」后，工作区相对 R1 **实质性关闭了 M1、M2**，并 **部分关闭 B1（校正主路径）、M3（文档）、M4（安装器）**。  
仍开且影响发布/运维的是：**M5 供应链、M6 提交卫生、M4 需重装 plist、M3 事件桥未落地、B1 最小块与 live 全链路、M7 本机 CSRF 边界**。  

可编辑安装意味着正式 worker **已经在跑**含 M1/M2/校正缓解的源码；但 git 历史与已装 plist 尚未对齐这些修复。  
**不要**把 `.test-*` / 证据目录打进业务提交；建议按执行记录主题拆 commit，重装 LaunchAgent 后再做一条 provider 全链路冒烟至 `completed`。

