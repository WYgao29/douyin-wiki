from __future__ import annotations

import argparse
import asyncio
import fcntl
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .config import AuthGuidanceSettings
from .errors import BrowserAuthRequiredError, JobStateError
from .models import AuthCheckResult, JobStatus

AuthScope = Literal["video", "image_note", "creator", "favorites"]
AuthChannel = Literal["video", "douyin"]

AUTH_DIALOG_SCRIPT = """
on run argv
    set authLabel to item 1 of argv
    set taskCount to item 2 of argv
    set taskDetail to item 3 of argv
    try
        set answer to display alert "抖库需要抖音授权" message ¬
            ("授权类型：" & authLabel & return & ¬
             "等待任务：" & taskCount & " 个（" & taskDetail & "）" & return & return & ¬
             "抖库不会读取或显示你的密码，也不会输出 Cookie。" & return & ¬
             "授权成功后将自动继续任务。") ¬
            buttons {"稍后处理", "打开浏览器授权"} ¬
            default button "打开浏览器授权" cancel button "稍后处理"
        if button returned of answer is "打开浏览器授权" then
            return "confirmed"
        end if
        return "cancelled"
    on error number -128
        return "cancelled"
    end try
end run
""".strip()

AUTH_NOTIFICATION_SCRIPT = """
on run argv
    display notification (item 2 of argv) with title (item 1 of argv)
end run
""".strip()


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


class MacOSDialog:
    def __init__(self, *, runner: Callable = subprocess.run) -> None:
        self.runner = runner

    def confirm(self, channel: str, affected_counts: dict[str, int]) -> bool:
        if channel == "video":
            label = "视频"
            total = affected_counts.get("video", 0)
            detail = f"视频 {total} 个"
        elif channel == "douyin":
            label = "图文与博主"
            image_count = affected_counts.get("image_note", 0)
            creator_count = affected_counts.get("creator", 0)
            favorites_count = affected_counts.get("favorites", 0)
            total = image_count + creator_count + favorites_count
            detail = f"图文 {image_count} 个，博主 {creator_count} 个"
            if favorites_count:
                label = "图文、博主与收藏"
                detail += f"，收藏 {favorites_count} 个"
        else:
            return False
        result = self.runner(
            [
                "osascript",
                "-e",
                AUTH_DIALOG_SCRIPT,
                "--",
                label,
                str(total),
                detail,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0 and result.stdout.strip() == "confirmed"

    def notify(self, title: str, message: str) -> None:
        self.runner(
            ["osascript", "-e", AUTH_NOTIFICATION_SCRIPT, "--", title, message],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


class FileChannelLock:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    @contextmanager
    def acquire(self, channel: str):
        if channel not in {"video", "douyin"}:
            yield False
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.state_dir / f"auth-guidance-{channel}.lock").open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class SubprocessAuthGuidanceLauncher:
    def __init__(
        self,
        *,
        config_path: Path,
        settings: AuthGuidanceSettings,
        platform_name: str = sys.platform,
        process_launcher: Callable = subprocess.Popen,
    ) -> None:
        self.config_path = config_path
        self.settings = settings
        self.platform_name = platform_name
        self.process_launcher = process_launcher

    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        if (
            not self.settings.enabled
            or self.platform_name != "darwin"
            or scope not in {"video", "image_note", "creator", "favorites"}
            or re.fullmatch(r"[0-9a-f]{32}", trigger_job_id) is None
        ):
            return False
        argv = [
            sys.executable,
            "-m",
            "douyin_wiki.auth_guidance",
            "--config-path",
            str(self.config_path),
            "--scope",
            scope,
            "--job-id",
            trigger_job_id,
        ]
        try:
            self.process_launcher(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            return False
        return True


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
    if scope in {"image_note", "creator", "favorites"}:
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
        scopes = {"video"} if channel == "video" else {"image_note", "creator", "favorites"}
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
        scopes = ("video",) if channel == "video" else ("image_note", "creator", "favorites")
        counts = {
            scope: sum(job.result.get("auth_scope") == scope for job in jobs) for scope in scopes
        }
        if not counts.get("favorites"):
            counts.pop("favorites", None)
        return counts

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


async def run_auth_guidance_process(
    *,
    config_path: Path,
    scope: AuthScope,
    trigger_job_id: str,
) -> AuthGuidanceOutcome:
    from .config import load_config
    from .service import DouyinWikiService

    config = load_config(config_path)
    service = DouyinWikiService(config)
    service.initialize_runtime()
    coordinator = AuthGuidanceCoordinator(
        service,
        dialog=MacOSDialog(),
        channel_lock=FileChannelLock(config.state_dir),
        settings=config.auth_guidance,
    )
    return await coordinator.run(scope, trigger_job_id)


def _job_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError("job ID 必须是 32 位小写十六进制")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抖库 macOS 授权助手")
    parser.add_argument("--config-path", type=Path, required=True)
    parser.add_argument(
        "--scope",
        choices=("video", "image_note", "creator", "favorites"),
        required=True,
    )
    parser.add_argument("--job-id", type=_job_id, required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            run_auth_guidance_process(
                config_path=args.config_path,
                scope=args.scope,
                trigger_job_id=args.job_id,
            )
        )
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
