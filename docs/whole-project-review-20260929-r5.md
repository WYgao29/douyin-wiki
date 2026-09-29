# 抖库整库 Review R5（2026-09-29）

日期：2026-09-29（Asia/Shanghai）。对象：`main@d348e09` + 当前未提交工作区（自动校对去人工复核、R5/R6/Grok 修复、N13 已提交）。相对 `origin/main` 超前 **16** 个本地提交；工作区另有未提交改动。**未 push**。本轮只读审查为主，未改业务代码。

## 结论

- **无新 Blocker**（不阻止正式双模冒烟）。
- **2 个 Major**：①失败重试可能粘住 `analysis_candidate`；②Provider 长字幕校正未知/重复片段 ID（live 复现）。
- **若干 Nit / 风险线索**（未隔离复现或属已知边界）。
- **前轮复核**：N13 **已关闭**；M3 **按约定跳过**（不装 Agent 事件桥）。

## 前轮项复核

| 项 | 状态 | 说明 |
|---|---|---|
| **N13** mixin 跨文件 `self._*` | **已关闭** | commit `d348e09`；静态检查跨 mixin 私有方法调用 **0**；协作 API 已去下划线（如 `prepare_entry_bundle` / `persist_entry_documents_and_bundle`） |
| **M3** Gateway 自动唤醒 | **跳过 OK** | 用户约定不安装具体 Agent 桥；`docs/gateway-agents.md` 保留接入说明 |
| R5-1～R5-4 | 工作区已修 | 不可再切超限失败；模型保存重建 provider + reload；`requeue_job` 同事务状态/版本；Gateway 校对需完整 ID |
| R6-1～R6-2 | 工作区已修 | 发布前 `assert_job_claim` + `publish_documents`；重新分析传入 checkpoint |
| Grok P2-1/P2-2 | 工作区已修 | 空白校正拒绝；图文 checkpoint + OCR confidence 进提示 |

## 发现

### Major-1 · 失败重试不清理 `analysis_candidate`，可能永久复用未入库分析

- **位置**：`src/douyin_wiki/service.py:335`（`FAILED`/`NEEDS_AUTH` 分支仅 `requeue_job`）；对照 `src/douyin_wiki/service_capture.py:598-599`（有 candidate 则跳过模型）、`:634-654`（先落 candidate，再 prune/validate）。
- **机制**：Provider 视频路径在调用模型后立即写入 `analysis_candidate`，随后 prune 与 `validate_analysis_evidence`。若校验抛错导致任务 `failed`，candidate 仍留在 artifacts。`retry_job` 对 `NEEDS_REVIEW` 会 `remove_artifacts` 含 `analysis_candidate`（`:325-328`），但对普通 **FAILED** 不清理。下次认领直接 `model_validate(analysis_candidate)`，不再请求 LLM；同一失败输入可反复失败。
- **影响**：证据校验失败、偶发坏 JSON 形状等场景下，重试无法「换一次模型输出」自愈；浪费排队却不重跑分析。
- **建议**：`FAILED`/`NEEDS_AUTH` 重试时至少移除 `analysis_candidate`（或仅在校验通过后再持久化 candidate）；与 `NEEDS_REVIEW` 清理集合对齐。补回归：candidate 已存 → validate 失败 → retry → 必须再次调用 analyze。


### Major-2 · Provider 长字幕校正可报「未知或重复片段 ID」（live 复现）

- **位置**：`src/douyin_wiki/adapters/llm.py:889`（`未知或重复片段 ID`）；关联分片/上下文构造 `:771-855`（`bounds_by_id` / `context_before|after` / piece ID）。
- **live 证据**：正式库 provider 采集 `7660045462695521974`，校正 `total_chunks=105`，任务 `failed` / `external_tool_error`：`模型校正结果包含未知或重复片段 ID：80`（约 187s）。同视频 gateway identity 校正 105 段成功入库，排除纯媒体问题。
- **影响**：较长口播在本机 LLM 校正路径直接失败，无法进入分析；与「去人工复核、模型直接校对」目标冲突。
- **建议**：对模型返回的本地 chunk 下标与 piece ID 映射做单测（含切分后 ID、重复 id、越界 id）；校验失败时保留 checkpoint 并可重试单 chunk，避免整任务失败且不落可诊断产物。详见 live 报告。

### Nit-1 · 发布窗口持有 SQLite 写锁并同步写 Vault/Git

- **位置**：`src/douyin_wiki/database.py:2138-2142`（`BEGIN IMMEDIATE` 后 `publish_documents()`）。
- **说明**：为修 R6-1 有意拉长写锁覆盖发布。事件循环在此期间难续租；默认租约 180s，短资料通常安全。超长 Git/大文件发布仍可能在完成更新时丢租约（Grok 待验证项，本轮未复现）。
- **建议**：保持现状可接受；若出现「已入库但任务未 completed」再考虑缩短锁内工作或异步提交 Git。

### Nit-2 · 未提交改动体量大、尚未落 commit

- **范围**：`llm.py` 校对提示 v2.3、去 `review_issues` 模型输出、Web 历史复核改「重试」、vault 增加 `transcript_edits`/`ocr_quality_notes` 等（约 22 文件未暂存）。
- **说明**：功能方向与「模型直接校对、不人工复核」一致；但相对已推/未推提交混杂，回滚与 bisect 成本高。**不阻塞冒烟**，建议冒烟通过后单独整理 commit（仍可不 push）。

### Nit-3 · Worker 长时间同 PID，需确认已加载当前可编辑源码

- LaunchAgent worker pid 观测时约运行 3h+；editable install 指向本仓库 `src/`。源码 mtime 变化应触发「代码已更新」退出重启（`worker.py` `source_changed`）。冒烟前通过 `configure-analysis-mode` 显式 reload 更稳妥。

## 本轮未当作缺陷

- **M3 未装 Hermes/OpenClaw 桥**：范围外。
- **CSRF / 本机信任边界**：维持既有本地绑定设计。
- **多 Worker 父任务计数覆盖**：默认单 Worker，未复现。

## 范围

检查了 git vs origin、N13 静态跨文件调用、未提交 diff（service/database/llm/mcp/web/vault）、前轮 R5/R6/Grok 修复落点。未在本文件内重跑全量 pytest（以既有 400 passed 记录为参考）；未改正式配置与数据。正式双模冒烟见 `docs/live-mcp-dual-mode-20260929.md`。

## 建议优先级

1. 修 Major-2（校正片段 ID 映射，live 已复现）并补分片单测。
2. 修 Major-1（失败重试清理 `analysis_candidate`）并补测。  
2. 将未提交修复整理为本地 commit（不 push，除非用户要求）。  
4. Nit-1 仅在出现丢租约现场时再动。
