# 抖库（douyin-wiki）整仓复审（R4）

- **审查角色**：审码
- **日期**：2026-09-28（Asia/Shanghai / CST）
- **机位**：用户 Mac `78c6c145-5c87-45ae-a4e0-35e6516c0d67`
- **项目根**：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`
- **对照**：[`docs/whole-project-review-20260928-r3.md`](whole-project-review-20260928-r3.md) 开项 B1、M3–M7、N9–N12；B1 修复说明 [`docs/b1-min-chunk-fix-20260928.md`](b1-min-chunk-fix-20260928.md)
- **范围**：当前 `HEAD` 整树只读复核 + 相对 R3 新风险扫描
- **约束**：只读审查；未改业务代码、未跑测试、未 push（本文件为审查交付物）

---

## 发现摘要（findings-first）

### 自 R3 已关闭

| ID | 主题 | 关闭证据（当前树） |
|---|---|---|
| **B1** | 最小块 `model_output_limit` 整单失败 | `llm.py` 校正保留原文+`ReviewIssue`（约 941–948）；分析降级 `open_questions`（约 1216–1231）；`_soft_merge_analysis_partials` / `_split_transcript_text`；单测改期望；live job→**`已完成`**（B1 笔记，entry 已清理）；commit `34d6441` ∈ HEAD |
| **M4** | `PYTHONUNBUFFERED` 运行时 | 安装器仍写该键；**本机三份已装 plist**（worker/web/maintenance）`EnvironmentVariables` **均含** `PYTHONUNBUFFERED=1` |
| **M5** | `trust_remote_code` | 仍为 `False`（`media_models.py` 61/68 + 测试）；无回退 |
| **M7** | 本地 CSRF 无 Origin/Referer 放行 | `_local_write_origin_ok` **末尾 `return False`**；无头写→403；测试 `test_web.py` 1083–1084；仍拒 `Sec-Fetch-Site` cross/same-site |
| **N9** | `unknown_confidence` 死参 | `review.py` 已无该形参；全仓 `grep` 无残留 |
| **N10** | 分析递归进度停滞 | 列表/文本/OCR 二分均 `completed_chunks += 1` 且 `total_chunks += 2` 后 `report_progress()`；不可再切叶子也计入；service `progress=max(...)` |
| **N11** | ASR/VAD 未钉 revision | 默认 `asr_model_revision=70514a3d…`、`vad_model_revision=v2.0.4`；`AutoModel(..., model_revision=…)` |
| **N12** | doctor 与新 ASR/OCR 栈脱节 | `setup.py` 查 `funasr`/`rapidocr`，并按 `asr_provider`/`ocr_provider` 判定 `transcription`/`ocr`；`test_setup.py` 覆盖 |

**M6 主陷阱已关（见「仍部分」）**：`service_*.py` + `media_models.py` **已入索引并随 `b251b93` 提交**；工作区 **干净**（`git status` porcelain 空）。

### 仍开 / 仍部分

| ID | 状态 | 说明 |
|---|---|---|
| **M3** | **仍开** | 事件桥能力与文档仍在；本仓仍无 Hermes/OpenClaw 专用自动唤醒 LaunchAgent；产品未验收 |
| **M6** | **部分关闭** | 未跟踪 mixin→坏包风险已消除；残留：相对 `origin/main` **ahead 14**、主题混提（尤其 `b251b93` 瘦身+ASR+CSRF+doctor+docs）、磁盘上仍有已 ignore 的 `.test-r*` / 证据目录（约 9MB+） |
| **N11 残留** | 轻微 | SenseVoice 钉 commit hash；FSMN-VAD 钉 **tag** `v2.0.4`（可动标签，弱于 hash） |
| **N1–N6** | 延续 Nit | 见下表；非本轮回归 |

### 新发现

| ID | 级别 | 主题 |
|---|---|---|
| **N13** | Nit | Service 六 mixin 间 **43** 处跨文件 `self._*` 私有耦合；瘦身后可维护，但封装边界弱，后续拆分易断 |
| — | — | **无新 Blocker / 无新 Major** |

---

## 仓库状态（相对 R3）

| 项 | R3 | R4（当前） |
|---|---|---|
| `HEAD` | `5ff63fc` + 脏工作区 | **`b251b93`**（含 `34d6441` B1、`7876dfe` B1 笔记哈希、`b251b93` 瘦身+审码收口） |
| 相对 `origin/main` | ahead 11 + 大量未提交 | **ahead 14**；**工作区干净** |
| mixin / `media_models` | **未跟踪**（M6 抬升） | **已跟踪、已提交** |
| 方法并集 | 121=121 | **121 unique / 0 重名**（`service.py` 25 + mixins 96） |
| LaunchAgent plist | 三份均无 `PYTHONUNBUFFERED` | **三份均有** `PYTHONUNBUFFERED=1` |
| 隔离目录 | 在盘 + gitignore | 仍在盘；gitignore 规则仍精确匹配 |

**祖先校验**：`34d6441` ⊆ `HEAD`；`b251b93` == `HEAD`。

---

## R3 开项逐条复核

### B1 — **已关闭**

| 维度 | 结论 | 证据 |
|---|---|---|
| 字符级硬切 | 关 | `_split_transcript_text`：标点优先，否则对半；`<2` 字才不可切 |
| 校正不可再切 | 关 | 保留原文 + skip `ReviewIssue`，不整单失败 |
| 分析不可再切 | 关 | 降级 `AnalysisResult` + `open_questions`；汇总超限走 `_soft_merge_analysis_partials` |
| 单测 | 关 | `test_correction_*minimum*` / `hard_splits*` / `analysis_reports_unsplittable*` 期望降级而非抛错 |
| live | 关 | B1 笔记：provider 视频 `7689429622975314067`，job→**已完成**，entry 后已 trash 清理 |

不回升。发布前无需再挡 B1。

### M3. Gateway 事件桥无自动唤醒 — **仍开**

- 仍有：`list_job_events` / `acknowledge_job_event` / `gateway monitor-events`（`cli.py`）
- 文档仍写「写入事件不启动 Agent」+ cron/伪代码（`docs/gateway-agents.md` 207–254）
- **仍缺**：仓内专用调度 / 一键 LaunchAgent；真实自动唤醒未验收

与 R3 相同，产品 Major。

### M4. Worker/Web 日志无 `PYTHONUNBUFFERED` — **已关闭**

| 层 | R4 |
|---|---|
| 安装器 | worker/maintenance≈601；Web≈696 均写 `"PYTHONUNBUFFERED": "1"` |
| 测试 | `test_interfaces.py` 287–300 |
| **已装 plist** | worker / web / maintenance **均有**该键 |

相对 R3「源码齐、plist 未刷」——运行时缺口已补上（与 B1 笔记中 kickstart/重装一致）。

### M5 — **仍关闭**

无回退；见上表。

### M6. 未分提交 / 测试残留 — **部分关闭（陷阱关闭）**

| 点 | R3 | R4 |
|---|---|---|
| mixin + `media_models` 未跟踪 | 抬升坏包风险 | **已提交** |
| 工作区混脏 | 是 | **干净** |
| 按主题拆 commit | 未 | `34d6441` 单独 B1；但 `b251b93` 仍混瘦身+ASR+Web 审码+大量 docs |
| 未 push | +11 | **+14** |
| 隔离目录在盘 | 是（已 ignore） | 同左 |

**判定**：R3 抬升的「不完整暂存即 ImportError」已关；残留为发布卫生（push 前主题整理 / 是否清理磁盘隔离物），不升 Blocker。

### M7. 本地 CSRF 无 Origin/Referer — **已关闭**

相对 R3「无头仍 `return True`」：

- 文档字符串改为：无 Origin/Referer 的写请求 **拒绝**
- 实现末尾 **`return False`**
- 测试明确：无头 POST → **403**；匹配 Origin/Referer → 201；`Sec-Fetch-Site` cross/same-site → 403
- TrustedHost 仍限 `127.0.0.1` / `localhost` / `testserver`
- 浏览器 `fetch` 同源会带 Origin；Web UI 契约兼容

有意边界：本机脚本须显式加头。双重提交 token 仍无——不升为开项（本机绑定 + Origin 强制已达当前威胁模型）。

### N9 — **已关闭**

`detect_review_issues` 仅 `low_confidence` / `very_low_confidence` / material 规则；无死参。

### N10 — **已关闭**

失败父批计入 `completed_chunks`，分母 `+= 2`；叶子成功/降级亦 `+= 1`。UI `jobs.js` 展示 `completed_chunks/total_chunks`；百分比仍 `max` 防倒退。

### N11 — **已关闭（VAD 为 tag 残留）**

- ASR：`70514a3da51f1160f51d18449dab6128bbd4928b`
- VAD：`v2.0.4`（tag，可被上游移动；可接受的轻残留，不单开 Major）

### N12 — **已关闭**

`doctor` 输出 `funasr`/`rapidocr` 检查项，并按 provider 决定 `transcription`/`ocr` ok；有单测。

---

## R1–R3 Nit 简表（抽查）

| ID | 主题 | R4 |
|---|---|---|
| N1 | `_deduplicate_issues` 私有导入 | **仍开**：`review.py` 定义；`service_capture` / `service_analysis` 仍导入下划线名 |
| N2 | Selector 构造即双后端 | **仍开**：`SelectedTranscriber`/`SelectedOCR` `__init__` 同时构造两侧 |
| N3 | OCR `timestamp_ms=source_index` | **仍开**：RapidOCR 路径 `media_models.py` 199 |
| N4 | `media_models`→`media` 重依赖 | **仍开**：`from .media import VisionOCR, WhisperTranscriber` |
| N5 | 升级默认 whisper vs 新装 auto | **仍成立**：`config.py` load `setdefault` whisper/vision |
| N6 | 状态中英混用 | **仍开**：内部 enum 英文 + UI `stage_label` 中文 |
| N7 / N12 | doctor 旧栈 | **N12 关** → N7 实质关闭 |
| N8 | Web unbuffered | **关**（R3 源码 + R4 plist） |
| N9–N12 | 见上 | **关**（N11 轻残留） |
| **N13** | mixin 私有跨文件耦合 | **新 Nit**：43 处 `self._*` 跨 mixin |

---

## 新发现详述

### 无新 Blocker / Major

相对 R3，热路径（CDN 绑定、校正/分析预算、置信度 provenance、SenseVoice、CSRF、service 瘦身入树）未见高于仍开 M3 的新阻断。

### N13. Mixin 私有方法跨模块耦合（Nit）

六 mixin + `service.py` 通过多重继承共享 `self`，静态统计约 **43** 处「定义在 A、`self._*` 用于 B」（如 capture→import/analysis/maintenance；trash→entries 等）。行为与瘦身前单体一致（方法并集 121、无重名），但：

- 私有命名不再表示模块内封装；
- 后续再拆文件或做类型检查时易漏改。

建议：后续迭代将跨域钩子提升为无下划线协作 API，或按调用图收拢；**不阻塞发布**。

### 有意降级路径（记录，非缺陷）

B1 的校正跳过 / 分析 `open_questions` / `_soft_merge` 可能降低单块质量但避免整单失败；入库前仍有 `_validate_analysis_evidence` / prune。与设计一致。

---

## 热路径速览（R4）

1. **CDN**：aweme 绑定与等待环加固仍在已提交 `media.py`。
2. **B1**：最小块不可切 → 降级而非失败；live 已跑通 `已完成`。
3. **进度**：父批失败计入 + 分母扩大；百分比 `max`。
4. **ASR**：`trust_remote_code=False` + revision 钉扎 + doctor 感知新栈。
5. **CSRF**：强制 Origin/Referer + Fetch Metadata；TrustedHost 本机。
6. **Service 瘦身**：已入 git；工作区干净；mixin 耦合见 N13。

---

## 风险矩阵（R4）

| 优先级 | ID | 主题 | R4 状态 |
|---|---|---|---|
| P1 | **M3** | Gateway 自动唤醒未装 | **仍开** |
| P2 | **M6 残留** | ahead 14 / 混提 / 磁盘隔离物 | 部分关闭 |
| ~~P1~~ | ~~B1~~ | 最小块超限 | **关闭** |
| ~~P1~~ | ~~M4~~ | PYTHONUNBUFFERED 运行时 | **关闭** |
| ~~P1~~ | ~~M5~~ | trust_remote_code | **关闭** |
| ~~P2~~ | ~~M7~~ | CSRF 无头放行 | **关闭** |
| ~~P2~~ | ~~N9–N12~~ | 死参 / 进度 / revision / doctor | **关闭**（N11 VAD tag 轻残留） |
| P3 | N1–N6、N13 | API 整洁 / 双后端 / mixin 耦合 | 可迭代 |

---

## 结论摘要（供父 Agent）

### 关闭（相对 R3 开项）

**B1，M4，M5（维持），M7，N9，N10，N11，N12**；**M6 的未跟踪坏包陷阱**。

### 仍开 / 仍部分

- **仍开**：质询 **M3**（Gateway 自动唤醒产品缺口）
- **仍部分**：质询 **M6**（未 push + 混提历史 + 磁盘测试残留）
- **Nit 延续**：N1–N6；N11 VAD tag；**新 N13** mixin 私有耦合

### 新发现

- 无新 Blocker / Major
- **N13** 如上

**建议（审查意见，本轮未执行）**：M3 按产品决定是装 cron/LaunchAgent 还是保持文档态；push 前可按主题整理或接受当前 14 commit 历史；磁盘 `.test-*` 可手工删（已 ignore）。无需为已关的 B1/M4/M7/N9–N12 再挡发布。

---

## Executor follow-up（2026-09-28 CST，忽略 M3）

| ID | 动作 |
|---|---|
| **M6 残留** | 已删除磁盘隔离物：`.test-backup-20260927`、`.test-r1-20260927`、`.test-r2-20260928`、`docs/_mcp-sim-evidence-20260927/`、`7686768044248452390/`；保留 `.gitignore`；未触碰真实 vault `~/Documents/Obsidian/抖库` |
| **N1** | `_deduplicate_issues` → 公开 `deduplicate_review_issues`；capture/analysis 改导入 |
| **N2** | `SelectedTranscriber` / `SelectedOCR` 改为按需构造后端（property 懒创建） |
| **N3** | `FrameStampMs` 别名 + RapidOCR docstring：帧元组第一元即 `timestamp_ms`（图文序数由调用方再映射） |
| **N4** | `media_models` 对 `VisionOCR`/`WhisperTranscriber` 改为属性内懒 import，避免 import 时拉起整份 `media.py` |
| **N5** | 行为未改；`load_config` 注释写清「升级缺字段仍 whisper/vision，新装 auto」；README 已有说明 |
| **N6** | `docs/gateway-agents.md` + MCP `INSTRUCTIONS` 强调机读 `status` vs 中文 `*_label` |
| **N11** | `vad_model_revision` 默认由 tag `v2.0.4` 改为 tip 提交 `662fc7a38813d81305085696d59eb5b1141a204a` |
| **N13** | **推迟**：43 处跨 mixin `self._*` 为瘦身继承面；无单点小改可消；后续再拆协作 API，不阻塞发布 |
| **M3** | 本轮按产品指示忽略 |
