# service.py 瘦身切片（行为不变）

> 日期：2026-09-27  
> 原则：**只拆文件 / 挪方法，不改产品行为**；默认不 push；每个切片提交前通过完整离线测试，再进下一步。  
> 现状：`service.py` ≈ **4575 行**；最大热点 `_process_resolved_capture` **626 行**、`_process_image_note_capture` **335 行**。

---

## 新增问题与优先级：行为修复先于机械拆分

2026-09-27 补充：Gateway 接续、provider 单片段校正超限和进度展示属于独立的行为修复，不混入 S1–S6 的机械搬迁提交。建议先完成 R1/R2 并建立通过验证的基线，再进行 service 拆分；Gateway 的 R3/R4 单独推进，其部署选择不阻塞文件拆分。

用户报告短片在 `max_output_tokens=8192` 下发生 `model_output_limit`，任务失败且未入库。静态代码已确认 `OpenAICompatibleProvider.correct_transcript` 在 `len(chunk) < 2` 时无法继续二分，但本次计划更新未重放真实模型请求；具体样本的响应、用量及截断原因仍需在 R1 中核对。不能仅凭视频时长推断输出预算，单片段结构和模型实际输出都需要纳入验证。

### R1 — provider 单片段校正超限（最高优先级）

- 保存可复现样本与配置：原始片段数量、文本长度、模型标识、输出预算、错误码和响应截断信息。区分输入预算不足与输出超限，不以调高 token 上限作为唯一修复。
- 为单个长片段增加内部文本分块：优先句子边界，超过预算时再按有界文本长度切分；请求按输入和预期输出预算约束。超限后允许继续缩小，设置最小分块及重试上限，避免无限递归或重复相同失败请求。
- 内部分块使用明确的原始片段 ID 与子块序号映射；按顺序合并回原始片段，保留原 ID 和时间范围，不伪造词级时间戳。复核疑点映射回原片段；不丢句、不重复、不摘要。OCR 继续按原时间范围选取并受预算限制。
- checkpoint 必须区分各子块及实际请求内容，成功子块可复用；重试不能串用缓存或重复合并。无法完成校正时保留明确错误和可恢复材料，不静默把未校正内容视为成功结果。
- 验收：单片段首次输出超限后可拆分完成、多片段原有二分仍有效、连续超限有界退出、缺失/重复子块不会被误判成功、重试复用 checkpoint；核对合并后的 ID、顺序、文本完整性和复核映射。用原短片和一个长片复测 provider 路径，区分模型成功、需要人工复核及失败，不以绕过复核强制入库。

### R2 — provider 校正阶段的状态和进度（与 R1 同优先级）

当前 `model_progress` 写入 `analysis_progress.phase=correction` 和百分比，但不切换任务状态，可能继续显示 `transcribing@0.62`。校正开始前应使用语义合适的现有状态，并由阶段字段明确显示“LLM 校正”；检查 Web、CLI、MCP 的展示和读取，不能只修改百分比。

等待分析资源、正在校正和正在内容分析需如实呈现；递归拆分或缓存复用时进度不倒退、不提前显示完成。避免为这一修复引入不必要的新 JobStatus；若现有状态无法表达，再单独评估状态契约变更。

验收：阻塞模拟模型响应时，任务已显示校正阶段；成功、超限失败、重试和恢复后阶段信息正确，原有终态与事件行为保持有效。

### R3 — Gateway 异步接续（集成与部署任务）

执行决定（2026-09-28）：用户希望以后可对接不同 Agent 软件。本轮交付通用事件监控与
MCP 接续契约、离线验证和安装时的配置说明；具体 Agent 的调度/唤醒由其安装时配置，
不在本机部署 Hermes/OpenClaw 专用桥。真实自动唤醒因此不作为本次已完成验收项，
详见 [执行记录](service-slim-execution.md)。

项目已有持久事件接口 `list_job_events`、确认工具 `acknowledge_job_event` 和 `gateway monitor-events` 监控命令；写入事件本身不会唤醒 Agent。无后台续跑、轮询或事件桥时，“待 AI 处理”会持续等待。既有说明见 [Gateway 接入指南](gateway-agents.md#事件接续)。

- 先核实目标 Gateway（如 OpenClaw/Hermes）以及实际部署的接续机制，区分代码能力和部署状态；未查询前不宣称本机事件桥已经安装或失效。
- 选择已有后台续跑、定时查询或事件桥中的一种，明确调度归属与启动/重启方式。优先由无模型的监控程序检查事件，有可操作事件再唤醒 Agent；目标 Gateway 未确定前可完成通用事件契约测试和文档，但不擅自配置外部调度。
- 明确 `gateway_context` 路由、确认时机、重复投递与过时事件处理。处理或交付失败时不能提前确认；重启后继续处理未确认事件，提交前重新读取当前任务阶段。
- 验收：采集后无需手动继续即可到达校正/分析工具调用；校正到分析的同状态不同阶段事件能继续处理；监控与 Agent 重启后可恢复，重复事件不重复提交，空队列不启动模型。真实部署验收与离线测试分别记录。

### R4 — Gateway 工具顺序与 schema（接口集成任务）

以 MCP 作为 Agent 的集成入口，按 `get_analysis_context` 返回的阶段与 schema 提交。视频首次处理需先 `submit_transcript_correction`，有疑点先完成复核，再 `submit_gateway_analysis`；图文直接分析，重新分析复用已有校正稿，不重复校正。

Service 的 `submit_transcript_correction` 接收 `TranscriptCorrection` / `ReviewIssue` 实例，MCP 包装层负责将 JSON 校验并转换；直接传字典调用 Service 不等价于调用 MCP。本轮不为绕过 MCP 放宽 Service 类型契约。

验收：通过 MCP 工具包装层验证正确顺序、无效 schema、未知/重复片段 ID、复核未完成、图文及重新分析路径；失败应给出可理解的错误且不污染任务。文档提供与工具 schema 一致的最小请求示例，集成成功不能仅由直调 Service 的测试代替。

---

## 目标形态

```text
service.py              # 统一入口：DI、初始化、任务分发、共享持久化与路径辅助
service_capture.py      # 视频/图文采集 + ASR/OCR + 确认门
service_analysis.py     # 校正、gateway/provider 分析、证据校验、复核
service_import.py       # 博主清点、选择、派发子任务和博主文档辅助
favorites.py            # 保留已有 FavoritesService 组合，不纳入本轮搬迁
service_entries.py      # 资料 CRUD、灵感、收藏、话题、搜索入口胶水
service_trash.py        # 回收站 / 恢复 / 永久删除 / 崩溃恢复
service_maintenance.py  # rebuild、maintenance、封面回填、orphan
```

`DouyinWikiService` 继续作为统一对外入口；本轮采用 **mixin 继承**，保留导入路径、构造参数和公开方法签名。已有 `FavoritesService` 组合保持不变，不为统一形式改写它：

```python
class DouyinWikiService(
    CaptureMixin,
    AnalysisMixin,
    ImportMixin,
    EntriesMixin,
    TrashMixin,
    MaintenanceMixin,
):
    ...
```

本轮收益是职责定位和缩小单文件范围；Mixin 仍共享同一个实例，不宣称降低运行时耦合或提升性能。

### 模块边界与 Mixin 约束

- 依赖、锁和信号量继续由主类初始化；Mixin 不定义 `__init__`，不重复创建状态。
- Mixin 不互相运行时导入，也不运行时导入主类；必要的类型引用使用 `TYPE_CHECKING`。跨职责调用保留原有 `self.method(...)`。
- 每个方法只有一个归属，不允许同名方法覆盖或依赖 MRO 顺序选择实现；保留原有装饰器、默认参数和同步/异步形式。
- `_prepare_entry_bundle`、`_write_entry_documents`、`_persist_entry_documents_and_bundle*`、`_commit_vault`、`_vault_path`、`_vault_relative` 本轮留在 `service.py`。它们服务于多个职责，不归入采集模块。
- `process_claimed_job` 的分发、异常转换和父任务刷新整体留在主类，不借搬迁调整执行顺序。
- 下表行号仅用于定位当前版本；按方法名和职责搬迁，不按行号范围整段删除。基线确定后记录完整的方法归属，未列明的辅助方法先保留在主类。

禁止：重写状态机、合并 JobStatus、删 gateway 模式、动 SenseVoice/OCR 选型逻辑。

---

## 切片顺序（先验证职责集中的模块）

### S0 — 基线（0 行为 diff）

R1/R2 的行为修复完成后，先独立验证并提交，再记录本轮拆分基线；其变更不计入“纯搬迁” diff。

- 当前评审时工作区已有 ASR/OCR 等未提交变更，涉及 `service.py`、适配器、配置及测试。先清点 `git status` 与 diff，保留已有工作，确定本轮基于哪个功能版本；单纯切换分支不会隔离未提交变更。
- 若基于当前功能版本拆分，先将功能变化整理为独立、可复现的基线提交，再创建重构分支；若基于已提交版本拆分，使用从明确提交创建的独立 worktree，并记录尚未包含的功能变化。不自动 stash 或清理其他任务的文件。
- 记录基线提交 SHA、Python/依赖环境，以及 `uv run ruff check .` 和 `uv run pytest -m "not live"` 的结果（通过、失败、跳过及原因）。失败先定位和处理，未通过时不宣称基线全绿或继续批量搬迁。
- 本瘦身与 ASR/OCR 功能变化分开提交、分开 PR。默认不 push。
- 清点模块级符号使用及测试 patch 路径，特别是 `douyin_wiki.service.send2trash`；确定搬迁后的替换位置。

### S1 — `service_trash.py`（约 600–700 行可挪）

挪方法（行号约）：

| 方法 | 约行 |
|---|---|
| `_entry_trash_root` … `_recover_entry_trash_operations_locked` | 1266–1959 |
| `list_trashed_entries` / `trash_entry` / `restore_*` / `permanently_delete_*` | 含上 |

验收：`tests` 里 trash / restore 相关全绿；对外 API 签名不变。

### S2 — `service_maintenance.py`（约 400 行）

| 方法 | 约行 |
|---|---|
| `backfill_video_covers*` / `rebuild_database_from_vault*` / `run_maintenance*` | 3431–4103 |
| `_find_orphan_pages` / `_trash_assets` | 4511–4534 / 4104–4112 |

`_trash_assets` 同时被 capture 调用，归维护模块管理媒体过期清理；保留跨职责调用，不复制实现。

验收：maintenance、媒体恢复、documentation / setup 相关测试；更新 `send2trash` 测试补丁的目标模块，确认测试仍拦截真实调用。

### S3 — `service_import.py`（约 350–450 行）

| 方法 | 约行 |
|---|---|
| `capture_douyin_creator` / `sync_creator` / inventory / selection / confirm / import | 265–464 |
| `_process_creator_import` / `_refresh_creator_parent` / creator 文档辅助 | 2211–2290, 3598–3788 中与 creator 强相关者 |

已有收藏夹清点和导入继续由 `FavoritesService` 负责。

验收：creator、favorites flow / interfaces 相关测试；子任务仍进同一 capture 路径，父任务刷新与选择确认行为不变。

### S4 — `service_analysis.py`（约 500–700 行）

| 方法 | 约行 |
|---|---|
| `get_analysis_context` / `submit_transcript_correction` / `submit_gateway_analysis` | 529–686 |
| `approve_job` / `resolve_review` / `submit_analysis*` / `reanalyze*` | 687–909 |
| `_prune_unverified_analysis_evidence` / `_validate_analysis_evidence` | 4185–4468 |
| `_detect_image_review_issues` / `_apply_image_review_resolutions` | 4136–4184 |
| `_process_reanalysis` | 2291–2415 |

验收：analysis / review / gateway 路径测试；覆盖 local、provider、gateway 模式，对比状态、错误码、artifacts 键和值、结果及落库内容。时间、随机 ID、临时路径等非确定字段应固定或按明确规则归一化，不以键集合相同代替行为一致。

### S5 — `service_capture.py`（最大收益，约 1000+ 行）

| 方法 | 约行 |
|---|---|
| `_process_capture` / `_work_capture_locked` / `_process_resolved_capture` | 2416–3095 |
| `_process_image_note_capture` | 3096–3430 |
| `_process_media_restore` | 2158–2210 |
| `_persist_video_cover` | 4469–4510 |

本轮只整段搬迁方法，不拆分函数内部流程。公共持久化方法留在主类。

验收：视频无音轨、retained audio、OCR warning、时长确认、图文采集、媒体恢复，以及重试、取消、锁释放和已有并发路径。保持装饰器、`await`、锁与信号量的作用域、异常传播和状态写入顺序不变；尤其保留 `_work_capture_locked` 的取消清理逻辑。

### S6 — `service_entries.py`（剩余门面）

话题、灵感、条目收藏标记、entry get、搜索胶水、reminder confirm 等；此处“收藏”不包含已有 `FavoritesService` 收藏夹导入。

验收：topics、灵感、条目收藏、搜索、reminder 及 Web / CLI / MCP 入口相关测试。`service.py` 保留 DI、初始化、任务分发和共享基础能力，不设 400–600 行硬指标。

## 后续独立任务（不作为本轮完成条件）

### F1 — 采集大函数拆分（原 S5b）

待 S1–S6 稳定后另开重构，评估抽取 `_run_asr_ocr_phase`、`_duration_gates`、`_finalize_video_entry` 等私有方法。不以单函数 ≤200 行为硬指标，按输入输出和控制流边界决定。

这一步涉及局部变量、提前返回、异常、锁和中间状态，不按机械搬迁验收。先确认关键分支的现有测试覆盖，再对真实缺口补充行为测试，验证状态转换、失败重试、取消清理及持久化结果；保持功能行为不变。

### F2 — `media.py` 瘦身

与 service 拆分正交，勿同 PR：

- `media_download.py`：CDN / Playwright / yt-dlp  
- `media_ffmpeg.py`：extract_audio / extract_frames  
- `media.py`：再导出旧符号，避免 import 断裂  

Whisper/Vision 可留在 `media.py` 或并入已有 `media_models.py` 旁的 `media_legacy.py`。

---

## 每步机械清单

1. 新建模块，剪切方法；补 `from __future__` / typing / 原有 import。  
2. Mixin 挂到 `DouyinWikiService`；**不改**方法名、参数、JobStatus、artifacts 键和值及其更新语义。  
3. 检查新模块中的全局符号解析、装饰器、导入循环和同名方法；模块移动后更新测试 patch 目标，保留原有断言。仅在旧模块再导出符号不能保证旧 patch 继续生效。
4. 开发中跑相关测试；每个切片提交前执行 `uv run ruff check .` 和 `uv run pytest -m "not live"`，记录结果及跳过原因。上文各切片验收是重点覆盖范围，不替代完整离线测试。真实抖音 live 测试单独记录是否执行，不混入离线全绿结论。
5. 审查 diff：除 imports、类归属、必要的测试 patch 路径外，方法签名、装饰器与函数体保持一致；核对状态、错误码、artifacts 内容、锁范围和持久化顺序。现有覆盖充分时不为机械搬迁新增重复测试。  
6. 每个切片独立 commit：`refactor(service): extract <module> mixin (no behavior change)`。  
7. 不 push，除非明确要求。

---

## 明确不做

- 合并/删除 JobStatus 或 gateway 模式  
- 「顺手」改 SenseVoice / RapidOCR / 置信度规则  
- 大范围 rename 对外 API  
- 为拆而引入新抽象层（Repository / UseCase 全家桶）——本轮只要 **文件边界**

---

## 预期收益

| 指标 | 现在 | S6 后（估） |
|---|---|---|
| `service.py` | ~4575 行 | 显著缩小，按职责保留共享方法，不设行数硬指标 |
| 最大单函数 | 626 行 | 本轮不变，后续 F1 单独评估 |
| 改 ASR 时打开的文件 | service + media* | 主要 `service_capture` + `media_models` |

---

## 建议开工顺序一句话

**R1/R2 独立修复 → S0 基线 → S1 trash → S2 maintenance → S3 creator import → S4 analysis → S5 capture 整段搬迁 → S6 entries。**  
先挪职责较集中、容易验证的 trash/maintenance，验证搬迁方式后再动 capture。它们仍依赖索引、话题和共享持久化能力，不视为低耦合独立服务。
