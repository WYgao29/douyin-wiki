# 抖库 Web UI 重设计执行文件

> 状态：可执行
>
> 版本：1.0
>
> 日期：2026-08-23
>
> 适用目录：`src/douyin_wiki/webapp/`

## 1. 文件目的

本文件用于指导开发者或编码 Agent，在不破坏现有本地知识库、搜索、文章阅读、AI 对话、灵感写回和模型设置功能的前提下，完成抖库 Web UI 的系统性重设计。

目标界面结合三类产品的优点：

- Notion：低噪声视觉、克制的排版、数据库多视图、侧边导航和渐进披露。
- Google NotebookLM / Gemini Notebook：来源范围、内容、AI 对话和引用之间的清晰关系。
- Linear：高信息密度、键盘优先、保存视图、筛选状态可恢复。

本文档是实施约束和验收依据。除非任务明确改变范围，执行者不应在本轮把前端迁移到 React、Vue 或其他 SPA 框架。

## 2. 实施结论

### 2.1 本轮技术选择

保留：

- FastAPI
- Jinja2 模板
- 原生 HTML
- 原生 CSS
- 原生 JavaScript
- 现有 REST API 和 SSE 流式对话

允许新增：

- 本地 SVG 图标资源
- CSS design tokens
- 少量无依赖的 UI 工具函数
- 为 UI 行为增加的测试

本轮不引入：

- React、Vue、Svelte
- Tailwind CSS
- shadcn/ui
- 外部 CDN
- 构建时 Node.js 依赖
- 需要联网才能显示的字体或图标

### 2.2 设计方向

将现有的“封面媒体墙”调整为“安静、清晰、高效的个人知识工作台”。

关键词：

- 低饱和
- 少阴影
- 小圆角
- 中等信息密度
- 默认列表视图
- 画廊作为可选视图
- 可收起的 AI 面板
- 明确的来源范围
- 可验证的引用
- 完整的键盘和焦点状态

## 3. 范围

### 3.1 必须完成

- 重建全局颜色、字体、字号、间距、圆角、阴影、动画 token。
- 重设计资料库首页。
- 新增列表与画廊两种视图。
- 重整左侧导航和筛选入口。
- 重设计文章阅读页。
- 重设计 AI 对话面板和引用样式。
- 统一模型设置页的视觉语言。
- 替换 Unicode 图标。
- 增加亮色、暗色、跟随系统模式。
- 增加键盘焦点、减少动画、文本缩放和 320px 重排支持。
- 保持现有业务行为和安全限制。
- 增加与 UI 结构和静态资源有关的自动化测试。

### 3.2 建议完成

- 支持左侧栏和 AI 栏收起。
- 记忆视图模式、密度、主题和面板状态。
- 将筛选、排序、搜索状态写入 URL。
- 增加全局命令搜索入口。
- 为长文章增加目录和阅读进度。
- AI 引用支持原句预览。

### 3.3 本轮不做

- 修改采集、分析、向量搜索或嵌入逻辑。
- 改变知识库文件格式。
- 改变现有 API 的响应结构，除非实现引用预览确实需要新增向后兼容字段。
- 引入用户账号、云同步或远程数据库。
- 实现多人协作。
- 重写 Markdown 渲染器。

## 4. 不可破坏的现有行为

完成重设计后，下列行为必须保持：

- `/` 显示资料库。
- `/articles/{entry_id}` 可直接打开文章并支持浏览器前进、后退。
- `/settings/model` 可配置并测试模型。
- `/api/library` 的搜索和 facets 保持可用。
- `/api/articles/{entry_id}` 继续返回经过清理的文章 HTML。
- `/api/chat/sessions` 的创建、切换、删除保持可用。
- AI 回复继续通过 SSE 流式显示。
- 引用继续包含文章、位置和原作品链接。
- 灵感写回必须继续要求明确确认。
- 本地媒体安全校验、TrustedHost 和同源限制不得放松。
- 文件监听刷新资料库的行为保持不变。

## 5. 信息架构

### 5.1 桌面布局

```text
┌──────── 240–256px ────────┬────────────── 弹性主区域 ──────────────┬──── 360–420px ────┐
│ 品牌与主导航               │ 顶部工具栏                            │ AI 对话              │
│                            │                                      │                     │
│ 资料库                     │ 页面标题 / 数量                       │ 对话标题 / 设置       │
│ 最近访问                   │ 视图 / 筛选 / 排序 / 密度             │ 当前来源范围          │
│ 灵感                       │                                      │                     │
│ 收藏                       │ 资料列表、画廊或文章正文               │ 消息和引用             │
│                            │                                      │                     │
│ 已保存视图                 │                                      │ 输入框                │
│ 标签（折叠）               │                                      │ 模型和隐私状态         │
│ 设置                       │                                      │                     │
└───────────────────────────┴──────────────────────────────────────┴─────────────────────┘
```

尺寸规则：

- 左栏默认宽度：248px。
- 左栏可收起为 0px；收起按钮仍可访问。
- 中间区域最小宽度：540px。
- AI 栏默认宽度：384px。
- AI 栏允许在 340–460px 范围调整；至少支持收起。
- 顶部工具栏高度：56px。
- 主内容最大宽度不强制限制；文章正文单独限制。

### 5.2 中等屏幕

视口宽度 960–1279px：

- 左栏宽度 224px。
- AI 栏变为右侧抽屉，默认关闭。
- 顶部工具栏显示 AI 开关。
- 画廊最小列宽 180px。

### 5.3 小屏幕

视口宽度小于 960px：

- 主界面单栏。
- 左栏和 AI 栏都使用模态抽屉。
- 抽屉打开时必须锁定背景滚动并管理焦点。
- 顶部工具栏保留导航、搜索、AI 三个入口。

视口宽度小于 640px：

- 页面水平内边距 16px。
- 默认强制列表视图。
- 卡片缩略图 56px。
- 页面标题 28px。
- 文章标题 28px。
- 所有主要点击区域至少 44px 高。

视口宽度 320px：

- 不允许出现整页横向滚动。
- 表格、代码块和媒体可以在自身容器内横向滚动。
- Dialog 宽度不得超出视口。

## 6. 导航与页面结构

### 6.1 左栏

从上到下排列：

1. 品牌区
2. 主导航
3. 已保存视图
4. 可折叠标签区
5. 底部设置入口

主导航固定项目：

- 资料库
- 最近加入
- 灵感
- 收藏（数据未实现时可以暂不显示）

删除当前左栏中的“全部文章长列表”。文章导航应由主区域列表和全局搜索承担。

博主、内容类型和标签不再完整铺开：

- 博主和内容类型进入顶部筛选浮层。
- 标签在左栏仅显示固定或常用标签。
- 其余标签通过“查看全部标签”进入筛选面板。

### 6.2 顶部工具栏

从左到右：

- 移动端导航按钮
- 全局搜索框
- `Cmd/Ctrl + K` 快捷键提示
- 筛选按钮
- 排序按钮
- 视图切换按钮
- 密度切换按钮
- 桌面 AI 面板开关

工具栏必须使用 `position: sticky`，但背景只使用不透明或接近不透明的 surface 色。不要使用强烈毛玻璃。

### 6.3 页面标题区

包含：

- 页面标题
- 结果数量
- 当前筛选摘要
- 清除筛选操作

不再使用全大写英文 eyebrow 作为主要装饰。需要辅助标签时使用 12px medium、普通中文，不增加过大的字符间距。

## 7. 资料库视图

### 7.1 默认列表视图

每项结构：

```text
┌──────┐  标题，最多两行
│ 72px │  一句话摘要，最多两行
│ 图像 │  博主 · 类型 · 发布日期 · 阅读/媒体状态
└──────┘  标签 1  标签 2                   更多操作
```

桌面规格：

- 最小高度：104px。
- 内边距：16px。
- 缩略图：72×72px，圆角 8px。
- 标题：15px/22px，font-weight 600。
- 摘要：13px/20px，次要文字色。
- 元数据：12px/18px。
- 行间使用 1px divider，不给每一项添加独立大阴影。
- hover 使用浅中性色背景。
- selected 使用品牌浅色背景和左侧 2px 指示条。
- 键盘 focus 使用 2px focus ring。

### 7.2 紧凑列表

- 行高：48–56px。
- 缩略图可隐藏。
- 只显示标题、博主、类型、日期。
- 适合大资料库快速扫描。

### 7.3 画廊视图

- 作为可选视图保留。
- 卡片封面建议使用 4:3 或 16:10，而不是强制正方形。
- 卡片圆角 10px。
- 默认无阴影；hover 只增加细边框或非常轻的阴影。
- 无封面时使用单色或轻微色调，不使用八套高饱和渐变。
- 文本封面必须保证对比度。

### 7.4 排序

首批支持：

- 最近加入
- 发布时间从新到旧
- 发布时间从旧到新
- 标题
- 博主

### 7.5 筛选

首批支持：

- 博主
- 内容类型
- 标签
- 来源类型：视频 / 图文
- 是否有灵感

筛选规则：

- 同一分类内默认 OR。
- 不同分类之间 AND。
- 已应用筛选显示为可删除的 chips。
- 提供“清除全部”。
- 筛选写入 URL 查询参数。
- URL 恢复失败时回退到默认状态，不阻塞页面。

建议查询参数：

```text
?q=关键词&view=list&sort=captured_desc&author=名称&type=tutorial&tag=咖啡&has_inspiration=1
```

## 8. 文章阅读页

### 8.1 文章头部

顺序：

1. 返回资料库 / 面包屑
2. 内容类型和状态
3. 标题
4. 博主、发布时间、采集时间
5. 标签
6. 原作品入口
7. 封面（有则显示）

规格：

- 文章标题：32px/40px；移动端 28px/36px。
- 文章内容容器：`max-inline-size: 42em`。
- 普通中文正文：17px/29px。
- UI 和默认正文使用无衬线字体。
- 提供“阅读字体”设置时，才允许切换到宋体。
- 标题层级不得靠任意字号表达，必须保留正确的 `h1/h2/h3` 语义。

### 8.2 正文块

统一规格：

- 段落间距：0 0 16px。
- H2：24px/32px，上方 40px，下方 12px。
- H3：20px/28px，上方 28px，下方 8px。
- 无序/有序列表左缩进 24px，项目间距 6px。
- 引用：左侧 3px 品牌色边框、12px 16px 内边距、浅色背景。
- 图片：圆角 8px，不使用大阴影；支持 caption。
- 表格：表头浅底、行 divider、容器可横向滚动。
- 代码块：等宽字体、14px/22px、提供复制按钮。
- 行内链接必须保持下划线或另一种非颜色识别方式。

### 8.3 可选增强

- 桌面端文章目录。
- 阅读进度条。
- 回到顶部。
- 视频引用点击后跳到对应时间点。
- 图文引用点击后定位到对应图片。

## 9. AI 对话面板

### 9.1 面板头部

包含：

- 标题“抖库 AI”
- 新建对话
- 历史对话选择
- 更多菜单：删除、清空、模型设置
- 收起按钮

删除操作不得只使用垃圾桶图标。菜单中显示明确文字，并继续二次确认。

### 9.2 来源范围

来源范围必须始终可见，使用带图标的 context chip：

- 整个资料库
- 当前文章：文章名
- 已选择来源：N 项

范围改变时：

- 明确提示将新建相应范围的对话，或
- 明确更新当前会话范围。

不得只在发送时静默改变会话。

### 9.3 消息

用户消息：

- 右对齐。
- 使用中深品牌色或深中性色背景。
- 圆角 12px 12px 4px 12px。
- 最大宽度 88%。

AI 消息：

- 左对齐。
- 使用 surface 背景和 1px 边框。
- 圆角 4px 12px 12px 12px。
- 字号 14px/22px。
- 支持段落、列表、表格、代码和链接。

流式生成时：

- 显示文字增量。
- 发送按钮变为停止按钮；若暂不实现真正取消，则显示 loading 且不可重复提交。
- 保证消息容器不会因每个 token 产生明显布局抖动。

### 9.4 引用

引用使用编号形式：`[1] [2] [3]`。

引用卡片至少显示：

- 文章标题
- 时间点或图片位置
- 原作品入口

交互：

- hover/focus 显示原句预览；如果 API 暂无 excerpt，可先显示现有元数据。
- 点击文章标题打开对应文章。
- 点击时间点或图片位置定位到证据位置。
- 点击原作品在新标签页打开。
- 引用文字不得小于 12px。

### 9.5 输入区

- 输入区固定在面板底部。
- textarea 最小高度 44px，最大高度 160px。
- Enter 发送，Shift+Enter 换行。
- 显示清晰的键盘提示。
- 发送按钮最小 40×40px；移动端 44×44px。
- disabled、loading、error 状态必须可感知。
- 隐私说明 12px/18px，不使用 9px。

## 10. 灵感 Dialog

保留原生 `<dialog>`，调整：

- 宽度最大 560px。
- 圆角 16px。
- 标题 20px/28px。
- 标签 13px/20px。
- 输入框最小高度 40px。
- textarea 至少 96px。
- 取消和确认操作使用明确文字。
- 打开时焦点进入第一个可编辑字段。
- Tab 焦点不得离开 Dialog。
- Escape 关闭。
- 关闭后焦点返回触发按钮。
- 错误显示在 Dialog 内，不只显示 toast。

## 11. 模型设置页

模型设置页与主应用共用全部 token。

调整：

- 页面标题由最大 62px 降到 32px。
- 删除第三栏大块说明面板，或改为主内容内的帮助 callout。
- 设置内容最大宽度 720px。
- 表单卡片圆角 12px。
- 分区使用标题、说明和 divider，而不是每块都使用大卡片。
- API Key 字段的显示/隐藏按钮使用统一图标和可访问名称。
- 测试连接结果使用 inline status，不能只靠颜色。
- 安全说明保留，但字号不得小于 12px。
- 保存、测试连接按钮在移动端铺满或采用清晰堆叠。

## 12. Design tokens

执行者应使用语义 token，不在组件中直接散布色值。

建议将下列内容作为 `app.css` 开头的基线；实现时可以按现有命名调整，但语义和数值范围应保持。

```css
:root {
  color-scheme: light;

  /* Font families */
  --font-ui: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", "Noto Sans SC", "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", sans-serif;
  --font-reading: "Noto Serif SC", "Source Han Serif SC", "Songti SC", serif;
  --font-mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;

  /* Type scale */
  --text-xs: 0.75rem;
  --text-sm: 0.8125rem;
  --text-ui: 0.875rem;
  --text-body: 1rem;
  --text-reading: 1.0625rem;
  --text-lg: 1.125rem;
  --text-xl: 1.25rem;
  --text-2xl: 1.5rem;
  --text-3xl: 2rem;

  --leading-xs: 1rem;
  --leading-sm: 1.125rem;
  --leading-ui: 1.25rem;
  --leading-body: 1.625rem;
  --leading-reading: 1.8125rem;
  --leading-lg: 1.75rem;
  --leading-xl: 2rem;
  --leading-2xl: 2.5rem;

  /* Spacing */
  --space-0: 0;
  --space-0-5: 0.125rem;
  --space-1: 0.25rem;
  --space-1-5: 0.375rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-5: 1.25rem;
  --space-6: 1.5rem;
  --space-8: 2rem;
  --space-10: 2.5rem;
  --space-12: 3rem;
  --space-16: 4rem;

  /* Radius */
  --radius-xs: 4px;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 999px;

  /* Layout */
  --sidebar-width: 248px;
  --chat-width: 384px;
  --toolbar-height: 56px;
  --reading-width: 42em;

  /* Light colors: Notion-inspired warm neutral + blue */
  --color-canvas: #f7f7f5;
  --color-surface: #ffffff;
  --color-surface-subtle: #f3f3f1;
  --color-surface-hover: #eeeeeb;
  --color-surface-pressed: #e7e7e3;
  --color-surface-selected: #eaf2ff;

  --color-text-primary: #252525;
  --color-text-secondary: #6f6f6b;
  --color-text-tertiary: #7d7d77;
  --color-text-disabled: #9d9d97;
  --color-text-inverse: #ffffff;

  --color-border-subtle: #e8e8e4;
  --color-border-default: #d8d8d2;
  --color-border-strong: #b8b8b1;

  --color-primary: #2f6feb;
  --color-primary-hover: #275fc9;
  --color-primary-pressed: #204fa7;
  --color-primary-subtle: #eaf2ff;
  --color-primary-text: #2557a7;

  --color-success: #2f7d50;
  --color-success-subtle: #e5f3ea;
  --color-warning: #9a6700;
  --color-warning-subtle: #fff2cc;
  --color-danger: #c93c37;
  --color-danger-hover: #a9322e;
  --color-danger-subtle: #fbe9e7;

  --focus-ring: 0 0 0 3px rgba(47, 111, 235, 0.28);
  --shadow-popover: 0 8px 30px rgba(37, 37, 37, 0.12);
  --shadow-dialog: 0 24px 80px rgba(37, 37, 37, 0.2);

  --duration-fast: 120ms;
  --duration-normal: 180ms;
  --duration-panel: 220ms;
  --ease-standard: cubic-bezier(0.2, 0, 0, 1);
}

:root[data-theme="dark"] {
  color-scheme: dark;

  --color-canvas: #171816;
  --color-surface: #20211e;
  --color-surface-subtle: #1d1e1b;
  --color-surface-hover: #292a26;
  --color-surface-pressed: #32332e;
  --color-surface-selected: #27364f;

  --color-text-primary: #f1f1ed;
  --color-text-secondary: #abaca6;
  --color-text-tertiary: #969790;
  --color-text-disabled: #6f706a;
  --color-text-inverse: #171816;

  --color-border-subtle: #343630;
  --color-border-default: #454740;
  --color-border-strong: #62645c;

  --color-primary: #8faff5;
  --color-primary-hover: #a7c0f7;
  --color-primary-pressed: #c0d2fa;
  --color-primary-subtle: #27364f;
  --color-primary-text: #b8ccfa;

  --color-success: #75c995;
  --color-success-subtle: #21382a;
  --color-warning: #e0b45f;
  --color-warning-subtle: #3b321e;
  --color-danger: #ef8b86;
  --color-danger-hover: #f2a19d;
  --color-danger-subtle: #472827;

  --focus-ring: 0 0 0 3px rgba(143, 175, 245, 0.4);
  --shadow-popover: 0 8px 30px rgba(0, 0, 0, 0.35);
  --shadow-dialog: 0 24px 80px rgba(0, 0, 0, 0.5);
}

html {
  font-family: var(--font-ui);
  background: var(--color-canvas);
  color: var(--color-text-primary);
}

body {
  margin: 0;
  min-width: 320px;
  background: var(--color-canvas);
}

:where(button, a, input, select, textarea, [tabindex]):focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
  box-shadow: var(--focus-ring);
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

### 12.1 Token 使用约束

- `--color-primary` 只用于主要操作、焦点、链接和当前选择。
- `--color-danger` 只用于错误和危险操作。
- 标签颜色不能覆盖语义状态颜色。
- 组件不得自行创建新的近似灰色。
- 圆角只能使用 token。
- 阴影只用于 popover、dropdown、drawer、dialog 和极少数 raised surface。
- 普通列表项和普通卡片不使用大阴影。
- 字号不得小于 `--text-xs`，可操作文字不得小于 13px。

## 13. 图标系统

统一采用 Lucide 风格线性图标。

实施方式优先级：

1. 将本项目用到的少量 SVG 作为本地 sprite 或模板 partial 打包。
2. 在 HTML 中使用 `<svg><use href="/static/icons.svg#search"></use></svg>`。
3. 不使用远程图标 CDN。

首批图标：

- library
- clock-3
- sparkles
- settings
- search
- panel-left
- panel-right
- list
- layout-grid
- filter
- arrow-up-down
- chevron-down
- plus
- trash-2
- x
- send
- square
- external-link
- bookmark
- tag
- user-round
- file-text
- video
- image
- moon
- sun
- monitor
- command

规格：

- 默认 18×18px。
- 紧凑位置 16×16px。
- 主工具栏 20×20px。
- 默认 stroke-width 1.75–2。
- 纯图标按钮必须提供 `aria-label`。
- 装饰图标必须 `aria-hidden="true"`。

## 14. 主题和偏好持久化

### 14.1 主题

支持：

- light
- dark
- system

存储键：

```text
douyin-wiki.theme
```

规则：

- system 使用 `prefers-color-scheme`。
- 主题脚本必须尽可能在首屏绘制前设置 `data-theme`，避免闪烁。
- HTML `color-scheme` 需与实际主题一致。

### 14.2 UI 偏好

建议存储：

```text
douyin-wiki.library-view       list | compact | gallery
douyin-wiki.library-density    comfortable | compact
douyin-wiki.sidebar-collapsed  true | false
douyin-wiki.chat-collapsed     true | false
douyin-wiki.chat-width         number
```

存储数据必须容错；解析失败时使用默认值。

## 15. 键盘规范

必须支持：

| 快捷键 | 行为 |
|---|---|
| `Cmd/Ctrl + K` | 打开全局搜索或命令菜单 |
| `/` | 当前不在输入框时聚焦搜索 |
| `Esc` | 关闭最近打开的弹层、抽屉或 Dialog |
| `Enter` | 打开聚焦的文章；聊天输入时发送 |
| `Shift + Enter` | 聊天输入换行 |
| `Arrow Up/Down` | 搜索结果、菜单和可导航列表移动焦点 |
| `[` | 可选：收起/展开左栏 |
| `]` | 可选：收起/展开 AI 栏 |

快捷键不得覆盖浏览器或输入框的正常文本编辑行为。

## 16. 状态设计

每个主要区域必须设计以下状态：

### 16.1 资料库

- 初始加载：Skeleton，不显示空状态。
- 有数据：列表或画廊。
- 空资料库：说明如何添加资料。
- 搜索无结果：显示当前关键词和清除搜索。
- 筛选无结果：显示清除筛选。
- 加载失败：显示错误原因和重试按钮。
- 实时刷新：轻量 toast，不中断当前阅读位置。

### 16.2 文章

- 加载中。
- 正常内容。
- 文章不存在。
- 媒体加载失败。
- Markdown 渲染为空。

### 16.3 AI

- 未配置模型。
- 无对话。
- 正在生成。
- 生成完成。
- 连接错误。
- 内容错误。
- 会话已删除。

错误不得只显示在短暂 toast 中；关键错误必须保留在相关区域，直到用户处理或关闭。

## 17. 可访问性要求

目标：WCAG 2.2 AA。

必须满足：

- 普通文字对比度至少 4.5:1。
- 大文字至少 3:1。
- 关键 UI 边界和状态至少 3:1。
- 每个键盘可操作元素都有可见 focus。
- 所有纯图标按钮都有名称。
- 标题层级连续且语义正确。
- 页面主要区域使用正确的 `main`、`nav`、`aside`、`header`。
- 多个导航 landmark 使用不同 label。
- Dialog 具有可访问标题和描述。
- 抽屉打开时正确管理焦点和背景交互。
- 200% 文本缩放不丢失信息。
- 400% 页面缩放时主要内容可重排。
- 最窄 320 CSS px 不出现整页双向滚动。
- 交互目标至少 24×24px；主要和触屏操作目标采用 40–44px。
- 不通过颜色单独表达状态。
- 动画遵循 `prefers-reduced-motion`。
- `aria-live` 只用于必要更新，避免逐 token 朗读 AI 输出。

AI 流式输出建议：

- 可视区域实时更新。
- 屏幕阅读器状态区只宣布“正在生成”和“回答完成”。
- 完成后再将完整回答作为可读内容提供。

## 18. 性能要求

目标：

- LCP ≤ 2.5s。
- INP ≤ 200ms。
- CLS ≤ 0.1。
- 首屏不依赖远程字体、远程图标或远程 CSS。

实现要求：

- 图片必须设置稳定尺寸或 `aspect-ratio`。
- 非首屏图片使用 lazy loading。
- 搜索保留约 150–250ms debounce。
- 不在每个 SSE token 到达时执行昂贵的完整 DOM 重建。
- 大资料库后续可加入虚拟列表，但本轮不强制。
- CSS 背景优先纯色，不加载装饰背景图。
- SVG sprite 仅包含使用到的图标。

## 19. 文件级实施清单

### 19.1 `src/douyin_wiki/webapp/templates/app.html`

修改：

- 重组左栏结构。
- 新增左栏和 AI 栏收起按钮。
- 重组顶部工具栏。
- 新增筛选、排序、视图和密度控件容器。
- 新增已应用筛选区域。
- 为列表和画廊提供单一渲染根节点。
- 重组 AI 头部和来源范围。
- 替换 Unicode 图标为本地 SVG。
- 增加主题切换入口。
- 保留现有 ID，或同步更新 JavaScript 和测试。
- 补齐 Dialog 的 label 关系。

### 19.2 `src/douyin_wiki/webapp/templates/model_settings.html`

修改：

- 使用统一 shell 和 token。
- 降低页面标题尺寸。
- 简化多栏布局。
- 加入主题入口或继承主应用主题。
- 替换 Unicode 图标。
- 保留表单字段 ID 和现有业务行为。

### 19.3 `src/douyin_wiki/webapp/static/app.css`

重构顺序：

1. tokens
2. reset/base
3. accessibility utilities
4. layout shell
5. sidebar
6. toolbar
7. filters and menus
8. library list/gallery
9. article
10. chat
11. dialog/toast
12. settings
13. responsive
14. dark theme
15. reduced motion/print

要求：

- 删除当前八套 placeholder gradient。
- 大幅减少 backdrop blur。
- 删除普遍使用的大阴影。
- 删除 9–10px 可操作文字。
- 不允许出现未解释的新色值和任意圆角。

### 19.4 `src/douyin_wiki/webapp/static/app.js`

修改：

- 扩展状态：view、density、sort、多值筛选、panel 状态、theme。
- 拆分 renderLibrary 为 list/gallery 渲染器。
- 将 UI 偏好写入 localStorage。
- 将搜索、筛选和排序写入 URL。
- 页面启动时从 URL 和 localStorage 恢复状态。
- 增加主题管理。
- 增加面板收起。
- 增加 command/search overlay。
- 增加统一键盘事件处理。
- 改进移动端抽屉焦点和背景处理。
- 引用增加预览和键盘操作。
- 避免使用 `innerHTML` 处理不可信数据；保留当前服务端清理后的文章和聊天 HTML 边界。

建议函数边界：

```text
readPreferences()
writePreference(key, value)
readStateFromURL()
writeStateToURL()
applyTheme()
renderToolbarState()
renderAppliedFilters()
renderLibraryList()
renderLibraryGallery()
renderLibraryEmptyState()
openCommandMenu()
closeCommandMenu()
setSidebarOpen()
setChatPanelOpen()
trapDrawerFocus()
```

### 19.5 `src/douyin_wiki/webapp/static/model-settings.js`

修改：

- 共享主题逻辑或复用一个新增的 `theme.js`。
- 保留连接测试、保存和 API Key 行为。
- 为 loading、success、warning、error 增加语义状态。

### 19.6 建议新增静态文件

```text
src/douyin_wiki/webapp/static/icons.svg
src/douyin_wiki/webapp/static/theme.js      # 如果主页面与设置页需要共享
```

不要新增完整前端构建目录。

### 19.7 `tests/test_web.py`

新增断言：

- `/` 返回 200。
- 主页面包含主要 landmark 和可访问名称。
- 主页面引用本地图标资源，不引用外部 CDN。
- 页面包含列表/画廊视图控件。
- 页面包含主题控制入口。
- 页面包含可收起 AI 面板控制。
- 模型设置页继承相同主题结构。
- 静态图标文件可访问。
- 现有 API、安全、聊天和灵感测试全部继续通过。

不要用脆弱的完整 HTML snapshot；只断言关键结构和行为。

## 20. 实施阶段

### 阶段 P0：保护基线

- [ ] 运行当前测试并记录结果。
- [ ] 确认现有页面、文章、AI、灵感和设置行为。
- [ ] 不覆盖工作区内与本任务无关的修改。
- [ ] 建立本文件列出的视觉和功能验收清单。

完成条件：现有行为和失败情况已知。

### 阶段 P1：视觉基线

- [ ] 引入完整 design tokens。
- [ ] 实现亮色和暗色主题。
- [ ] 加入 focus-visible 和 reduced-motion。
- [ ] 替换字体、字号、间距、圆角和阴影。
- [ ] 加入本地图标系统。
- [ ] 统一基础 button/input/select/textarea 样式。

完成条件：即使不调整信息架构，所有页面已具有一致的新视觉语言。

### 阶段 P2：资料库结构

- [ ] 重组左栏。
- [ ] 重组顶部工具栏。
- [ ] 实现默认列表视图。
- [ ] 保留并重设计画廊视图。
- [ ] 实现视图和密度偏好。
- [ ] 实现筛选 chips、排序和清除。
- [ ] URL 恢复状态。
- [ ] 完成资料库加载、空、无结果和错误状态。

完成条件：资料库能高效浏览，并可在刷新后恢复视图和筛选。

### 阶段 P3：文章与 AI

- [ ] 重设计文章头部和正文排版。
- [ ] 重设计 AI 头部、来源范围、消息和输入区。
- [ ] 引用字号、层级和操作完善。
- [ ] AI 栏可收起。
- [ ] 流式生成不产生明显布局抖动。
- [ ] 灵感 Dialog 完成视觉和焦点改造。

完成条件：文章阅读和 AI 对话形成清晰的证据闭环。

### 阶段 P4：设置、响应式与无障碍

- [ ] 重设计模型设置页。
- [ ] 完成 1280/960/640/320 断点。
- [ ] 完成移动端抽屉焦点管理。
- [ ] 完成键盘快捷键。
- [ ] 检查对比度、焦点、landmark、label、heading。
- [ ] 检查 200% 文本缩放和 400% 页面缩放。
- [ ] 检查 reduced motion。

完成条件：WCAG 2.2 AA 关键要求和移动端行为通过人工检查。

### 阶段 P5：测试和收尾

- [ ] 更新 `tests/test_web.py`。
- [ ] 运行 Web 相关测试。
- [ ] 运行全量测试。
- [ ] 运行 Ruff。
- [ ] 用 Playwright 检查主要视口。
- [ ] 检查亮色、暗色和系统主题。
- [ ] 检查无封面、长标题、大量标签和长 AI 回复。
- [ ] 检查网络/模型错误。
- [ ] 检查没有外部 CDN 和意外网络请求。

完成条件：自动化测试通过，验收矩阵无阻塞项。

## 21. 验证命令

使用项目已有环境：

```bash
uv run pytest tests/test_web.py -q
uv run pytest -q
uv run ruff check .
```

启动本地 Web 应用时，优先使用项目 README 已记录的现有命令，不在本文件中引入新的启动方式。

建议视觉检查视口：

```text
1440 × 1000  桌面三栏
1280 × 800   最小完整三栏
1024 × 768   AI 抽屉
768 × 1024   平板
390 × 844    手机
320 × 568    最窄重排
```

## 22. 人工验收矩阵

### 22.1 视觉

- [ ] 整体呈暖灰低噪声风格。
- [ ] 页面中没有大面积无意义渐变。
- [ ] 普通列表卡片没有大阴影。
- [ ] 圆角主要集中在 6–12px。
- [ ] 页面标题不超过 32px。
- [ ] 可操作文字不小于 13px。
- [ ] 引用和隐私说明不小于 12px。
- [ ] 图标风格一致。
- [ ] 亮暗主题都不存在不可读文字。

### 22.2 资料库

- [ ] 默认列表更适合扫描知识条目。
- [ ] 用户可以切换列表和画廊。
- [ ] 视图偏好刷新后保留。
- [ ] 筛选清晰显示且可单独删除。
- [ ] URL 可以恢复搜索、筛选和排序。
- [ ] 长标题不会破坏布局。
- [ ] 没有封面的条目仍然美观。
- [ ] 大量标签不会撑爆卡片。

### 22.3 文章

- [ ] 中文正文宽度舒适。
- [ ] 正文默认无衬线。
- [ ] 标题层级清晰。
- [ ] 图片、表格、引用和列表风格统一。
- [ ] 原作品入口清晰。
- [ ] 返回、前进、直接链接都正常。

### 22.4 AI

- [ ] 当前来源范围始终可见。
- [ ] 用户能明确判断回答基于全库还是当前文章。
- [ ] 引用不小于 12px。
- [ ] 引用支持鼠标和键盘操作。
- [ ] 流式回复稳定。
- [ ] Enter/Shift+Enter 正常。
- [ ] 模型未配置和连接错误有持久反馈。
- [ ] AI 面板能收起并恢复。

### 22.5 移动端

- [ ] 左栏和 AI 抽屉不会同时失控。
- [ ] 抽屉打开后焦点正确。
- [ ] 背景不能误操作。
- [ ] 所有主要按钮至少 44px。
- [ ] 320px 宽度无整页横向滚动。
- [ ] 输入法弹出时聊天输入区仍可操作。

### 22.6 无障碍

- [ ] 仅使用键盘可以完成主要工作流。
- [ ] focus 始终清晰可见。
- [ ] Esc 可以关闭弹层。
- [ ] 屏幕阅读器可以理解主要区域。
- [ ] 状态不只依靠颜色。
- [ ] 200% 文本缩放不截断。
- [ ] reduced motion 生效。

## 23. 失败条件

出现下列任一情况，本轮不得视为完成：

- 为追求外观破坏现有 API 或安全检查。
- 页面必须联网才能正常显示字体、图标或样式。
- 默认视图仍只有大封面画廊。
- 大量关键文字仍为 9–10px。
- AI 来源范围不可见。
- 引用仍难以阅读或无法键盘访问。
- 主题切换出现明显白屏闪烁且未处理。
- 移动端出现整页横向滚动。
- 删除、保存灵感或模型设置行为回归。
- 现有测试出现未经解释的失败。

## 24. 后续可选升级

本文件全部完成后，再评估：

- React + shadcn/ui + Radix 的前端迁移。
- 可调整宽度的三栏 splitter。
- 虚拟化大型资料列表。
- 自定义保存视图。
- 多选来源后发起 AI 对话。
- 文章内批注和高亮。
- 全局命令中心。
- 关系图谱、时间线和标签聚类。
- 离线 PWA。
- 独立 design-system 演示页。

迁移框架的触发条件应是状态复杂度、组件复用和可维护性，而不是单纯为了换框架。

## 25. 官方参考资料

- Notion 数据库视图、筛选、排序与 side peek：<https://www.notion.com/help/views-filters-and-sorts>
- Notion 搜索与命令入口：<https://www.notion.com/help/search>
- Google Notebook/Gemini Notebook 引用交互：<https://support.google.com/gemininotebook/answer/16179559?hl=en>
- Google Notebook/Gemini Notebook 笔记：<https://support.google.com/gemininotebook/answer/16262519?hl=en>
- Radix 无障碍组件：<https://www.radix-ui.com/primitives/docs/overview/accessibility>
- Web Awesome：<https://webawesome.com/>
- Lucide Icons：<https://lucide.dev/>
- Atlassian Design Foundations：<https://atlassian.design/foundations>
- Atlassian Typography：<https://atlassian.design/foundations/typography/>
- Atlassian Color：<https://atlassian.design/foundations/color>
- W3C WCAG 对比度：<https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html>
- W3C WCAG 目标尺寸：<https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html>
- W3C WCAG Reflow：<https://www.w3.org/WAI/WCAG22/Understanding/reflow.html>
- W3C WCAG Focus Visible：<https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html>
- W3C 中文排版需求：<https://www.w3.org/International/clreq/>
- Core Web Vitals：<https://web.dev/articles/defining-core-web-vitals-thresholds>

---

执行原则：先保护行为，再建立 token；先完成信息架构，再处理装饰；每完成一个阶段都运行相关测试，不在最后一次性验证。
