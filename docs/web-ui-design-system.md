# Web UI 设计系统（Apple 风格重构）

本文档记录 2026-09 Web 界面 Apple 风格视觉重构的设计规范，供后续维护参考。
实现位于 `src/douyin_wiki/webapp/`（`templates/` + `static/`），无前端框架，
Jinja2 模板 + 原生 JS + CSS 变量令牌。

## 设计令牌（app.css `:root` / `[data-theme="dark"]`）

| 令牌 | 亮色 | 暗色 |
|---|---|---|
| `--color-canvas` | `#f5f5f7`（Apple 标志灰） | `#000000`（纯黑） |
| `--color-surface` | `#ffffff` | `#1c1c1e` |
| `--color-text-primary` | `#1d1d1f` | `#f5f5f7` |
| `--color-text-secondary` | `#6e6e73` | `#a1a1a6` |
| `--color-primary` | `#0071e3` | `#0a84ff` |
| `--color-primary-text` | `#0066cc` | `#409cff` |
| 边框 | `rgba(0,0,0,.06/.12/.22)` | `rgba(255,255,255,.08/.14/.24)` |

- 圆角：`--radius-xs/sm/md/lg/xl` = 6/9/12/16/22px，胶囊 `--radius-pill`
- 阴影：`--shadow-card`（柔和静置）、`--shadow-card-lifted`（hover 浮起）、
  `--shadow-popover`、`--shadow-dialog`
- 毛玻璃：`--glass-canvas` / `--glass-surface` + `--glass-blur`（
  `saturate(180%) blur(20px)`），用于侧栏、顶栏、对话面板、Toast
- 动效：`--duration-fast/normal/panel` = 140/200/280ms，
  `--ease-out: cubic-bezier(.23,1,.32,1)`（UI 入场），
  `--ease-standard: cubic-bezier(.32,.72,0,1)`（抽屉），
  `--ease-hover: ease`（颜色/边框）。命令面板零动画。导航选中不缩放。
- 导航瓷贴色：`--nav-library/recent/favorite/inspiration/topic/trash/video/image/settings`，
  亮暗双主题各有色值（iOS 系统色）

## 图标系统

侧栏、顶栏、对话区图标全部为**内联实心 SVG**（SF Symbols 风格），
不再引用 `icons.svg` 精灵图（精灵图仍服务于 JS 渲染的小型内联图标）。

约定：

- 24×24 viewBox，统一包裹 `<g fill="currentColor" stroke="none">`；
  `fill/stroke` 属性写在 `<g>` 上，可压过 `app.css` 全局 `svg` 规则的继承值
- 细节用 `fill-rule="evenodd"` 镂空（如钟表指针、垃圾桶槽、播放三角），
  镂空处透出瓷贴底色，选中反白时自动反色，无需双套图标
- 次要层级用 `opacity=".35/.45/.6"` 同色系半透明

## 侧栏瓷贴语言

- 导航图标为 28px 圆角灰底瓷贴；仅当前项用主色实心 + 白图形。Chrome 单色，封面才是彩的。
- 侧栏分组：资料（资料库/最近/收藏/灵感）→ 已保存视图 → 标签 → 操作（导入/任务/专题/废纸篓）→ 设置。
- 对话栏默认收起，桌面从右侧 overlay 滑入，不占第三列。
- hover 不位移导航文字，且包在 `@media (hover: hover) and (pointer: fine)`；可点按元素 `:active` 为 `scale(.97)`。
- 页面加载：导航不做入场动画。⌘K 命令面板无动画。

## 视图过渡（卡片 → 文章）

`app.js openArticle()` 使用 `document.startViewTransition`（Safari 18+/Chrome），
不支持时直接切换，不整页淡入。关键规则（`app.css`）：

- 专辑墙封面与文章页 `.article-cover` 共享 `viewTransitionName: "active-album-cover"`，
  两者都是 3:4，组动画 280ms `--ease-standard`，旧/新封面不做交叉淡化
- 侧栏、顶栏、对话栏有独立 `view-transition-name`，过渡期间保持原地
- 根图层仅 160ms 淡入淡出（正文区域），`mix-blend-mode: normal`
- 过渡快照生成前调用 `hideCardOverlays()` 隐藏卡片浮动按钮
  （收藏/专题指示器），避免其留在根图层形成"残影"；结束恢复 `visibility`

## 品牌资产

- `static/brand-mark.png`：256×256 TikTok 风 logo（黑底 + 白色音符 +
  青/品红错位重影），AI 生成后经过去水印与压缩；用于主页面与设置页品牌区
- AI 相关标识（对话区徽标、折叠入口 FAB）使用紫色渐变
  `linear-gradient(180deg, #c96ef6, #a83ae6)` + 顶部高光 + 同色投影

## 材质与组件

- 弹窗：背景 `blur(10px) saturate(160%)` 磨砂，`dialog-enter` 弹性入场
- 浮层（筛选/对话菜单）：opacity + `scale(.97)` 的 CSS transition，可中断
- 消息气泡：用户=主色实心，AI=白底 + `--shadow-card`；发送钮为 32px
  圆形，按下 `scale(.97)`，hover 不覆盖 transform
- 所有动效受全局 `prefers-reduced-motion` 规则约束

## 测试契约

`tests/test_web.py` 断言的前端契约（改动时需同步维护）：

- CSS 含 `--color-canvas`、`[data-theme="dark"]`、`.gallery-cover`、
  `.article-cover`、`aspect-ratio: 3 / 4`、`.article-body { max-inline-size: 40rem`、
  `::view-transition-group(active-album-cover)`
- HTML 含各 `aria-label`（资料库导航/抖库 AI 对话/专辑墙视图等）、
  `data-theme-select`、`id="chat-close"`、`id="applied-filters"`

## 本地预览（隔离运行实例）

```bash
DOUYIN_WIKI_CONFIG=$PWD/.local-test/config.toml uv run douyin-wiki web run
# 端口 8766，复用真实 Vault 只读浏览；不影响 8765 常驻实例
uv run pytest tests/test_web.py
```
