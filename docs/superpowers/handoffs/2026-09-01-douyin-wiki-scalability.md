# douyin-wiki 知识库扩展性 Handoff

> 交接日期：2026-09-01
>
> 目标读者：接手 `douyin-wiki` 知识库扩展性工作的开发者或 Agent。
>
> 本文件是架构建议和实施入口，不表示下方改造已经完成。

## 1. 目标

让 `douyin-wiki` 在知识条目从当前规模扩展到几百、几千篇时，仍然能够：

- 快速打开 Web 页面和目录；
- 分页浏览、全文搜索和语义搜索；
- 持续执行下载、ASR、OCR、分析和重新索引任务；
- 不因为一次请求加载全部 Markdown、正文、向量或媒体而造成明显内存增长；
- 在 embedding 模型更换、任务失败、进程重启或文件变更后可以恢复；
- 保持 Markdown、SQLite、媒体文件和 Git 之间边界清楚、可备份、可重建。

第一阶段以 1,000 篇知识条目为目标；第二阶段验证 5,000 篇；超过这个规模前，先根据基准测试决定是否拆出专门的向量或搜索服务。

## 2. 当前架构和已确认事实

先阅读：

- `README.md`：用户安装、Vault 布局、数据库重建、后台服务和 Web 行为；
- `src/douyin_wiki/database.py`：SQLite schema、WAL、FTS5、事务和索引；
- `src/douyin_wiki/search.py`：词法检索、embedding、余弦相似度和索引兼容性；
- `src/douyin_wiki/webapp/catalog.py`：Web 目录缓存、文件扫描和目录过滤；
- `src/douyin_wiki/webapp/app.py`：Web API 和页面数据装配；
- `src/douyin_wiki/worker.py`：任务租约、心跳、并发和恢复；
- `src/douyin_wiki/vault.py`：Markdown 写入、文件锁、原子写入和 Vault 目录结构；
- `src/douyin_wiki/service.py`：macOS LaunchAgent 的安装、卸载和维护任务。

当前有利条件：

- SQLite 已使用 WAL、外键、FTS5 和多张业务索引表；
- `chunks`、`chunks_fts`、任务、条目、关系和 Web 聊天数据已经有持久化模型；
- Worker 已有租约和 heartbeat 基础，不需要重新发明任务系统；
- Markdown 是可读的知识产物，数据库可以从持久化资料重建；
- Web 和 Worker 已经能够通过 macOS LaunchAgent 常驻运行。

当前主要风险：

- `webapp/catalog.py` 在进程内保存 `_items`，过滤时会拼接标题、摘要、标签和完整正文后做 Python 字符串匹配；
- Web 目录可能一次性读取过多 Markdown，而不是让 SQLite 完成筛选和分页；
- `search.py` 的 embedding 兼容性变更会触发全量重建，条目增加后可能长时间阻塞；
- 向量以 JSON 文本保存和解析，向量数量增加后会带来数据库体积和查询开销；
- 视频、图片、封面和临时文件会比 Markdown 条目更快消耗磁盘；
- 如果外部 HTML 知识库直接操作 `wiki/sources/`，必须避免它拥有删除正式知识源文件的权限。

## 3. 目标架构

```text
抖音链接 / 博主目录 / 文件变更
          ↓
     Worker 任务队列
          ↓
下载、ASR、OCR、分析、人工修正
          ↓
┌──────────────────────────────┐
│ 持久化知识层                  │
│ Markdown：人类可读的资料产物  │
│ SQLite：状态、元数据、FTS、向量│
│ 媒体目录：视频、图片、封面     │
└──────────────────────────────┘
          ↓
Web/API：SQLite 查询、游标分页、按需加载正文
          ↓
用户浏览、全文检索、语义检索、证据回溯
```

### 3.1 数据边界

- Markdown 和原始/分析产物是可读、可备份的持久化资料。
- SQLite 保存任务状态、条目元数据、关系、检索块、FTS 和聊天状态。
- SQLite 检索表必须可以从 Markdown/机器侧车重建；不要让一次缓存成为唯一数据源。
- 媒体文件保留在 Vault 的媒体目录中，不进入 Git；数据库只保存路径、哈希、大小、类型和生命周期状态。
- `.douyin-wiki/` 保存任务、索引、向量和临时数据，不能成为不可恢复的唯一知识副本。

### 3.2 查询边界

- 目录列表只返回标题、作者、摘要、标签、状态、封面路径和更新时间等轻量字段。
- 搜索由 SQLite FTS5 和元数据条件完成；Web 进程不逐条读取完整正文进行过滤。
- 详情页、预览和证据查看时才读取完整 Markdown 或机器侧车。
- 所有列表接口必须分页；默认返回 50 条，最大返回 100 条。
- 使用 `(updated_at, id)` 等稳定排序键实现游标分页，避免深页 `OFFSET` 逐渐变慢。

## 4. 实施顺序

### 阶段 0：建立基线和保护网

目的：在改架构前知道当前耗时、内存、数据库大小和检索质量，避免凭感觉优化。

涉及文件：

- `tests/test_database.py`
- `tests/test_search.py`（若当前不存在，创建）
- `tests/test_web.py`
- `src/douyin_wiki/database.py`
- `src/douyin_wiki/search.py`
- `src/douyin_wiki/webapp/catalog.py`

工作项：

- 生成 100、1,000、5,000 条合成条目，包含不同长度的正文、标签和知识块；
- 记录首次建立索引、增量更新、全文搜索、语义搜索和目录打开的耗时；
- 记录 Python Web 进程 RSS、SQLite 文件大小、FTS 大小、向量数据大小和 Worker 内存；
- 为目录 API 增加测试，确认默认不会返回完整正文数组；
- 为删除/重建/进程重启增加恢复测试，确认索引可以重建而不是依赖内存状态。

验收：基准数据可重复生成；每次架构修改都能比较同一组数据；现有测试全部通过。

### 阶段 1：数据库驱动的 Web 目录和搜索

这是最高优先级，因为当前 Web 目录的全正文 Python 过滤会最先影响几千篇场景。

涉及文件：

- `src/douyin_wiki/database.py`
- `src/douyin_wiki/webapp/catalog.py`
- `src/douyin_wiki/webapp/app.py`
- `src/douyin_wiki/webapp/static/app.js`
- `tests/test_database.py`
- `tests/test_web.py`

工作项：

- 在 `database.py` 增加目录查询接口：关键词、作者、标签、来源类型、状态、更新时间和游标均作为 SQL 条件；
- 复用或扩展现有 FTS5 表，不新增第二套全文索引；
- 让 `catalog.py` 只负责参数转换、结果 DTO 和有限的缓存，不再缓存所有正文；
- 让 Web API 返回 `items`、`next_cursor`、`total`（如能低成本计算）和当前筛选条件；
- 前端滚动或翻页时请求下一页，详情弹窗单独请求正文；
- 保留文件监听，但文件变更只刷新受影响的条目或使对应缓存失效，不重新读取整个 Vault；
- 对不存在的 Markdown、失效封面和孤儿数据库记录返回可解释状态，不让单个坏文件阻断整个目录。

验收条件：

- 1,000 条数据时，目录首屏最多读取一页，不读取全部正文；
- 5,000 条数据时，关键词搜索仍然通过 SQLite FTS5 完成；
- 详情页仍能正确展示完整 Markdown、图片和来源链接；
- 文件新增、修改、删除后，相关条目最终能出现在正确的搜索结果中；
- 测试验证分页不会重复或跳过条目。

### 阶段 2：增量索引和可恢复的文件同步

涉及文件：

- `src/douyin_wiki/vault.py`
- `src/douyin_wiki/database.py`
- `src/douyin_wiki/webapp/catalog.py`
- `src/douyin_wiki/service.py`
- `tests/test_web.py`
- `tests/test_database.py`

工作项：

- 为资料文件保存路径、大小、mtime 和内容哈希；
- 文件变更时只解析和索引变化的条目；
- 监听事件使用 debounce，合并同一条目的连续写入；
- 定期运行完整 reconciliation，处理监听器漏事件、移动、删除和恢复的文件；
- 增量同步过程采用“先写文件、再单事务更新索引”的顺序，并为中断留下可重试状态；
- 提供明确的 `database rebuild` 或维护命令，用于从 Vault 重建检索投影；
- 对扫描耗时、变更数量、失败数量和跳过数量写入日志或维护记录。

验收条件：修改一篇资料不会触发所有资料重解析；杀掉进程后再次启动可以继续同步；从干净 SQLite 重建后搜索结果与正常运行结果一致。

### 阶段 3：embedding 索引版本和后台重建

涉及文件：

- `src/douyin_wiki/search.py`
- `src/douyin_wiki/database.py`
- `src/douyin_wiki/worker.py`
- `src/douyin_wiki/models.py`
- `tests/test_search.py`
- `tests/test_database.py`

工作项：

- 给 embedding 索引增加模型名、维度、归一化方式、版本号和创建时间；
- 模型或签名变化时创建新索引版本，不在 Web 请求中同步全量重建；
- 使用可恢复的后台任务逐条或分批生成向量；
- 支持失败重试、暂停、继续和查看进度；
- 新索引未完成前继续使用旧索引；完成并校验后再原子切换活动版本；
- 在向量数量明显增加后，将 JSON 向量迁移为 float32 BLOB 或专用向量存储；
- 只有基准测试证明 SQLite 全表余弦扫描不足时，才引入 ANN 或独立向量数据库。

验收条件：更换 embedding 模型不会阻塞 Web；重建中途退出后可以继续；切换前后搜索接口都能返回可解释的索引版本；旧版本在新版本校验失败时仍可回退。

### 阶段 4：媒体、备份和存储生命周期

涉及文件：

- `src/douyin_wiki/config.py`
- `src/douyin_wiki/vault.py`
- `src/douyin_wiki/database.py`
- `src/douyin_wiki/service.py`
- `tests/test_media.py`
- `tests/test_service.py`
- `README.md`

工作项：

- 为视频、原图、封面、OCR 图片和临时文件记录大小、哈希、来源、创建时间和清理状态；
- 使用稳定的 entry/video ID 组织媒体目录，避免单目录堆积数千文件；
- 对重复下载内容去重；
- 区分原始资料、派生资料、临时文件和可删除缓存；
- 提供磁盘占用统计、过期缓存清理和明确的回收策略；
- 备份 SQLite、Markdown、机器侧车和配置；媒体按策略备份，不提交到 Git；
- 提供维护前的数据库备份和维护后的完整性检查。

验收条件：删除或清理不会误删仍被条目引用的媒体；恢复备份后可以重新生成 SQLite 检索投影；Git 状态不会因为视频和大图片持续膨胀。

### 阶段 5：后台服务和性能运营

涉及文件：

- `src/douyin_wiki/service.py`
- `src/douyin_wiki/worker.py`
- `README.md`
- `tests/test_service.py`

工作项：

- 保留 Web、Worker、维护任务分进程运行；
- 增加轻量、普通、完整三种运行配置：Web only、Web + Worker、Web + Worker + maintenance；
- 模型只在 Worker 的实际任务中按需加载；
- 为下载、ASR、OCR、分析、embedding 设置独立并发上限和资源类别；
- 队列满时产生 backpressure，不继续无限制接收任务；
- 为任务、扫描、索引和数据库查询提供耗时、失败率、队列长度和内存指标；
- 在 LaunchAgent 重启后检查数据库锁、孤儿任务和未完成索引版本。

验收条件：Web 进程不因模型加载而占用大量内存；Worker 重启后任务不会永久卡在 running；维护任务不会与大批量分析任务无限争抢资源。

## 5. 不要提前做的事情

- 不要为了几千篇条目立刻引入 Elasticsearch、Milvus 或云端数据库。
- 不要同时维护一套 Markdown 搜索逻辑和一套独立的 Web 内存搜索逻辑。
- 不要把完整正文、全部封面或全部向量加载到 Web 进程内存。
- 不要把视频、图片和临时文件放进 Git。
- 不要把数据库中的 embedding JSON 一次性改成复杂向量基础设施，先完成基准测试和版本化重建。
- 不要让通用 HTML 知识库对 `douyin-wiki` 的正式源目录拥有默认删除权限。

## 6. 与 HTML 知识库的集成边界

如果以后需要让 `/Users/weisengao/Documents/ChatGPT/HTML 知识库` 浏览 `douyin-wiki` 的资料：

- 优先让它读取 `wiki/sources/` 以及必要的 Markdown 导出目录；
- `douyin-wiki` 仍然是采集、分析、正式资料和删除操作的管理方；
- HTML 知识库把该目录登记为只读外部来源；
- 不复制完整正文到第二份“正式数据库”；
- 使用 `entry_id` 或 `video_id` 做跨系统稳定标识；
- HTML 侧的索引失效不应影响 `douyin-wiki` 的任务、SQLite 或 Markdown；
- HTML 侧删除默认只移除自己的索引，不删除 `douyin-wiki` 的文件。

## 7. 性能验收基线

以下是第一版可执行的目标，最终数值应在当前 Mac 上用阶段 0 的基准数据校准：

| 场景 | 目标 |
|---|---|
| 1,000 条目录首屏 | 只查询并返回一页，不读取全库正文 |
| 1,000 条全文搜索 | 通过 SQLite FTS5 完成，默认最多返回 50 条 |
| 5,000 条目录翻页 | 游标分页，不因页码增加而线性读取全部前页 |
| 修改 1 条 Markdown | 只更新受影响条目的元数据、FTS 和向量 |
| embedding 重建 | 后台可暂停、恢复、重试，Web 仍可使用 |
| Web 内存 | 不随条目正文总量线性增长 |
| 进程重启 | 任务、索引版本和文件同步状态可恢复 |
| SQLite 重建 | 从持久化资料重建后，全文检索结果可用 |

## 8. 推荐开发顺序

1. 先做阶段 0 基准和测试保护网。
2. 先改阶段 1 的 Web 目录和搜索，因为这是当前最明显的扩展性瓶颈。
3. 再做阶段 2 的增量同步，减少文件扫描和重复解析。
4. 再做阶段 3 的 embedding 版本化和后台重建。
5. 根据实际磁盘增长进入阶段 4 媒体治理。
6. 最后完善阶段 5 的服务配置和运营指标。

每个阶段都应独立测试、独立提交，并在提交前运行：

```bash
cd "/Users/weisengao/Documents/ChatGPT/douyin-wiki"
uv run pytest
```

如果涉及 Web 页面，再补充现有 Web 测试和本地手工检查：

```bash
uv run douyin-wiki web run
```

## 9. 最终交接结论

`douyin-wiki` 目前不需要推倒重来。SQLite、FTS5、Markdown Vault、Worker 和 LaunchAgent 已经构成了可以扩展的基础。后续工作的核心不是换技术栈，而是把 Web 目录从“全量文件读入后在 Python 内存过滤”改为“数据库查询、分页和按需加载”，再把 embedding 重建、媒体生命周期和备份恢复做成可观察、可恢复的后台流程。

达到 1,000 篇时，SQLite + FTS5 + 后台 Worker 足够；达到 5,000 篇时，继续保留这套架构并根据基准测试优化；只有在向量数量、查询延迟或媒体规模确实超过 SQLite 和本地文件系统的承受范围后，才拆分专门服务。
