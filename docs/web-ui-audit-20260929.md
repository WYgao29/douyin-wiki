# 抖库 Web UI 正式环境审计报告

- **审计时间**：2026-09-29（Asia/Shanghai）
- **环境**：本机正式 LaunchAgent `com.local.douyin-wiki.web` → `http://127.0.0.1:8765/`（Web v0.2.19）
- **配套进程**：`com.local.douyin-wiki.worker` 运行中；maintenance 已安装
- **方法**：路由/模板映射 + curl 全页/关键 API + Playwright/Chromium 点击走查（只读为主；未提交导入、未清空废纸篓、未执行维护/重建/清缓存）
- **截图目录**：[`docs/_web-ui-audit-20260929/`](_web-ui-audit-20260929/)
- **原始结果**：[`docs/_web-ui-audit-20260929/playwright-results.json`](_web-ui-audit-20260929/playwright-results.json)

---

## 1. 结论摘要（Findings First）

整体可用性良好：12 个 HTML 壳页均 200，核心 GET API 均 200，静态资源齐全，CSRF（缺 Origin → 403；浏览器同源 POST → 200）正常，Doctor 全绿，模型可连，Worker 心跳正常。正式库当前仅 **1** 篇资料、**1** 个已完成任务、专题/废纸篓/博主均为空。

**优先关注：**

| 严重度 | 问题 |
| --- | --- |
| **Major** | 任务详情对「有提示」状态未展示 `result.warnings` 正文；复核 API `issues=[]`，用户只能看到笼统「已完成（有提示）」 |
| **Major** | 任务详情重复渲染两个「打开知识资料」按钮（`next_action` + `entry_id` 链接） |
| **Major** | 侧栏「系统设置」在 1440×900 下默认位于视口外（需滚动侧栏才能看见） |
| **Major** | 视频下载授权为「可能有效但未完成服务器验证」；与抖音账号授权（已授权）双通道不一致，影响正式采集可靠性 |
| **Nit / UX** | 「创建专题」按钮并不打开对话框，而是跳回资料库进入多选；空态预期不清晰 |
| **Nit / UX** | 未知路径返回裸 JSON `{"detail":"Not Found"}`，无应用内 404 页 |
| **Nit** | `system-nav` 缺少 `data-route`，与其它设置项 SPA 导航不一致（整页跳转） |
| **Nit** | 单条任务仍展示全 0 的「子任务」统计块，信息噪音 |

未发现 Blocker 级「页面打不开 / API 大面积失败 / 静态资源 404」。未对真实 Vault 做破坏性写操作。

---

## 2. 路由与功能地图

### 2.1 HTML / SPA 页面（均 200，同壳 `app.html`，按 path 切 view）

| 路径 | 标题 | 主视图 ID | 结果 |
| --- | --- | --- | --- |
| `/` | 资料库 · 抖库 | `library-view` | OK，1 张卡片 |
| `/imports` | 导入内容 | `imports-view` | OK，三种导入入口 |
| `/imports/single` | 单条导入 | `imports-single-view` | OK（未提交） |
| `/imports/creators` | 博主导入 | `imports-creators-view` | OK（未清点） |
| `/imports/favorites` | 我的收藏 | `imports-favorites-view` | OK（未扫描/清缓存） |
| `/jobs` | 任务中心 | `jobs-view` | OK，1 条 |
| `/jobs/{id}` | 任务详情 | `job-detail-view` | OK，见 Major |
| `/articles/{id}` | 文章详情 | `article-view` | OK |
| `/topics` | 专题 | `topics-view` | OK（空） |
| `/trash` | 废纸篓 | `trash-view` | OK（空） |
| `/settings/auth` | 授权状态 | `auth-view` | OK，见 Major |
| `/settings/analysis` | 导入分析 | `analysis-view` | OK |
| `/settings/model` | 对话模型 | `model-view` | OK，连通测试通过 |
| `/settings/system` | 系统设置 | `system-view` | OK，Doctor 通过 |

### 2.2 关键 GET API（抽样）

| API | HTTP | 备注 |
| --- | --- | --- |
| `/api/library` | 200 | `total=1`；**无 `limit` 参数**（查询串会被忽略） |
| `/api/overview` | 200 | auth / worker / analysis / jobs |
| `/api/jobs` | 200 | 1 条 `completed_with_warnings` |
| `/api/jobs/{id}` | 200 | `result.warnings` 有内容 |
| `/api/jobs/{id}/review` | 200 | `issues=[]`（与 warnings 不对齐） |
| `/api/articles/{id}` | 200 | `item` + `html` |
| `/api/trash` `/api/topics` `/api/creators` | 200 | 空列表 |
| `/api/favorites/imports` | 200 | `[]` |
| `/api/auth/status` | 200 | video 未服务器验证；douyin ready |
| `/api/system/health` `/storage` `/analysis` `/model-health` | 200 | model-health=`ready` |
| `/api/settings/model` | 200 | 已配置 |
| `/api/chat/sessions` | 200 | 无会话 |
| `/api/library/events` `/api/events` | 200 SSE | 首包 `event: ready`；长连接导致 `networkidle` 不适用 |
| `/media/.../cover.jpg` | 200 | image/jpeg |

### 2.3 CSRF

- 无 Origin 的 `POST /api/system/doctor` → **403** `拒绝跨来源请求`
- 浏览器同源 fetch / `Origin: http://127.0.0.1:8765` → **200**

### 2.4 交互覆盖（Playwright）

已覆盖：首页分区（最近/收藏/灵感）、首页进文章、导入枢纽与三子页（dry 填入未提交）、任务列表/详情、专题/废纸篓、四设置页、命令面板 ⌘K、聊天面板开合、窄屏导航、Doctor、模型测试、顶栏「导入内容」弹出层。  
**刻意跳过**：真实 capture 提交、收藏扫描/确认、博主 inventory 确认、废纸篓 purge、维护 apply、重建 apply、清空收藏缓存、删除对话。

---

## 3. 缺陷清单

### Blocker

（无）

### Major

1. **任务「有提示」但提示正文不可见（API/UI 错位）**  
   - API：`GET /api/jobs/{id}` → `result.warnings = ["模型生成的 6 条无法核实的证据或内容已移除"]`  
   - UI（`jobs.js` `loadDetail`）只渲染 `review_issues` / `analysis_evidence_audit`，**不读 `result.warnings`**  
   - `GET /api/jobs/{id}/review` 返回 `issues: []`，无法补位  
   - 截图：`09-job-detail.png` — 仅有「已结束，但有需要留意的提示」/「已完成（有提示）」  
   - **建议**：详情页增加「提示」区块渲染 `result.warnings`；或让 review API 聚合 warnings；文案与数据源统一。

2. **任务详情重复「打开知识资料」**  
   - `next_action`（`open_entry`）生成主按钮；`job.entry_id` 再追加同名 secondary 链接  
   - 截图：`09-job-detail.png`  
   - **建议**：若 `next_action.code === 'open_entry'` 则不再追加 entry 链接，或合并为单一 CTA。

3. **侧栏「系统设置」默认可发现性差**  
   - DOM 存在且可点，但在 1440×900 下 `getBoundingClientRect().top ≈ 903`（视口高度 900），**默认不可见**，需滚动侧栏  
   - 截图对比：首页/授权页侧栏底部止于「对话模型」；系统页需直链 `/settings/system`  
   - **建议**：压缩侧栏 footer、标签区可折叠默认收起、或把系统设置固定在 footer 可见区。

4. **正式环境视频下载授权未完成服务器验证**  
   - 通道「视频下载授权」：`可能有效但未完成服务器验证`（检测到 Cookie，未联网验证）  
   - 「抖音账号授权」：已授权  
   - 截图：`12-settings-auth.png`  
   - **建议**：在资料库/导入页对 video 通道未验证给出持久提示；引导一键「检查状态/授权」。

### Nit / UX 改进

5. **「创建专题」心智不符**：`#topics-create-button` 会 `showLibrary()` + 进入多选并 toast「请先选择…」，并不 `showModal`。可改文案为「从资料库选择来源」或在专题空态提供分步说明。  
6. **未知路由裸 JSON 404**：`/no-such-page-audit` → `{"detail":"Not Found"}`（截图 `19-unknown-route.png`）。建议回退到 SPA 壳 + 友好空态，或统一 HTML 404。  
7. **`#system-nav` 无 `data-route`**：其它设置项走 SPA `navigate`，系统设置整页刷新，状态/动画不一致。  
8. **子任务全 0 块**：单条采集 `child_stats` 全 0 且 `children=[]` 仍渲染「子任务」标题与四格占位，建议无子任务时隐藏整块。  
9. **导入双入口**：侧栏「导入内容」直达 `/imports`；顶栏「+ 导入内容」打开 popover。行为正确但需在文案上区分「枢纽页」vs「快捷菜单」（非缺陷）。  
10. **`/api/library?limit=` 无效**：handler 无 limit 参数，静默忽略；若对外约定分页需文档化或实现。  
11. **静态 `favorites.js`**：目录中有文件但模板加载的是 `imports-favorites.js`；确认是否死代码以免漂移。  
12. **Doctor 文案**：检查项提示「请运行 douyin-wiki auth status」偏 CLI；Web 正式用户更宜链到「授权状态」页。

---

## 4. 健康与配置快照（审计当时）

- Web：运行中，`127.0.0.1:8765`，v0.2.19  
- Worker：running，心跳约审计时刻，队列 0  
- 分析模式：provider（后台模型接口）  
- 模型健康：ready / 可连接  
- 存储：Vault ~194.1 MB，DB ~4.8 MB，资料 1 篇，永久媒体 0，临时保留 30 天  
- 最近维护：2026-09-27 03:00:19（北京时间）  
- Doctor：检查通过（yt-dlp / ffmpeg / whisper / playwright / vault / 模型等均为正常）

---

## 5. 截图索引

| 文件 | 内容 |
| --- | --- |
| `01-home.png` | 资料库首页 |
| `02-section-*.png` | 最近 / 收藏 / 灵感 |
| `03-article.png` / `20-article-from-home.png` | 文章详情 |
| `04-imports.png` / `04d-imports-toggle.png` | 导入枢纽 / 顶栏弹出层 |
| `05`–`07` | 单条 / 博主 / 收藏导入 |
| `08-jobs.png` / `09-job-detail.png` | 任务列表与详情（重复按钮、缺 warnings） |
| `10-topics.png` / `11-trash.png` | 专题、废纸篓 |
| `12`–`15` / `15b` | 授权、分析、模型、系统、Doctor |
| `16-command-palette.png` | ⌘K 搜索 |
| `17-chat.png` | 聊天面板 |
| `18-mobile-nav.png` | 窄屏导航 |
| `19-unknown-route.png` | 裸 JSON 404 |
| `21-sidebar-system-nav.png` | 侧栏滚动后露出系统设置 |

---

## 6. 范围与限制

- 正式数据量极小（1 条目），列表分页、筛选组合、专题多源、废纸篓批量等路径未能用真实数据压测。  
- 未走真实抖音导入闭环（避免污染队列/Vault）。  
- 复核/批准类写接口仅做 GET review；未点「批准/重试」类破坏性或半破坏性操作。  
- 代码仅只读核对（`app.py` / `jobs.js` / `app.js` / 模板）；**未改代码、未 push、未清空 Vault**。

---

## 7. 建议修复优先级

1. P0：任务详情展示 `result.warnings`；去掉重复 CTA。  
2. P1：侧栏保证「系统设置」默认可视；视频授权未验证的全局提示。  
3. P2：专题创建文案/流程、404 页、`system-nav` 的 `data-route`、空子任务隐藏、Doctor 链到 Web 授权页。

---

## 8. 修复清单（2026-09-29，Web v0.2.20）

| # | 项 | 状态 | 说明 |
| --- | --- | --- | --- |
| Major 1 | 任务详情展示 `result.warnings`；`/review` 对齐 | ✅ | `jobs.js` 渲染「提示」区块；`GET /api/jobs/{id}/review` 返回 `warnings` |
| Major 2 | 去掉重复「打开知识资料」 | ✅ | `next_action.code === open_entry` 时不再追加 `entry_id` 链接 |
| Major 3 | 侧栏「系统设置」默认可视 | ✅ | `sidebar-body` 滚动、`sidebar-footer` 固定；压缩间距 |
| Major 4 | 视频/账号双通道授权文案与提示 | ✅ | 状态文案澄清；授权页强调联网检查；资料库/导入持久 banner；overview `video_auth_unverified` |
| Nit 5 | 「创建专题」文案 | ✅ | 改为「从资料库选择来源」，toast 说明多选流程 |
| Nit 6 | 未知路径裸 JSON 404 | ✅ | 未知浏览器路径返回 SPA HTML 壳 + `not-found-view`（HTTP 404） |
| Nit 7 | `#system-nav` 缺 `data-route` | ✅ | 已补 `data-route="/settings/system"` |
| Nit 8 | 全 0 子任务块 | ✅ | 无子任务且统计全 0 时隐藏 |
| Nit 9 | 导入双入口文案 | ✅ | 导入枢纽页说明侧栏 vs 顶栏快捷菜单（行为本身非缺陷） |
| Nit 10 | `/api/library?limit=` | ✅ | 已实现 `limit`（1–500），响应带回 `limit` |
| Nit 11 | 死代码 `favorites.js` | ✅ | 已删除（模板使用 `imports-favorites.js`） |
| Nit 12 | Doctor CLI 文案 | ✅ | `douyin_browser_profile` 提示链到网页「授权状态」 |

测试：`tests/test_web_ui_audit_20260929.py`
