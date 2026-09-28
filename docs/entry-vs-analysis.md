# 入口面 × 分析模式

两轴正交：左边怎么进门，右边谁做 AI 分析。任意入口都能配任意分析模式。

```mermaid
flowchart LR
  subgraph entry ["入口面 · 怎么连上"]
    W[Web]
    C[CLI]
    M[MCP]
  end

  core[本机：建任务 → 下载 → ASR → OCR]

  subgraph mode ["分析模式 · 谁整理"]
    G[gateway<br/>外部 Agent 校正+分析]
    P[provider<br/>本机 LLM]
    L[local<br/>本地降级]
  end

  out[Vault + SQLite]

  W --> core
  C --> core
  M --> core
  core --> G
  core --> P
  core --> L
  G --> out
  P --> out
  L --> out
```

| 组合举例 | 含义 |
|---|---|
| Web + provider | 网页导入，本机模型自动整理（你当前常见用法） |
| MCP + gateway | Agent 调 MCP 建任务，校正分析回 Agent 会话 |
| CLI + local | 命令行导入，不调外部模型 |
