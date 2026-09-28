# 抖库 ASR / OCR 替换实施方案

> 更新：2026-09-27。代码已按本方案接入；旧格式配置默认保持 Whisper / Vision。本机配置已显式切换到 `auto`。

## 实施记录

已新增可选依赖、配置迁移、SenseVoice 手动 VAD 分段、RapidOCR small、后端选择、任务详情中的模型溯源和无可信分数时的复核路径。项目环境的 90 秒样本得到 6 个 ASR 段；ONNX Runtime CPU 跑 16 帧得到 412 行、耗时约 12.6 秒。另用该音频和样本帧制作隔离视频，完成导入并得到 6 段逐字稿、3 条 OCR；隔离图文用 3 张样本图完成导入，图片序号为 1–3。项目完整测试为 354 passed、1 skipped，随后新增的任务详情测试与相关测试 25 passed；Ruff、`node --check` 与 `git diff --check` 通过。项目环境还需 `kaldi-native-fbank` 才能运行 FunASR VAD，已列入 ASR extra。RapidOCR 在本机启用 CoreML 时输出模型形状错误日志，当前实现明确使用 ONNX Runtime CPU。本机 `config.toml` 已显式设置两个 `auto`，Web / Worker 已重启且健康；未重跑已入库资料。

## 结论

新安装优先使用 **SenseVoiceSmall + FSMN-VAD** 转录，使用 **RapidOCR 的 PP-OCRv6 small 检测与识别模型**识字；保留 Whisper 和 macOS Vision 作为可显式选择的后端。先在隔离环境验证模型加载、输出结构、许可证和质量，再锁定依赖版本及权重来源。仓库内的 [16 帧 OCR 对照](../7686768044248452390/OCR_V6_MEDIUM_VS_SMALL.md)支持先试 small：它在该样本的中文正文上较稳，但报告只覆盖一条视频，不能当作图文和其他视频的最终验收。

**现有配置继续使用原引擎。**已有 `config.toml` 没有新 `asr_provider` / `ocr_provider` 时，仍走 Whisper / Vision；用户明确增加新字段才切换。新生成的配置使用 `auto`，优先新后端，未安装可选依赖或模型不可加载时选旧后端。显式指定 `sensevoice` 或 `rapidocr` 时不静默换模型。这样既能让新安装默认使用新栈，又不让旧 Vault 的下一次导入悄悄改变产物。

## 当前代码边界

| 环节 | 实际行为 | 实施影响 |
|---|---|---|
| 配置 | `MediaSettings` 只有 `whisper_provider/model/cli_model`；`load_config` 直接验证 TOML，`render_default_config` 写出全部字段 | 新增引擎选择字段；`whisper_provider` 仅保留为 Whisper 内部的 MLX/CLI 选择，不能映射为 `asr_provider` |
| 视频 | `FFmpegMediaProcessor` 从优先保留的 `audio.*` 或视频提取 16 kHz、单声道 PCM WAV；无音轨记 `transcript_skipped`，仍抽帧 OCR | 不改变下载及抽音频链；新 ASR 消费同一 WAV |
| ASR | `WhisperTranscriber` 的 `auto` 只在 MLX 包缺失时转 CLI；推理异常通常会使任务失败 | 新引擎选择与 Whisper 内部选择分开；保留失败可见性 |
| OCR | `VisionOCR` 经 Swift 返回每帧一条 `OCRObservation`；视频和图文共用 `self.ocr` | 新后端同样按输入图片返回至多一条非空记录，保留帧时间或图片序号 |
| 错误 | service 只捕获 OCR 的 `ExternalToolError` 并写 `ocr_warning`；ASR 错误会使任务失败 | 所有新后端预期错误归一为 `ExternalToolError`；不能把 OCR 失败当成无文字 |
| 持久化 | 视频在 `transcript_raw` 尚不存在时一起做 ASR、抽帧和 OCR；图文在 `ocr` 尚不存在时识字；`reanalyze` 复用已有结果 | 默认切换只影响后续新导入，不会重跑、覆盖已有原始资料 |
| 下游 | `TranscriptSegment` 和 `OCRObservation` 经 Pydantic 验证；LLM 仅用文字及定位字段；复核按 `confidence < 0.55` 或 `avg_logprob < -1.0` 触发 | 保持字段语义；新模型分数必须先确认含义，不能伪造 Whisper 风格 `avg_logprob` |

主要入口：`src/douyin_wiki/config.py`、`src/douyin_wiki/adapters/media.py`、`src/douyin_wiki/service.py`、`src/douyin_wiki/models.py`、`src/douyin_wiki/review.py`。图文 OCR 的 `image_index` 在 service 中由 `source_index` 或图片路径映射，并清空 `timestamp_ms`；视频必须保留真实毫秒时间。下游已有按行去重的 **LLM 输入过滤**，原始 OCR 不应在识别层做跨帧去重。

工作区目前还有下载器及其测试的未提交改动；本次实施应与它们分开审查，避免把音轨采集问题误判成 ASR 模型问题。

## 配置和迁移

建议在 `[media]` 增加最少的选择项：

```toml
asr_provider = "auto"       # auto | sensevoice | whisper
ocr_provider = "auto"       # auto | rapidocr | vision
asr_model = "iic/SenseVoiceSmall"
vad_model = "fsmn-vad"      # 实施时换成验证过的准确模型标识
asr_device = "auto"         # auto | cpu | mps；可用性须实测
# 保留现有 whisper_provider、whisper_model、whisper_cli_model 和抽帧配置
```

`auto` 是**引擎选择**，不是 `whisper_provider=auto` 的别名。`load_config` 读取旧文件时按“字段是否存在”判别：缺 `asr_provider` → `whisper`，缺 `ocr_provider` → `vision`。不能仅依赖 Pydantic 默认值，因为旧配置往往已经写有 `whisper_provider=auto`。新 `render_default_config` 明确写入两个 `auto`。用受限取值类型拒绝拼写错误，并给旧配置、部分指定、完整新配置各做一次加载与渲染回归。

暂不把 ONNX 文件名写进默认 TOML。先固定并验证 RapidOCR 版本、该版本的模型配置 API、small det/rec 及分类器的下载来源和缓存行为；如果实际部署需要自定义路径，再加显式路径选项。依赖放在 `asr`、`ocr` optional extras，保留现有 `mlx` extra；锁文件只写入实测兼容版本，不在方案里猜版本号。记录权重许可证及再分发要求，权重不直接提交仓库。

## 实现顺序

1. **锁定基线与模型输入。**在隔离环境用同一段当前可正常抽出的 16 kHz WAV 和现有 16 帧跑模型。记录 FunASR/VAD 与 RapidOCR 的实际 API、返回结构、设备支持、缓存路径、首次下载和 warm 启动耗时。为 ASR 挑选带人工校对的片段，特别检查开头、数字、专有名词、否定词和静音；现有仓库只有 OCR 对照报告，方案原文提到的 `ASR_*_REPORT.md` 不在本仓库，不能将其当成已复核证据。
2. **接入边界。**为 `transcribe(audio_path, output_dir)` 和 `recognize(list[(source_index, Path)])` 加轻量协议或等价类型；保留 service 的测试注入方式。由配置工厂组装引擎及 `auto` 的选择逻辑。`service.py` 中 `work_dir / "whisper"` 改为中性的输出目录；音频、帧提取和任务状态不变。
3. **实现 ASR。**懒加载 SenseVoice 与 FSMN-VAD，在现有 `media_semaphore` 下执行阻塞推理；必要时用实例锁防止同进程重复初始化。将 VAD 毫秒区间裁剪到 WAV 时长，按区间识别并生成按时间排序、`start_ms < end_ms`、ID 唯一的 `TranscriptSegment`。去掉模型控制标签，但保留原话和标点；仅当模型确实给出有明确语义、可校准的分数时填写 `confidence`，否则保留 `None`，`avg_logprob` 留空。空 VAD/空文本、短音频和识别异常必须区分并记录；不能因一次整文件识别把长视频压成一段。
4. **实现 OCR。**RapidOCR 使用经过验证的 PP-OCRv6 small det/rec 配置；每张图按阅读顺序合并非空行，输出一条 observation。视频沿用输入的毫秒 `source_index`/`timestamp_ms`；图文沿用 1-based `source_index`，由 service 映射为 `image_index`。`image_path` 保持输入路径，使图文映射能核对。仅当行分数语义明确时聚合为 `[0,1]` 的 `confidence`；不做会删除正文或字幕的全局过滤。
5. **打通复核与溯源。**当前规则对 `confidence=None` 且 `avg_logprob=None` 的 ASR 段不会创建低置信复核项。因此必须按实测分数校准阈值，或为无分数且包含关键数字/专有词的段加入明确的“置信度未知”复核路径；不能填一个常数来压制复核。记录每次新采集实际使用的 ASR/OCR 后端与模型、是否回退及原因，优先存在任务 artifacts，并使用户可在任务结果中查看。现有 `model_provenance` 只描述分析模型，不能冒充 ASR/OCR 溯源。
6. **更新用户文档。**README 的安装、模型下载与配置迁移说明，以及“图文仅 Vision OCR”等旧表述同步更新；完成上线后再写 CHANGELOG。保留 Vision 脚本和 Whisper 路径以供显式选择与回滚。

## 失败与回退规则

| 配置 | 模型/依赖不可用 | 推理已开始后失败 |
|---|---|---|
| `asr_provider=auto` | 记录原因并尝试 Whisper；Whisper 仍遵循自己的 MLX/CLI 规则 | 任务失败并保留错误；避免悄悄用另一个模型生成证据 |
| `asr_provider=sensevoice` | 明确失败，提示安装或预下载 | 明确失败 |
| `asr_provider=whisper` | 按当前 Whisper 行为处理 | 按当前 Whisper 行为处理 |
| `ocr_provider=auto` | 记录原因并尝试 Vision；Vision 不可用则由 service 写 `ocr_warning` | 转为 `ExternalToolError`，由 service 写 `ocr_warning` |
| `ocr_provider=rapidocr` / `vision` | 明确报错，由 service 写 `ocr_warning` | 同左 |

`auto` 仅在选定后端**尚未开始处理输入**且确认不可用时回退；识别返回空列表属于可能的正常结果，不能以“空”触发回退。初始化失败和运行中失败应有可观察的不同日志。OCR 的软失败保持现有任务语义；ASR 不新增静默空稿回退。若用户要求 OCR 显式选择也必须让整个任务失败，需要另行调整 service 当前统一软失败规则。

## 验收与发布

先做与改动直接相关的自动验证：旧配置兼容、`auto`/显式后端选择、缺包与加载失败、VAD 时间边界、标签清洗、重复/空段、OCR 多行和图文序号、`ExternalToolError` 到 `ocr_warning`、无音轨视频仍做 OCR、低置信与无分数复核。再跑项目现有测试、Ruff、`git diff --check`。

真实验收分两路：

- **视频**：用同一份下载媒体分别跑旧/新后端，核对抽音轨来源、人工校对片段的错漏字、否定词、时间段覆盖、复核项、帧 OCR 和下游证据定位。不能只用“段数大于 1”或总字数判断质量。
- **图文**：至少选一篇多图笔记，检查每张图片的 `image_index`、原图关联、OCR 正文和入库页面。16 帧报告不能替代图文验收。

记录两路的冷/热启动、耗时与峰值内存，再根据实际结果决定是否将新模型保持为 `auto` 首选。完整导入应在隔离 Vault 或不覆盖已有资料的测试任务上完成；`reanalyze` 不会重跑 ASR/OCR，不能用来验证切换。保留显式回滚为 `asr_provider="whisper"`、`ocr_provider="vision"` 的步骤；不批量重跑历史内容。当前已切换本机配置，尚未用真实作品完成一次全流程新导入。
