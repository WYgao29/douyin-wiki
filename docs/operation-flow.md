# 抖库操作流程图

> 根目录：`/Users/weisengao/Documents/ChatGPT/douyin-wiki`  
> 视角：**你怎么操作 → 系统做什么**（本机 `analysis_mode=provider`）。  
> 业务泳道细图见 `docs/current-project-flow.md`。

```mermaid
flowchart TD
    start([打开 Web / CLI]) --> choose{要做什么?}

    choose -->|单条分享链接| single[粘贴链接 + 可选灵感 / 保留媒体]
    choose -->|博主或收藏夹| batch[发起批量导入]
    choose -->|已有资料| reuse[搜索 / 打开资料 / 再分析 / 提醒]

    single --> q1[入队 capture 任务]
    batch --> inv[清点作品清单]
    inv --> pick[勾选作品并确认]
    pick --> kids[为每条作品建子任务]
    kids --> q1

    q1 --> worker[Worker 领取]
    worker --> resolve[解析链接与类型]
    resolve --> dedupe{作品是否已入库?}

    dedupe -->|是且完整| done[完成：返回已有资料]
    dedupe -->|新视频| vdl[下载视频 / 封面 / 音轨]
    dedupe -->|新图文| idl[下载原图与正文]

    vdl --> auth{授权是否有效?}
    idl --> auth
    auth -->|否| needsAuth[NEEDS_AUTH]
    needsAuth --> login[你在浏览器登录后重试]
    login --> worker

    auth -->|是| dur{视频是否超时长门槛?}
    dur -->|需确认| wait[WAITING_CONFIRMATION]
    wait --> approve[你确认继续]
    approve --> worker
    dur -->|可继续 / 图文跳过| media[媒体阶段]

    media --> asr[有音轨：VAD → SenseVoice ASR]
    media --> ocr[抽帧或原图：RapidOCR]
    asr --> merge[写入 transcript + OCR + 溯源]
    ocr --> merge

    merge --> corr[LLM 校正逐字稿·仅视频]
    corr --> rev{有疑点?}
    ocr --> irev{图文 OCR 有疑点?}
    rev -->|是| nrev[NEEDS_REVIEW：你校对]
    irev -->|是| nrev
    nrev --> cont[继续分析]
    rev -->|否| cont
    irev -->|否| cont

    cont --> analyze[LLM 结构化分析 + 向量关联]
    analyze --> store[写入 Vault + SQLite]
    store --> done
    done --> reuse

    classDef you fill:#fff4e5,stroke:#c27803;
    class login,approve,nrev,pick,single,batch,reuse you;
```

## 操作速查

| 你的操作 | 典型结果状态 |
|---|---|
| 提交单条链接 | `QUEUED` → … → `COMPLETED` |
| 批量清点后勾选导入 | 父任务 `NEEDS_SELECTION` → 子任务走同一采集 |
| 登录失效 | `NEEDS_AUTH` → 登录后 `retry` |
| 超长视频 / AI 分析门槛 | `WAITING_CONFIRMATION` → 确认 |
| 低置信 ASR / OCR 疑点 | `NEEDS_REVIEW` → 校对后继续 |
| 再分析 | 复用逐字稿与 OCR，不重跑 ASR/OCR |

回退：`asr_provider`/`ocr_provider=auto` 时，仅 **初始化失败** 才 Whisper / Vision。
