from __future__ import annotations

import json
import warnings
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .config import load_config
from .errors import DouyinWikiError
from .localization import (
    add_display_labels,
    parse_creator_decision,
    parse_job_status,
    parse_retention,
)
from .models import (
    CaptureOptions,
    GatewayContext,
    InspirationInput,
    ReviewIssue,
    SourceKind,
    TranscriptCorrection,
)
from .service import DouyinWikiService

INSTRUCTIONS = """这是抖库，本地抖音知识库默认由当前 Gateway Agent 完成 AI 校正与分析。
工具会保留英文内部状态码供程序判断，并同时返回对应的中文 label 字段；面向用户回复时只能
使用中文 label，不得展示英文状态码。
采集时调用 capture_douyin，并传入 gateway_context 以便异步结果回到原会话。任务显示为
“待 AI 处理”后：调用 get_analysis_context；先调用 submit_transcript_correction，
如 source_kind=image_note 且 phase=analysis，则直接根据 analysis_schema 调用
submit_gateway_analysis；视频在无人工疑点后再提交分析。显示“需要人工复核”时必须向用户展示
疑点并调用 resolve_review；显示“需要登录授权”时先调用 get_auth_status，再根据 auth_scope
提示用户运行
douyin-wiki auth video 或 douyin-wiki auth douyin，并在登录后重试；
“等待用户确认”表示长视频将消耗 Agent 模型
token，必须经用户确认后调用 approve_job。无常驻 Agent 时可用 list_job_events 轮询可操作
事件，处理成功后调用 acknowledge_job_event。查询时优先调用 search_knowledge，并保留
original_url，以及视频 timestamp_ms 或图文 image_index；创建提醒前必须获得用户明确确认。
博主批量采集调用 capture_douyin_creator。任务显示为“待选择作品”后，调用
get_creator_inventory 一次读取并展示全部作品，再用 set_creator_work_selection 保存选择；全部作品均已
选择或跳过后才可调用 confirm_creator_import。
带 favorites_context.batch_silent=true 或 creator_context.batch_silent=true 的子任务仍需
完成校正和分析，但不要逐条向用户发送完成消息；以父任务汇总结果为准。同步只在用户明确调用
sync_creator 时执行，不得自动或定时访问博主主页。
专题研究必须使用 create_topic 等专题工具；search_topic 只检索当前启用来源。专题没有相关证据时
明确回复“当前专题没有相关证据”，不得调用 search_knowledge 回退到全库。成果只在用户明确要求时
调用 generate_topic_artifact，专题笔记只有用户确认后才能调用 save_topic_note。
"""


class DouyinWikiMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause: BaseException | None = exc
            while cause is not None and not isinstance(cause, DouyinWikiError):
                cause = cause.__cause__
            if isinstance(cause, DouyinWikiError):
                payload = {
                    "error": {
                        "code": cause.code,
                        "message": str(cause),
                        "details": cause.details,
                    }
                }
                raise ToolError(
                    json.dumps(payload, ensure_ascii=False, default=str)
                ) from cause
            raise


with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Field 'lifespan' has an incomplete definition.*")
    mcp = DouyinWikiMCP("抖库", instructions=INSTRUCTIONS)

_SERVICE: DouyinWikiService | None = None


def _service() -> DouyinWikiService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = DouyinWikiService(load_config())
        _SERVICE.initialize_runtime()
    return _SERVICE


def _payload(value: Any) -> Any:
    return add_display_labels(value)


@mcp.tool()
def capture_douyin(
    share_text: str,
    inspirations: list[dict[str, Any]] | None = None,
    retention: str = "temporary",
    allow_long: bool = False,
    approve_cloud_analysis: bool = False,
    approve_ai_analysis: bool = False,
    gateway_context: dict[str, Any] | None = None,
    purposes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """提交分享文本和灵感并返回 job_id；Gateway 应同时传入原会话路由上下文。"""
    values = inspirations if inspirations is not None else purposes or []
    job = _service().capture_douyin(
        share_text,
        [InspirationInput.model_validate(item) for item in values],
        CaptureOptions(
            retention=parse_retention(retention),
            allow_long=allow_long,
            approve_cloud_analysis=approve_ai_analysis or approve_cloud_analysis,
        ),
        GatewayContext.model_validate(gateway_context) if gateway_context else None,
    )
    return _payload(job)


@mcp.tool()
def capture_douyin_creator(
    source_text: str,
    inspirations: list[dict[str, Any]] | None = None,
    retention: str = "temporary",
    allow_long: bool = False,
    approve_ai_analysis: bool = False,
    gateway_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从博主主页或其任意作品清点公开作品；清点阶段不下载媒体或调用 AI。"""
    job = _service().capture_douyin_creator(
        source_text,
        [InspirationInput.model_validate(item) for item in (inspirations or [])],
        CaptureOptions(
            retention=parse_retention(retention),
            allow_long=allow_long,
            approve_cloud_analysis=approve_ai_analysis,
        ),
        GatewayContext.model_validate(gateway_context) if gateway_context else None,
    )
    return _payload(job)


@mcp.tool()
def get_creator_inventory(
    job_id: str,
    page: int | None = None,
    limit: int | None = None,
    decision: str | None = None,
    source_kind: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """默认一次读取全部带封面的作品；超大清单仍可显式传 page/limit。"""
    return _payload(
        _service().get_creator_inventory(
            job_id,
            page=page,
            limit=limit,
            decision=parse_creator_decision(decision) if decision else None,
            source_kind=SourceKind(source_kind) if source_kind else None,
            query=query,
        )
    )


@mcp.tool()
def set_creator_work_selection(
    job_id: str,
    decision: str,
    ordinals: list[int] | None = None,
    work_ids: list[str] | None = None,
) -> dict[str, Any]:
    """把清单作品标记为“已选入库”或“未入库”；编号在本次清单中稳定。"""
    return _payload(
        _service().set_creator_work_selection(
            job_id,
            parse_creator_decision(decision),
            ordinals=ordinals,
            work_ids=work_ids,
        )
    )


@mcp.tool()
def confirm_creator_import(job_id: str, accept_partial: bool = False) -> dict[str, Any]:
    """所有作品均完成决定后，确认并只派发“已选入库”的作品。"""
    return _payload(_service().confirm_creator_import(job_id, accept_partial=accept_partial))


@mcp.tool()
def import_creator_works(
    creator_id: str,
    work_ids: list[str],
    gateway_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把之前跳过的指定作品改为需要并创建采集任务。"""
    return _payload(
        _service().import_creator_works(
            creator_id,
            work_ids,
            gateway_context=(
                GatewayContext.model_validate(gateway_context) if gateway_context else None
            ),
        )
    )


@mcp.tool()
def get_creator(creator_id: str) -> dict[str, Any]:
    """读取博主资料、独立目录和作品状态汇总。"""
    return _payload(_service().get_creator(creator_id))


@mcp.tool()
def list_creators() -> list[dict[str, Any]]:
    """列出已经建立的博主知识目录。"""
    return _payload(_service().list_creators())


@mcp.tool()
def list_creator_works(creator_id: str) -> list[dict[str, Any]]:
    """列出博主全部已发现作品及其选择、入库和可用状态。"""
    return _payload(_service().list_creator_works(creator_id))


@mcp.tool()
def sync_creator(creator_id: str, gateway_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """仅在用户明确调用时完整同步一次主页；不会创建定时任务。"""
    return _payload(
        _service().sync_creator(
            creator_id,
            gateway_context=(
                GatewayContext.model_validate(gateway_context) if gateway_context else None
            ),
        )
    )


@mcp.tool()
def get_job(job_id: str) -> dict[str, Any]:
    """读取任务进度、疑点、错误和提醒候选。"""
    return _payload(_service().get_job(job_id))


@mcp.tool()
def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """列出最近任务，可按状态过滤。"""
    value = parse_job_status(status) if status else None
    return _payload(_service().list_jobs(value, limit))


@mcp.tool()
async def get_auth_status(video_url: str | None = None) -> dict[str, Any]:
    """检查视频浏览器 Cookie 与图文专用会话；不返回或上传 Cookie 值。"""
    return _payload(await _service().get_auth_status(video_url=video_url))


@mcp.tool()
def list_job_events(
    after_event_id: int = 0,
    unacknowledged_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出待 Gateway 处理或投递的持久事件；不会调用模型。"""
    return _payload(
        _service().list_job_events(
            after_event_id=after_event_id,
            unacknowledged_only=unacknowledged_only,
            limit=limit,
        )
    )


@mcp.tool()
def acknowledge_job_event(event_id: int) -> dict[str, Any]:
    """Gateway 成功处理或投递事件后确认，防止重复通知。"""
    return _payload(_service().acknowledge_job_event(event_id))


@mcp.tool()
def get_analysis_context(job_id: str) -> dict[str, Any]:
    """读取分析所需的逐字稿或图文正文、逐图 OCR、灵感、既有知识和输出 schema。"""
    return _payload(_service().get_analysis_context(job_id))


@mcp.tool()
def submit_transcript_correction(
    job_id: str,
    corrections: list[dict[str, Any]],
    producer: str,
    model: str = "agent",
    review_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Gateway Agent 提交逐字稿校正版；原始 ASR 永不覆盖。"""
    return _payload(
        _service().submit_transcript_correction(
            job_id,
            [TranscriptCorrection.model_validate(item) for item in corrections],
            producer=producer,
            model=model,
            review_issues=[ReviewIssue.model_validate(item) for item in (review_issues or [])],
        )
    )


@mcp.tool()
def submit_gateway_analysis(
    job_id: str,
    analysis: dict[str, Any],
    producer: str,
    model: str = "agent",
) -> dict[str, Any]:
    """Gateway Agent 提交符合 analysis_schema 的结果并让 Worker 确定性入库。"""
    return _payload(
        _service().submit_gateway_analysis(job_id, analysis, producer=producer, model=model)
    )


@mcp.tool()
def approve_job(job_id: str) -> dict[str, Any]:
    """用户确认后，批准超过 30 分钟的视频继续消耗 Agent/模型 token。"""
    return _payload(_service().approve_job(job_id))


@mcp.tool()
def retry_job(job_id: str) -> dict[str, Any]:
    """重试失败或需要登录授权的任务；保留已完成阶段产物并从最近检查点继续。"""
    return _payload(_service().retry_job(job_id))


@mcp.tool()
def resolve_review(
    job_id: str, resolutions: dict[str, str] | None = None, accept_uncertain: bool = False
) -> dict[str, Any]:
    """解决全部低置信逐字稿或逐图 OCR 疑点，或明确接受不确定内容。"""
    return _payload(
        _service().resolve_review(job_id, resolutions, accept_uncertain=accept_uncertain)
    )


@mcp.tool()
def submit_analysis(
    entry_id: str,
    analysis: dict[str, Any],
    producer: str,
    model: str = "agent",
) -> dict[str, Any]:
    """Agent 提交符合结构化 schema 的补充或重分析结果，并记录生成者。"""
    return _payload(_service().submit_analysis(entry_id, analysis, producer=producer, model=model))


@mcp.tool()
def reanalyze_entry(
    entry_id: str,
    force: bool = False,
    gateway_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """复用现有逐字稿和 OCR，为单条资料排队生成 v2 分析；不下载媒体。"""
    job = _service().reanalyze_entry(
        entry_id,
        force=force,
        gateway_context=(
            GatewayContext.model_validate(gateway_context) if gateway_context else None
        ),
    )
    return _payload(job)


@mcp.tool()
def reanalyze_all(
    force: bool = False,
    gateway_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """批量排队重分析所有 v1 资料；默认跳过已是 v2 的条目。"""
    return _payload(
        _service().reanalyze_all(
            force=force,
            gateway_context=(
                GatewayContext.model_validate(gateway_context) if gateway_context else None
            ),
        )
    )


@mcp.tool()
def search_knowledge(
    query: str,
    include_stale: bool = False,
    limit: int = 10,
    entry_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """混合检索作品知识，返回原链接，以及视频时间戳或图文图片编号。"""
    return _payload(
        _service().search_knowledge(
            query,
            include_stale=include_stale,
            limit=limit,
            entry_ids=entry_ids,
        )
    )


@mcp.tool()
def create_topic(
    title: str,
    entry_ids: list[str],
    goal: str = "",
    instructions: str = "",
) -> dict[str, Any]:
    """从明确选定的文章创建研究专题；标题、目标与指令均逐字保存。"""
    return _payload(
        _service().create_topic(
            title, entry_ids, goal=goal, instructions=instructions
        )
    )


@mcp.tool()
def get_topic(topic_id: str) -> dict[str, Any]:
    """读取专题、来源启用状态、历史成果和版本状态。"""
    return _payload(_service().get_topic(topic_id))


@mcp.tool()
def list_topics() -> list[dict[str, Any]]:
    """列出全部研究专题。"""
    return _payload(_service().list_topics())


@mcp.tool()
def set_topic_sources(
    topic_id: str, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    """按传入顺序设置专题来源及 enabled 启用状态；停用后立即退出问答范围。"""
    return _payload(_service().set_topic_sources(topic_id, sources))


@mcp.tool()
def search_topic(
    topic_id: str,
    query: str,
    include_stale: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """只在专题当前启用来源内检索；不会回退到全库。"""
    return _payload(
        _service().search_topic(
            topic_id, query, include_stale=include_stale, limit=limit
        )
    )


@mcp.tool()
async def generate_topic_artifact(topic_id: str, kind: str) -> dict[str, Any]:
    """用户明确要求后生成专题成果；会调用配置模型并消耗 token。"""
    allowed = {
        "overview",
        "comparison",
        "evidence_map",
        "consensus",
        "decision_brief",
        "faq",
    }
    if kind not in allowed:
        raise ValueError("不支持的专题成果类型")
    return _payload(await _service().generate_topic_artifact(topic_id, kind))


@mcp.tool()
def save_topic_note(
    topic_id: str,
    content: str,
    title: str = "专题笔记",
    confirmed: bool = False,
) -> dict[str, Any]:
    """用户明确确认后，把内容逐字保存为专题笔记；AI 不自动写入。"""
    return _payload(
        _service().save_topic_note(
            topic_id, content, title=title, confirmed=confirmed
        )
    )


@mcp.tool()
def get_entry(entry_id: str, include_documents: bool = False) -> dict[str, Any]:
    """读取一条抖音作品的结构化数据；可选择包含完整 Markdown。"""
    return _payload(_service().get_entry(entry_id, include_documents=include_documents))


@mcp.tool()
def add_inspiration(
    entry_id: str,
    text: str,
    quote: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """为已有作品追加用户灵感；灵感原文不会被 AI 改写。"""
    entry = _service().add_inspiration(
        entry_id, InspirationInput(text=text, quote=quote, start_ms=start_ms, end_ms=end_ms)
    )
    return _payload(entry)


@mcp.tool()
def add_purpose(
    entry_id: str,
    text: str,
    quote: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """旧版兼容工具；新 Agent 应调用 add_inspiration。"""
    entry = _service().add_inspiration(
        entry_id, InspirationInput(text=text, quote=quote, start_ms=start_ms, end_ms=end_ms)
    )
    return _payload(entry)


@mcp.tool()
def confirm_reminder(
    entry_id: str,
    reminder_id: str,
    due_at: str | None = None,
    title: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """用户明确确认后传 confirmed=true，在 macOS 提醒事项中创建候选提醒。"""
    return _payload(
        _service().confirm_reminder(
            entry_id,
            reminder_id,
            due_at=due_at,
            title=title,
            confirmed=confirmed,
        )
    )


@mcp.tool()
def run_maintenance(apply: bool = False) -> dict[str, Any]:
    """预览或执行过期标记、孤立页面检查与可恢复媒体清理。"""
    return _payload(_service().run_maintenance(apply=apply))


@mcp.tool()
def doctor() -> dict[str, Any]:
    """检查本地工具、浏览器、Vault、模型和 Embedding 配置。"""
    from .setup import doctor as run_doctor

    return _payload(run_doctor(load_config()))


@mcp.tool()
def scan_favorites(
    folder_ids: list[str] | None = None,
    include_images: bool = False,
    directory_only: bool = False,
    gateway_context: dict[str, Any] | None = None,
) -> dict:
    """只清点收藏，不下载或导入。后续展示清单并取得用户确认后才调用 confirm_favorites_import。"""
    job = _service().favorites.start(
        folder_ids=folder_ids,
        include_images=include_images,
        directory_only=directory_only,
        gateway_context=GatewayContext.model_validate(gateway_context) if gateway_context else None,
    )
    return _payload({"job_id": job.id, "status": job.status})


@mcp.tool()
def list_favorites_imports(limit: int = 50) -> list[dict]:
    """恢复已有收藏导入任务，不访问收藏网页。"""
    return _payload(_service().favorites.history(limit=limit))


@mcp.tool()
def get_favorites_import(
    job_id: str, page: int = 1, limit: int = 50, folder_id: str | None = None, query: str = ""
) -> dict:
    """读取收藏清单及批量汇总；has_more 为真时继续翻页。"""
    return _payload(
        _service().favorites.get(job_id, page=page, limit=limit, folder_id=folder_id, query=query)
    )


@mcp.tool()
def set_favorites_selection(
    job_id: str, selected: bool, work_ids: list[str] | None = None, folder_id: str | None = None
) -> dict:
    """保存选择；不指定作品或收藏夹时作用于整份清单。文章暂不支持导入。"""
    return _payload(
        _service().favorites.select(
            job_id, selected=selected, work_ids=work_ids, folder_id=folder_id
        )
    )


@mcp.tool()
def confirm_favorites_import(job_id: str, accept_partial: bool = False) -> dict:
    """用户明确确认后批量入队；不完整清单需单独接受。重复确认不会重复创建任务。"""
    job = _service().favorites.confirm(job_id, accept_partial=accept_partial)
    return _payload({"job_id": job.id, "status": job.status})


@mcp.tool()
def retry_favorites_import(job_id: str) -> dict:
    """重试失败的清点或子任务，保留已完成结果；不绕过选择确认。"""
    job = _service().favorites.retry_failed(job_id)
    return _payload({"job_id": job.id, "status": job.status})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
