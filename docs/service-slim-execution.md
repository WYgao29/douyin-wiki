# service 瘦身执行记录

日期：2026-09-27 至 2026-09-28。范围为 [计划](service-slim-plan.md)中的 R1–R4、S0–S6；F1/F2 未执行。

## 交付状态

- R1：provider 校正支持单片段输入预分块、输出超限后有界缩小、请求局部片段 ID、稳定子块 checkpoint 与严格完整性校验；复核疑点合并回原片段。真实短片校正已成功，后续停在人工复核。
- R2：provider 等待分析资源和正在校正时使用 `analyzing` 状态与 `analysis_progress.phase=correction`；Web/operation 的阶段文案区分等待、校正和内容分析，终态不展示进行中校正。
- R3：保留持久事件和 `gateway monitor-events` 无模型监控接口，完善任意 Agent 安装时的 MCP、调度、重试、路由、重启与确认契约。用户决定安装时由所选 Agent 自行配置；本次未安装 Hermes/OpenClaw 专用调度，真实自动唤醒未验收。
- R4：通过 `mcp.call_tool` 测试视频校正→分析顺序、非法 schema、未知/重复片段 ID、复核门、图文直接分析及重新分析；文档示例按当前模型校验。
- S1–S6：分别迁至 `service_trash.py`、`service_maintenance.py`、`service_import.py`、`service_analysis.py`、`service_capture.py`、`service_entries.py`，统一入口 `DouyinWikiService` 保留。`service.py` 为 611 行。两个模块级 helper 分别移至 `review.py` 与 `service_analysis.py`。

## 基线和提交边界

开始时 `HEAD=5ff63fc17e60d0790b2b56ad6ccfc25b67612834`，`main` 超前 `origin/main` 11 个提交；工作区已有未提交的 ASR/OCR 功能、配置、测试及文档变更，含 `service.py`。实际测试解释器为 `uv run python --version` → Python 3.12.13，uv 0.12.19。

R1/R2 后、机械拆分前的 `service.py` 快照保存在 [service-before-split.py](../.test-r1-20260927/service-before-split.py)。最终 AST 核对：原有 121 个方法仍各有唯一归属，签名、装饰器与函数体均相同；两个搬迁的模块级 helper AST 也相同。因原工作区包含其他任务的未提交功能变更，本次未执行计划中的独立功能基线提交、每切片提交或 push，避免把无关内容混入提交。仓库当前交付物为工作区变更，供主 Agent review 后再决定如何分离提交。

## 离线验证

每个机械切片在定向测试后均运行 `uv run ruff check .` 和 `uv run pytest -m "not live"`；S1–S5 的完整离线结果为 359 passed、1 live deselected。R1/R2、R3/R4 测试补齐后的最终结果为 **365 passed、1 live deselected**，ruff 全绿。真实抖音 live 测试未纳入该离线数字。

## 隔离真实回归

测试输入为用户指定的 `https://www.douyin.com/user/self?from_tab_name=main&modal_id=7689429622975314067&showSubTab=video&showTab=favorite_collection`。测试配置及隔离 Vault 位于 [`.test-r1-20260927`](../.test-r1-20260927/)；配置基于先前的本机 provider 样本，使用 `Qwen3.6-35B-A3B-4bit`、`max_output_tokens=8192`、`enable_thinking=false`、`thinking_budget=0`。未修改正式配置或正式 Vault。

首轮发现该 `modal_id` 输入被解析为无作品 ID，已补充解析，规范 URL 为 `https://www.douyin.com/video/7689429622975314067`。隔离 job `7572797d6f4c4c23a107f97ac389b89c` 的媒体阶段由 SenseVoice 与 RapidOCR 完成，元数据时长 41.888 秒，原始逐字稿 1 段、242 字。校正的首次尝试因模型返回 ID `0`、当时内部请求却使用负数 ID 而失败，严格校验阻止了错误合并；随后改为请求局部 ID 并重试同一 job，没有重复下载媒体。

重试后 provider 校正完成 5/5 子块，持久 checkpoint 5 个；成功调用累计输入 2915 tokens、输出 738 tokens（不含失败请求），最后一次响应输入 586、输出 56 tokens。合并后仍为原 ID `0`、时间范围 `0–41870 ms`、242 字；本样本校正文本与原稿相同。最终状态 `needs_review@0.66`，有一条 ASR 人工疑点，尚未做内容分析或入库，隔离 Vault 的 entry 数为 0。没有代用户接受不确定内容。后续如需完成该 job，应由用户核对疑点，再走正常 `resolve_review` 和分析路径；本轮没有把停在复核点记作分析成功。

先前 [双模式仿真报告](mcp-dual-mode-sim-report.md)中的原 provider `model_output_limit` 只有错误码和 `analysis_progress`，未持久化失败响应 usage/finish_reason，不能倒推出具体截断细节。本次记录的 tokens 是成功子块响应数据。真实长片未测试；长逐字稿的预分块、缩小、重试、完整性和 checkpoint 由确定性离线测试覆盖。

## 2026-09-28 整库审查后续：M1 / M4

本轮基于上述未提交工作区继续修复，修改前的 `llm.py`、`setup.py`、相关测试和本文件快照保存在 `/tmp/douyin-wiki-m1-m4-before-20260928/`，供主 Agent 辨认本轮增量；不将相对 `HEAD` 的整份 `llm.py` diff 当成本轮改动。

- M1：provider `analyze` 对单个长字幕段按标点附近或中点预切，既按实际输入窗口检查，也用文本上限约束预期输出。内部片段携带原始 `source_segment_id`，请求 ID 仅在当前请求内从 0 编号；每块保留原始起止时间，不按字符比例推算子块时间。提示要求音频引文使用原段 `start_ms` 作为粗粒度定位。模型输入或输出超限时按列表或片内文本递归缩小，最多处理 256 次超限；最小文本块仍超限时带原段 ID 明确失败。checkpoint 指纹包含实际请求文本、ID、配置及提示；进度在整个初始块的子请求完成后前进。现有 OCR 选取、部分分析、合并及证据校验继续使用。两份部分结果连模型汇总都无法容纳时仍明确报预算错误，未用首块摘要冒充全片结果。
- M4：新生成的 worker 和 maintenance LaunchAgent plist 添加 `PYTHONUNBUFFERED=1`，仅改善文件日志及时性。未重新安装 plist、未重启正式进程，也不把缓冲解释成先前 sidecar 退出的根因。
- 定向测试覆盖单原段受控输入预算预切、文本拼接完整、输出超限分块、成功子块 checkpoint 复用、最小块有界失败、原段时间和引用映射经 Service 证据校验、现有多段及 OCR 图文路径。受控输入预算用例替换 `_fits` 来验证分支；原有 `test_analysis_respects_reduced_context_budget` 仍检查真实 `_fits`。LaunchAgent 测试读取生成 plist，验证 worker/maintenance 环境变量。
- 完整离线测试：`uv run pytest -m 'not live'` 为 **369 passed、1 live deselected**（39.36 秒）；`uv run ruff check .` 与 `git diff --check` 通过。真实长段输入及合并预算极限未做 provider 实测。
- 对用户指定视频保存的隔离校正稿单独调用真实 provider `analyze`，配置为 `Qwen3.6-35B-A3B-4bit`、`max_output_tokens=8192`、`enable_thinking=false`、`thinking_budget=0`，输入为 1 段 / 242 字、OCR 11 条；约 43 秒返回标题“AI博主推荐与评价”、4 章、5 个知识原子；Service `_validate_analysis_evidence` 通过，进度为 analysis 0/1 → 1/1。进程输出留在本任务工具记录，未保存模型完整 JSON，未为补证重复请求。该输入未触发新增长段预切；结果没有回写 job。隔离 job `7572797d6f4c4c23a107f97ac389b89c` 仍停在 `needs_review`，不能视为用户复核或全链路入库完成。

### 建议提交边界（本轮未执行）

先保留当前 `main@5ff63fc` 及其超前远端的 11 个既有提交。未提交树宜按 ASR/OCR 后端与配置、R1 校正及 URL 解析、R2 UX、M1 分析预算、M4 日志配置、S1–S6 service 搬迁、文档分别审查。`llm.py` 与 `tests/test_service.py` 同时含 R1、M1 和其他测试，提交时需按 hunk 精确暂存；`tests/test_interfaces.py` 同时含 Gateway/R4 与 M4。service 搬迁应以当前工作区的功能版本为基线核对，不直接暂存整个工作区。`.test-*`、`docs/_mcp-sim-evidence-*`、根目录数字实验目录均保留为隔离材料，不进入业务提交。本轮未 `git add`、commit 或 push。


## 缺少 ASR 置信度的复核策略修复

GPT-6-Sol / medium 子 Agent 因额度限制中断，本次由主 Agent 接手。修改前快照位于 `/var/folders/s_/pbv327pj05j9rjk26h50qqrr0000gn/T/douyin-confidence-before-h4m6x8iq/`；既有工作区变更均保留。

缺少 `confidence` / `avg_logprob` 不再单独生成疑点。保留实际低分规则、provider 的具体 LLM 疑点与 Gateway 提交的具体疑点；没有给 SenseVoice 填充虚构分数，也没有代用户确认历史任务。新任务以 `media_provenance.asr.confidence_status` / `confidence_note` 记录分数可用性，Web 详情和 MCP 分析上下文均可读取。无其他异常时正常 `completed`，不因分数缺失改成 `completed_with_warnings`。

关键数字、金额与画面冲突继续通过现有校正输入（原稿 + OCR）和疑点提交流程复核，本轮没有新增不可靠的文本匹配器、第二 ASR 调用或自动可信分数。更细音频分段、词级时间戳、CTC 分数校准留作后续。现有 `.test-r1-20260927` 中的历史 `needs_review` 任务未修改；本次测试使用临时隔离库和确定性媒体/模型替身，不宣称新的真实视频全链路完成。

验证：定向测试 15 passed；最终 `uv run pytest -m 'not live' -q` 为 **379 passed、1 deselected**（39.87 秒）。`uv run ruff check .`、`git diff --check` 与 `node --check src/douyin_wiki/webapp/static/jobs.js` 均通过。新增端到端替身测试同时走 provider 与 MCP Gateway：无分数且有数字正常入库、具体价格歧义仍复核、Web/MCP 提示一致；真实低分规则另有回归。只读复查历史隔离 job 仍为 `needs_review@0.66` 且保留 1 条 open 疑点。未提交或推送。

## 2026-09-28 R2 复审后续修复（离线）

- Web LaunchAgent 安装器与 worker/maintenance 对齐：新生成的 web plist 设置 `PYTHONUNBUFFERED=1`。已安装的 plist 不会因源码改动自动刷新；本轮未重装或重启正式服务。
- `analyze` 发生模型预算超限而递归拆分时，按当前真实叶子请求数更新 `analysis_progress`，每个成功子请求都报告完成数；OCR-only 拆分同样覆盖。Service 仍用 `max(current_progress, target)` 保持整体进度百分比不倒退。
- `.gitignore` 精确忽略已知的 `.test-backup-20260927/`、`.test-r1-20260927/`、`.test-r2-20260928/`、`docs/_mcp-sim-evidence-20260927/` 和根目录 `7686768044248452390/`。文件原样保留；未扩大为全部 `.test-*` 或数字目录。
- FunASR 1.4.16 中 `trust_remote_code=True` 会执行模型仓代码，并可能安装其中的 `requirements.txt`。本机缓存的标准 SenseVoiceSmall 与 FSMN-VAD 使用 `trust_remote_code=False` 均成功加载，权重键匹配；两个 `AutoModel` 调用已显式关闭远端代码。依赖模型仓自定义 Python 的自定义 ASR 配置会在显式 `sensevoice` 模式报加载失败，`auto` 模式按原有规则回退 Whisper。此改动未固定模型权重 revision，也未消除其他模型供应链风险。
- Web 写请求现在拒绝 `Sec-Fetch-Site: cross-site` 或 `same-site`，即使缺少 Origin/Referer；同源浏览器请求及无这三个头的本机 API 客户端仍按现有契约工作。无头的本机进程仍可调用写 API，当前 Web 未引入鉴权凭据，这一信任边界未改变。
- 定向回归 5 passed；修改文件 `ruff check`、`git diff --check` 通过；`git check-ignore` 确认隔离目录被精确规则覆盖。此处仅记录离线结果，不代替正式 LaunchAgent 更新或真实任务全链路验收。

### 主 Agent review 与真实视频回归

主 Agent review 后补齐了测试对可选 `torch` 依赖的隔离，确认同源 Fetch Metadata 不被拦截，且分片进度仍表示真实请求批次数。`unknown_confidence` 保留为兼容入口；Gateway 专用事件桥按用户确定的范围留给安装时的 Agent 配置，不算本轮缺失实现。最小块预算耗尽仍明确失败，不静默降级或丢弃文本。未修改两份历史整库 review 文件，其状态以本节后续证据补充。

使用用户指定作品 `7689429622975314067` 的已下载真实媒体，在全新的 [R2 隔离目录](../.test-r2-20260928/) 创建任务 `6f50cfaa2e624ded9885d8fa07c0a240`。本次复用已解析 URL、元数据及媒体文件，没有重新验收 URL 解析或下载；没有复用 ASR、OCR、校正结果或模型 checkpoint。实际执行新的 SenseVoice（关闭远端代码）、RapidOCR 和本机 Qwen provider，耗时 23.44 秒，得到 1 段原稿、11 条 OCR。校正 1 次成功请求，输入 707 tokens、输出 335 tokens，没有输出超限。

终态为 `needs_review@0.66`，模型提出“免费屏替→免费平替”“提示时→提示词”及断句等具体疑点；缺少置信度只作为 provenance 说明，没有制造旧式缺分疑点。本次未确认这些内容，未调用后续分析和入库，不能宣称真实全链路 `completed`。原隔离任务 `7572797d6f4c4c23a107f97ac389b89c` 的状态和 artifacts 经只读查询前后完全一致。复现脚本、运行日志、结构化结果分别保存在隔离目录的 `run_sample.py`、`run.log`、`result.json`；它们由精确忽略规则排除，不进入业务提交。隔离任务不会显示在正式 Web 的 Vault 中。

只读检查已安装的 worker、web、maintenance plist 均尚无 `PYTHONUNBUFFERED`；本轮没有重装 LaunchAgent。可编辑安装仅说明源码路径，不作为正在运行的进程已加载新代码的证据。正式服务运行时验收、真实长段 provider 验证以及混合工作树的按主题提交仍未完成；未提交、未推送。

最终主 Agent 独立验证：`uv run pytest -m 'not live' -q` 为 **382 passed、1 deselected**（39.29 秒）；`uv run ruff check .` 与 `git diff --check` 通过。没有发现本轮新增的阻断问题；以上真实样本的复核门仍按预期保留。

## R3 后续（2026-09-28）

- CSRF：写请求改为必须带匹配 Host 的 Origin 或 Referer；继续拒绝 `Sec-Fetch-Site` 的 cross-site/same-site。
- 分析进度：递归缩批时父批计入 `completed_chunks` 且 `total_chunks += 2`，叶子成功后分母/分子一致。
- ASR/VAD：`model_revision` 默认钉住 SenseVoice `model.pt` 提交与 FSMN-VAD `v2.0.4`。
- `doctor`：按配置的 asr/ocr provider 检查 funasr/rapidocr 与 Whisper/Swift。
- 删除 `detect_review_issues(..., unknown_confidence=)` 死参。

