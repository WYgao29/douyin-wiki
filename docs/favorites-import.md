# 收藏批量导入

此功能分成清点和确认两个步骤。清点仅生成可恢复的作品清单；确认后才创建现有采集管线的子任务，执行下载、转录、分析和入库。

## 网页操作

1. 点击工具栏的「导入收藏」。默认清点全部收藏；如需指定收藏夹，先读取目录，再勾选收藏夹。
2. 默认只导入视频；可选择同时导入静态图文。文章保留在清单中，暂不支持导入。
3. 生成作品清单，检查账号、数量及完整性提示。可搜索、分页、逐条选择，或选择／排除整份清单。整份清单操作不受当前页和搜索筛选限制。
4. 确认导入所选作品。不完整清单需要额外勾选接受；无法识别稳定收藏夹 ID 时，不会用名称代替 ID。
5. 从历史任务恢复进度，或重试失败任务。读取历史只读取本地状态，不会重新访问抖音。

全部收藏直接读取作品列表，包含未分组作品。该模式不逐个遍历收藏夹；归属信息不足时会单独提示，不应据此断言作品没有加入任何收藏夹。指定多个收藏夹取并集，以作品 ID 去重。

## 登录与后台处理

- 收藏清点和图文采集使用专用 Playwright Profile。首次或登录过期时运行 `douyin-wiki auth douyin`，然后重试。账号变化会暂停原批次。
- 视频下载使用已有视频授权通道；需要时运行 `douyin-wiki auth video`。专用浏览器已登录不代表视频下载通道已就绪。
- 后台 Worker 必须运行。Gateway 模式需 Agent 继续消费事件完成校正、分析和复核；待 AI 处理、待确认和待复核不算完成。
- 新建子任务沿用默认媒体临时保留规则及现有长视频确认规则。已入库作品跳过，进行中的采集任务复用，不覆盖已有灵感、保留策略或原任务 Gateway 上下文。
- 批次汇总通过父任务及 `favorites_context.batch_silent` 表达；Agent 仍应报告需要用户处理的等待项。失败重试只重试失败子任务；需要登录的子任务通过原任务队列或授权流程恢复。
- 清点和重扫均由用户手动触发。没有定时同步，也不会在打开网页时自动清点或导入。

## CLI

```sh
# 仅清点，返回任务 ID。后台 Worker 执行清点。
douyin-wiki favorites scan
# 仅目录；使用返回的稳定 ID，不能直接填收藏夹名称。
douyin-wiki favorites scan --directory-only
douyin-wiki favorites scan --folder-id ID_1 --folder-id ID_2 --include-images
# 恢复历史、分页查看、排除个别作品。
douyin-wiki favorites list
douyin-wiki favorites show JOB_ID --page 1 --limit 50
douyin-wiki favorites select JOB_ID --exclude --work-id WORK_ID
# 不带 work-id/folder-id 的 select 作用于整份清单。
douyin-wiki favorites confirm JOB_ID
# 仅在明确接受不完整清单时加 --accept-partial。
douyin-wiki favorites retry JOB_ID
```

`scan` 可传 `--gateway openclaw --conversation-id ...` 绑定后续事件。
MCP 对应工具为 `scan_favorites`、`list_favorites_imports`、`get_favorites_import`、`set_favorites_selection`、`confirm_favorites_import`、`retry_favorites_import`。
Agent 必须先展示清单并获得用户的导入指令，才能调用确认工具。

## 验证边界

本次仅开发代码，自动化数据全部为临时模拟数据，没有读取或导入真实收藏，也没有重启已安装服务。已覆盖数据库事务、幂等派发、断点、账户变化、跨来源去重、超过 5000 条清单、Web/CLI/MCP 接口及离线浏览器页面行为。

实际抖音页面会变化：作品列表读取基于先前观察到的 DOM 边界；收藏夹稳定 ID 和目录／详情选择器仍需真实页面验证。边界、稳定 ID 或结束信号不足时返回不完整结果或暂停，不把失败当作空收藏。真实账号清点、下载和完整入库尚未做端到端验收。

测试命令：`.venv/bin/pytest -q`、`.venv/bin/ruff check src tests` 和 `node --check src/douyin_wiki/webapp/static/favorites.js`。离线浏览器测试需要已安装 Playwright Chromium 和允许启动测试浏览器的环境；它使用临时上下文，拦截所有网络请求，不加载个人 Profile。
