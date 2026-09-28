# 抖库端到端主流程（视频入库）

> 按当前代码重画（SenseVoice + RapidOCR；Whisper / Vision 仅初始化回退）。

```mermaid
flowchart TD
  A[入口: CLI / Web 127.0.0.1 / MCP] --> B[DouyinWikiService 建 Job]
  B --> C{内容类型}
  C -->|视频| D[下载媒体]
  C -->|图文| E[下载图片 + OCR]
  C -->|博主/收藏| F[列表清点 → 勾选 → 逐条子任务入库]

  D --> D1[Playwright CDN 拦截<br/>绑 play_addr / work_id]
  D1 -->|成功| D3[落盘 video + 保留 audio.mp4]
  D1 -->|失败| D2[yt-dlp 回退<br/>优先专用 Profile Cookie]
  D2 --> D3

  D3 --> G{有音轨?}
  G -->|是| H[FSMN-VAD → SenseVoiceSmall ASR]
  G -->|否| I[跳过转录 transcript_skipped]
  H --> J[抽帧 + RapidOCR PP-OCRv6 small]
  I --> J

  E --> E1[RapidOCR 识别原图<br/>失败可回退 Vision]
  E1 --> K[分析阶段]
  J --> K

  H -.->|仅 asr 初始化失败且 auto| H2[回退 Whisper<br/>mlx / CLI]
  J -.->|仅 ocr 初始化失败且 auto| J2[回退 macOS Vision]

  K --> L{分析模式}
  L -->|gateway| M[本地做完下载/ASR/OCR<br/>外部 Agent 校正+分析]
  L -->|provider| N[本机 LLM<br/>校正 → 结构化分析]
  L -->|local| O[本地降级整理]
  M --> P{需人工?}
  N --> P
  O --> P
  P -->|长视频批准 / ASR·OCR 复核| Q[暂停等 Web/CLI 决议]
  Q --> P
  P -->|否| R[写 SQLite + Obsidian Vault]
  R --> S[完成: 笔记 / FTS / 任务详情]
```

旁路（认证）：`auth douyin` → 专用 `browser_profile_dir` 登录；CDN 主路径用该 Profile；yt-dlp 侧路仅在 Profile 会话可用时优先，否则回退系统 Chrome Cookie。

说明：`auto` 只在 SenseVoice / RapidOCR **初始化失败**时回退；运行中失败不会换后端。OCR 推理失败写 `ocr_warning` 继续；ASR 推理失败会让任务失败。
