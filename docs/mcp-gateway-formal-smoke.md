# 正式环境 MCP≈Gateway 冒烟报告

- **测试时间**：2026-09-27 19:41–19:44（CST / Asia/Shanghai）
- **环境**：正式/生产 vault（非隔离测试库）
- **视频**：`https://www.douyin.com/video/7689429622975314067`（约 41.9s）
- **入口**：MCP 等价（`DouyinWikiService.capture_douyin` + CLI `gateway submit-*`，与 `mcp_server` 工具同路径）
- **analysis_mode**：临时 `gateway` → 测完恢复 `provider`
- **Worker**：官方 LaunchAgent `com.local.douyin-wiki.worker`（未起 sidecar `--forever`）
- **报告路径**：`docs/mcp-gateway-formal-smoke.md`

---

## 结论（摘要）

| 项 | 结果 |
|---|---|
| queued 等待 | **~2.0s** 即离开 queued（远低于 2–3min 失败阈值） |
| Worker 认领 | **是**（LaunchAgent pid 重启后认领） |
| → awaiting_agent_analysis | **~22.1s**（媒体 SenseVoice+RapidOCR 完成） |
| list_job_events | **有**（awaiting 未 ack 事件，含 gateway_context，可供 Agent 通知） |
| get_analysis_context | **成功**（phase=`transcript_correction`，含 transcript/ocr/schema） |
| 全闭环 | stub 校正+分析 → **completed**（再 ~10s） |
| 失败 | **无** |
| 清理 | entry/job/media 已删，正式库无残留 |
| 配置恢复 | `analysis_mode=provider`，与备份一致 |

**判定：正式 gateway 冒烟通过。**

---

## 1. 配置与 Worker

1. 备份：`~/Library/Application Support/douyin-wiki/config.toml.bak-formal-smoke-20260927-194113`  
   原 `analysis_mode = "provider"`。
2. `configure-analysis-mode gateway`（仅改 mode；vault/LLM/asr 未动）。
3. LaunchAgent worker 已在跑；`configure-analysis-mode` 触发重载，`kickstart` 确认存活（正式 vault）。
4. 测后 `configure-analysis-mode provider` 恢复；`diff` 与备份一致；worker 仍为 LaunchAgent。

---

## 2. 时间线（CST）

| 时间 | 事件 |
|---|---|
| 19:42:31 | MCP 等价 capture；job `1dd167378b9449a7a121b3761d2ccef8` → `queued@0` |
| 19:42:33 | **离开 queued**（~2.0s）→ `downloading@0.12`（worker 已认领） |
| 19:42:39 | `transcribing@0.34` |
| 19:42:53 | **`awaiting_agent_analysis@0.62`**（phase=`transcript_correction`，~22.1s） |
| 19:42:53+ | `list_job_events`：1 条未 ack，`status=awaiting_agent_analysis`，`gateway=formal-smoke` |
| 同刻 | `get_analysis_context`：transcript×1、ocr×11、`AnalysisResultV2` schema |
| 19:43:28 | CLI `gateway submit-correction`（identity）→ 仍 awaiting，phase→分析，progress 0.68 |
| 19:43:28 | CLI `gateway submit-analysis`（最小 stub）→ requeue `queued@0.68` |
| 19:43:30 | `analyzing@0.7` |
| 19:43:39 | **`completed@1.0`**，entry `dy-7689429622975314067` |
| 19:44:07 | trash → 彻底删除 entry；`delete_job_record`；清 lock；无 vault 残留 |
| 19:44:16 | 恢复 `provider` 并校验 |

媒体：ASR `sensevoice` / OCR `rapidocr`。无 queued 静默卡住。

---

## 3. Agent 通知面（list_job_events）

到达 awaiting 时未确认事件：

- `job_id=1dd167378b9449a7a121b3761d2ccef8`
- `status=awaiting_agent_analysis`
- `gateway_context={gateway:formal-smoke, channel:local, conversation_id:mcp-gateway-formal-smoke}`

完成后历史事件：awaiting（superseded）×2 + completed×1 → **Gateway/Agent 可被事件唤醒**。

---

## 4. 清理与恢复

- `trash_entry` → `permanently_delete_trashed_entry`（仅 `dy-7689429622975314067` / video `7689429622975314067`）
- 删除 job 记录；assets 随 trash 移除；删除 lock 文件
- 校验：entry/job 不存在，vault 无该 video_id 残留
- `analysis_mode` 已回 **provider**；LaunchAgent worker 仍在跑

---

## 5. 与隔离仿真对比（参考）

隔离 Mode B（同视频）约 24s 到 awaiting，但曾依赖 sidecar `worker --once`。  
本次正式库 + 官方 LaunchAgent：**~2s 出队、~22s 到 awaiting**，无 queued 假死。
