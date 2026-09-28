# 抖库入口面流程图（简版）

> `/Users/weisengao/Documents/ChatGPT/douyin-wiki/docs/entry-surfaces-flow.md`  
> 只回答一件事：**Web / CLI / MCP 怎么进同一套本机处理。**

```mermaid
flowchart TD
    A1[你用浏览器] --> W[Web 入口]
    A2[你用终端] --> C[CLI 入口]
    A3[外部 Agent] --> M[MCP 入口]

    W --> S[本机 service 编排]
    C --> S
    M --> S

    S --> Q[Worker 领任务]
    Q --> P[下载 · ASR · OCR · 入库]
    P --> V[写入 Vault 与 SQLite]
```

三句话：

1. **Web**：日常点选导入、授权、看任务。  
2. **CLI**：安装、排障、命令行采集。  
3. **MCP**：Agent 调工具。  

三条路进同一个 `service`，后面流程一样。导入后「谁做 AI 分析」是设置项，不画在这张图里。
