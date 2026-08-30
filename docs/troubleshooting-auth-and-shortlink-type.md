# 抖音登录与短链作品类型误判排障记录

本文记录一次真实采集过程中同时出现的两个问题：浏览器已经登录，但任务仍提示需要登录；两条抖音短链被识别成视频，实际作品均为图文笔记。文中的命令和判断方法可用于以后处理同类故障。

## 结论

这次故障的主要原因不是用户没有登录，而是短链解析阶段把图文笔记误分类成了视频。任务随后错误进入视频下载与 Cookie 校验流程，并以 `cookie_required` 停在“需要登录授权”。

在 Chrome 中访问错误生成的 `/video/<作品ID>` 地址时，抖音自动跳转到了 `/note/<作品ID>`。将最终地址以 `/note/` 形式重新提交后，两条作品都被正确识别为 `image_note`，完成图片下载、OCR、分析和入库。

需要特别区分三件事：

- Chrome 中能够正常看到已登录页面，只能证明当前 Chrome 会话已登录。
- ChatGPT 的 Computer Use 权限和 Chrome 扩展，只负责允许 Agent 操作浏览器，不等于把登录 Cookie 自动交给抖库。
- 抖库的视频下载与图文采集使用不同的授权路径；在确认作品类型前，不应根据一次 `cookie_required` 就断定用户需要重新登录。

## 当时观察到的现象

两条分享短链提交后出现了相同的状态：

- 解析结果被写成 `source_kind=video`；
- 任务在约 12% 进度进入 `needs_auth`；
- 错误码为 `cookie_required`；
- `auth status --video-url` 报告视频访问需要登录；
- 用户已经在 Chrome 登录抖音，且浏览器页面可以看到登录后的个人入口。

仅看任务状态时，这组现象很像 Cookie 失效。但通过浏览器检查作品地址后发现：

```text
https://www.douyin.com/video/<作品ID>
                 ↓ 抖音自动跳转
https://www.douyin.com/note/<作品ID>
```

两条作品都发生了相同跳转，因此它们实际是图文笔记，而不是视频。

## 根因分析

短链解析器在早期阶段根据不完整信息生成了 `/video/<作品ID>`，没有用抖音页面的最终落地 URL 再次校正作品类型。错误的类型随后选择了错误的处理链：

```text
短链
  → 误判为 video
  → 视频 Cookie/yt-dlp 检查
  → cookie_required
  → needs_auth
```

正确处理链应该是：

```text
短链
  → 跟随跳转并确认最终 URL
  → /note/<作品ID>
  → source_kind=image_note
  → 图文专用浏览器下载图片
  → 逐图 OCR
  → 内容分析与入库
```

因此，`cookie_required` 是误分类后的下游症状，不是本次故障的根因。

## 登录机制的边界

抖库目前有两条不同的授权路径。

### 视频

视频下载使用配置中的日常浏览器 Cookie，并由 `yt-dlp` 验证目标视频是否可访问：

```bash
uv run douyin-wiki auth status \
  --video-url 'https://www.douyin.com/video/<作品ID>'
uv run douyin-wiki auth video
uv run douyin-wiki jobs retry <JOB_ID>
```

只有作品已确认是视频，且只读探测确实失败时，才应该引导用户执行 `auth video`。

### 图文笔记

图文采集使用抖库专用的 Playwright Profile，而不是视频下载链路：

```bash
uv run douyin-wiki auth status
uv run douyin-wiki auth douyin
uv run douyin-wiki jobs retry <JOB_ID>
```

只有作品已确认是图文，且专用浏览器确实要求登录或出现验证码时，才需要执行 `auth douyin`。

### Computer Use 与 Chrome 扩展

系统设置中的 Computer Use 权限和 Chrome 扩展可以让 Agent 打开、查看和操作用户的 Chrome。它们适合用来确认：

- Chrome 是否正在运行；
- 扩展是否启用；
- 页面是否显示登录后的状态；
- `/video/` 地址是否被网站重定向到 `/note/`。

它们不代表：

- `yt-dlp` 一定能读取或使用当前 Chrome Cookie；
- 抖库图文专用 Profile 已经登录；
- 可以读取、复制或输出 Cookie、Local Storage、密码等敏感信息。

排障时只检查可见页面状态和 URL 跳转，不读取浏览器秘密数据。

## 推荐排障顺序

遇到“浏览器已经登录，但任务仍要求登录”时，按以下顺序处理。

1. 查看任务的 `source_kind`、`error_code`、`auth_scope` 和解析后的 canonical URL。
2. 在已授权的浏览器中打开作品地址，观察最终落地 URL；不要读取 Cookie 或浏览器存储。
3. 若 `/video/<ID>` 自动跳转为 `/note/<ID>`，先判定为作品类型误分类，不要继续重复视频登录。
4. 用最终 `/note/<ID>` 地址重新提交采集任务，并确认新任务的 `source_kind=image_note`。
5. 若最终 URL 确实是 `/video/<ID>`，再运行带 `--video-url` 的视频授权检查。
6. 若最终 URL 是 `/note/<ID>`，但图文专用浏览器出现登录页或验证码，再运行 `auth douyin`。
7. 任务完成后核验来源类型、图片数量、OCR、文章文件和检索分块。

可用以下命令查看任务，而不接触 Cookie 内容：

```bash
uv run douyin-wiki jobs get <JOB_ID>
uv run douyin-wiki auth status
```

## 本次处理与验证

本次采用的临时修复是保留原失败任务，并将浏览器确认后的两个 canonical `/note/<作品ID>` 地址重新提交。结果如下：

- 新任务均解析为 `source_kind=image_note`；
- 每条笔记下载 1 张图片并完成 OCR；
- 两条任务均从 `awaiting_agent_analysis` 进入 `completed`；
- 两篇文章均生成了可检索分块；
- 没有读取或输出 Cookie；
- 没有安装图文内容中推荐的任何 Skill。

## 避免复发的产品改进建议

后续修复解析器时，建议增加以下保护：

1. 短链必须跟随重定向，并以最终 URL 的 `/video/` 或 `/note/` 路径校正作品类型。
2. 如果访问 `/video/<ID>` 后跳转到 `/note/<ID>`，应在进入下载器前把 `source_kind` 改为 `image_note`。
3. `cookie_required` 出现时，先重新核对作品类型，再返回登录建议。
4. 增加“短链初步识别为视频、最终跳转为图文”的回归测试。
5. 任务诊断信息应同时展示初始 URL、最终 URL、`source_kind` 和 `auth_scope`，但绝不展示 Cookie 值。

## 快速判断表

| 可见现象 | 优先判断 | 下一步 |
| --- | --- | --- |
| Chrome 已登录，`/video/` 自动跳到 `/note/` | 类型误判 | 用最终 `/note/` 地址重新提交 |
| 最终仍是 `/video/`，只读视频探测失败 | 视频授权问题 | `auth video` 后重试 |
| 最终是 `/note/`，图文专用窗口要求登录或验证码 | 图文授权问题 | `auth douyin` 后重试 |
| Chrome 扩展启用，但 CLI 仍提示授权失败 | 授权通道不同 | 按作品类型检查对应授权路径 |
| 任务停在 `awaiting_agent_analysis` | 本地媒体处理已完成 | 提交图文分析，不要继续登录排障 |
