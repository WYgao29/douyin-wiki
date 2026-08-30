# macOS 授权引导与短链类型校正设计

## 背景

抖库的采集入口目前是 Agent 对话，不是本机 Web 页面。Web 仅负责展示和检索已入库笔记，因此授权过期时不能依赖网页弹框。

一次真实采集中，两条图文短链在 HTTP 重定向链的中间地址包含 `/video/<作品ID>`，最终地址均为 `/note/<作品ID>`。现有解析器在第一次发现作品 ID 时立即返回，把图文误判为视频，继而进入视频 Cookie 检查并产生 `cookie_required`。用户当时已经在 Chrome 登录，重复视频授权不能解决该根因。

本设计同时解决：

1. 短链必须以最终 HTTP 地址校正视频或图文类型。
2. 真正缺少授权或授权过期时，使用 macOS 系统弹框引导用户，并在验证成功后自动重试暂停任务。

## 目标

- 短链重定向链中先出现 `/video/`、最终出现 `/note/` 时，结果必须为 `source_kind=image_note`。
- 任务因视频、图文或博主授权进入 `needs_auth` 后，后台 Worker 不阻塞，并最多启动一个对应授权范围的引导助手。
- 用户确认后自动打开正确的浏览器会话，验证成功后自动重试同一授权范围内仍处于 `needs_auth` 的任务。
- 用户取消、超时、无图形会话或系统弹框失败时，保留现有 CLI/Agent 修复命令作为降级路径。
- 全流程不读取、返回、记录或展示 Cookie 值。

## 非目标

- 不增加 Web 端的视频或图文提交入口。
- 不把授权弹框放入笔记展示网页。
- 不新增第二个常驻 LaunchAgent。
- 不自动处理验证码，也不绕过抖音登录或安全验证。
- 不自动重试普通 `failed`、`needs_review` 或 `waiting_confirmation` 任务。
- 不修改真实历史任务；部署后只影响新发生的授权状态转换。

## 方案选择

采用独立、短生命周期的授权助手进程。

Worker 只负责先持久化 `needs_auth`，然后以无 shell 的参数数组启动助手进程并继续处理队列。助手进程负责同一授权范围的弹框去重、浏览器打开、授权验证和任务重试；完成、取消或超时后退出。

不采用 Worker 内等待，因为用户操作可能阻塞整个采集队列。不采用额外 LaunchAgent，因为当前需求不需要第二个常驻服务及其安装、升级和卸载生命周期。

## 短链解析设计

### 当前问题

`DouyinShareResolver.resolve()` 在重定向循环中一旦从 `Location` 发现作品 ID 就立即调用 `_resolved()` 返回。对以下链路，它会停在第二行：

```text
v.douyin.com/<短码>
  → .../video/<作品ID>
  → www.douyin.com/note/<作品ID>?previous_page=web_code_link
```

### 新行为

- 对已经是完整 `/video/`、`/note/` 或 `/gallery/` 的输入，继续直接解析，不增加网络请求。
- 对短链，跟随最多 8 次 HTTP 重定向；每一步记录 `redirect_chain`，但不因中间地址出现作品 ID 而提前返回。
- 保存重定向链中最近一次可识别的作品 ID 和类型。
- 遇到没有 `Location` 的最终响应后，优先使用 `response.url` 中的身份；否则使用最近一次可识别身份。
- canonical URL 只保留 `https://www.douyin.com/video/<ID>` 或 `https://www.douyin.com/note/<ID>`，不保留跟踪查询参数。
- 如果同一重定向链出现不同作品 ID，视为无效分享链接并返回 `invalid_share_text`，避免把一个作品的类型套到另一个作品上。
- 超过重定向上限仍有 `Location` 时返回明确的“重定向次数过多”，不使用尚未确认的中间身份。

### 回归场景

至少覆盖：

- 短链最终为视频。
- 短链最终为图文。
- 中间为视频、最终为同 ID 图文。
- 中间为图文、最终为同 ID 视频，以最终地址为准。
- 重定向链出现不同作品 ID 时拒绝。
- 完整 canonical URL 不联网并保持现有行为。

## macOS 授权助手设计

### 配置

在 `AppConfig` 增加 `AuthGuidanceSettings`：

```python
class AuthGuidanceSettings(BaseModel):
    enabled: bool = True
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    poll_seconds: float = Field(default=5, ge=2, le=30)
```

默认配置新增：

```toml
[auth_guidance]
enabled = true
timeout_seconds = 600
poll_seconds = 5
```

功能仅在 macOS 且 `enabled=true` 时启动。非 macOS 或无法访问图形会话时直接降级，不改变任务状态。

### 组件边界

新增 `src/douyin_wiki/auth_guidance.py`，职责如下：

- `AuthGuidanceLauncher`：Worker 侧的轻量启动器；把 scope 映射为授权通道，校验 job ID，并启动独立助手进程。
- `run_auth_guidance()`：助手进程入口；获取范围锁、展示弹框、执行授权、验证结果并重试任务。
- `MacOSDialog`：只封装 `osascript` 的系统弹框和完成/失败通知，不访问数据库或 Cookie。
- `AuthGuidanceCoordinator`：协调服务、弹框和时间控制，便于用假实现进行离线测试。

`DouyinWikiService` 继续拥有授权检查与 `retry_job()` 规则，并新增
`check_auth_scope(scope, video_url=None) -> AuthCheckResult`，让助手只检查目标授权范围；现有
`get_auth_status()` 复用该方法组合三个范围的结果。助手不直接拼写 SQL，也不更改其他状态的任务。

`DouyinWikiService` 的默认注入仍是无操作启动器，防止库调用和离线测试意外弹窗。CLI Worker
的 composition root 使用 `config_path or default_config_path()` 构造真实启动器；MCP、Web、测试和
一次性查询命令不会仅因创建 Service 就产生 GUI 行为。

### 进程启动

Worker 捕获 `CookieRequiredError` 或 `BrowserAuthRequiredError` 后：

1. 先用现有逻辑把任务更新为 `needs_auth` 并释放任务锁。
2. 调用注入的 `AuthGuidanceLauncher.launch(scope, trigger_job_id)`。
3. 启动失败只作为内部诊断，不覆盖任务的 `error_code`、`result.auth_scope`、`next_command` 或 `retry_command`。

子进程使用以下原则启动：

- 使用 `sys.executable -m douyin_wiki.auth_guidance`，确保与已安装 Worker 使用同一 Python 环境。
- 使用参数数组和 `start_new_session=True`，不经过 shell。
- 只传配置路径、受严格枚举约束的 scope 和十六进制 job ID。
- stdin、stdout 和 stderr 不包含 Cookie；部署运行时可定向到空设备，测试使用注入的进程启动器。

### 弹框去重

助手把三个任务 scope 映射为两个实际授权通道：`video → video`，
`image_note/creator → douyin`。启动后立即在 `AppConfig.state_dir` 获取按授权通道区分的非阻塞文件锁：

```text
auth-guidance-video.lock
auth-guidance-douyin.lock
```

未取得锁的助手直接退出。锁由进程持有并在退出时释放；进程异常终止不会留下永久占用。这样多个任务同时失败时，同一授权通道只出现一个弹框，不需要数据库迁移。

图文与博主使用同一专用 Playwright Profile 和同一把 `douyin` 锁。弹框文案可同时说明图文与博主任务数量，授权成功后一起恢复这两个 scope。

### 用户交互

弹框标题为“抖库需要抖音授权”，正文包含：

- 授权类型：视频、图文或博主。
- 当前仍受影响的任务数量。
- “抖库不会读取或显示你的密码，也不会输出 Cookie。”
- 说明授权成功后将自动继续任务。

按钮为“稍后处理”和“打开浏览器授权”。默认按钮为“打开浏览器授权”，取消按钮为“稍后处理”。

用户选择“稍后处理”后，助手退出；任务保持 `needs_auth`，不自动再次弹框。只有任务以后被 Agent 或 CLI 显式重试、再次进入 `needs_auth` 时，才会产生新的引导机会。

### 授权与验证

#### 视频

1. 调用现有 `authenticate_video()` 打开配置中的日常浏览器和抖音首页。
2. 从触发任务的 `artifacts.resolved.canonical_url` 取得视频 URL。
3. 每隔 `poll_seconds` 调用下载适配器的只读 `check_auth(video_url=...)`。
4. 只有 `ok=true` 且 `server_verified=true` 才视为成功。
5. 达到 `timeout_seconds` 后停止，不重试任务。

#### 图文与博主

1. 调用现有 `authenticate_douyin(timeout_seconds=...)`，打开抖库专用 Playwright Profile。
2. 该流程检测到未过期登录 Cookie 且页面未被登录/验证码拦截后返回。
3. 返回后再次调用对应 `check_auth()`；只有 `ok=true` 才视为成功。

验证码仍由用户在浏览器中完成，助手不识别、不填写、不绕过验证码。

### 自动重试

验证成功后，助手通过数据库新增的 `list_jobs(status=..., limit=None)` 读取当时全部
`needs_auth` 任务。`video` 通道只选择 `result.auth_scope=video`；`douyin` 通道选择
`image_note` 和 `creator`。

对每个候选任务：

- 再次读取最新状态。
- 只有仍为 `needs_auth` 才调用 `service.retry_job(job.id)`。
- 单个任务因并发变化无法重试时跳过，不影响其他任务。
- 不修改已完成、失败、待复核或等待确认任务。

重试完成后显示 macOS 通知：“授权成功，已继续 N 个任务”。如果授权已恢复但没有待重试任务，则显示“授权已恢复，没有等待中的任务”。

### 失败与降级

- 用户取消：静默退出，保留现有 Agent/CLI 引导。
- 弹框或 `open` 失败：助手退出，任务仍有 `next_command`。
- 授权超时：显示“尚未验证成功，任务仍保持暂停”，不自动重开浏览器。
- 授权检查异常：不重试任务，不覆盖原始错误。
- 子进程启动失败：Worker 继续处理队列。
- 服务重启：不会自动扫描并弹出历史 `needs_auth`；避免重启后突然打扰用户。

## 安全与隐私

- 不调用浏览器密码、Local Storage 或 Cookie 值读取接口。
- 现有 `AuthCheckResult` 只保留状态、来源说明和修复动作；新组件不得扩展为返回 Cookie 值。
- `osascript` 文案使用固定模板；标题、错误文本和分享内容不直接拼入 AppleScript 源码。
- 子进程不使用 shell，job ID 和 scope 必须先验证。
- 自动重试仅恢复用户已经提交、且因授权暂停的任务，不创建新采集任务。
- 所有自动测试使用临时 Vault、假对话框、假授权适配器和假进程启动器，不访问真实浏览器、真实数据库或真实配置。

## 测试策略

### 单元测试

- `tests/test_share.py`：重定向最终类型、作品 ID 冲突和上限行为。
- `tests/test_auth_guidance.py`：弹框确认/取消、范围锁、视频轮询、图文验证、超时、并发状态变化和批量重试。
- `tests/test_service.py`、`tests/test_image_note.py`：任务先持久化为 `needs_auth`，随后只触发一次启动器；启动失败不改变任务结果。
- `tests/test_config.py` 或现有配置测试：默认值、TOML 渲染和禁用开关。

所有生产行为先写失败测试，确认失败原因正确后再写最小实现。

### 完整验证

- 运行相关定向测试。
- 运行 Ruff、完整离线测试和构建。
- 运行仓库统一 `./scripts/verify`；若目标主分支尚无该脚本，记录缺失并运行主分支现有的等价命令。
- 经用户明确同意后，才在真实环境执行只读 live smoke 和一次手工授权交互检查。

## 文档更新

- 更新根目录 `README.md` 的授权说明，加入系统弹框、取消、自动验证和自动重试行为。
- 更新 `docs/gateway-agents.md`，说明 Agent 收到 `needs_auth` 后仍可提供状态，但不要与本机助手重复打开浏览器。
- 更新 `docs/troubleshooting-auth-and-shortlink-type.md`，把临时 `/note/` 重提方案标记为旧版本降级办法，并记录修复后的预期行为。

## 部署与回滚

本改动涉及已安装 Worker 的运行行为。代码合并后，只有用户明确确认部署时，才从长期保留的 `main` checkout 重新安装 Worker LaunchAgent。

部署后验证：

- `doctor` 和 Worker 状态正常。
- 完整视频和图文 URL 的现有采集不回归。
- 测试短链最终 `/note/` 时直接进入图文流程。
- 使用隔离测试账号或可控失效会话检查一次弹框、授权和自动重试。

回滚代码后重新安装上一版本 Worker。处于 `needs_auth` 的任务不会丢失，仍可使用原有 `auth video`、`auth douyin` 和 `jobs retry` 命令恢复。
