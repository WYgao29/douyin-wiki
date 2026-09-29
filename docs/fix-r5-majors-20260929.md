# R5 Majors 修复（2026-09-29）

## 修复

1. **Major-1 · 失败重试粘住 `analysis_candidate`**  
   `retry_job` 对 `FAILED` / `NEEDS_AUTH` 增加 `remove_artifacts={"analysis_candidate"}`，避免重试直接 `model_validate` 旧候选、不再请求模型。

2. **Major-2 · Provider 校正未知/重复片段 ID**  
   新增 `_normalize_correction_segments`：接受本地 `0..n-1`、常见的 1-based `1..n`、按序回退；忽略未知/重复多余项，缺口保留原文，避免整单失败。系统提示改为明确要求本地从 0 起的 id。

## 单测

`tests/test_r5_majors.py` + `test_correction_recovers_incomplete_or_duplicate_piece_ids`：1-based（含 max_items=80）、重复/缺口、空结果、失败重试清 candidate 并再次 analyze。相关校正/重试用例 **33 passed**。

## 正式 Provider 复测（仅 V2）

- 视频：`7660045462695521974`
- 结果：**completed_with_warnings**（约 658s），entry `dy-766004…`（测后已 trash + permanently_delete，job 已删）
- 对照：同视频此前在校正约 105 分片处因 `未知或重复片段 ID：80` 失败（~187s）
- 配置：测前备份 `config.toml.bak-r5-majors-20260929`；`analysis_mode=provider` 未改；Worker 经 `configure-analysis-mode provider` 热加载

## 提交

本地 commit，**未 push**。
