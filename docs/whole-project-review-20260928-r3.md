# 抖库（douyin-wiki）整仓复审（R3）

- **审查角色**：审码
- **日期**：2026-09-28（Asia/Shanghai / CST）
- **机位**：用户 Mac `78c6c145-5c87-45ae-a4e0-35e6516c0d67`
- **项目根**：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- **对照**：[`docs/whole-project-review-20260928-r2.md`](whole-project-review-20260928-r2.md)（R2）中仍开/部分项：B1、M3–M7、N8–N10
- **范围**：当前工作区整树 + `git status` / `diff` / `log`；逐条复核上述项，并扫描相对 R2 的新风险
- **约束**：只读审查；未改业务代码、未跑测试、未 push（本文件为审查交付物）

---

## 仓库状态（相对 R2）

| 项 | 状态 |
|---|---|
| 分支 | `main` @ `5ff63fc`（与 R1/R2 相同） |
| 相对 `origin/main` | 仍超前 **11** 个已提交、未 push |
| 工作区 | 仍大量未提交修改 + 未跟踪文件；相对 R2 又叠了 **M5 `trust_remote_code=False`、N8 Web unbuffered、M7 `Sec-Fetch-Site`、N10 分片进度分母、`.gitignore` 精确忽略、执行记录 R2 后续** |
| 方法集合 | 工作区 `DouyinWikiService` mixin 并集仍为 **121**（`service.py` 25 + `service_*.py` 96；名称与 HEAD 单体对齐） |
| 正式 LaunchAgent | worker/web/maintenance **仍在**；三份已装 plist 的 `EnvironmentVariables` **均无** `PYTHONUNBUFFERED`（安装器源码已齐，需重装） |
| 包加载 | 可编辑安装指向 `src/`；**不能**单独证明正在跑的 worker 已热加载本轮改动 |

**未提交核心增量（相对 R2 报告时，工作区仍相对 HEAD）**：

- `adapters/media_models.py`（未跟踪）：`trust_remote_code=False`；SenseVoice / RapidOCR / Selector
- `setup.py`：worker/maintenance **与 Web** 均写 `PYTHONUNBUFFERED=1`（约 547–550、642–645）
- `webapp/app.py`：`_local_write_origin_ok` 拒绝 `sec-fetch-site` ∈ `{cross-site, same-site}`（411–412）
- `adapters/llm.py`：`analyze_chunk` 超限拆分时 `total_chunks += 1` 并 `report_progress()`（1101–1102、1125–1126、1133–1134）
- `.gitignore`：精确忽略 `.test-backup/r1/r2`、`docs/_mcp-sim-evidence-20260927/`、`7686768044248452390/`
- `docs/service-slim-execution.md`：记录 R2 后续修复与隔离样本 `6f50cfaa…`（仍停在 `needs_review`，未宣称全链路 `completed`）
- 其余：ASR/OCR 配置与 provenance、service 六 mixin、CDN/校正/置信度等与 R2 所述同树

**提交卫生告警（抬升 M6）**：`service.py` 已改为导入六个 mixin，但 `src/douyin_wiki/service_*.py` 与 `adapters/media_models.py` **仍未跟踪**。若只 `git add -u` / 只提交已修改文件而不加入未跟踪模块，包会直接 `ImportError`。

---

## R2 开项逐条复核

### B1. Provider `model_output_limit` / 校正·分析最小块 — **仍部分关闭**

| 维度 | 结论 | 证据 |
|---|---|---|
| 校正/分析预切 + 有界缩小 | **仍在** | `llm.py` 729–778、815–873（校正）；952–1021、1064–1138（分析） |
| 最小块仍超限 | **仍开** | 校正 `llm.py` 851–860；分析 1109–1118：`split_text` 为 `None` 时原样重抛，无 thinking/抬 token/缩 OCR 等降级 |
| 离线覆盖 | **有** | `tests/test_service.py` 约 1568、1823 断言「最小文本块」失败 |
| 全链路到 `completed` | **仍未验收** | 执行记录：隔离 job `7572797d…` / `6f50cfaa…` 均停在 `needs_review`；未 resolve→analyze→入库 live |

**判定**：与 R2 相同——主路径缓解成立；发布前残留为最小块失败语义 + live 全链路缺口。不回升硬 Blocker。

---

### M3. Gateway 事件桥无自动唤醒 — **仍开（文档部分缓解不变）**

- 能力仍在：`list_job_events` / `acknowledge_job_event` / `gateway monitor-events`（`cli.py` 611+）
- 文档仍明确「写入事件不启动 Agent」、安装清单与伪代码（`docs/gateway-agents.md` 207–254）
- **仍缺**：本仓无 Hermes/OpenClaw 专用调度或一键 LaunchAgent；真实自动唤醒未验收（执行记录 R3）

**判定**：产品 Major 仍开；相对 R2 **无新关闭证据**。

---

### M4. Worker 日志无 `PYTHONUNBUFFERED` — **仍部分关闭**

| 层 | 状态 | 证据 |
|---|---|---|
| 安装器源码（worker/maintenance） | **已修** | `setup.py` 547–550：`common_env` 含 `"PYTHONUNBUFFERED": "1"` |
| Web 安装器 | **本轮已对齐**（原 N8） | `setup.py` 642–645 |
| 单元测试 | **已有** | `tests/test_interfaces.py` 287–300 |
| 本机已装 plist | **未生效** | worker / web / maintenance 的 `EnvironmentVariables` 仅有 `DOUYIN_WIKI_CONFIG`、`PATH`；**无** `PYTHONUNBUFFERED` |

**判定**：代码侧关闭面扩大到 Web；运行时仍须 `service install` / Web 重装后才算关闭。

---

### M5. `trust_remote_code=True` — **已关闭（工作区）**

| 点 | 证据 |
|---|---|
| FunASR 调用 | `media_models.py` 58–68：VAD/ASR 均为 `trust_remote_code=False`，并 `disable_update=True` |
| 测试 | `tests/test_media_models.py` 57：`assert all(call["trust_remote_code"] is False …)` |
| 文档 | README 写明不执行模型仓远端 Python；自定义仓依赖远端代码时显式模式报错 / `auto` 回退 Whisper |

**残留（降为 Nit，见 N11）**：权重仍按上游默认 revision，未钉提交哈希。供应链「远端代码执行」主因已关；「未钉版」另记。

---

### M6. 未分提交 / 测试残留 — **仍开（风险抬升）**

- 工作区仍混：service 六模块、ASR/OCR、校正、analyze、置信度、UX、CSRF、gitignore、锁文件、大量 docs
- **抬升**：`service.py`（已改）依赖的 `service_*.py` + `media_models.py` **未跟踪** → 不完整暂存即可毁掉可安装性
- 隔离目录仍在磁盘（约 4.7M + 4.5M 等）；`.gitignore` **已**精确忽略（见下），不再容易被误加
- 仍未按主题拆 commit；相对 `origin/main` 仍 +11 未 push

---

### M7. 本地 CSRF 允许无 Origin/Referer — **部分关闭（有意边界仍在）**

| 点 | 状态 | 证据 |
|---|---|---|
| Origin/Referer 匹配 Host | **仍在** | `app.py` 403–424 |
| 无 Origin/Referer 放行 | **仍在**（curl/本机 API） | 同函数末尾 `return True`；`tests/test_web.py` 1076–1078 |
| `Sec-Fetch-Site` | **本轮加固** | 411–412 拒绝 `cross-site` / `same-site`；测试 1048–1057 |
| TrustedHost | **仍限本机** | 约 374：`127.0.0.1` / `localhost` / `testserver` |
| 双重提交 token | **仍无** | — |

**判定**：浏览器侧「无头但带 Fetch Metadata」的跨站写已被挡；任意本机进程无头写 API 的信任边界与 R2 相同。标为部分关闭，不升 Blocker。

---

### N8. Web LaunchAgent 未设 `PYTHONUNBUFFERED` — **已关闭（源码）**

`WebLaunchAgentInstaller.install`（`setup.py` 642–645）已写入 `"PYTHONUNBUFFERED": "1"`；`test_interfaces.py` 300 有断言。  
运行时与 M4 相同：已装 `com.local.douyin-wiki.web.plist` 仍无该键，需重装。

---

### N9. `unknown_confidence` 死参数 — **仍开**

`review.py` 21–22：形参保留；函数体完全未使用（27–42 仅 `low_confidence` / `very_low_confidence`）。测试仍传 `unknown_confidence=True` 并期望空列表（兼容入口），易误导「再开也能洪泛」。

---

### N10. Analyze 递归缩小时进度停滞 — **部分关闭**

相对 R2「只按初始 chunks、内切不改进度」：

- 现：`analyze_chunk` 在列表二分 / 文本对切 / OCR 二分时均 `total_chunks += 1` 并 `report_progress()`（`llm.py` 1101–1102、1125–1126、1133–1134）
- `completed_chunks` 仍仅在叶子成功时 +1（1089–1090）
- Service 侧 `progress=max(current_progress, target)`（`service_capture.py` 472）防百分比倒退

**判定**：UI 分母会动，不再完全「钉死同一格」；长连炸时分子仍可能长时间不动。部分关闭。

---

## R1/R2 Nit 简表（本轮抽查）

| ID | 主题 | R3 |
|---|---|---|
| N1 | `_deduplicate_issues` 私有导入 | **部分缓解**：实现已迁入 `review.py` 10–17；`service_capture.py` 32、`service_analysis.py` 26 仍从 `review` 导入下划线名 |
| N2 | Selector 构造即双后端 | **仍开**（`media_models.py` `SelectedTranscriber`/`SelectedOCR` `__init__` 同时构造两侧） |
| N3 | OCR `timestamp_ms=source_index` | **仍开**（RapidOCR 路径 `media_models.py` 197–198 同约定） |
| N4 | `media_models`→`media` 重依赖 | **仍开** |
| N5 | 升级默认 whisper vs 新装 auto | **仍成立**（`config.py` load 时 `setdefault` whisper/vision） |
| N6 | 状态中英混用 | **仍开** |
| N7 | doctor 未查 asr/ocr extra | **仍开且更显眼**：`setup.py` 476–488 仍只看 mlx/whisper CLI 与 `swift`；未查 `funasr`/`rapidocr` |
| N8 | Web unbuffered | **关闭（源码）** |
| N9 | 死参 | **仍开** |
| N10 | 分析进度 | **部分关闭** |

---

## 新发现（相对 R2）

### Major（新）

未发现高于既有仍开项的新 Blocker。  
**但**：M6 因「瘦身 `service.py` + 未跟踪 mixin」形成**不完整提交即坏包**的实操陷阱，审查上视为 M6 抬升，不另开新 ID。

### Nit（新）

#### N11. ASR/VAD 权重未钉 revision

`trust_remote_code=False` 已关远端代码执行；README / 执行记录承认模型仍按上游默认 revision 拉取。自定义 `asr_model`/`vad_model` 或上游默默换权重时，可复现性与供应链完整性仍弱于钉 hash/revision。

#### N12. `doctor` 的 transcription/ocr 检查与新默认栈脱节

新装 `asr_provider=auto` / `ocr_provider=auto` 优先 SenseVoice + RapidOCR，但 `doctor()`（`setup.py` 476–488）仍以 Whisper/MLX 与 Swift/Vision 判定 `transcription`/`ocr` ok。可能在「只装了 asr/ocr extra、未装 whisper/swift」时误报不健康，或在「只装旧栈」时掩盖新 extra 缺失。与 N7 同源，单列便于排期。

---

## 热路径速览（本轮）

1. **CDN**：`media_id` 身份键、锚点冲突罚分、`bit_rate_audio` 优先等加固仍在工作区 `media.py`。
2. **校正 + 分析**预算对称预切；分析超限时进度分母可增。
3. **置信度**：缺分不单独建疑点；provenance / Web「媒体识别模型」展示仍在。
4. **SenseVoice**：`trust_remote_code=False` 已落地并有测试。
5. **CSRF**：Fetch Metadata 加固 + 本机无头放行契约不变。
6. **Service 瘦身**：方法 121=121，但 mixin **未入 git 索引**。

---

## 风险矩阵（R3）

| 优先级 | ID | 主题 | R3 状态 |
|---|---|---|---|
| P1 | B1 残留 | 最小块超限 + live 全链路未闭环 | 部分关闭（同 R2） |
| P1 | M3 | Gateway 自动唤醒未装 | 仍开 |
| P1 | M4 | PYTHONUNBUFFERED 运行时 | 源码（含 Web）关闭；**已装 plist 未刷新** |
| ~~P1~~ | ~~M5~~ | `trust_remote_code` | **关闭**；残留见 N11 |
| P1 | M6 | 混提 / 未跟踪 mixin / 测试残留 | **仍开（抬升）** |
| P2 | M7 | CSRF 无头放行 | 部分关闭（Sec-Fetch-Site） |
| ~~P2~~ | ~~N8~~ | Web unbuffered | **关闭（源码）** |
| P2 | N9–N10 / N11–N12 / 旧 N* | 死参、进度、revision、doctor、API 整洁 | 可迭代 |

---

## 结论摘要（供父 Agent）

### 自 R2 关闭

- **M5**（`trust_remote_code=False`，`media_models.py` 58–68 + 测试）
- **N8**（Web LaunchAgent 安装器 `PYTHONUNBUFFERED`，`setup.py` 642–645）

### 自 R2 仍开 / 仍部分

- **仍部分**：B1（最小块 + live `completed`）、M4（plist 未重装）、M7（无头放行）、N10（进度分子）
- **仍开**：M3（事件桥产品）、M6（提交卫生，且 mixin 未跟踪抬升）、N9（死参）
- **Nit 延续/新增**：N1 部分缓解；N2–N7 仍开；**N11** revision；**N12**/N7 doctor 与新 ASR/OCR 栈脱节

### 新发现

- 无新 Blocker
- **M6 抬升**：提交时必须同时纳入未跟踪的 `service_*.py` 与 `media_models.py`，否则坏包
- **N11**、**N12** 如上

**建议（审查意见，本轮未执行）**：按主题拆 commit 时先保证 mixin + `media_models` 与瘦身 `service.py` 同批入树；`service install` / Web 重装刷新三份 plist；再跑一条 provider 全链路至用户确认后的 `completed`。不要把 `.test-*` / 证据目录打进业务提交（现已 ignore）。
