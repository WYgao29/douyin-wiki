from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from watchfiles import awatch

from ..adapters.share import extract_douyin_url
from ..config import (
    AppConfig,
    LLMSettings,
    default_config_path,
    llm_api_key_required,
    load_config,
    normalize_llm_base_url,
)
from ..errors import DouyinWikiError, EntryNotFoundError, JobStateError
from ..localization import label_entry_status
from ..models import InspirationDraft, InspirationInput, TopicArtifactKind
from ..secrets import get_secret, store_secret
from ..service import DouyinWikiService
from ..setup import update_config_values, write_config
from ..time_utils import beijing_iso, format_beijing
from .catalog import CONTENT_TYPE_LABELS, LibraryCatalog
from .chat import ChatContextBuilder, ChatProvider, OpenAICompatibleChatProvider
from .rendering import render_article, render_chat

WEB_VERSION = "0.1.6"


class CaptureSubmissionRequest(BaseModel):
    share_text: str = Field(min_length=1, max_length=20_000)


class CreateSessionRequest(BaseModel):
    scope: Literal["library", "entry", "topic"] = "library"
    context_entry_id: str | None = None
    context_topic_id: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class CreateTopicRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    entry_ids: list[str] = Field(min_length=1, max_length=500)
    goal: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=8000)


class TopicSourceRequest(BaseModel):
    entry_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class SetTopicSourcesRequest(BaseModel):
    sources: list[TopicSourceRequest] = Field(min_length=1, max_length=500)


class GenerateTopicArtifactRequest(BaseModel):
    kind: TopicArtifactKind


class SaveTopicNoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    title: str = Field(default="专题笔记", min_length=1, max_length=200)
    confirmed: bool = False


class ConfirmDestructiveActionRequest(BaseModel):
    confirmed: bool = False


class SetFavoriteRequest(BaseModel):
    favorite: bool


class ModelSettingsRequest(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    base_url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: str | None = Field(default=None, max_length=8192)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_llm_base_url(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in normalized for character in {'"', "\\", "\r", "\n"}):
            raise ValueError("模型名称为空或包含不支持的字符")
        return normalized

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class ChangeNotifier:
    def __init__(self) -> None:
        self.version = 0
        self._condition = asyncio.Condition()

    async def publish(self) -> None:
        async with self._condition:
            self.version += 1
            self._condition.notify_all()

    async def wait(self, last_version: int) -> int:
        async with self._condition:
            await self._condition.wait_for(lambda: self.version > last_version)
            return self.version


def _item_payload(item: Any) -> dict[str, Any]:
    payload = item.model_dump(mode="json", exclude={"body_markdown"})
    payload["content_type_label"] = CONTENT_TYPE_LABELS.get(item.content_type, "其他")
    payload["status"] = label_entry_status(item.status)
    payload["published_at"] = beijing_iso(item.published_at)
    payload["published_display"] = format_beijing(item.published_at)
    payload["captured_at"] = beijing_iso(item.captured_at)
    payload["captured_display"] = format_beijing(item.captured_at)
    payload["inspirations"] = [value.model_dump(mode="json") for value in item.inspirations]
    return payload


def _job_payload(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "progress": job.progress,
        "result": job.result,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def _event(name: str, payload: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _topic_payload(value: dict[str, Any]) -> dict[str, Any]:
    labels = {
        "overview": "专题总览",
        "comparison": "跨来源对比表",
        "evidence_map": "证据地图",
        "consensus": "共识与分歧",
        "decision_brief": "决策简报",
        "faq": "专题 FAQ",
        "note": "专题笔记",
    }
    topic = dict(value["topic"])
    topic["created_display"] = format_beijing(topic.get("created_at"))
    topic["updated_display"] = format_beijing(topic.get("updated_at"))
    artifacts = []
    for raw in value.get("artifacts", []):
        artifact = dict(raw)
        artifact["kind_label"] = labels.get(artifact["kind"], artifact["kind"])
        artifact["status_label"] = (
            "需要更新" if artifact["status"] == "needs_update" else "当前版本"
        )
        artifact["created_display"] = format_beijing(artifact.get("created_at"))
        artifact["html"] = render_chat(artifact["content_markdown"])
        artifacts.append(artifact)
    return {"topic": topic, "artifacts": artifacts}


async def _watch_catalog(
    catalog: LibraryCatalog,
    notifier: ChangeNotifier,
    stop_event: asyncio.Event,
) -> None:
    roots: list[Path] = []
    legacy = catalog.vault_path / "wiki" / "sources"
    creators = catalog.vault_path / "creators"
    topics = catalog.vault_path / "topics"
    roots.extend(path for path in (legacy, creators, topics) if path.is_dir())
    if not roots:
        roots = [catalog.vault_path]
    known_fingerprint = catalog.fingerprint
    async for changes in awatch(
        *roots,
        stop_event=stop_event,
        rust_timeout=500,
        yield_on_timeout=True,
    ):
        relevant = False
        for _, raw_path in changes:
            path = Path(raw_path)
            try:
                relative = path.resolve().relative_to(catalog.vault_path)
            except ValueError:
                continue
            parts = relative.parts
            if path.suffix.lower() == ".md" and (
                parts[:2] == ("wiki", "sources")
                or (len(parts) >= 3 and parts[0] == "creators" and parts[2] == "sources")
                or (parts and parts[0] == "topics")
            ):
                relevant = True
                break
        current_fingerprint = catalog.compute_fingerprint()
        if relevant or current_fingerprint != known_fingerprint:
            catalog.refresh()
            known_fingerprint = catalog.fingerprint
            await notifier.publish()


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: Path | None = None,
    service: DouyinWikiService | None = None,
    chat_provider: ChatProvider | None = None,
    chat_provider_factory: Callable[[LLMSettings], ChatProvider] | None = None,
    start_watcher: bool = True,
) -> FastAPI:
    cfg = config or load_config()
    target_config_path = config_path or default_config_path()
    core = service or DouyinWikiService(cfg)
    if service is None:
        core.initialize_runtime()
    else:
        core.database.initialize()
        core.recover_entry_trash_operations()
    catalog = LibraryCatalog(cfg.vault_path, core.database)
    catalog.refresh()
    notifier = ChangeNotifier()
    provider_factory = chat_provider_factory or OpenAICompatibleChatProvider
    provider = chat_provider or provider_factory(cfg.llm)
    context_builder = ChatContextBuilder(core)
    settings_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = asyncio.Event()
        task = (
            asyncio.create_task(_watch_catalog(catalog, notifier, stop_event))
            if start_watcher
            else None
        )
        try:
            yield
        finally:
            if task:
                stop_event.set()
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=2)
                if not task.done():
                    task.cancel()

    app = FastAPI(
        title="抖库 Web",
        version="0.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    package_root = Path(str(files("douyin_wiki.webapp")))
    templates = Jinja2Templates(directory=str(package_root / "templates"))
    app.mount("/static", StaticFiles(directory=str(package_root / "static")), name="static")
    app.state.config = cfg
    app.state.config_path = target_config_path
    app.state.service = core
    app.state.catalog = catalog
    app.state.chat_provider = provider

    async def publish_mutation(
        result: dict[str, Any], *, refresh_catalog: bool = True
    ) -> dict[str, Any]:
        """Keep a completed mutation successful when a UI refresh needs retrying."""
        warnings = list(result.get("warnings") or [])
        if refresh_catalog:
            try:
                catalog.refresh()
            except Exception as exc:
                warnings.append(f"操作已完成，但网页目录刷新失败：{exc}")
        try:
            await notifier.publish()
        except Exception as exc:
            warnings.append(f"操作已完成，但网页更新通知失败：{exc}")
        return {**result, "warnings": warnings}

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "")
                if origin.rstrip("/") != f"http://{host}":
                    return JSONResponse({"detail": "拒绝跨来源请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(
            request,
            "app.html",
            {"page_title": "资料库", "initial_entry_id": "", "web_version": WEB_VERSION},
        )

    @app.get("/articles/{entry_id}", response_class=HTMLResponse)
    async def article_page(request: Request, entry_id: str):
        if catalog.get(entry_id) is None:
            raise HTTPException(status_code=404, detail="文章不存在")
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "page_title": catalog.get(entry_id).title,
                "initial_entry_id": entry_id,
                "web_version": WEB_VERSION,
            },
        )

    @app.get("/topics/{topic_id}", response_class=HTMLResponse)
    async def topic_page(request: Request, topic_id: str):
        try:
            value = core.get_topic(topic_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "page_title": value["topic"]["title"],
                "initial_entry_id": "",
                "initial_topic_id": topic_id,
                "web_version": WEB_VERSION,
            },
        )

    @app.get("/topics", response_class=HTMLResponse)
    async def topics_page(request: Request):
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "page_title": "专题",
                "initial_entry_id": "",
                "initial_topic_id": "",
                "web_version": WEB_VERSION,
            },
        )

    @app.get("/trash", response_class=HTMLResponse)
    async def trash_page(request: Request):
        return templates.TemplateResponse(
            request,
            "app.html",
            {
                "page_title": "废纸篓",
                "initial_entry_id": "",
                "initial_topic_id": "",
                "web_version": WEB_VERSION,
            },
        )

    @app.get("/settings/model", response_class=HTMLResponse)
    async def model_settings_page(request: Request):
        return templates.TemplateResponse(
            request,
            "model_settings.html",
            {"page_title": "模型设置", "web_version": WEB_VERSION},
        )

    @app.get("/api/library")
    async def library(
        q: str = "",
        author: str = "",
        content_type: str = "",
        tag: str = "",
        inspiration_only: bool = False,
        recent: bool = False,
    ):
        items = catalog.filter(
            query=q,
            author=author,
            content_type=content_type,
            tag=tag,
            inspiration_only=inspiration_only,
        )
        if recent:
            items = items[:20]
        all_items = catalog.list_items()
        return {
            "items": [_item_payload(item) for item in items],
            "facets": {
                "authors": sorted({item.author for item in all_items}),
                "content_types": [
                    {"value": code, "label": label}
                    for code, label in CONTENT_TYPE_LABELS.items()
                    if any(item.content_type == code for item in all_items)
                ],
                "tags": sorted({tag for item in all_items for tag in item.tags}),
            },
            "total": len(items),
            "version": catalog.version,
        }

    @app.post("/api/captures", status_code=202)
    async def create_capture(payload: CaptureSubmissionRequest):
        try:
            extract_douyin_url(payload.share_text)
        except DouyinWikiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = core.capture_douyin(payload.share_text)
        return {"job_id": job.id, "status": job.status.value}

    @app.get("/api/articles/{entry_id}")
    async def article(entry_id: str):
        item = catalog.get(entry_id)
        if item is None:
            raise HTTPException(status_code=404, detail="文章不存在")
        return {
            "item": _item_payload(item),
            "html": render_article(
                item.body_markdown,
                source_path=item.source_path,
                catalog=catalog,
            ),
        }

    @app.put("/api/articles/{entry_id}/favorite")
    async def set_article_favorite(entry_id: str, payload: SetFavoriteRequest):
        existing_item = catalog.get(entry_id)
        if existing_item is None:
            raise HTTPException(status_code=404, detail="文章不存在")
        try:
            result = core.set_entry_favorite(entry_id, payload.favorite)
        except EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="文章不存在") from exc
        mutation = await publish_mutation({})
        item = catalog.get(entry_id) or existing_item
        item_payload = _item_payload(item)
        persisted = result["entry"]
        item_payload.update(
            {
                "favorite": persisted.favorite,
                "media_status": persisted.media_status,
                "retention": persisted.retention.value,
            }
        )
        restore_job = result["restore_job"]
        response = {
            "item": item_payload,
            "restore_job": _job_payload(restore_job) if restore_job else None,
            "warnings": mutation["warnings"],
        }
        return JSONResponse(response, status_code=202 if restore_job else 200)

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        try:
            return _job_payload(core.get_job(job_id))
        except JobStateError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str):
        try:
            core.get_job(job_id)
        except JobStateError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        try:
            return _job_payload(core.retry_job(job_id))
        except JobStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/articles/{entry_id}")
    async def trash_article(entry_id: str, payload: ConfirmDestructiveActionRequest):
        try:
            result = core.trash_entry(entry_id, confirmed=payload.confirmed)
        except EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="文章不存在") from exc
        except (ValueError, DouyinWikiError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await publish_mutation(result)

    @app.get("/api/trash")
    async def list_trash():
        items = []
        for raw in core.list_trashed_entries():
            item = dict(raw)
            item["deleted_display"] = format_beijing(item.get("deleted_at"))
            items.append(item)
        return {"items": items, "total": len(items)}

    @app.post("/api/trash/{trash_id}/restore")
    async def restore_trash_item(
        trash_id: str, payload: ConfirmDestructiveActionRequest
    ):
        try:
            result = core.restore_trashed_entry(trash_id, confirmed=payload.confirmed)
        except EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, DouyinWikiError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await publish_mutation(result)

    @app.delete("/api/trash/{trash_id}")
    async def permanently_delete_trash_item(
        trash_id: str, payload: ConfirmDestructiveActionRequest
    ):
        try:
            result = core.permanently_delete_trashed_entry(
                trash_id, confirmed=payload.confirmed
            )
        except EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await publish_mutation(result, refresh_catalog=False)

    @app.get("/api/topics")
    async def list_topics():
        return {"topics": [_topic_payload(item) for item in core.list_topics()]}

    @app.post("/api/topics", status_code=201)
    async def create_topic(payload: CreateTopicRequest):
        try:
            value = core.create_topic(
                payload.title,
                payload.entry_ids,
                goal=payload.goal,
                instructions=payload.instructions,
            )
        except (ValueError, EntryNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await notifier.publish()
        return _topic_payload(value)

    @app.get("/api/topics/{topic_id}")
    async def get_topic(topic_id: str):
        try:
            return _topic_payload(core.get_topic(topic_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/topics/{topic_id}/sources")
    async def set_topic_sources(topic_id: str, payload: SetTopicSourcesRequest):
        try:
            value = core.set_topic_sources(
                topic_id,
                [item.model_dump() for item in payload.sources],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, EntryNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await notifier.publish()
        return _topic_payload(value)

    @app.post("/api/topics/{topic_id}/artifacts", status_code=201)
    async def generate_topic_artifact(
        topic_id: str, payload: GenerateTopicArtifactRequest
    ):
        try:
            artifact = await core.generate_topic_artifact(
                topic_id,
                payload.kind,
                provider=app.state.chat_provider,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, DouyinWikiError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await notifier.publish()
        result = {"topic": core.get_topic(topic_id)["topic"], "artifacts": [artifact]}
        return _topic_payload(result)["artifacts"][0]

    @app.post("/api/topics/{topic_id}/notes", status_code=201)
    async def save_topic_note(topic_id: str, payload: SaveTopicNoteRequest):
        try:
            artifact = core.save_topic_note(
                topic_id,
                payload.content,
                title=payload.title,
                confirmed=payload.confirmed,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await notifier.publish()
        result = {"topic": core.get_topic(topic_id)["topic"], "artifacts": [artifact]}
        return _topic_payload(result)["artifacts"][0]

    @app.get("/api/library/events")
    async def library_events(request: Request):
        async def events() -> AsyncIterator[str]:
            version = notifier.version
            yield _event("ready", {"version": version})
            while not await request.is_disconnected():
                try:
                    version = await asyncio.wait_for(notifier.wait(version), timeout=20)
                    yield _event("library", {"version": version})
                except TimeoutError:
                    yield ": keep-alive\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/chat/sessions")
    async def list_sessions():
        current_provider = app.state.chat_provider
        sessions = await asyncio.to_thread(core.database.list_chat_sessions)
        return {
            "sessions": [item.model_dump(mode="json") for item in sessions],
            "model": current_provider.model or "未配置",
            "configured": current_provider.configured,
        }

    @app.post("/api/chat/sessions", status_code=201)
    async def create_session(payload: CreateSessionRequest):
        if payload.scope == "entry" and (
            not payload.context_entry_id or catalog.get(payload.context_entry_id) is None
        ):
            raise HTTPException(status_code=400, detail="文章范围对话需要有效的目标文章")
        if payload.scope == "topic":
            if not payload.context_topic_id:
                raise HTTPException(status_code=400, detail="专题范围对话需要有效的目标专题")
            try:
                core.get_topic(payload.context_topic_id)
            except KeyError as exc:
                raise HTTPException(
                    status_code=400, detail="专题范围对话需要有效的目标专题"
                ) from exc
        session = await asyncio.to_thread(
            core.database.create_chat_session,
            scope=payload.scope,
            context_entry_id=payload.context_entry_id,
            context_topic_id=payload.context_topic_id,
        )
        return session.model_dump(mode="json")

    @app.get("/api/chat/sessions/{session_id}")
    async def get_session(session_id: str):
        try:
            session = await asyncio.to_thread(core.database.get_chat_session, session_id)
            messages = await asyncio.to_thread(core.database.list_chat_messages, session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "session": session.model_dump(mode="json"),
            "messages": [
                {
                    **message.model_dump(mode="json"),
                    "html": render_chat(message.content) if message.role == "assistant" else "",
                }
                for message in messages
            ],
        }

    @app.delete("/api/chat/sessions/{session_id}")
    async def delete_session(session_id: str):
        if not await asyncio.to_thread(core.database.delete_chat_session, session_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        return {"status": "已删除"}

    @app.post("/api/chat/sessions/{session_id}/messages")
    async def send_message(session_id: str, payload: SendMessageRequest):
        try:
            session = await asyncio.to_thread(core.database.get_chat_session, session_id)
            history = await asyncio.to_thread(
                core.database.list_chat_messages, session_id, limit=24
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session.scope == "topic" and not session.context_topic_id:
            raise HTTPException(status_code=409, detail="该对话关联的专题已不存在，请新建对话")
        try:
            messages, citations = await asyncio.to_thread(
                context_builder.build,
                payload.content,
                context_entry_id=(
                    session.context_entry_id if session.scope == "entry" else None
                ),
                context_topic_id=(
                    session.context_topic_id if session.scope == "topic" else None
                ),
                history=history,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=409, detail="该对话关联的专题已不存在，请新建对话"
            ) from exc
        await asyncio.to_thread(
            core.database.add_chat_message, session_id, "user", payload.content
        )
        if session.title == "新对话":
            await asyncio.to_thread(
                core.database.update_chat_session,
                session_id,
                title=payload.content.strip().replace("\n", " ")[:32],
            )

        async def response_stream() -> AsyncIterator[str]:
            current_provider = app.state.chat_provider
            yield _event(
                "meta",
                {
                    "model": current_provider.model or "未配置",
                    "citations": [item.model_dump(mode="json") for item in citations],
                },
            )
            answer = ""
            usage: dict[str, int | None] = {}
            try:
                if session.scope == "topic" and not citations:
                    answer = "当前专题没有相关证据。"
                    yield _event("delta", {"text": answer})
                    saved = await asyncio.to_thread(
                        core.database.add_chat_message,
                        session_id,
                        "assistant",
                        answer,
                        citations=[],
                    )
                    yield _event(
                        "done",
                        {
                            "message": {
                                **saved.model_dump(mode="json"),
                                "html": render_chat(answer),
                            },
                            "usage": None,
                        },
                    )
                    return
                async for chunk in current_provider.stream(messages):
                    if chunk.text:
                        answer += chunk.text
                        yield _event("delta", {"text": chunk.text})
                    if chunk.usage:
                        usage = chunk.usage
                saved = await asyncio.to_thread(
                    core.database.add_chat_message,
                    session_id,
                    "assistant",
                    answer,
                    citations=citations,
                    model=current_provider.model or None,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                )
                yield _event(
                    "done",
                    {
                        "message": {
                            **saved.model_dump(mode="json"),
                            "html": render_chat(answer),
                        },
                        "usage": usage or None,
                    },
                )
            except (DouyinWikiError, httpx.HTTPError) as exc:
                if answer:
                    await asyncio.to_thread(
                        core.database.add_chat_message,
                        session_id,
                        "assistant",
                        answer,
                        citations=citations,
                        model=current_provider.model or None,
                    )
                details = getattr(exc, "details", {})
                cause = str(details.get("cause") or "").strip()
                yield _event(
                    "error",
                    {
                        "message": str(exc),
                        "cause": cause[:500] or None,
                        "partial_saved": bool(answer),
                    },
                )
            except Exception:
                yield _event("error", {"message": "AI 对话暂时不可用，请稍后重试。"})

        return StreamingResponse(response_stream(), media_type="text/event-stream")

    @app.get("/api/settings/model")
    async def get_model_settings():
        current_config = app.state.config
        current_provider = app.state.chat_provider
        key_required = llm_api_key_required(current_config.llm.base_url)
        key_present = bool(await asyncio.to_thread(get_secret, current_config.llm.api_key_env))
        return {
            "enabled": current_config.llm.enabled,
            "base_url": current_config.llm.base_url,
            "model": current_config.llm.model,
            "api_key_configured": key_present,
            "api_key_required": key_required,
            "api_key_source": (
                "本机接口无需密钥"
                if not key_required
                else "环境变量"
                if os.environ.get(current_config.llm.api_key_env)
                else "macOS Keychain"
                if key_present
                else "未配置"
            ),
            "configured": current_provider.configured,
            "analysis_mode": current_config.analysis_mode.value,
            "analysis_mode_label": {
                "gateway": "网关 Agent",
                "provider": "模型接口",
                "local": "本地模式",
            }[current_config.analysis_mode.value],
        }

    @app.post("/api/settings/model")
    async def save_model_settings(payload: ModelSettingsRequest):
        try:
            async with settings_lock:
                config_path_value = Path(app.state.config_path)
                current_config = (
                    load_config(config_path_value)
                    if config_path_value.exists()
                    else app.state.config
                )
                existing_key = await asyncio.to_thread(get_secret, current_config.llm.api_key_env)
                endpoint_changed = current_config.llm.base_url.rstrip("/") != payload.base_url
                if (
                    endpoint_changed
                    and llm_api_key_required(payload.base_url)
                    and existing_key
                    and not payload.api_key
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="更换云端接口时必须重新输入该服务对应的 API Key",
                    )
                llm = current_config.llm.model_copy(
                    update={
                        "enabled": True,
                        "base_url": payload.base_url,
                        "model": payload.model,
                    }
                )
                updated = current_config.model_copy(update={"llm": llm})
                if payload.api_key:
                    await asyncio.to_thread(store_secret, llm.api_key_env, payload.api_key)
                if config_path_value.exists():
                    await asyncio.to_thread(
                        update_config_values,
                        config_path_value,
                        {
                            "llm": {
                                "enabled": True,
                                "base_url": llm.base_url,
                                "model": llm.model,
                            }
                        },
                    )
                else:
                    await asyncio.to_thread(
                        write_config, updated, config_path_value, overwrite=True
                    )
                app.state.config = updated
                core.config = updated
                app.state.chat_provider = await asyncio.to_thread(provider_factory, llm)
        except (DouyinWikiError, OSError) as exc:
            raise HTTPException(status_code=500, detail=f"保存模型配置失败：{exc}") from exc
        current_provider = app.state.chat_provider
        key_present = bool(await asyncio.to_thread(get_secret, llm.api_key_env))
        key_required = llm_api_key_required(llm.base_url)
        return {
            "status": "配置已保存",
            "configured": current_provider.configured,
            "model": current_provider.model or "未配置",
            "api_key_configured": key_present,
            "api_key_required": key_required,
            "analysis_mode": updated.analysis_mode.value,
            "analysis_mode_label": {
                "gateway": "网关 Agent",
                "provider": "模型接口",
                "local": "本地模式",
            }[updated.analysis_mode.value],
        }

    @app.post("/api/settings/model/test")
    async def test_model_settings():
        current_provider = app.state.chat_provider
        if not current_provider.configured:
            raise HTTPException(status_code=400, detail="请先保存模型名称和 API Key")
        answer = ""
        usage: dict[str, int | None] = {}
        try:
            async for chunk in current_provider.stream(
                [
                    {
                        "role": "system",
                        "content": "这是连接测试。请只回复：连接成功",
                    },
                    {"role": "user", "content": "测试连接"},
                ]
            ):
                answer += chunk.text
                if chunk.usage:
                    usage = chunk.usage
        except DouyinWikiError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"模型连接失败：{exc}") from exc
        return {
            "status": "连接成功",
            "model": current_provider.model,
            "response": answer[:200],
            "usage": usage or None,
        }

    @app.post("/api/inspirations/confirm")
    async def confirm_inspiration(payload: InspirationDraft):
        if not payload.confirmed:
            raise HTTPException(status_code=400, detail="保存灵感前必须由用户确认")
        if catalog.get(payload.entry_id) is None:
            raise HTTPException(status_code=404, detail="目标文章不存在")
        try:
            entry = await asyncio.to_thread(
                core.add_inspiration,
                payload.entry_id,
                InspirationInput(
                    text=payload.text,
                    quote=payload.quote,
                    start_ms=payload.start_ms,
                    end_ms=payload.end_ms,
                ),
            )
        except EntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail="目标文章尚未进入知识索引") from exc
        await asyncio.to_thread(catalog.refresh)
        await notifier.publish()
        return {
            "status": "灵感已保存",
            "entry_id": entry.id,
            "inspirations": [item.model_dump(mode="json") for item in entry.inspirations],
        }

    @app.get("/media/{media_path:path}")
    async def media(media_path: str):
        target = catalog.safe_media_path(media_path)
        if target is None:
            raise HTTPException(status_code=404, detail="图片不存在或不允许访问")
        return FileResponse(target, headers={"Cache-Control": "private, max-age=3600"})

    return app


def run_web(config_path: Path | None = None) -> None:
    config = load_config(config_path)
    if not config.web.enabled:
        raise RuntimeError("Web 服务未启用，请在配置中设置 web.enabled=true")
    uvicorn.run(
        create_app(config, config_path=config_path or default_config_path()),
        host=config.web.host,
        port=config.web.port,
        access_log=False,
    )
