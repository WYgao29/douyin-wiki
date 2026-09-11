"""Web primary-entry API routes for jobs, auth, imports, and system operations."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..errors import DouyinWikiError, EntryNotFoundError, JobStateError
from ..localization import add_display_labels
from ..models import (
    CaptureOptions,
    CreatorWorkDecision,
    InspirationInput,
    RetentionPolicy,
    SourceKind,
)
from ..operation import analysis_presentation, sanitize_public_payload
from ..web_operation import WebOperationService


def _event(name: str, payload: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class AuthStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["video", "douyin"]
    trigger_job_id: str | None = None


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolutions: dict[str, str] | None = None
    accept_uncertain: bool = False


class CreatorInventoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_text: str = Field(min_length=1, max_length=20_000)
    inspirations: list[InspirationInput] = Field(default_factory=list)
    retention: RetentionPolicy = RetentionPolicy.TEMPORARY
    allow_long: bool = False
    approve_cloud_analysis: bool = False


class CreatorSelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: CreatorWorkDecision
    work_ids: list[str] | None = None
    ordinals: list[int] | None = None


class CreatorConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept_partial: bool = False


class CreatorImportSkippedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ids: list[str] = Field(min_length=1, max_length=500)


class ReanalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, EntryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, JobStateError):
        message = str(exc)
        if "not found" in message:
            return HTTPException(status_code=404, detail="任务不存在")
        return HTTPException(status_code=409, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, DouyinWikiError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="操作失败")


def register_operation_routes(
    app: FastAPI,
    *,
    core,
    operations: WebOperationService,
    templates,
    notifier,
    web_version: str,
) -> None:
    def spa(request: Request, title: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "page_title": title,
                "initial_entry_id": "",
                "initial_topic_id": "",
                "web_version": web_version,
            },
        )

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request):
        return spa(request, "导入内容")

    @app.get("/imports/single", response_class=HTMLResponse)
    async def imports_single_page(request: Request):
        return spa(request, "单条导入")

    @app.get("/imports/creators", response_class=HTMLResponse)
    async def imports_creators_page(request: Request):
        return spa(request, "博主导入")

    @app.get("/imports/favorites", response_class=HTMLResponse)
    async def imports_favorites_page(request: Request):
        return spa(request, "我的收藏")

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        return spa(request, "任务中心")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str):
        return spa(request, "任务详情")

    @app.get("/settings/auth", response_class=HTMLResponse)
    async def auth_page(request: Request):
        return spa(request, "授权状态")

    @app.get("/settings/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        return spa(request, "系统设置")

    @app.get("/api/overview")
    async def overview():
        return await operations.overview()

    @app.get("/api/jobs")
    async def list_jobs(
        kind: str | None = None,
        status: str | None = None,
        requires_user_action: bool | None = None,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
    ):
        try:
            return operations.list_jobs(
                kind=kind,
                status=status,
                requires_user_action=requires_user_action,
                page=page,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/review")
    async def get_review(job_id: str):
        try:
            job = operations.get_job(job_id)
        except JobStateError as exc:
            raise _http_error(exc) from exc
        return {
            "job_id": job_id,
            "issues": job.get("review_issues") or [],
            "status": job["status"],
        }

    @app.post("/api/jobs/{job_id}/approve", status_code=202)
    async def approve_job(job_id: str):
        try:
            core.get_job(job_id)
            job = core.approve_job(job_id)
        except JobStateError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.post("/api/jobs/{job_id}/review", status_code=202)
    async def resolve_review(job_id: str, body: ReviewBody):
        try:
            job = core.resolve_review(
                job_id,
                body.resolutions,
                accept_uncertain=body.accept_uncertain,
            )
        except JobStateError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.get("/api/auth/status")
    async def auth_status(refresh: bool = False):
        return await operations.auth_status(refresh=refresh)

    @app.post("/api/auth/sessions", status_code=202)
    async def start_auth(body: AuthStartRequest):
        try:
            session = await operations.auth.start(
                body.channel, trigger_job_id=body.trigger_job_id
            )
        except (JobStateError, ValueError) as exc:
            raise _http_error(exc) from exc
        await notifier.publish("auth")
        return sanitize_public_payload(session)

    @app.get("/api/auth/sessions/{session_id}")
    async def get_auth_session(session_id: str):
        try:
            return sanitize_public_payload(operations.auth.get(session_id))
        except JobStateError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/auth/sessions/{session_id}/cancel")
    async def cancel_auth(session_id: str):
        try:
            session = operations.auth.cancel(session_id)
        except JobStateError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("auth")
        return sanitize_public_payload(session)

    @app.get("/api/system/health")
    async def system_health():
        return operations.system_health()

    @app.post("/api/system/doctor")
    async def run_doctor():
        return await asyncio.to_thread(operations.run_doctor)

    @app.post("/api/system/maintenance")
    async def run_maintenance(body: ConfirmBody):
        result = await asyncio.to_thread(operations.maintenance, confirmed=body.confirmed)
        if body.confirmed:
            await notifier.publish("health")
        return result

    @app.post("/api/system/rebuild")
    async def run_rebuild(body: ConfirmBody):
        result = await asyncio.to_thread(operations.rebuild, confirmed=body.confirmed)
        if body.confirmed:
            await notifier.publish("health")
        return result

    @app.post("/api/system/worker/reload")
    async def reload_worker():
        return operations.request_worker_reload()

    @app.get("/api/system/storage")
    async def storage():
        return await asyncio.to_thread(operations.storage)

    @app.get("/api/system/analysis")
    async def analysis_status():
        return analysis_presentation(operations.analysis_mode)

    @app.post("/api/creators/inventory", status_code=202)
    async def creator_inventory(body: CreatorInventoryBody):
        try:
            job = core.capture_douyin_creator(
                body.source_text,
                inspirations=body.inspirations,
                options=CaptureOptions(
                    retention=body.retention,
                    allow_long=body.allow_long,
                    approve_cloud_analysis=body.approve_cloud_analysis,
                ),
            )
        except DouyinWikiError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.get("/api/creators")
    async def list_creators():
        return {"items": add_display_labels(core.list_creators())}

    @app.get("/api/creators/{creator_id}")
    async def get_creator(creator_id: str):
        try:
            return add_display_labels(core.get_creator(creator_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="博主不存在") from exc

    @app.get("/api/creators/{creator_id}/works")
    async def creator_works(creator_id: str):
        try:
            core.get_creator(creator_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="博主不存在") from exc
        return {"items": add_display_labels(core.list_creator_works(creator_id))}

    @app.post("/api/creators/{creator_id}/sync", status_code=202)
    async def sync_creator(creator_id: str):
        try:
            job = core.sync_creator(creator_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="博主不存在") from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.post("/api/creators/{creator_id}/import-skipped", status_code=202)
    async def import_skipped(creator_id: str, body: CreatorImportSkippedBody):
        try:
            job = core.import_creator_works(creator_id, body.work_ids)
        except (JobStateError, ValueError, KeyError) as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.get("/api/creator-imports/{job_id}")
    async def creator_import_detail(
        job_id: str,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        decision: CreatorWorkDecision | None = None,
        source_kind: SourceKind | None = None,
        query: str | None = None,
    ):
        try:
            data = core.get_creator_inventory(
                job_id,
                page=page,
                limit=limit,
                decision=decision,
                source_kind=source_kind,
                query=query,
            )
        except JobStateError as exc:
            raise _http_error(exc) from exc
        job = operations.present(core.get_job(job_id))
        return add_display_labels({**data, "job": job})

    @app.post("/api/creator-imports/{job_id}/selection")
    async def creator_selection(job_id: str, body: CreatorSelectionBody):
        try:
            result = core.set_creator_work_selection(
                job_id,
                body.decision,
                ordinals=body.ordinals,
                work_ids=body.work_ids,
            )
        except JobStateError as exc:
            raise _http_error(exc) from exc
        return add_display_labels(result)

    @app.post("/api/creator-imports/{job_id}/confirm", status_code=202)
    async def creator_confirm(job_id: str, body: CreatorConfirmBody):
        try:
            job = core.confirm_creator_import(job_id, accept_partial=body.accept_partial)
        except JobStateError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.post("/api/articles/{entry_id}/reanalyze", status_code=202)
    async def reanalyze_article(entry_id: str, body: ReanalyzeBody):
        try:
            job = core.reanalyze_entry(entry_id, force=body.force)
        except EntryNotFoundError as exc:
            raise _http_error(exc) from exc
        await notifier.publish("jobs")
        return operations.present(job)

    @app.delete("/api/imports/{job_id}")
    async def delete_import(job_id: str, body: ConfirmBody):
        try:
            result = operations.delete_import_history(job_id, confirmed=body.confirmed)
        except JobStateError as exc:
            raise _http_error(exc) from exc
        if body.confirmed:
            await notifier.publish("jobs")
        return result

    @app.post("/api/favorites/cache/clear")
    async def clear_favorites_cache(body: ConfirmBody):
        result = operations.clear_favorites_cache(confirmed=body.confirmed)
        if body.confirmed:
            await notifier.publish("jobs")
        return result

    @app.get("/api/events")
    async def operation_events(request: Request):
        async def events():
            version = notifier.version
            yield _event("ready", {"version": version})
            while not await request.is_disconnected():
                try:
                    version, name, payload = await asyncio.wait_for(
                        notifier.wait_named(version), timeout=20
                    )
                    yield _event(name, payload)
                except TimeoutError:
                    yield ": keep-alive\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
