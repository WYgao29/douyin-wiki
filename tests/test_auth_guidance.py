from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.auth_guidance import (
    AUTH_DIALOG_SCRIPT,
    AUTH_NOTIFICATION_SCRIPT,
    AuthGuidanceCoordinator,
    FileChannelLock,
    MacOSDialog,
    SubprocessAuthGuidanceLauncher,
)
from douyin_wiki.config import AppConfig, AuthGuidanceSettings, EmbeddingSettings
from douyin_wiki.models import AuthCheckResult, CaptureRequest, JobStatus
from douyin_wiki.service import DouyinWikiService


def auth_result(
    scope: str,
    *,
    ok: bool,
    server_verified: bool = True,
) -> AuthCheckResult:
    return AuthCheckResult(
        scope=scope,
        state="ready" if ok else "needs_login",
        ok=ok,
        server_verified=server_verified,
        cookie_source=f"fake-{scope}",
        message="ready" if ok else "login required",
    )


class SequenceVideoAdapter:
    def __init__(self, checks: list[AuthCheckResult]) -> None:
        self.checks = list(checks)
        self.authenticate_calls = 0
        self.checked_urls: list[str | None] = []
        self.on_check: Callable[[], None] | None = None

    async def authenticate(self) -> AuthCheckResult:
        self.authenticate_calls += 1
        return auth_result("video", ok=False, server_verified=False)

    async def check_auth(self, *, video_url: str | None = None) -> AuthCheckResult:
        self.checked_urls.append(video_url)
        if self.on_check:
            callback, self.on_check = self.on_check, None
            callback()
        if len(self.checks) > 1:
            return self.checks.pop(0)
        return self.checks[0]


class SequenceDouyinAdapter:
    def __init__(self, scope: str, checks: list[AuthCheckResult]) -> None:
        self.scope = scope
        self.checks = list(checks)
        self.authenticate_calls: list[int] = []

    async def authenticate(self, *, timeout_seconds: int) -> None:
        self.authenticate_calls.append(timeout_seconds)

    async def check_auth(self) -> AuthCheckResult:
        if len(self.checks) > 1:
            return self.checks.pop(0)
        return self.checks[0]


class FakeDialog:
    def __init__(self, *, confirmed: bool = True) -> None:
        self.confirmed = confirmed
        self.confirm_calls: list[tuple[str, dict[str, int]]] = []
        self.notifications: list[tuple[str, str]] = []

    def confirm(self, channel: str, affected_counts: dict[str, int]) -> bool:
        self.confirm_calls.append((channel, affected_counts))
        return self.confirmed

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))


class FakeChannelLock:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.channels: list[str] = []

    @contextmanager
    def acquire(self, channel: str):
        self.channels.append(channel)
        yield self.available


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeCommandRunner:
    def __init__(self, *, stdout: str = "confirmed\n", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            stdout=self.stdout,
            stderr="",
        )


class FakeProcessLauncher:
    def __init__(self, *, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return object()


def make_service(
    tmp_path: Path,
    *,
    video_checks: list[AuthCheckResult] | None = None,
    image_checks: list[AuthCheckResult] | None = None,
    creator_checks: list[AuthCheckResult] | None = None,
) -> tuple[
    DouyinWikiService,
    SequenceVideoAdapter,
    SequenceDouyinAdapter,
    SequenceDouyinAdapter,
]:
    embeddings = EmbeddingSettings(provider="hash", fallback_dimensions=32)
    config = AppConfig(vault_path=tmp_path / "vault", embeddings=embeddings)
    video = SequenceVideoAdapter(
        video_checks or [auth_result("video", ok=False, server_verified=True)]
    )
    image_note = SequenceDouyinAdapter(
        "image_note", image_checks or [auth_result("image_note", ok=False)]
    )
    creator = SequenceDouyinAdapter(
        "creator", creator_checks or [auth_result("creator", ok=False)]
    )
    service = DouyinWikiService(
        config,
        downloader=video,
        image_note_downloader=image_note,
        creator_adapter=creator,
        embeddings=EmbeddingService(embeddings),
    )
    service.initialize(initialize_git=False)
    return service, video, image_note, creator


def create_auth_job(
    service: DouyinWikiService,
    scope: str,
    *,
    canonical_url: str | None = None,
    status: JobStatus = JobStatus.NEEDS_AUTH,
):
    job = service.database.create_job(
        CaptureRequest(share_text=canonical_url or "https://v.douyin.com/example/")
    )
    artifacts = (
        {
            "resolved": {
                "canonical_url": canonical_url,
                "original_url": canonical_url,
                "video_id": "7659645255277039717",
                "redirect_chain": [canonical_url],
                "source_kind": "video" if scope == "video" else "image_note",
            }
        }
        if canonical_url
        else None
    )
    return service.database.update_job(
        job.id,
        status=status,
        artifacts=artifacts,
        result={"auth_scope": scope},
    )


def make_coordinator(
    service: DouyinWikiService,
    dialog: FakeDialog,
    channel_lock: FakeChannelLock,
    clock: FakeClock,
) -> AuthGuidanceCoordinator:
    return AuthGuidanceCoordinator(
        service,
        dialog=dialog,
        channel_lock=channel_lock,
        settings=AuthGuidanceSettings(timeout_seconds=30, poll_seconds=2),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_confirmed_video_auth_retries_only_matching_paused_jobs(tmp_path: Path) -> None:
    service, video, _, _ = make_service(
        tmp_path,
        video_checks=[
            auth_result("video", ok=False),
            auth_result("video", ok=True, server_verified=True),
        ],
    )
    first = create_auth_job(
        service,
        "video",
        canonical_url="https://www.douyin.com/video/7659645255277039717",
    )
    second = create_auth_job(
        service,
        "video",
        canonical_url="https://www.douyin.com/video/7678561149449331835",
    )
    image_note = create_auth_job(service, "image_note")
    completed = create_auth_job(service, "video", status=JobStatus.COMPLETED)
    dialog, lock, clock = FakeDialog(), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("video", first.id)

    assert outcome.status == "retried"
    assert set(outcome.retried_job_ids) == {first.id, second.id}
    assert service.database.get_job(first.id).status == JobStatus.QUEUED
    assert service.database.get_job(second.id).status == JobStatus.QUEUED
    assert service.database.get_job(image_note.id).status == JobStatus.NEEDS_AUTH
    assert service.database.get_job(completed.id).status == JobStatus.COMPLETED
    assert video.authenticate_calls == 1
    assert dialog.confirm_calls == [("video", {"video": 2})]


@pytest.mark.asyncio
async def test_cancel_keeps_jobs_paused_and_does_not_open_browser(tmp_path: Path) -> None:
    service, video, _, _ = make_service(tmp_path)
    job = create_auth_job(
        service,
        "video",
        canonical_url="https://www.douyin.com/video/7659645255277039717",
    )
    dialog, lock, clock = FakeDialog(confirmed=False), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("video", job.id)

    assert outcome.status == "cancelled"
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_AUTH
    assert video.authenticate_calls == 0


@pytest.mark.asyncio
async def test_video_timeout_never_retries_job(tmp_path: Path) -> None:
    service, _, _, _ = make_service(tmp_path)
    job = create_auth_job(
        service,
        "video",
        canonical_url="https://www.douyin.com/video/7659645255277039717",
    )
    dialog, lock, clock = FakeDialog(), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("video", job.id)

    assert outcome.status == "timeout"
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_AUTH
    assert clock.now == 30


@pytest.mark.asyncio
async def test_douyin_channel_retries_only_ready_scopes(tmp_path: Path) -> None:
    service, _, image_adapter, _ = make_service(
        tmp_path,
        image_checks=[
            auth_result("image_note", ok=False),
            auth_result("image_note", ok=True),
            auth_result("image_note", ok=True),
        ],
        creator_checks=[auth_result("creator", ok=False)],
    )
    image_job = create_auth_job(service, "image_note")
    creator_job = create_auth_job(service, "creator")
    dialog, lock, clock = FakeDialog(), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run(
        "image_note", image_job.id
    )

    assert outcome.status == "retried"
    assert outcome.retried_job_ids == (image_job.id,)
    assert service.database.get_job(image_job.id).status == JobStatus.QUEUED
    assert service.database.get_job(creator_job.id).status == JobStatus.NEEDS_AUTH
    assert image_adapter.authenticate_calls == [30]
    assert dialog.confirm_calls == [("douyin", {"image_note": 1, "creator": 1})]


@pytest.mark.asyncio
async def test_busy_channel_lock_suppresses_duplicate_prompt(tmp_path: Path) -> None:
    service, _, _, _ = make_service(tmp_path)
    job = create_auth_job(service, "creator")
    dialog, lock, clock = FakeDialog(), FakeChannelLock(available=False), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("creator", job.id)

    assert outcome.status == "unavailable"
    assert dialog.confirm_calls == []
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_AUTH


@pytest.mark.asyncio
async def test_video_without_canonical_url_fails_without_retry(tmp_path: Path) -> None:
    service, _, _, _ = make_service(tmp_path)
    job = create_auth_job(service, "video")
    dialog, lock, clock = FakeDialog(), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("video", job.id)

    assert outcome.status == "failed"
    assert service.database.get_job(job.id).status == JobStatus.NEEDS_AUTH


@pytest.mark.asyncio
async def test_job_leaving_needs_auth_during_verification_is_not_requeued(
    tmp_path: Path,
) -> None:
    service, video, _, _ = make_service(
        tmp_path,
        video_checks=[
            auth_result("video", ok=False),
            auth_result("video", ok=True, server_verified=True),
        ],
    )
    job = create_auth_job(
        service,
        "video",
        canonical_url="https://www.douyin.com/video/7659645255277039717",
    )
    video.on_check = lambda: service.database.update_job(job.id, status=JobStatus.FAILED)
    dialog, lock, clock = FakeDialog(), FakeChannelLock(), FakeClock()

    outcome = await make_coordinator(service, dialog, lock, clock).run("video", job.id)

    assert outcome.retried_job_ids == ()
    assert service.database.get_job(job.id).status == JobStatus.FAILED


def test_macos_dialog_passes_dynamic_values_as_argv() -> None:
    runner = FakeCommandRunner(stdout="confirmed\n")
    dialog = MacOSDialog(runner=runner)

    confirmed = dialog.confirm("douyin", {"image_note": 2, "creator": 1})

    assert confirmed is True
    argv, kwargs = runner.calls[0]
    assert argv == [
        "osascript",
        "-e",
        AUTH_DIALOG_SCRIPT,
        "--",
        "图文与博主",
        "3",
        "图文 2 个，博主 1 个",
    ]
    assert kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 30,
    }


def test_macos_dialog_cancel_and_notification_are_non_destructive() -> None:
    runner = FakeCommandRunner(stdout="cancelled\n")
    dialog = MacOSDialog(runner=runner)

    assert dialog.confirm("video", {"video": 1}) is False
    dialog.notify("抖库授权成功", "授权成功，已继续 1 个任务")

    assert runner.calls[1][0] == [
        "osascript",
        "-e",
        AUTH_NOTIFICATION_SCRIPT,
        "--",
        "抖库授权成功",
        "授权成功，已继续 1 个任务",
    ]


def test_file_channel_lock_deduplicates_douyin_scopes(tmp_path: Path) -> None:
    first = FileChannelLock(tmp_path)
    second = FileChannelLock(tmp_path)

    with first.acquire("douyin") as first_acquired, second.acquire(
        "douyin"
    ) as second_acquired:
        assert first_acquired is True
        assert second_acquired is False
    with second.acquire("douyin") as acquired_after_release:
        assert acquired_after_release is True


def test_subprocess_launcher_uses_current_python_without_shell(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    process_launcher = FakeProcessLauncher()
    launcher = SubprocessAuthGuidanceLauncher(
        config_path=config_path,
        settings=AuthGuidanceSettings(),
        platform_name="darwin",
        process_launcher=process_launcher,
    )

    launched = launcher.launch(scope="image_note", trigger_job_id="a" * 32)

    assert launched is True
    argv, kwargs = process_launcher.calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "douyin_wiki.auth_guidance",
        "--config-path",
        str(config_path),
        "--scope",
        "image_note",
        "--job-id",
        "a" * 32,
    ]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "start_new_session": True,
    }


@pytest.mark.parametrize(
    ("enabled", "platform_name", "scope", "job_id"),
    [
        (False, "darwin", "video", "a" * 32),
        (True, "linux", "video", "a" * 32),
        (True, "darwin", "unknown", "a" * 32),
        (True, "darwin", "video", "not-a-job-id"),
    ],
)
def test_subprocess_launcher_declines_unsupported_invocations(
    tmp_path: Path,
    enabled: bool,
    platform_name: str,
    scope: str,
    job_id: str,
) -> None:
    process_launcher = FakeProcessLauncher()
    launcher = SubprocessAuthGuidanceLauncher(
        config_path=tmp_path / "config.toml",
        settings=AuthGuidanceSettings(enabled=enabled),
        platform_name=platform_name,
        process_launcher=process_launcher,
    )

    assert launcher.launch(scope=scope, trigger_job_id=job_id) is False
    assert process_launcher.calls == []


def test_subprocess_launcher_failure_returns_false(tmp_path: Path) -> None:
    launcher = SubprocessAuthGuidanceLauncher(
        config_path=tmp_path / "config.toml",
        settings=AuthGuidanceSettings(),
        platform_name="darwin",
        process_launcher=FakeProcessLauncher(error=OSError("launch failed")),
    )

    assert launcher.launch(scope="video", trigger_job_id="b" * 32) is False
