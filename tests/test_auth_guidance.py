from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.auth_guidance import AuthGuidanceCoordinator
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
