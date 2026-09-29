# 抖库整库 Review R6（2026-09-29）

日期：2026-09-29（Asia/Shanghai）。对象：`main@43f1600`（含 `d348e09` N13 + `43f1600` R5 majors 及一并入库的自动校对 / 前轮 R5·R6·Grok 修复）。相对 `origin/main` 超前 **17** 个本地提交；工作区干净（仅若干未跟踪 docs）。**未 push**。本轮 **只读审查**，未改业务代码、测试、正式配置或正式数据。

## 结论

- **无新 Blocker**。
- **1 个 P2（新，已修）**：`_normalize_correction_segments` 的 1-based 启发式过宽；现已收紧为仅完整 `1..n` 才 remap（见下方修复记录）。
- **若干 Nit / 已知风险线索**（未隔离成正式缺陷，或不阻塞日常单 Worker 使用）。
- **前轮已关闭项复核通过**（N13、R5-1～4、旧 R6-1/2、Grok P2-1/2、R5 majors Major-1/2 的主路径）；M3 按约定跳过。


## 修复记录（R6-1）

- **状态**：已修（本地 commit，未 push）。
- **改动**：`_normalize_correction_segments` 仅当 `set(ids) == set(range(1, n+1))` 时按 1-based remap；缺 `0` 的部分子集一律按本地 `0..n-1` 填缺口。
- **单测**：`tests/test_r5_majors.py` 增补完整 0-based / 完整 1-based / 部分无 0（不 remap）/ 重复与未知 ID。

## 发现

### R6-1 · P2：校正片段 ID「缺 0 即当 1-based」会错位映射（已修）

- **位置**：`src/douyin_wiki/adapters/llm.py:122-184`（`_normalize_correction_segments`）；由 `43f1600` 为修 live「未知或重复片段 ID：80」引入。
- **机制**：`looks_one_based` 条件为 `min(ids) >= 1 and max(ids) <= size and 0 not in ids`。只要返回里没有 `0`、且最大值不超过 chunk 长度，就优先走 `id-1` 映射。因此：
  - **完整** `1..n` → 正确（覆盖 live max_items=80 形态）。
  - **完整** `0..n-1` → 因含 `0` 走 local，正确。
  - **仅返回 `id=1`**（或任意不含 `0` 的部分 0-based 子集）→ 被当成 1-based，文本落到 `index=0`，真正的 `index=1` 仍保留原文。
- **隔离复现**（只读探针，未改仓库）：

  | 输入 | 当前结果 | 期望（按 0-based） |
  |---|---|---|
  | 单条 `{id:1, text:FIXED}`，size=5 | index0=`FIXED`，index1=`ORIG1` | index1=`FIXED`，index0=`ORIG0` |
  | 部分 `{1,2,3}` 缺 0 | 落到 0,1,2 | 应落到 1,2,3 |

- **影响**：模型偶发漏段、或只改部分本地序号时，可能把订正写到相邻片段；整单仍成功，`transcript_edits` 看起来「有改动」但错位。完整全量 0/1-based 与 live V2 复测路径不受影响。
- **建议**：仅当 `set(ids) == set(range(1, size+1))`（**完整** 1-based）时才 remap；其余一律按 local `0..n-1` 填缺口。补单测：单 `id=1`、缺 `0` 的部分 0-based、完整 `1..n`、完整 `0..n-1`。可选：把 remap/丢弃统计写入 artifacts，便于诊断。

### Nit-1 · `ruff` E501：`llm.py` 校正系统提示超长

- `uv run ruff check .` 报 `adapters/llm.py:846` 行宽 137 > 100（`43f1600` 引入的「本地从 0 起」提示句）。不阻功能；整理 commit / CI 前应折行。

### Nit-2 · 不可再切的 **merge** 降级不进 `job.warnings`

- `_soft_merge_analysis_partials` 把原因写入 `open_questions` / `content_card.notes`，入库仍可为 `completed`（除非另有 OCR/证据警告）。与 R5-1「不可分析却无警告 completed」不同：此处保留分段内容，属有意降级。若希望任务态可机读，可把该 reason 提升为 `analysis_evidence_warning` 一类。

### Nit-3 · 发布窗口仍持有 SQLite 写锁并同步 Vault/Git

- `database.persist_entry_bundle` 在 `BEGIN IMMEDIATE` 后 `assert_job_claim` 再 `publish_documents()`（含 `list_entries` + 写盘 + 可选 git commit）。为修旧 R6-1 有意为之；默认租约 180s，短资料通常安全。库很大或 git 慢时可能挤占心跳（前轮待验证，本轮未复现）。

### Nit-4 · 多 Worker 博主父任务计数无版本条件

- `service_import.refresh_creator_parent` 先读子任务再写父任务，无 CAS。默认单 Worker；本轮未复现。

### Nit-5 · 审查类 docs 仍未跟踪

- 工作区未跟踪：`docs/whole-project-review-20260928-r5.md`、`…-20260929-r5.md`、`…-grok.md`、本文件、`n13-mixin-collab-api-brief.md`、`live-provider-7685667367616570047.md` 等。不阻塞运行；需要时可另 commit（仍可不 push）。

## 前轮项复核

| 项 | 状态 | 依据 |
|---|---|---|
| **N13** mixin 跨文件 `self._*` | **关闭** | `d348e09`；静态跨 mixin 私有方法调用 **0**；协作 API 如 `prepare_entry_bundle` / `persist_entry_documents_and_bundle*` 已公开 |
| **R5-1** 不可再切分析超限装 completed | **关闭** | 最小块超限抛 `ModelOutputError` / `ModelContextError`；相关回归在套件内 |
| **R5-2** Web 存模型不重载 | **关闭** | `webapp/app.py` 保存后重建 `core.analysis` 并 `request_worker_reload` |
| **R5-3** 迟到重试清租约 | **关闭** | `requeue_job` 同事务 `expected_updated_at` + `expected_statuses` |
| **R5-4** Gateway 空校对绕过 | **关闭** | 须覆盖全部 ID；空白文本 model + service 双拒 |
| **旧 R6-1** 失租约仍可入库 | **关闭** | `persist_entry_bundle` 内 `assert_job_claim`；`tests/test_review_r6.py` |
| **旧 R6-2** 重分析无 checkpoint | **关闭** | `process_reanalysis` 传 `checkpoints` / `on_checkpoint` / `on_progress` |
| **Grok P2-1** 空白校正删稿 | **关闭** | `TranscriptCorrection.reject_blank_text` + service strip 后校验 |
| **Grok P2-2** 图文无断点 / OCR 置信 | **关闭** | 图文 analyze 传 checkpoint；OCR prompt 保留 confidence |
| **R5 Major-1** 粘住 `analysis_candidate` | **关闭** | `retry_job` 对 FAILED/NEEDS_AUTH `remove_artifacts={"analysis_candidate"}` |
| **R5 Major-2** 校正未知/重复 ID 整单失败 | **主路径关闭** | 完整 1-based / 重复忽略 / 缺口保留原文；正式 V2 与短视频 provider 冒烟 `completed_with_warnings`。**残留见 R6-1** |
| **M3** Gateway 自动唤醒 | **跳过 OK** | 用户约定不装具体 Agent 桥 |

## 范围与验证

- 核对：`43f1600` / `d348e09` diff 要点、Service 七文件协作 API、Worker 租约与发布、校对 normalize、分析/重分析 checkpoint、Web 模型保存、Gateway 校对契约、废纸篓路径/符号链接拒绝、本机 CSRF 头检查、媒体 `safe_media_path`。
- `uv run pytest -m 'not live' -q`：**409 passed，1 deselected**（约 39.8s）。
- 定向：`test_r5_majors` / `test_review_r5` / `test_review_r6` / `test_grok_review_fixes` / `test_auto_correction` 共 **24 passed**。
- `uv run ruff check .`：**1 个 E501**（见 Nit-1）。`git diff --check`：通过（HEAD 干净）。
- 未重跑真实 LLM/抖音下载；未改 LaunchAgent / 正式 vault；未做依赖 CVE 审计。live 旁证见 `docs/fix-r5-majors-20260929.md`、`docs/live-provider-7685667367616570047.md`、`docs/live-mcp-dual-mode-20260929.md`。

## 建议优先级

1. 收紧 R6-1 的 1-based 判定（完整集合才 remap）并补上列单测。  
2. 折行消掉 Nit-1 E501。  
3. Nit-2～4 仅在出现现场或做多 Worker / 超大库时再动。  
4. 需要时把未跟踪审查 docs 收成一次本地 docs commit（不 push，除非用户要求）。
