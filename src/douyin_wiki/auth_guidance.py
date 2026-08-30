from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from .config import AuthGuidanceSettings
from .errors import BrowserAuthRequiredError, JobStateError
from .models import AuthCheckResult, JobStatus

AuthScope = Literal["video", "image_note", "creator"]
AuthChannel = Literal["video", "douyin"]


@dataclass(frozen=True)
class AuthGuidanceOutcome:
    status: Literal["cancelled", "unavailable", "timeout", "failed", "retried"]
    retried_job_ids: tuple[str, ...] = ()
    message: str = ""


class AuthGuidanceLauncher(Protocol):
    def launch(self, *, scope: str, trigger_job_id: str) -> bool: ...


class NoopAuthGuidanceLauncher:
    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        return False


class AuthDialog(Protocol):
    def confirm(self, channel: str, affected_counts: dict[str, int]) -> bool: ...

    def notify(self, title: str, message: str) -> None: ...


class ChannelLock(Protocol):
    def acquire(self, channel: str) -> AbstractContextManager[bool]: ...


class AuthGuidanceService(Protocol):
    database: object

    async def authenticate_douyin(self, *, timeout_seconds: int = 600) -> dict: ...

    async def authenticate_video(self) -> dict: ...

    async def check_auth_scope(
        self,
        scope: str,
        *,
        video_url: str | None = None,
    ) -> AuthCheckResult: ...

    def retry_job(self, job_id: str): ...


def auth_channel(scope: str) -> AuthChannel:
    if scope == "video":
        return "video"
    if scope in {"image_note", "creator"}:
        return "douyin"
    raise ValueError(f"不支持的授权范围：{scope}")


class AuthGuidanceCoordinator:
    def __init__(
        self,
        service: AuthGuidanceService,
        *,
        dialog: AuthDialog,
        channel_lock: ChannelLock,
        settings: AuthGuidanceSettings,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.service = service
        self.dialog = dialog
        self.channel_lock = channel_lock
        self.settings = settings
        self.monotonic = monotonic
        self.sleep = sleep

    async def run(self, scope: str, trigger_job_id: str) -> AuthGuidanceOutcome:
        channel = auth_channel(scope)
        with self.channel_lock.acquire(channel) as acquired:
            if not acquired:
                return AuthGuidanceOutcome(status="unavailable", message="已有授权引导正在运行")
            trigger = self.service.database.get_job(trigger_job_id)
            if trigger.status != JobStatus.NEEDS_AUTH:
                return AuthGuidanceOutcome(status="unavailable", message="任务已不再等待授权")
            affected = self._affected_jobs(channel)
            counts = self._affected_counts(affected, channel)
            if channel == "video":
                return await self._run_video(trigger, counts)
            return await self._run_douyin(counts)

    async def _run_video(self, trigger, counts: dict[str, int]) -> AuthGuidanceOutcome:
        video_url = str(
            trigger.artifacts.get("resolved", {}).get("canonical_url") or ""
        ).strip()
        if not video_url:
            return AuthGuidanceOutcome(status="failed", message="任务缺少可验证的视频地址")
        initial = await self.service.check_auth_scope("video", video_url=video_url)
        if self._video_ready(initial):
            return self._retry_scopes({"video"})
        if not self.dialog.confirm("video", counts):
            return AuthGuidanceOutcome(status="cancelled")
        try:
            await self.service.authenticate_video()
        except Exception as exc:  # Browser launch is an external boundary.
            self.dialog.notify("抖库授权未完成", "无法打开视频授权浏览器，任务仍保持暂停")
            return AuthGuidanceOutcome(status="failed", message=type(exc).__name__)

        deadline = self.monotonic() + self.settings.timeout_seconds
        while True:
            check = await self.service.check_auth_scope("video", video_url=video_url)
            if self._video_ready(check):
                return self._retry_scopes({"video"})
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                self.dialog.notify("抖库授权尚未完成", "视频任务仍保持暂停，可稍后重试")
                return AuthGuidanceOutcome(status="timeout")
            await self.sleep(min(self.settings.poll_seconds, remaining))

    async def _run_douyin(self, counts: dict[str, int]) -> AuthGuidanceOutcome:
        requested_scopes = {scope for scope, count in counts.items() if count}
        ready_scopes = await self._ready_douyin_scopes(requested_scopes)
        if requested_scopes <= ready_scopes:
            return self._retry_scopes(ready_scopes)
        if not self.dialog.confirm("douyin", counts):
            return AuthGuidanceOutcome(status="cancelled")
        try:
            await self.service.authenticate_douyin(
                timeout_seconds=self.settings.timeout_seconds
            )
        except BrowserAuthRequiredError:
            self.dialog.notify("抖库授权尚未完成", "图文与博主任务仍保持暂停，可稍后重试")
            return AuthGuidanceOutcome(status="timeout")
        except Exception as exc:  # Browser launch is an external boundary.
            self.dialog.notify("抖库授权未完成", "无法打开图文授权浏览器，任务仍保持暂停")
            return AuthGuidanceOutcome(status="failed", message=type(exc).__name__)
        ready_scopes = await self._ready_douyin_scopes(requested_scopes)
        if not ready_scopes:
            self.dialog.notify("抖库授权尚未验证", "图文与博主任务仍保持暂停，可稍后重试")
            return AuthGuidanceOutcome(status="failed")
        return self._retry_scopes(ready_scopes)

    async def _ready_douyin_scopes(self, scopes: set[str]) -> set[str]:
        ready: set[str] = set()
        for scope in sorted(scopes):
            check = await self.service.check_auth_scope(scope)
            if check.ok:
                ready.add(scope)
        return ready

    def _affected_jobs(self, channel: AuthChannel):
        scopes = {"video"} if channel == "video" else {"image_note", "creator"}
        return [
            job
            for job in self.service.database.list_jobs(
                status=JobStatus.NEEDS_AUTH,
                limit=None,
            )
            if job.result.get("auth_scope") in scopes
        ]

    @staticmethod
    def _affected_counts(jobs, channel: AuthChannel) -> dict[str, int]:
        scopes = ("video",) if channel == "video" else ("image_note", "creator")
        return {
            scope: sum(job.result.get("auth_scope") == scope for job in jobs)
            for scope in scopes
        }

    @staticmethod
    def _video_ready(check: AuthCheckResult) -> bool:
        return check.ok and check.server_verified

    def _retry_scopes(self, scopes: set[str]) -> AuthGuidanceOutcome:
        retried: list[str] = []
        for job in self.service.database.list_jobs(
            status=JobStatus.NEEDS_AUTH,
            limit=None,
        ):
            if job.result.get("auth_scope") not in scopes:
                continue
            try:
                self.service.retry_job(job.id)
            except JobStateError:
                continue
            retried.append(job.id)
        message = (
            f"授权成功，已继续 {len(retried)} 个任务"
            if retried
            else "授权已恢复，没有等待中的任务"
        )
        self.dialog.notify("抖库授权成功", message)
        return AuthGuidanceOutcome(
            status="retried",
            retried_job_ids=tuple(retried),
            message=message,
        )
