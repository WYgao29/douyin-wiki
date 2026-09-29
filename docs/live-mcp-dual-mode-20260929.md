# 正式环境 MCP 双模冒烟报告（2026-09-29）

- **时间**：2026-09-29 08:23–08:42（CST / Asia/Shanghai）
- **环境**：正式 vault `/Users/weisengao/Documents/Obsidian/抖库` + LaunchAgent worker/web
- **入口**：MCP 等价（`DouyinWikiService.capture_douyin` + CLI `gateway submit-*`）
- **整库审查**：见 [whole-project-review-20260929-r5.md](whole-project-review-20260929-r5.md)
- **证据**：`.test-live-dual-20260929/results.json`、`continue.log`
- **未 push**；测后已恢复 `analysis_mode=provider` 并清理测试资料

## 视频与矩阵映射

| # | modal_id | URL |
|---|---|---|
| V1 | `7685755290243256689` | `https://www.douyin.com/video/7685755290243256689` |
| V2 | `7660045462695521974` | `https://www.douyin.com/video/7660045462695521974` |

| 跑次 | 模式 | 视频 | 结果 | 耗时 | entry / 备注 |
|---|---|---|---|---|---|
| 1 | **provider**（本机 oMLX） | V1 | **completed_with_warnings** | ~281s | `dy-768575…`；ASR SenseVoice + OCR RapidOCR；证据 prune 警告 2 条 |
| 2 | **provider** | V2 | **failed** | ~187s | `external_tool_error`：校正结果含未知/重复片段 ID `80`（105 分片预算路径） |
| 3 | **gateway**（stub） | V1 | **completed** | ~67s | → awaiting ~42s → identity 校正 + stub 分析 |
| 4 | **gateway** | V2 | **completed** | ~72s | → awaiting ~60s；原稿 105 段 identity 校正成功 |

**判定**：两种模式均已在正式环境跑通至少一条完整闭环；**2×2 中 3/4 成功**。Provider@V2 暴露长字幕校正 ID 校验缺陷（见审查 Major-2）。

## 配置与 Worker

1. 备份：`~/Library/Application Support/douyin-wiki/config.toml.bak-dual-mode-20260929-082347`（原 `analysis_mode=provider`）。
2. Provider 段：`configure-analysis-mode provider`（显式 reload LaunchAgent）。
3. Gateway 段：临时 `gateway`；每段结束后及最终均恢复 **provider**。
4. Worker：仅 `com.local.douyin-wiki.worker`（`--forever` LaunchAgent）；**未**另起 sidecar。
5. 可编辑安装：`.venv` → 本仓库 `src/`；web 仍在 `127.0.0.1:8765`。
6. 测后 `diff` 与备份一致；`analysis_mode=provider`。

## Provider 要点

- V1：媒体约 2 分钟内完成；本机 Qwen 校正+分析约 5 分钟；入库成功，带证据移除警告。
- V2：媒体成功；校正阶段 `total_chunks=105` 时失败：`模型校正结果包含未知或重复片段 ID：80`（`adapters/llm.py` 校验）。同视频在 gateway 用 identity 校正可完成，说明媒体链路正常、问题在 provider 校正分片/ID 映射。

## Gateway 要点

- 两视频均：`queued`→下载→转写→**awaiting_agent_analysis**（phase=`transcript_correction`）。
- `list_job_events` / `get_analysis_context` 可用；CLI `submit-correction`（identity）→ `submit-analysis`（最小 `other` stub）→ worker 入库 **completed**。
- V2 原稿 105 段整集提交通过（对比 provider 同分片路径失败）。

## 清理与恢复

- 最终 trash → permanently_delete 测试 entry；删除对应 job / events。
- 中途为避免同 `video_id` 冲突，gateway 前已 purge provider@V1 entry（结果已记入本报告）。
- 残留工作锁文件已删；vault/DB 中两 `video_id` **无 entry、无 job**。
- `analysis_mode` 已回 **provider**；LaunchAgent worker 仍在跑。

## 与审查的关系

- N13 已关闭；M3 跳过。
- 本轮 live 新增：**Provider 长字幕校正未知/重复片段 ID**（审查文档 Major-2）。
- 另有代码审查 Major-1：`FAILED` 重试不清理 `analysis_candidate`（本轮 live 未专门复现）。
