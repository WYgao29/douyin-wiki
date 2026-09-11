"""Web-driven authorization sessions. State is persisted; browser objects are not."""

from __future__ import annotations

import asyncio
import fcntl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .auth_guidance import auth_channel
from .errors import BrowserAuthRequiredError, ExternalToolError, JobStateError
from .models import JobStatus
from .operation import cookie_source_label

ACTIVE_STAGES = {"queued", "launching", "waiting_login", "verifying"}


def profile_lock_held(profile_dir: Path) -> bool:
    lock_path = profile_dir.parent / f"{profile_dir.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


class WebAuthManager:
    def __init__(
        self,
        service,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.sleep = sleep
        self._tasks: dict[str, asyncio.Task] = {}

    def _database(self):
        return self.service.database

    def affected_count(self, channel: str) -> int:
        scopes = {"video"} if channel == "video" else {"image_note", "creator", "favorites"}
        return sum(
            1
            for job in self._database().list_jobs(status=JobStatus.NEEDS_AUTH, limit=None)
            if job.result.get("auth_scope") in scopes
        )

    async def start(
        self,
        channel: str,
        *,
        trigger_job_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        if channel not in {"video", "douyin"}:
            raise ValueError("授权通道必须是 video 或 douyin")
        existing = self._database().latest_auth_session(channel, active_only=True)
        if existing:
            return existing
        scope = "video" if channel == "video" else "image_note"
        if trigger_job_id:
            trigger = self.service.get_job(trigger_job_id)
            if trigger.status == JobStatus.NEEDS_AUTH:
                scope = str(trigger.result.get("auth_scope") or scope)
                if auth_channel(scope) != channel:
                    raise JobStateError("任务等待的授权通道与当前选择不一致")
        session = self._database().create_auth_session(
            channel=channel,
            scope=scope,
            stage="queued",
            trigger_job_id=trigger_job_id,
            cookie_source_label=cookie_source_label(channel),
            affected_job_count=self.affected_count(channel),
        )
        timeout = timeout_seconds or self.service.config.auth_guidance.timeout_seconds
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._run(session["id"], timeout_seconds=timeout))
            self._tasks[session["id"]] = task
            task.add_done_callback(lambda _: self._tasks.pop(session["id"], None))
        except RuntimeError:
            await self._run(session["id"], timeout_seconds=timeout)
        return self._database().get_auth_session(session["id"])

    def get(self, session_id: str) -> dict[str, Any]:
        return self._database().get_auth_session(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._database().get_auth_session(session_id)
        if session["stage"] not in ACTIVE_STAGES:
            return session
        task = self._tasks.get(session_id)
        if task:
            task.cancel()
        return self._database().update_auth_session(
            session_id,
            stage="cancelled",
            error_summary="用户选择稍后处理",
            complete=True,
        )

    async def _run(self, session_id: str, *, timeout_seconds: int) -> dict[str, Any]:
        session = self._database().get_auth_session(session_id)
        channel = session["channel"]
        try:
            self._database().update_auth_session(session_id, stage="launching")
            if channel == "douyin" and profile_lock_held(self.service.config.browser_profile_dir):
                return self._database().update_auth_session(
                    session_id,
                    stage="profile_locked",
                    error_summary="专用浏览器正被其他采集任务占用，请稍后再授权",
                    complete=True,
                )
            if channel == "video":
                return await self._run_video(session_id, timeout_seconds=timeout_seconds)
            return await self._run_douyin(session_id, timeout_seconds=timeout_seconds)
        except asyncio.CancelledError:
            current = self._database().get_auth_session(session_id)
            if current["stage"] in ACTIVE_STAGES:
                self._database().update_auth_session(
                    session_id,
                    stage="cancelled",
                    error_summary="用户选择稍后处理",
                    complete=True,
                )
            raise
        except BrowserAuthRequiredError as exc:
            return self._database().update_auth_session(
                session_id,
                stage="timeout",
                error_summary=str(exc),
                complete=True,
            )
        except ExternalToolError as exc:
            return self._database().update_auth_session(
                session_id,
                stage="failed",
                error_summary=str(exc),
                complete=True,
            )
        except Exception as exc:  # Browser launch is an external boundary.
            return self._database().update_auth_session(
                session_id,
                stage="failed",
                error_summary=type(exc).__name__,
                complete=True,
            )

    async def _run_video(self, session_id: str, *, timeout_seconds: int) -> dict[str, Any]:
        session = self._database().get_auth_session(session_id)
        video_url = None
        if session["trigger_job_id"]:
            trigger = self.service.get_job(session["trigger_job_id"])
            raw_url = trigger.artifacts.get("resolved", {}).get("canonical_url") or ""
            video_url = str(raw_url).strip() or None
        initial = await self.service.check_auth_scope("video", video_url=video_url)
        if self._video_ready(initial, video_url):
            return self._finish_success(session_id, {"video"})
        try:
            await self.service.authenticate_video()
        except Exception as exc:
            return self._database().update_auth_session(
                session_id,
                stage="failed",
                error_summary=f"无法打开视频授权浏览器：{type(exc).__name__}",
                complete=True,
            )
        self._database().update_auth_session(session_id, stage="waiting_login")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        poll = self.service.config.auth_guidance.poll_seconds
        while True:
            current = self._database().get_auth_session(session_id)
            if current["stage"] not in ACTIVE_STAGES:
                return current
            self._database().update_auth_session(session_id, stage="verifying")
            check = await self.service.check_auth_scope("video", video_url=video_url)
            if self._video_ready(check, video_url):
                return self._finish_success(session_id, {"video"})
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._database().update_auth_session(
                    session_id,
                    stage="timeout",
                    error_summary="未在限定时间内完成视频授权",
                    complete=True,
                )
            self._database().update_auth_session(session_id, stage="waiting_login")
            await self.sleep(min(poll, remaining))

    async def _run_douyin(self, session_id: str, *, timeout_seconds: int) -> dict[str, Any]:
        requested = {"image_note", "creator", "favorites"}
        ready = await self._ready_douyin_scopes(requested)
        if requested <= ready:
            return self._finish_success(session_id, ready)
        self._database().update_auth_session(session_id, stage="waiting_login")
        await self.service.authenticate_douyin(timeout_seconds=timeout_seconds)
        self._database().update_auth_session(session_id, stage="verifying")
        ready = await self._ready_douyin_scopes(requested)
        if not ready:
            return self._database().update_auth_session(
                session_id,
                stage="failed",
                error_summary="登录后仍未通过抖音账号验证",
                complete=True,
            )
        return self._finish_success(session_id, ready)

    async def _ready_douyin_scopes(self, scopes: set[str]) -> set[str]:
        ready: set[str] = set()
        for scope in sorted(scopes):
            check = await self.service.check_auth_scope(scope)
            if check.ok:
                ready.add(scope)
        return ready

    @staticmethod
    def _video_ready(check, video_url: str | None) -> bool:
        if video_url:
            return bool(check.ok and check.server_verified)
        return bool(check.ok)

    def _finish_success(self, session_id: str, scopes: set[str]) -> dict[str, Any]:
        retried: list[str] = []
        for job in self._database().list_jobs(status=JobStatus.NEEDS_AUTH, limit=None):
            if job.result.get("auth_scope") not in scopes:
                continue
            try:
                self.service.retry_job(job.id)
            except JobStateError:
                continue
            retried.append(job.id)
        return self._database().update_auth_session(
            session_id,
            stage="succeeded",
            affected_job_count=len(retried),
            retried_job_ids=retried,
            complete=True,
        )


