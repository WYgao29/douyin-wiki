# B1 最小块超限降级修复（2026-09-28）

## 改了什么

文件：`src/douyin_wiki/adapters/llm.py`、`tests/test_service.py`

1. **字符级硬切**：抽出 `_split_transcript_text`。标点切分优先；否则按字符对半切。仅当不足 2 字时视为不可再切（原先 `< min_chars*2` 即放弃）。
2. **校正最终降级**：`correct_transcript` 在单块 `ModelLimitError` 且不可再切时，**保留原文**、写入明确 `ReviewIssue`（「跳过该块校正并保留原文」），**不再整单失败**。
3. **分析对称处理**：`analyze` 对不可再切块返回带 `open_questions` 的降级结果；两段汇总仍超限时 `_soft_merge_analysis_partials` 拼接，避免整单失败。
4. **保留**多段预算切分 / 列表二分；放宽 `max_limit_splits` 以覆盖字符级切分次数。
5. 单测：最小块由「期望抛错」改为「跳过+告警」；新增短段硬切成功用例。

## 测试结果

### 单元测试

`.venv/bin/pytest` 相关用例 **通过**（含 `test_correction_empty_input_and_minimum_piece_limit`、`test_correction_hard_splits_short_single_segment_before_skip`、`test_analysis_reports_unsplittable_output_limit` 等；更广 LLM 相关 24 passed）。

### 正式环境 live（provider）

- 配置：`analysis_mode=provider`（本已是，未改）。
- 可编辑安装：`uv pip install -e .`；worker LaunchAgent 已 `kickstart`；plist 已有 `PYTHONUNBUFFERED=1`。
- 视频：`https://www.douyin.com/video/7689429622975314067`（~42s）
- Job：`b3b714c322384c68b25d1c02be8857c3`
- 校正阶段：遇限后切至 3→4 块并完成，**未**再出现 `model_output_limit` 整单失败。
- 一度 `需要人工复核`（模型疑点）；`--accept-uncertain` 后继续分析。
- 终态：**`已完成`**，entry `dy-7689429622975314067`。
- 清理：trash → 彻底删除该 entry；删除 job 记录。正式 `analysis_mode` 仍为 `provider`。

## 提交

commit `34d64419f3998cb22e4ab517686798963e7c528b`（仅 B1 相关文件；未 push）。
