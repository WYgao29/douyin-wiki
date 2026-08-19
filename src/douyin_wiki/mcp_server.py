from __future__ import annotations

import warnings
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .models import (
    CaptureOptions,
    GatewayContext,
    InspirationInput,
    JobStatus,
    RetentionPolicy,
    ReviewIssue,
    TranscriptCorrection,
)
from .service import DouyinWikiService

INSTRUCTIONS = """这是本地抖音知识库，默认由当前 Gateway Agent 完成 AI 校正与分析。
采集时调用 capture_douyin，并传入 gateway_context 以便异步结果回到原会话。任务到达
awaiting_agent_analysis 后：调用 get_analysis_context；先调用 submit_transcript_correction，
如 source_kind=image_note 且 phase=analysis，则直接根据 analysis_schema 调用
submit_gateway_analysis；视频在无人工疑点后再提交分析。遇到 needs_review 时必须向用户展示
疑点并调用 resolve_review；needs_auth 时先调用 get_auth_status，再根据 auth_scope 提示用户运行
douyin-wiki auth video 或 douyin-wiki auth douyin，并在登录后重试；
waiting_confirmation 表示长视频将消耗 Agent 模型
token，必须经用户确认后调用 approve_job。无常驻 Agent 时可用 list_job_events 轮询可操作
事件，处理成功后调用 acknowledge_job_event。查询时优先调用 search_knowledge，并保留
original_url，以及视频 timestamp_ms 或图文 image_index；创建提醒前必须获得用户明确确认。"""

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Field 'lifespan' has an incomplete definition.*")
    mcp = FastMCP("douyin-wiki", instructions=INSTRUCTIONS)

_SERVICE: DouyinWikiService | None = None


def _service() -> DouyinWikiService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = DouyinWikiService(load_config())
        _SERVICE.database.initialize()
    return _SERVICE


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
            retention=RetentionPolicy(retention),
            allow_long=allow_long,
            approve_cloud_analysis=approve_ai_analysis or approve_cloud_analysis,
        ),
        GatewayContext.model_validate(gateway_context) if gateway_context else None,
    )
    return job.model_dump(mode="json")


@mcp.tool()
def get_job(job_id: str) -> dict[str, Any]:
    """读取任务进度、疑点、错误和提醒候选。"""
    return _service().get_job(job_id).model_dump(mode="json")


@mcp.tool()
def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """列出最近任务，可按状态过滤。"""
    value = JobStatus(status) if status else None
    return [item.model_dump(mode="json") for item in _service().list_jobs(value, limit)]


@mcp.tool()
async def get_auth_status(video_url: str | None = None) -> dict[str, Any]:
    """检查视频浏览器 Cookie 与图文专用会话；不返回或上传 Cookie 值。"""
    return await _service().get_auth_status(video_url=video_url)


@mcp.tool()
def list_job_events(
    after_event_id: int = 0,
    unacknowledged_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出待 Gateway 处理或投递的持久事件；不会调用模型。"""
    return [
        item.model_dump(mode="json")
        for item in _service().list_job_events(
            after_event_id=after_event_id,
            unacknowledged_only=unacknowledged_only,
            limit=limit,
        )
    ]


@mcp.tool()
def acknowledge_job_event(event_id: int) -> dict[str, Any]:
    """Gateway 成功处理或投递事件后确认，防止重复通知。"""
    return _service().acknowledge_job_event(event_id).model_dump(mode="json")


@mcp.tool()
def get_analysis_context(job_id: str) -> dict[str, Any]:
    """读取分析所需的逐字稿或图文正文、逐图 OCR、灵感、既有知识和输出 schema。"""
    return _service().get_analysis_context(job_id)


@mcp.tool()
def submit_transcript_correction(
    job_id: str,
    corrections: list[dict[str, Any]],
    producer: str,
    model: str = "agent",
    review_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Gateway Agent 提交逐字稿校正版；原始 ASR 永不覆盖。"""
    return (
        _service()
        .submit_transcript_correction(
            job_id,
            [TranscriptCorrection.model_validate(item) for item in corrections],
            producer=producer,
            model=model,
            review_issues=[ReviewIssue.model_validate(item) for item in (review_issues or [])],
        )
        .model_dump(mode="json")
    )


@mcp.tool()
def submit_gateway_analysis(
    job_id: str,
    analysis: dict[str, Any],
    producer: str,
    model: str = "agent",
) -> dict[str, Any]:
    """Gateway Agent 提交符合 analysis_schema 的结果并让 Worker 确定性入库。"""
    return (
        _service()
        .submit_gateway_analysis(job_id, analysis, producer=producer, model=model)
        .model_dump(mode="json")
    )


@mcp.tool()
def approve_job(job_id: str) -> dict[str, Any]:
    """用户确认后，批准超过 30 分钟的视频继续消耗 Agent/模型 token。"""
    return _service().approve_job(job_id).model_dump(mode="json")


@mcp.tool()
def retry_job(job_id: str) -> dict[str, Any]:
    """重试 failed 或 needs_auth 任务；保留已完成阶段产物并从最近检查点继续。"""
    return _service().retry_job(job_id).model_dump(mode="json")


@mcp.tool()
def resolve_review(
    job_id: str, resolutions: dict[str, str] | None = None, accept_uncertain: bool = False
) -> dict[str, Any]:
    """解决全部低置信逐字稿或逐图 OCR 疑点，或明确接受不确定内容。"""
    return (
        _service()
        .resolve_review(job_id, resolutions, accept_uncertain=accept_uncertain)
        .model_dump(mode="json")
    )


@mcp.tool()
def submit_analysis(
    entry_id: str,
    analysis: dict[str, Any],
    producer: str,
    model: str = "agent",
) -> dict[str, Any]:
    """Agent 提交符合结构化 schema 的补充或重分析结果，并记录生成者。"""
    return (
        _service()
        .submit_analysis(entry_id, analysis, producer=producer, model=model)
        .model_dump(mode="json")
    )


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
    return job.model_dump(mode="json")


@mcp.tool()
def reanalyze_all(
    force: bool = False,
    gateway_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """批量排队重分析所有 v1 资料；默认跳过已是 v2 的条目。"""
    return _service().reanalyze_all(
        force=force,
        gateway_context=(
            GatewayContext.model_validate(gateway_context) if gateway_context else None
        ),
    )


@mcp.tool()
def search_knowledge(
    query: str, include_stale: bool = False, limit: int = 10
) -> list[dict[str, Any]]:
    """混合检索作品知识，返回原链接，以及视频时间戳或图文图片编号。"""
    return [
        item.model_dump(mode="json")
        for item in _service().search_knowledge(query, include_stale=include_stale, limit=limit)
    ]


@mcp.tool()
def get_entry(entry_id: str, include_documents: bool = False) -> dict[str, Any]:
    """读取一条抖音作品的结构化数据；可选择包含完整 Markdown。"""
    return _service().get_entry(entry_id, include_documents=include_documents)


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
    return entry.model_dump(mode="json")


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
    return entry.model_dump(mode="json")


@mcp.tool()
def confirm_reminder(
    entry_id: str,
    reminder_id: str,
    due_at: str | None = None,
    title: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """用户明确确认后传 confirmed=true，在 macOS 提醒事项中创建候选提醒。"""
    return _service().confirm_reminder(
        entry_id,
        reminder_id,
        due_at=due_at,
        title=title,
        confirmed=confirmed,
    )


@mcp.tool()
def run_maintenance(apply: bool = False) -> dict[str, Any]:
    """预览或执行过期标记、孤立页面检查与可恢复媒体清理。"""
    return _service().run_maintenance(apply=apply)


@mcp.tool()
def doctor() -> dict[str, Any]:
    """检查本地工具、浏览器、Vault、模型和 Embedding 配置。"""
    from .setup import doctor as run_doctor

    return run_doctor(load_config())


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
