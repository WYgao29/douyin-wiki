from __future__ import annotations

import asyncio
import copy
import json
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.adapters.llm import (
    ModelLimitError,
    ModelOutputError,
    OpenAICompatibleProvider,
    _analysis_response_schema,
    _nearby_ocr,
    _parse_json_content,
    _preserve_partial_coverage,
)
from douyin_wiki.config import EmbeddingSettings, LLMSettings
from douyin_wiki.errors import (
    CookieRequiredError,
    ExternalToolError,
    JobStateError,
    ModelServiceError,
)
from douyin_wiki.models import (
    AnalysisMode,
    AnalysisResult,
    AuthCheckResult,
    CaptureOptions,
    GatewayContext,
    InspirationInput,
    JobStatus,
    OCRObservation,
    RetentionPolicy,
    TranscriptCorrection,
    TranscriptSegment,
)
from douyin_wiki.operation import present_job
from douyin_wiki.service import DouyinWikiService
from douyin_wiki.vault import VaultWriter
from douyin_wiki.worker import Worker

from .conftest import (
    FakeAnalysisProvider,
    FakeDownloader,
    FakeMediaProcessor,
    FakeOCR,
    FakeResolver,
    FakeTranscriber,
)


def test_vault_writer_load_error_collections_start_empty(tmp_path: Path) -> None:
    vault = VaultWriter(tmp_path)

    assert vault.last_entry_load_errors == []
    assert vault.last_creator_load_errors == []
    assert vault.last_topic_load_errors == []
    assert vault.last_artifact_load_errors == []


class CookieExpiredDownloader:
    async def download(self, url: str, video_id: str, target_dir: Path):
        raise CookieRequiredError("需要更新浏览器 cookie")


class ScopedAuthAdapter:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.check_calls: list[dict] = []

    async def check_auth(self, **kwargs) -> AuthCheckResult:
        self.check_calls.append(kwargs)
        return AuthCheckResult(
            scope=self.scope,
            state="ready",
            ok=True,
            server_verified=True,
            cookie_source=f"fake-{self.scope}",
            message=f"{self.scope} ready",
        )


class InspectingAuthGuidanceLauncher:
    def __init__(self, database) -> None:
        self.database = database
        self.calls: list[tuple[str, str, JobStatus, dict]] = []

    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        persisted = self.database.get_job(trigger_job_id)
        self.calls.append((scope, trigger_job_id, persisted.status, persisted.result))
        return True


class FailingAuthGuidanceLauncher:
    def __init__(self) -> None:
        self.calls = 0

    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        self.calls += 1
        raise OSError("system dialog unavailable")


class BrokenOCR:
    async def recognize(self, frames):
        raise ExternalToolError("synthetic OCR failure")


def test_work_capture_lock_rejects_unsafe_work_ids(tmp_path: Path) -> None:
    vault = VaultWriter(tmp_path / "vault")

    with pytest.raises(ValueError), vault.work_capture_locked("../../outside"):
        pass

    assert not (tmp_path / "vault" / ".douyin-wiki").exists()


class BlockingDownloader(FakeDownloader):
    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, url: str, video_id: str, target_dir: Path):
        self.entered += 1
        if self.entered == 1:
            self.started.set()
            await self.release.wait()
        return await super().download(url, video_id, target_dir)


class CountingAnalysisProvider(FakeAnalysisProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, segments, ocr, inspirations, metadata):
        self.calls += 1
        return await super().analyze(segments, ocr, inspirations, metadata)


class GatedLockContext:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.started = threading.Event()
        self.acquired = threading.Event()
        self.allow_return = threading.Event()
        self.returned = threading.Event()
        self.released = threading.Event()

    def __enter__(self):
        self.started.set()
        result = self.inner.__enter__()
        self.acquired.set()
        self.allow_return.wait(timeout=5)
        self.returned.set()
        return result

    def __exit__(self, *args):
        self.released.set()
        return self.inner.__exit__(*args)


@pytest.mark.asyncio
async def test_gateway_agent_handoff_completes_and_emits_routed_events(service) -> None:
    service.config.analysis_mode = AnalysisMode.GATEWAY
    job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        [InspirationInput(text="建立个人财经信息源筛选标准")],
        gateway_context=GatewayContext(
            gateway="hermes",
            channel="telegram",
            conversation_id="chat-42",
            reply_target="user-7",
        ),
    )

    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert paused.result["phase"] == "transcript_correction"
    context = service.get_analysis_context(job.id)
    assert context["gateway_context"]["conversation_id"] == "chat-42"
    assert context["inspirations_verbatim"][0]["text"] == "建立个人财经信息源筛选标准"
    assert context["analysis_schema"]["title"] == "AnalysisResultV2"
    assert "chapters" in context["analysis_schema"]["properties"]
    assert "key_moments" not in context["analysis_schema"]["properties"]

    corrected = service.submit_transcript_correction(
        job.id,
        [
            TranscriptCorrection(
                id=item["id"],
                text="离职以后，我取关了很多财经媒体。" if item["id"] == 0 else item["text"],
            )
            for item in paused.artifacts["transcript_raw"]
        ],
        producer="hermes",
        model="gateway-model",
    )
    assert corrected.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert corrected.result["phase"] == "analysis"
    queued = service.submit_gateway_analysis(
        job.id,
        {
            "title": "离职后如何筛选财经媒体",
            "summary": "优先保留能够提供一手证据的信息源。",
            "core_points": ["信息源质量比数量重要"],
            "tags": ["财经", "信息筛选"],
            "ai_judgment": "值得保存。",
        },
        producer="hermes",
        model="gateway-model",
    )
    assert queued.status == JobStatus.QUEUED

    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    data = service.database.get_entry_data(completed.result["entry_id"])
    assert data["transcript_raw"][0]["text"] == "离职以后我取关了很多财经媒体。"
    assert data["transcript_corrected"][0]["text"] == "离职以后，我取关了很多财经媒体。"
    assert data["provider"] == "agent:hermes"
    assert data["model"] == "gateway-model"

    events = service.list_job_events()
    assert [event.status for event in events] == [JobStatus.COMPLETED]
    assert all(event.gateway_context.gateway == "hermes" for event in events)
    history = service.list_job_events(unacknowledged_only=False)
    assert [event.status for event in history] == [
        JobStatus.AWAITING_AGENT_ANALYSIS,
        JobStatus.AWAITING_AGENT_ANALYSIS,
        JobStatus.COMPLETED,
    ]
    assert history[0].superseded_at is not None
    assert history[1].superseded_at is not None
    acknowledged = service.acknowledge_job_event(events[0].id)
    assert acknowledged.acknowledged_at is not None
    assert service.acknowledge_job_event(events[0].id).acknowledged_at is not None
    assert events[0].id not in {event.id for event in service.list_job_events()}


@pytest.mark.asyncio
async def test_jobs_without_gateway_route_do_not_create_delivery_events(service) -> None:
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.id == job.id
    assert completed.status == JobStatus.COMPLETED
    assert service.list_job_events() == []


@pytest.mark.asyncio
async def test_provider_mode_does_not_silently_fall_back_when_unconfigured(service) -> None:
    service.analysis = OpenAICompatibleProvider(service.config.llm)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    failed = await Worker(service).run_once()
    assert failed.id == job.id
    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "model_not_configured"
    assert service.database.list_entries() == []


@pytest.mark.asyncio
async def test_ocr_failure_is_visible_as_completed_warning(service) -> None:
    service.ocr = BrokenOCR()
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED_WITH_WARNINGS
    assert "OCR 未完成" in completed.result["warnings"][0]


@pytest.mark.asyncio
async def test_end_to_end_capture_writes_vault_and_searches(service) -> None:
    inspiration = InspirationInput(
        text="建立个人财经信息源筛选标准",
        quote="筛选信息源要看它能否提供一手证据",
        start_ms=5000,
        end_ms=9000,
    )
    job = service.capture_douyin(
        "6.43 复制打开抖音 https://v.douyin.com/uvHsRpXIn8s/",
        [inspiration],
    )
    completed = await Worker(service).run_once()
    assert completed.id == job.id
    assert completed.status == JobStatus.COMPLETED
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    raw = service.config.vault_path / entry.raw_path
    source = service.config.vault_path / entry.source_path
    assert raw.exists() and source.exists()
    assert "建立个人财经信息源筛选标准" in raw.read_text(encoding="utf-8")
    assert "https://v.douyin.com/uvHsRpXIn8s/" in source.read_text(encoding="utf-8")
    assert "author: Nee霓公子" in source.read_text(encoding="utf-8")
    source_text = source.read_text(encoding="utf-8")
    assert "status: 正常" in source_text
    assert "media_status: 已保留" in source_text
    assert "media_retention: 临时保留" in source_text
    assert "cover_image: raw/covers/7672717300746907078.jpg" in source_text
    assert "cover_kind: fallback" in source_text
    assert "analysis_version: 2" in source_text
    assert "machine_data_path: wiki/.data/sources/7672717300746907078.md" in source_text
    assert "![抖音视频封面](../../raw/covers/7672717300746907078.jpg)" in source_text
    assert "## 一句话" in source_text
    assert "## 关键主张" not in source_text
    assert "核验" not in source_text
    assert "外部来源" not in source_text
    assert "verification_status" not in source_text
    assert (service.config.vault_path / "raw" / "covers" / "7672717300746907078.jpg").is_file()
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / "7672717300746907078.md"
    assert machine.is_file()
    assert "knowledge_atoms:" in machine.read_text(encoding="utf-8")
    assert "[00:05]" in raw.read_text(encoding="utf-8")

    evidence = service.search_knowledge("如何筛选财经信息源")
    assert evidence
    assert evidence[0].original_url == "https://v.douyin.com/uvHsRpXIn8s/"
    assert any(item.timestamp_ms == 5000 for item in evidence)


@pytest.mark.asyncio
async def test_video_without_audio_still_extracts_frames_without_transcribing(service) -> None:
    class NoAudioProcessor(FakeMediaProcessor):
        async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
            raise ExternalToolError("视频没有音轨")

    class UnexpectedTranscriber:
        async def transcribe(self, audio_path: Path, output_dir: Path):
            raise AssertionError("无音轨时不应调用转录")

    class OcrOnlyAnalysis(FakeAnalysisProvider):
        async def analyze(self, segments, ocr, inspirations, metadata):
            return AnalysisResult(
                title="财经媒体筛选",
                one_liner="画面显示财经媒体筛选",
                takeaways=["画面显示财经媒体筛选"],
            )

    service.media = NoAudioProcessor()
    service.transcriber = UnexpectedTranscriber()
    service.analysis = OcrOnlyAnalysis()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")

    completed = await Worker(service).run_once()

    assert completed.id == job.id
    assert completed.artifacts["transcript_raw"] == []
    assert completed.artifacts["transcript_skipped"] == "no_audio_available"
    assert completed.artifacts["ocr"]
    assert completed.status == JobStatus.COMPLETED_WITH_WARNINGS, completed.error_message
    assert any("无音轨" in warning for warning in completed.result["warnings"])


@pytest.mark.asyncio
async def test_video_audio_tool_failure_is_not_mistaken_for_missing_audio(service) -> None:
    class BrokenAudioProcessor(FakeMediaProcessor):
        async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
            raise ExternalToolError("缺少外部工具：ffmpeg")

    service.media = BrokenAudioProcessor()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")

    failed = await Worker(service).run_once()

    assert failed.id == job.id
    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "external_tool_error"


@pytest.mark.asyncio
async def test_asr_prefers_retained_audio_over_video(service) -> None:
    """CDN may keep audio.m4a / audio.mp4; ASR should prefer those over video."""
    extracted: list[Path] = []

    class RetainedAudioDownloader(FakeDownloader):
        async def download(self, url: str, video_id: str, target_dir: Path):
            metadata = await super().download(url, video_id, target_dir)
            # Simulate CDN retaining audio.m4a (before rename) as ASR source.
            (target_dir / "audio.m4a").write_bytes(b"retained-audio")
            return metadata

    class TrackingMedia(FakeMediaProcessor):
        async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
            extracted.append(video_path)
            return await super().extract_audio(video_path, audio_path)

    service.downloader = RetainedAudioDownloader()
    service.media = TrackingMedia()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()

    assert completed.id == job.id
    assert completed.status == JobStatus.COMPLETED
    assert extracted, "extract_audio should have been called"
    assert extracted[0].name == "audio.m4a"
    assert extracted[0].suffix.lower() == ".m4a"
    # Video is only a fallback; must not be the first (or only) source used.
    assert all(path.name != "original.mp4" for path in extracted)


@pytest.mark.asyncio
async def test_asr_prefers_audio_mp4_over_other_audio_extensions(service) -> None:
    extracted: list[Path] = []

    class BothAudioDownloader(FakeDownloader):
        async def download(self, url: str, video_id: str, target_dir: Path):
            metadata = await super().download(url, video_id, target_dir)
            (target_dir / "audio.m4a").write_bytes(b"m4a")
            (target_dir / "audio.mp4").write_bytes(b"mp4")
            return metadata

    class TrackingMedia(FakeMediaProcessor):
        async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
            extracted.append(video_path)
            return await super().extract_audio(video_path, audio_path)

    service.downloader = BothAudioDownloader()
    service.media = TrackingMedia()
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()

    assert completed.status == JobStatus.COMPLETED
    assert extracted[0].name == "audio.mp4"


@pytest.mark.asyncio
async def test_duplicate_video_appends_inspiration_without_redownload(service) -> None:
    downloader = service.downloader
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="灵感一")])
    first = await Worker(service).run_once()
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="灵感二")])
    second = await Worker(service).run_once()
    assert second.result["duplicate"] is True
    assert downloader.calls == 1
    entry = service.database.get_entry(first.result["entry_id"])
    assert [item.text for item in entry.inspirations] == ["灵感一", "灵感二"]


@pytest.mark.asyncio
async def test_concurrent_first_capture_serializes_video_pipeline_and_merges_inspirations(
    config, fake_reminders
) -> None:
    downloader = BlockingDownloader()
    analysis = CountingAnalysisProvider()
    service = DouyinWikiService(
        config,
        resolver=FakeResolver(),
        downloader=downloader,
        media=FakeMediaProcessor(),
        transcriber=FakeTranscriber(),
        ocr=FakeOCR(),
        analysis=analysis,
        embeddings=EmbeddingService(config.embeddings),
        reminders=fake_reminders,
    )
    service.initialize(initialize_git=False)
    first_job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="一")]
    )
    second_job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="二")]
    )
    first_claimed = service.database.claim_next_job(worker_id="worker-one")
    second_claimed = service.database.claim_next_job(worker_id="worker-two")
    assert first_claimed and first_claimed.id == first_job.id
    assert second_claimed and second_claimed.id == second_job.id

    first_task = asyncio.create_task(service.process_claimed_job(first_claimed))
    await downloader.started.wait()
    second_task = asyncio.create_task(service.process_claimed_job(second_claimed))
    ticks = 0
    for _ in range(20):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks > 0
    assert downloader.entered == 1
    downloader.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.status == JobStatus.COMPLETED
    assert second.status == JobStatus.COMPLETED
    assert downloader.calls == 1
    assert analysis.calls == 1
    entry = service.database.get_entry(first.result["entry_id"])
    assert {item.text for item in entry.inspirations} == {"一", "二"}
    assert any(job.result.get("duplicate") for job in (first, second))


@pytest.mark.asyncio
async def test_canceled_work_lock_waiter_does_not_leak_lock(service) -> None:
    work_id = "7672717300746907078"
    factory = service.vault.work_capture_locked
    holder = factory(work_id)
    await asyncio.to_thread(holder.__enter__)
    try:
        gated = GatedLockContext(factory(work_id))
        used = False

        def gated_factory(candidate_work_id: str):
            nonlocal used
            if not used:
                used = True
                return gated
            return factory(candidate_work_id)

        service.vault.work_capture_locked = gated_factory
        waiter_context = service._work_capture_locked(work_id)
        waiter = asyncio.create_task(waiter_context.__aenter__())
        assert await asyncio.to_thread(gated.started.wait, 1)
        ticks = 0
        for _ in range(20):
            await asyncio.sleep(0)
            ticks += 1
        assert ticks == 20
        assert not waiter.done()

        waiter.cancel()
        await asyncio.to_thread(holder.__exit__, None, None, None)
        assert await asyncio.to_thread(gated.acquired.wait, 1)
        gated.allow_return.set()
        assert await asyncio.to_thread(gated.returned.wait, 1)
        with pytest.raises(asyncio.CancelledError):
            await waiter

        third_context = service._work_capture_locked(work_id)
        third = asyncio.create_task(third_context.__aenter__())
        done, _ = await asyncio.wait({third}, timeout=1)
        third_acquired = third in done
        if not third_acquired and not gated.released.is_set():
            await asyncio.to_thread(gated.inner.__exit__, None, None, None)
            await third
        assert third_acquired
        await third_context.__aexit__(None, None, None)
    finally:
        if not waiter.done():
            waiter.cancel()


@pytest.mark.asyncio
async def test_favorite_keeps_media_and_unfavorite_restarts_retention_window(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]

    favorited = service.set_entry_favorite(entry_id, True)

    assert favorited["restore_job"] is None
    assert favorited["entry"].favorite is True
    assert favorited["entry"].retention == RetentionPolicy.KEEP
    assert favorited["entry"].media_expires_at is None
    assert service.database.entries_with_expired_media() == []
    source = service.config.vault_path / favorited["entry"].source_path
    assert "favorite: true" in source.read_text(encoding="utf-8")

    before = datetime.now(UTC) + timedelta(days=29, hours=23)
    unfavorited = service.set_entry_favorite(entry_id, False)

    assert unfavorited["restore_job"] is None
    assert unfavorited["entry"].favorite is False
    assert unfavorited["entry"].retention == RetentionPolicy.TEMPORARY
    assert unfavorited["entry"].media_expires_at is not None
    assert unfavorited["entry"].media_expires_at > before
    assert "favorite: false" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_favorite_restores_removed_media_without_reanalysis(service, monkeypatch) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    original_analysis = copy.deepcopy(service.database.get_entry_data(entry_id)["analysis"])
    assets = service.config.vault_path / "raw" / "assets" / entry.video_id
    shutil.rmtree(assets)
    service.database.mark_media_removed(entry_id)

    async def unexpected_transcription(*_args, **_kwargs):
        raise AssertionError("media restoration must not transcribe or analyze")

    monkeypatch.setattr(service.transcriber, "transcribe", unexpected_transcription)

    favorited = service.set_entry_favorite(entry_id, True)

    assert favorited["entry"].favorite is True
    assert favorited["entry"].retention == RetentionPolicy.KEEP
    assert favorited["restore_job"].kind == "media_restore"
    restored = await Worker(service).run_once()

    assert restored.status == JobStatus.COMPLETED
    assert restored.result == {"entry_id": entry_id, "media_restored": True}
    refreshed = service.database.get_entry(entry_id)
    assert refreshed.favorite is True
    assert refreshed.media_status == "present"
    assert refreshed.retention == RetentionPolicy.KEEP
    assert (assets / "original.mp4").is_file()
    assert service.downloader.calls == 2
    assert service.database.get_entry_data(entry_id)["analysis"] == original_analysis


@pytest.mark.asyncio
async def test_repeated_favorite_reuses_pending_media_restore_job(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    service.database.mark_media_removed(entry_id)

    first = service.set_entry_favorite(entry_id, True)
    second = service.set_entry_favorite(entry_id, True)

    assert second["restore_job"].id == first["restore_job"].id
    assert [job.kind for job in service.list_jobs() if job.kind == "media_restore"] == [
        "media_restore"
    ]


@pytest.mark.asyncio
async def test_unfavorite_skips_queued_media_restore(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    service.database.mark_media_removed(entry_id)
    service.set_entry_favorite(entry_id, True)

    service.set_entry_favorite(entry_id, False)
    skipped = await Worker(service).run_once()

    assert skipped.result["skipped"] is True
    assert service.downloader.calls == 1
    entry = service.database.get_entry(entry_id)
    assert entry.favorite is False
    assert entry.retention == RetentionPolicy.TEMPORARY


@pytest.mark.asyncio
async def test_media_restore_auth_failure_keeps_favorite_and_can_retry(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    service.database.mark_media_removed(entry_id)
    service.downloader = CookieExpiredDownloader()
    favorited = service.set_entry_favorite(entry_id, True)

    paused = await Worker(service).run_once()

    assert paused.id == favorited["restore_job"].id
    assert paused.status == JobStatus.NEEDS_AUTH
    assert paused.result["auth_scope"] == "video"
    entry = service.database.get_entry(entry_id)
    assert entry.favorite is True
    assert entry.retention == RetentionPolicy.KEEP
    assert service.retry_job(paused.id).status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_image_note_favorite_is_organizational_and_does_not_queue_video_restore(
    service,
) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"]).model_copy(
        update={"media_status": "removed"}
    )
    data = service.database.get_entry_data(entry.id)
    data["metadata"]["source_kind"] = "image_note"
    service.database.upsert_entry(entry, data)

    favorited = service.set_entry_favorite(entry.id, True)
    assert favorited["entry"].favorite is True
    assert favorited["entry"].retention == RetentionPolicy.KEEP
    assert favorited["restore_job"] is None

    unfavorited = service.set_entry_favorite(entry.id, False)
    assert unfavorited["entry"].favorite is False
    assert unfavorited["entry"].retention == RetentionPolicy.KEEP
    assert unfavorited["entry"].media_expires_at is None


@pytest.mark.asyncio
async def test_database_rebuild_preserves_favorite_state(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    favorited = service.set_entry_favorite(entry_id, True)["entry"]
    source = service.config.vault_path / favorited.source_path
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "media_retention: 永久保留", "media_retention: 临时保留"
        ),
        encoding="utf-8",
    )

    service.rebuild_database_from_vault(apply=True)

    rebuilt = service.database.get_entry(entry_id)
    assert rebuilt.favorite is True
    assert rebuilt.retention == RetentionPolicy.KEEP
    assert rebuilt.media_expires_at is None


@pytest.mark.asyncio
async def test_backfill_video_cover_for_legacy_entry(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    cover = service.config.vault_path / data.pop("cover_path")
    cover.unlink()
    service.database.upsert_entry(entry, data)
    service.vault.refresh_source(entry, data)

    report = service.backfill_video_covers()
    assert report["updated_entries"] == [entry.id]
    migrated = service.database.get_entry_data(entry.id)
    assert migrated["cover_path"] == f"raw/covers/{entry.video_id}.jpg"
    assert (service.config.vault_path / migrated["cover_path"]).is_file()
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    assert "![抖音视频封面]" in source


@pytest.mark.asyncio
async def test_remove_external_validation_migrates_legacy_storage(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    data = service.database.get_entry_data(entry_id)
    data["fact_checks"] = {"claim-1": {"verdict": "supported"}}
    data["analysis"]["claims"][0]["verification_status"] = "verified"
    data["analysis"]["risks"] = ["具体版本和参数仍处于外部未核验状态。"]
    service.database.upsert_entry(entry, data)

    with service.database.connect() as conn:
        conn.execute(
            "ALTER TABLE entries ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.execute(
            """CREATE TABLE fact_checks (
                claim_id TEXT NOT NULL,
                entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(entry_id, claim_id)
            )"""
        )
        conn.execute(
            "INSERT INTO fact_checks VALUES (?, ?, ?, ?)",
            ("claim-1", entry_id, "{}", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """UPDATE jobs SET status='completed_with_warnings',
               result_json=? WHERE id=?""",
            (
                '{"pending_fact_checks":["claim-1"],"warnings":["关键主张尚未核验"]}',
                completed.id,
            ),
        )

    report = service.remove_external_validation()
    assert report["removed_checks"] == 1
    migrated = service.database.get_entry_data(entry_id)
    assert "fact_checks" not in migrated
    assert "verification_status" not in migrated["analysis"]["claims"][0]
    assert migrated["analysis"]["risks"] == ["具体版本和参数需结合原始配方与实际冲煮进一步确认。"]
    assert service.get_job(completed.id).status == JobStatus.COMPLETED
    with service.database.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_checks'"
        ).fetchone()
    assert "verification_status" not in columns
    assert table is None


@pytest.mark.asyncio
async def test_low_confidence_does_not_pause_model_correction(service) -> None:
    service.transcriber = FakeTranscriber(low_confidence=True)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    assert completed.result["warnings"] == []
    assert service.database.get_review_issues(job.id) == []
    assert completed.artifacts["correction_notes"][0]["id"] == "asr-1"


@pytest.mark.asyncio
async def test_long_video_requires_confirmation(service) -> None:
    service.downloader = FakeDownloader(duration=31 * 60)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    service.approve_job(job.id)
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_two_hour_limit_can_be_overridden(service) -> None:
    service.downloader = FakeDownloader(duration=121 * 60)
    service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        options=CaptureOptions(approve_cloud_analysis=True),
    )
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    assert paused.result["reason"] == "video_too_long"
    service.approve_job(paused.id)
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_long_job_can_continue_from_checkpoint(service) -> None:
    service.downloader = FakeDownloader(duration=121 * 60)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    assert "metadata" in paused.artifacts
    retried = service.approve_job(job.id)
    assert retried.status == JobStatus.QUEUED
    assert retried.artifacts["metadata"] == paused.artifacts["metadata"]


@pytest.mark.asyncio
async def test_video_cookie_failure_enters_needs_auth_and_can_retry(service) -> None:
    launcher = InspectingAuthGuidanceLauncher(service.database)
    service.auth_guidance_launcher = launcher
    service.downloader = CookieExpiredDownloader()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.NEEDS_AUTH
    assert paused.error_code == "cookie_required"
    assert paused.result["auth_scope"] == "video"
    assert paused.result["next_command"] == "douyin-wiki auth video"
    assert paused.result["retry_command"].endswith(job.id)
    assert launcher.calls == [("video", job.id, JobStatus.NEEDS_AUTH, paused.result)]
    service.downloader = FakeDownloader()
    assert service.retry_job(job.id).status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_auth_guidance_launch_failure_preserves_paused_job(service) -> None:
    launcher = FailingAuthGuidanceLauncher()
    service.auth_guidance_launcher = launcher
    service.downloader = CookieExpiredDownloader()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")

    paused = await Worker(service).run_once()

    assert paused.status == JobStatus.NEEDS_AUTH
    assert paused.error_code == "cookie_required"
    assert paused.result["auth_scope"] == "video"
    assert paused.result["next_command"] == "douyin-wiki auth video"
    assert launcher.calls == 1
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT locked_at, lock_owner FROM jobs WHERE id=?", (job.id,)
        ).fetchone()
    assert row["locked_at"] is None
    assert row["lock_owner"] is None


@pytest.mark.asyncio
async def test_check_auth_scope_video_does_not_probe_other_adapters(service) -> None:
    video = ScopedAuthAdapter("video")
    image_note = ScopedAuthAdapter("image_note")
    creator = ScopedAuthAdapter("creator")
    service.downloader = video
    service.image_note_downloader = image_note
    service.creator_adapter = creator
    video_url = "https://www.douyin.com/video/7659645255277039717"

    result = await service.check_auth_scope("video", video_url=video_url)

    assert result.scope == "video"
    assert result.server_verified is True
    assert video.check_calls == [{"video_url": video_url}]
    assert image_note.check_calls == []
    assert creator.check_calls == []


@pytest.mark.asyncio
async def test_check_auth_scope_image_note_does_not_probe_other_adapters(service) -> None:
    video = ScopedAuthAdapter("video")
    image_note = ScopedAuthAdapter("image_note")
    creator = ScopedAuthAdapter("creator")
    service.downloader = video
    service.image_note_downloader = image_note
    service.creator_adapter = creator

    result = await service.check_auth_scope("image_note")

    assert result.scope == "image_note"
    assert image_note.check_calls == [{}]
    assert video.check_calls == []
    assert creator.check_calls == []


@pytest.mark.asyncio
async def test_duplicate_reacquires_removed_media_and_keeps_stable_page(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="初始灵感")])
    first = await Worker(service).run_once()
    entry = service.database.get_entry(first.result["entry_id"])
    service.database.mark_media_removed(entry.id)

    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="追加灵感")])
    second = await Worker(service).run_once()
    updated = service.database.get_entry(entry.id)
    assert second.result["reacquired"] is True
    assert service.downloader.calls == 2
    assert updated.media_status == "present"
    assert updated.raw_path == entry.raw_path
    assert updated.source_path == entry.source_path
    source = (service.config.vault_path / updated.source_path).read_text(encoding="utf-8")
    assert "初始灵感" in source and "追加灵感" in source


@pytest.mark.asyncio
async def test_agent_can_submit_reanalysis_with_provenance(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    data = service.database.get_entry_data(entry_id)
    analysis = data["analysis"]
    analysis["summary"] = "Agent 补充后的摘要。"
    analysis["title"] = "Agent 重分析标题"
    entry = service.submit_analysis(entry_id, analysis, producer="codex", model="agent-model")
    updated = service.database.get_entry_data(entry_id)
    assert entry.summary == "Agent 补充后的摘要。"
    assert updated["provider"] == "agent:codex"
    assert updated["model"] == "agent-model"
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    assert "Agent 补充后的摘要" in source


@pytest.mark.asyncio
async def test_contradiction_keeps_both_sources_and_creates_relation(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    data = service.database.get_entry_data(entry_id)

    target = entry.model_copy(
        update={
            "id": "dy-prior-video",
            "video_id": "prior-video",
            "title": "既有相反观点",
            "original_url": "https://www.douyin.com/video/prior-video",
            "canonical_url": "https://www.douyin.com/video/prior-video",
            "raw_path": "raw/prior.md",
            "source_path": "wiki/sources/prior.md",
        }
    )
    target_data = copy.deepcopy(data)
    target_data["analysis"]["title"] = target.title
    target_data["analysis"]["claims"][0]["id"] = "prior-claim"
    target_data["analysis"]["claims"][0]["text"] = "高质量信息源不需要提供一手证据"
    service.database.upsert_entry(target, target_data)
    service.indexer.index_entry(target, target_data)

    analysis = copy.deepcopy(data["analysis"])
    analysis["contradictions"] = [
        {
            "id": "conflict-1",
            "claim_id": "claim-1",
            "conflicts_with_entry_id": target.id,
            "conflicts_with_claim_id": "prior-claim",
            "reason": "两条主张对一手证据的必要性结论相反",
            "confidence": 0.96,
            "status": "open",
        }
    ]
    service.submit_analysis(entry.id, analysis, producer="codex")
    relations = service.database.get_relations(entry.id)
    assert any(item["relation_type"] == "contradiction" for item in relations)
    machine = (
        service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    ).read_text(encoding="utf-8")
    assert "contradictions:" in machine
    assert "既有相反观点" in machine
    assert "两条主张对一手证据的必要性结论相反" in machine


@pytest.mark.asyncio
async def test_maintenance_marks_expired_claim_stale(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    data["analysis"]["claims"][0]["valid_until"] = "2020-01-01T00:00:00+00:00"
    service.database.upsert_entry(entry, data)
    service.indexer.index_entry(entry, data)
    report = service.run_maintenance(apply=True)
    assert report["stale_chunks"] == 1
    updated = service.database.get_entry_data(entry.id)
    assert updated["analysis"]["claims"][0]["stale"] is True
    machine = (
        service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    ).read_text(encoding="utf-8")
    assert "stale: true" in machine


@pytest.mark.asyncio
async def test_maintenance_honors_review_after_without_double_counting(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    for field in ("claims", "knowledge_atoms"):
        data["analysis"][field][0]["review_after"] = "2020-01-01T00:00:00+00:00"
    service.database.upsert_entry(entry, data)
    report = service.run_maintenance(apply=True)
    assert report["stale_chunks"] == 1
    updated = service.database.get_entry_data(entry.id)["analysis"]
    assert updated["claims"][0]["stale"] is True
    assert updated["knowledge_atoms"][0]["stale"] is True


@pytest.mark.asyncio
async def test_confirm_reminder_requires_absolute_time(service, fake_reminders) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    with pytest.raises(JobStateError, match="confirmed=true"):
        service.confirm_reminder(
            entry_id,
            "reminder-1",
            due_at="2026-09-01T09:00:00+08:00",
        )
    result = service.confirm_reminder(
        entry_id,
        "reminder-1",
        due_at="2026-09-01T09:00:00+08:00",
        confirmed=True,
    )
    assert result["system_id"] == "system-reminder-1"
    assert fake_reminders.created
    repeated = service.confirm_reminder(entry_id, "reminder-1", confirmed=True)
    assert repeated["system_id"] == "system-reminder-1"
    assert repeated["candidate"]["due_at"] == "2026-09-01T09:00:00+08:00"
    assert len(fake_reminders.created) == 1
    service.rebuild_database_from_vault(apply=True)
    rebuilt = service.confirm_reminder(entry_id, "reminder-1", confirmed=True)
    assert rebuilt["system_id"] == "system-reminder-1"
    assert len(fake_reminders.created) == 1


@pytest.mark.asyncio
async def test_two_character_chinese_search_uses_lexical_index(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    data["analysis"]["one_liner"] = "今天教你苹果手机拍照技巧"
    service.database.upsert_entry(entry, data)
    service.indexer.index_entry(entry, data)

    assert service.search_knowledge("苹果")[0].entry_id == entry.id
    assert service.search_knowledge("拍照")[0].entry_id == entry.id
    assert service.search_knowledge("手机技巧")[0].entry_id == entry.id

    substring_rows = service.database.fts_search('("不匹配")', raw_query="手机拍照")
    assert substring_rows[0]["match_kind"] == "exact_substring"
    assert substring_rows[0]["rank"] is None
    assert substring_rows[0]["match_quality"] < 1


@pytest.mark.asyncio
async def test_rebuild_reports_bad_entry_without_clearing_database(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    machine.write_text("---\ntype: broken\n---\n", encoding="utf-8")

    report = service.rebuild_database_from_vault(apply=False)
    assert report["entry_errors"]
    with pytest.raises(JobStateError, match="entry_errors"):
        service.rebuild_database_from_vault(apply=True)
    assert service.database.get_entry(entry_id).id == entry_id


@pytest.mark.asyncio
async def test_rebuild_rejects_entry_machine_source_path_mismatch(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    source = service.config.vault_path / entry.source_path
    duplicate = service.config.vault_path / "wiki" / "sources" / "copied-source.md"
    duplicate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    machine.write_text(
        machine.read_text(encoding="utf-8").replace(
            f"source_page: {entry.source_path}", "source_page: wiki/sources/copied-source.md"
        ),
        encoding="utf-8",
    )

    report = service.rebuild_database_from_vault(apply=False)

    assert any(
        item["path"] == str(machine.relative_to(service.config.vault_path))
        for item in report["entry_errors"]
    )


@pytest.mark.asyncio
async def test_embedding_signature_change_rebuilds_all_cached_vectors(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    replacement = EmbeddingService(EmbeddingSettings(provider="hash", fallback_dimensions=32))
    service.embeddings = replacement
    service.indexer.embeddings = replacement
    service.searcher.embeddings = replacement
    assert service.search_knowledge("一手证据")
    rows = service.database.fetch_chunks(include_stale=True)
    assert rows
    assert {len(json.loads(row["embedding_json"])) for row in rows} == {32}
    assert service.database.get_index_metadata("embedding_signature").endswith("dim=32")


@pytest.mark.asyncio
async def test_maintenance_moves_expired_media_recoverably(service, monkeypatch, tmp_path) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    expired = entry.model_copy(update={"media_expires_at": datetime.now(UTC) - timedelta(days=1)})
    data = service.database.get_entry_data(entry.id)
    service.database.upsert_entry(expired, data)
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_send2trash(value: str) -> None:
        shutil.move(value, trash / Path(value).name)

    monkeypatch.setattr("douyin_wiki.service_maintenance.send2trash", fake_send2trash)
    report = service.run_maintenance(apply=True)
    assert entry.id in report["media_removed"]
    assert service.database.get_entry(entry.id).media_status == "removed"
    assert (trash / entry.video_id).exists()
    assert (service.config.vault_path / "raw" / "covers" / f"{entry.video_id}.jpg").is_file()


def test_worker_catches_up_missed_weekly_maintenance(service) -> None:
    worker = Worker(service)
    now = datetime(2026, 8, 18, 10, tzinfo=UTC)
    first = worker.run_due_maintenance(now)
    second = worker.run_due_maintenance(now)
    assert first is not None and first["dry_run"] is False
    assert second is None


def test_worker_detects_source_change_before_claiming_new_jobs(service) -> None:
    worker = Worker(
        service,
        loaded_signature="loaded-code",
        signature_provider=lambda: "updated-code",
    )
    assert worker.source_changed() is True


@pytest.mark.asyncio
async def test_worker_continues_when_catch_up_maintenance_fails(
    service, monkeypatch, capsys
) -> None:
    worker = Worker(
        service,
        loaded_signature="loaded-code",
        signature_provider=lambda: "updated-code",
    )

    def fail_maintenance():
        raise ValueError("broken maintenance")

    monkeypatch.setattr(worker, "run_due_maintenance", fail_maintenance)
    await worker.run_forever()

    captured = capsys.readouterr()
    assert "Worker 将继续处理采集任务" in captured.err


def test_embedding_model_failure_uses_deterministic_fallback(monkeypatch) -> None:
    class BrokenSentenceTransformer:
        def __init__(self, _: str) -> None:
            raise ValueError("broken model cache")

    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        BrokenSentenceTransformer,
    )
    embeddings = EmbeddingService(
        EmbeddingSettings(provider="sentence-transformers", fallback_dimensions=32)
    )

    first = embeddings.embed(["本地检索"])[0]
    second = embeddings.embed(["本地检索"])[0]

    assert first == second
    assert len(first) == 32
    assert embeddings.provider_name == "char-ngram-fallback"
    assert embeddings.last_error == "ValueError: broken model cache"


def test_loaded_embedding_model_failure_never_persists_fallback_vectors() -> None:
    class FlakyModel:
        calls = 0

        def get_sentence_embedding_dimension(self) -> int:
            return 3

        def encode(self, texts, **_):
            self.calls += 1
            if self.calls == 1:
                return [[1.0, 0.0, 0.0] for _ in texts]
            raise RuntimeError("temporary model failure")

    embeddings = EmbeddingService(EmbeddingSettings(provider="sentence-transformers"))
    embeddings._model = FlakyModel()
    embeddings._load_attempted = True
    embeddings._dimensions = 3
    embeddings.provider_name = "sentence-transformers:test"

    assert embeddings.embed(["first"], persistent=True) == [[1.0, 0.0, 0.0]]
    with pytest.raises(ExternalToolError, match="未写入不兼容向量"):
        embeddings.embed(["second"], persistent=True)
    assert embeddings.provider_name == "sentence-transformers:test"
    assert embeddings.embed(["query"]) == [[0.0, 0.0, 0.0]]


def test_provider_mode_allows_loopback_endpoint_without_api_key(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://localhost:11434/v1", model="local-model")
    )

    assert provider.configured is True
    assert provider.api_key == ""


def test_provider_mode_forwards_configured_loopback_api_key(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="local-model")
    )

    assert provider.configured is True
    assert provider.api_key == "omlx-local-key"


@pytest.mark.asyncio
async def test_long_transcript_uses_bounded_compact_model_requests(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="local-model")
    )
    requests: list[dict] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        requests.append(json.loads(user))
        result = {
            "segments": [
                {"id": item["id"], "text": item["text"]}
                for item in requests[-1]["segments"]
            ],
            "review_issues": [],
        }
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(
            id=index,
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text="这是一段字幕",
            confidence=0.9,
        )
        for index in range(161)
    ]
    ocr = [OCRObservation(timestamp_ms=500, text="画面文字", image_path="/private/frame.jpg")]
    corrected, _ = await provider.correct_transcript(segments, ocr)

    assert len(corrected) == 161
    assert [len(request["segments"]) for request in requests] == [80, 80, 1]
    assert set(requests[0]["segments"][0]) == {
        "id", "start_ms", "end_ms", "text", "context_before", "context_after"
    }
    assert requests[1]["segments"][0]["context_before"] == segments[79].text
    assert requests[1]["segments"][0]["context_after"] == segments[81].text
    assert set(requests[0]["ocr"][0]) == {"timestamp_ms", "image_index", "text", "confidence"}
    assert requests[0]["ocr"][0]["confidence"] is None


@pytest.mark.asyncio
async def test_correction_splits_length_failures_and_reuses_completed_checkpoints(
    monkeypatch,
) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    checkpoints: dict = {}
    calls: list[tuple[int, ...]] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        ids = tuple(item["id"] for item in json.loads(user)["segments"])
        calls.append(ids)
        if len(ids) > 2:
            raise ModelLimitError("模型输出达到 token 上限")
        result = {
            "segments": [
                {"id": item["id"], "text": item["text"]}
                for item in json.loads(user)["segments"]
            ],
            "review_issues": [],
        }
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(id=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text="一段字幕")
        for i in range(5)
    ]
    first, _ = await provider.correct_transcript(segments, [], checkpoints=checkpoints)
    assert len(first) == 5
    identifiers = calls[0]
    assert identifiers == (0, 1, 2, 3, 4)
    assert calls == [
        identifiers,
        identifiers[:2],
        (0, 1, 2),
        (0,),
        (0, 1),
    ]
    assert len(checkpoints) == 3
    calls.clear()
    second, _ = await provider.correct_transcript(segments, [], checkpoints=checkpoints)
    assert [item.text for item in second] == [item.text for item in first]
    assert calls == [identifiers, (0, 1, 2)]
    calls.clear()
    changed = [segments[0].model_copy(update={"text": "已更新的字幕"}), *segments[1:]]
    await provider.correct_transcript(changed, [], checkpoints=checkpoints)
    assert calls == [identifiers, (0, 1), (0, 1, 2)]


@pytest.mark.asyncio
async def test_single_segment_output_limit_splits_and_maps_review_to_whole_segment(
    monkeypatch,
) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    raw = "这是一段需要逐字校对的文本。" * 12
    segment = TranscriptSegment(id=7, start_ms=1000, end_ms=9000, text=raw)
    calls: list[tuple[int, str]] = []
    review_emitted = False

    async def fake_json_call(system, user, *, response_schema, response_validator):
        nonlocal review_emitted
        item = json.loads(user)["segments"][0]
        calls.append((item["id"], item["text"]))
        if len(item["text"]) > 80:
            raise ModelLimitError("模型输出达到 token 上限")
        issue = not review_emitted
        review_emitted = True
        updated_text = item["text"].replace("校对", "校订", 1) if issue else item["text"]
        result = {
            "segments": [{"id": item["id"], "text": updated_text}],
            "review_issues": [
                {"segment_id": item["id"], "reason": "专有名词存疑", "suggestions": ["替换词"]}
            ] if issue else [],
        }
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    corrected, issues = await provider.correct_transcript([segment], [])
    assert len(calls) > 1
    assert corrected[0].id == 7
    assert (corrected[0].start_ms, corrected[0].end_ms) == (1000, 9000)
    expected = raw.replace("校对", "校订", 1)
    assert corrected[0].text == expected
    assert issues == []



@pytest.mark.asyncio
async def test_provider_correction_state_covers_resource_wait_and_failure(
    service, monkeypatch
) -> None:
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    service.analysis = provider
    service.analysis_semaphore = asyncio.Semaphore(0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_correction(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise ModelLimitError("模型输出达到 token 上限")

    monkeypatch.setattr(provider, "correct_transcript", blocked_correction)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    task = asyncio.create_task(Worker(service).run_once())

    async def wait_for_waiting() -> None:
        for _ in range(100):
            if (service.database.get_job(job.id).artifacts.get("analysis_progress") or {}).get(
                "waiting_for_resource"
            ):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("correction resource wait did not appear")

    await wait_for_waiting()
    waiting = service.database.get_job(job.id)
    assert waiting.status == JobStatus.ANALYZING
    assert present_job(waiting, analysis_mode="provider")["stage_label"] == "LLM 校正"
    assert (
        present_job(waiting, analysis_mode="provider")["message_for_user"]
        == "等待模型校正资源。"
    )
    service.analysis_semaphore.release()
    await asyncio.wait_for(entered.wait(), 2)
    active = service.database.get_job(job.id)
    assert active.artifacts["analysis_progress"]["waiting_for_resource"] is False
    assert (
        present_job(active, analysis_mode="provider")["message_for_user"]
        == "后台模型正在校正逐字稿。"
    )
    release.set()
    failed = await asyncio.wait_for(task, 2)
    assert failed.status == JobStatus.FAILED
    assert present_job(failed, analysis_mode="provider")["stage_label"] != "LLM 校正"


@pytest.mark.asyncio
async def test_single_segment_input_budget_presplits_and_reuses_child_checkpoint(
    monkeypatch,
) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            max_output_tokens=256,
            context_window_tokens=4096,
        )
    )
    raw = "".join(f"{index:03d}长文本句子。" for index in range(100))
    segment = TranscriptSegment(id=3, start_ms=0, end_ms=10000, text=raw)
    checkpoints: dict = {}
    seen: list[tuple[str, ...]] = []
    fail_once = True

    async def fake_json_call(system, user, *, response_schema, response_validator):
        nonlocal fail_once
        items = json.loads(user)["segments"]
        seen.append(tuple(item["text"] for item in items))
        if len(seen) == 2 and fail_once:
            fail_once = False
            raise ModelServiceError("temporary")
        result = {
            "segments": [{"id": item["id"], "text": item["text"]} for item in items],
            "review_issues": [],
        }
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    with pytest.raises(ModelServiceError):
        await provider.correct_transcript([segment], [], checkpoints=checkpoints)
    first_success = seen[0]
    assert checkpoints
    seen.clear()
    corrected, _ = await provider.correct_transcript([segment], [], checkpoints=checkpoints)
    assert first_success not in seen
    assert corrected[0].text == raw


@pytest.mark.asyncio
async def test_single_segment_dynamic_split_reuses_completed_child(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    raw = "".join(f"第{index:02d}句需要校对。" for index in range(16))
    segment = TranscriptSegment(id=8, start_ms=200, end_ms=8200, text=raw)
    checkpoints: dict = {}
    seen: list[str] = []
    failed_right = False

    async def fake_json_call(system, user, *, response_schema, response_validator):
        nonlocal failed_right
        item = json.loads(user)["segments"][0]
        value = item["text"]
        seen.append(value)
        if len(value) > 90:
            raise ModelLimitError("模型输出达到 token 上限")
        if value.startswith("第08") and not failed_right:
            failed_right = True
            raise ModelServiceError("temporary")
        result = {"segments": [{"id": 0, "text": value}], "review_issues": []}
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    with pytest.raises(ModelServiceError):
        await provider.correct_transcript([segment], [], checkpoints=checkpoints)
    successful_child = next(
        value for value in seen if value.startswith("第00") and len(value) <= 90
    )
    assert len(checkpoints) == 1
    seen.clear()
    corrected, _ = await provider.correct_transcript([segment], [], checkpoints=checkpoints)
    assert successful_child not in seen
    assert corrected[0].text == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "duplicate"])
async def test_correction_recovers_incomplete_or_duplicate_piece_ids(monkeypatch, invalid) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )

    async def fake_json_call(system, user, *, response_schema, response_validator):
        item = json.loads(user)["segments"][0]
        returned = [] if invalid == "missing" else [
            {"id": item["id"], "text": "已校对"},
            {"id": item["id"], "text": "重复应忽略"},
        ]
        result = {"segments": returned, "review_issues": []}
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segment = TranscriptSegment(id=1, start_ms=0, end_ms=1000, text="一段字幕")
    corrected, issues = await provider.correct_transcript([segment], [])
    assert issues == []
    if invalid == "missing":
        assert corrected[0].text == "一段字幕"
    else:
        assert corrected[0].text == "已校对"


@pytest.mark.asyncio
async def test_correction_empty_input_and_minimum_piece_limit(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    assert await provider.correct_transcript([], []) == ([], [])
    calls = 0

    async def always_limited(system, user, *, response_schema, response_validator):
        nonlocal calls
        calls += 1
        raise ModelLimitError("模型输出达到 token 上限")

    monkeypatch.setattr(provider, "_json_call", always_limited)
    segment = TranscriptSegment(id=1, start_ms=0, end_ms=1000, text="abcdefghijklmnopqrstuvwx")
    with pytest.raises(ModelOutputError, match="最小文本块"):
        await provider.correct_transcript([segment], [])
    assert calls >= 2
    calls = 0
    tiny = TranscriptSegment(id=2, start_ms=0, end_ms=100, text="字")
    with pytest.raises(ModelOutputError, match="最小文本块"):
        await provider.correct_transcript([tiny], [])
    assert calls == 1



@pytest.mark.asyncio
async def test_correction_hard_splits_short_single_segment_before_skip(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    seen: list[str] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        item = json.loads(user)["segments"][0]
        seen.append(item["text"])
        # Fail until hard-split reaches single characters, then succeed.
        if len(item["text"]) > 1:
            raise ModelLimitError("模型输出达到 token 上限")
        result = {"segments": [{"id": 0, "text": item["text"]}], "review_issues": []}
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    # Below former min_chars*2=24 so punctuation-gated split would have given up.
    raw = "abcdefghijkl"  # 12 chars
    segment = TranscriptSegment(id=9, start_ms=0, end_ms=500, text=raw)
    corrected, issues = await provider.correct_transcript([segment], [])
    assert corrected[0].text == raw
    assert any(len(text) == 1 for text in seen)
    assert max(len(text) for text in seen) == len(raw)
    assert not any("跳过该块校正" in issue.reason for issue in issues)


@pytest.mark.asyncio
async def test_ocr_only_analysis_covers_all_images(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    seen_indices: list[int] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        seen_indices.extend(item["image_index"] for item in payload.get("ocr", []))
        return AnalysisResult(title="图文", one_liner="图文摘要").model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    ocr = [OCRObservation(image_index=i, text=f"图片 {i} " + "内容" * 100) for i in range(1, 31)]
    result = await provider.analyze([], ocr, [], {"source_kind": "image_note"})
    assert result.title == "图文"
    assert seen_indices == list(range(1, 31))


def test_adjacent_ocr_lines_are_deduplicated_only_in_model_input() -> None:
    source = [
        OCRObservation(timestamp_ms=1000, text="固定标题\n第一段"),
        OCRObservation(timestamp_ms=11000, text="固定标题\n第二段"),
    ]
    chunk = [TranscriptSegment(id=1, start_ms=0, end_ms=12000, text="旁白")]
    prepared = _nearby_ocr(chunk, source)
    assert [item.text for item in prepared] == ["固定标题\n第一段", "第二段"]
    assert [item.timestamp_ms for item in prepared] == [1000, 11000]
    assert source[1].text == "固定标题\n第二段"


@pytest.mark.asyncio
async def test_analysis_respects_reduced_context_budget(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            context_window_tokens=16000,
            max_output_tokens=512,
        )
    )
    requests: list[tuple[str, dict, dict]] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        requests.append((system, payload, response_schema))
        return AnalysisResult(title="预算测试", one_liner="摘要").model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(id=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text="字幕" * 100)
        for i in range(60)
    ]
    await provider.analyze(segments, [], [], {})
    parts = [payload for _, payload, _ in requests if "transcript" in payload]
    assert len(parts) > 1
    assert max(len(payload["transcript"]) for payload in parts) <= 20
    assert "".join(
        item["text"] for payload in parts for item in payload["transcript"]
    ) == "".join(segment.text for segment in segments)
    assert all(provider._fits(system, payload, schema) for system, payload, schema in requests)


@pytest.mark.asyncio
async def test_analysis_presplits_one_long_segment_and_preserves_coarse_citations(
    monkeypatch, service,
) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            context_window_tokens=16000,
            max_output_tokens=256,
        )
    )
    source = TranscriptSegment(id=37, start_ms=1000, end_ms=9000, text="甲乙丙丁。" * 40)
    seen: list[dict] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        if "partial_analyses" in payload:
            return AnalysisResult(title="汇总").model_dump(mode="json")
        seen.append(payload)
        piece = payload["transcript"][0]
        quote = piece["text"][:5]
        return AnalysisResult(
            title="分段",
            chapters=[
                {
                    "start_ms": 1000,
                    "end_ms": 9000,
                    "title": "原段",
                    "summary": "原段内容",
                    "evidence": [
                        {"timestamp_ms": 1000, "quote": quote, "evidence_type": "audio"}
                    ],
                }
            ],
            knowledge_atoms=[
                {"id": "local", "statement": piece["text"], "timestamp_ms": 1000,
                 "quote": quote, "provenance": "audio"}
            ],
        ).model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    result = await provider.analyze([source], [], [], {})
    sent = [item for request in seen for item in request["transcript"]]
    assert len(sent) > 1
    assert "".join(item["text"] for item in sent) == source.text
    assert all(item["start_ms"] == 1000 and item["end_ms"] == 9000 for item in sent)
    assert all(item["source_segment_id"] == 37 for item in sent)
    assert all(
        [item["id"] for item in request["transcript"]]
        == list(range(len(request["transcript"])))
        for request in seen
    )
    assert all(item.timestamp_ms == 1000 for item in result.chapters[0].evidence)
    assert {atom.statement for atom in result.knowledge_atoms} == {
        item["text"] for item in sent
    }
    service.validate_analysis_evidence(
        result,
        {
            "metadata": {"source_kind": "video", "duration_seconds": 10},
            "transcript_corrected": [source.model_dump(mode="json")],
            "ocr": [],
        },
    )


@pytest.mark.asyncio
async def test_analysis_presplits_single_segment_for_input_budget(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    source = TranscriptSegment(id=17, start_ms=200, end_ms=300, text="编号内容。" * 20)
    requests: list[dict] = []

    def tight_input_budget(system, payload, schema):
        return sum(len(item["text"]) for item in payload.get("transcript", [])) <= 70

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        if "transcript" in payload:
            requests.append(payload)
        return AnalysisResult(title="测试").model_dump(mode="json")

    monkeypatch.setattr(provider, "_fits", tight_input_budget)
    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    await provider.analyze([source], [], [], {})
    sent = [item for request in requests for item in request["transcript"]]
    assert len(sent) > 1
    assert "".join(item["text"] for item in sent) == source.text
    assert all(len(item["text"]) <= 70 for item in sent)
    assert all(item["source_segment_id"] == 17 for item in sent)


@pytest.mark.asyncio
async def test_analysis_splits_output_limit_and_reuses_child_checkpoints(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    source = TranscriptSegment(
        id=8, start_ms=0, end_ms=10000,
        text="".join(f"第{index}句话。" for index in range(20)),
    )
    checkpoints: dict = {}
    seen: list[str] = []
    progress: list[tuple[str, int, int]] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        if "partial_analyses" in payload:
            return AnalysisResult(title="汇总").model_dump(mode="json")
        text = payload["transcript"][0]["text"]
        seen.append(text)
        if len(text) > 50:
            raise ModelLimitError("模型输出达到 token 上限")
        return AnalysisResult(title="分段", knowledge_atoms=[
            {"id": "local", "statement": text, "quote": text[:5],
             "timestamp_ms": 0, "provenance": "audio"}
        ]).model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    first = await provider.analyze(
        [source], [], [], {}, checkpoints=checkpoints,
        on_progress=lambda *args: progress.append(args),
    )
    successful = [text for text in seen if len(text) <= 50]
    assert "".join(successful) == source.text
    assert "".join(atom.statement for atom in first.knowledge_atoms) == source.text
    assert progress[0] == ("analysis", 0, 1)
    analysis_progress = [item for item in progress if item[0] == "analysis"]
    assert any(total > 1 for _, _, total in analysis_progress)
    assert any(0 < completed < total for _, completed, total in analysis_progress)
    assert analysis_progress[-1][1] == analysis_progress[-1][2]
    assert any(len(text) > 50 for text in seen)
    failures = [text for text in seen if len(text) > 50]
    seen.clear()
    second = await provider.analyze([source], [], [], {}, checkpoints=checkpoints)
    assert second == first
    assert seen == failures


@pytest.mark.asyncio
async def test_ocr_only_analysis_reports_recursive_chunk_progress(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    progress: list[tuple[str, int, int]] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        if "partial_analyses" in payload:
            return AnalysisResult(title="汇总").model_dump(mode="json")
        if len(payload["ocr"]) > 1:
            raise ModelLimitError("模型输出达到 token 上限")
        return AnalysisResult(title="单图").model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    ocr = [OCRObservation(timestamp_ms=i * 1000, text=f"画面{i}") for i in range(2)]
    await provider.analyze([], ocr, [], {}, on_progress=lambda *args: progress.append(args))
    analysis_progress = [item for item in progress if item[0] == "analysis"]
    assert analysis_progress[0] == ("analysis", 0, 1)
    # Split counts the failed parent attempt and schedules two child batches.
    assert ("analysis", 1, 3) in analysis_progress
    assert ("analysis", 2, 3) in analysis_progress
    assert analysis_progress[-1] == ("analysis", 3, 3)


@pytest.mark.asyncio
async def test_analysis_reports_unsplittable_output_limit(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    source = TranscriptSegment(id=42, start_ms=100, end_ms=200, text="短句。" * 8)
    calls = 0

    async def always_limited(system, user, *, response_schema, response_validator):
        nonlocal calls
        calls += 1
        raise ModelOutputError("模型输出达到 token 上限")

    monkeypatch.setattr(provider, "_json_call", always_limited)
    with pytest.raises(ModelOutputError, match="最小文本块"):
        await provider.analyze([source], [], [], {})
    assert 2 <= calls < 20

    calls = 0
    tiny = TranscriptSegment(id=7, start_ms=0, end_ms=50, text="啊")
    with pytest.raises(ModelOutputError, match="最小文本块"):
        await provider.analyze([tiny], [], [], {})
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["context", "output", "server_json", "truncated_json"])
async def test_model_limits_do_not_retry_identical_request(monkeypatch, failure) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "context":
            return httpx.Response(400, text='{"error":"prompt too long"}')
        if failure == "server_json":
            return httpx.Response(500, text="Failed to extract valid JSON from output")
        if failure == "truncated_json":
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": "{"}}],
                    "usage": {"completion_tokens": 256},
                },
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            max_retries=2,
            max_output_tokens=256,
        )
    )
    with pytest.raises(ModelLimitError) as exc:
        await provider._json_call("system", "user", response_schema={"type": "object"})
    assert exc.value.code == (
        "model_context_limit" if failure == "context" else "model_output_limit"
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_model_502_has_specific_error_after_bounded_retries(monkeypatch) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="Bad Gateway")

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            max_retries=1,
        )
    )
    with pytest.raises(ModelServiceError) as exc:
        await provider._json_call("system", "user", response_schema={"type": "object"})
    assert exc.value.code == "model_unavailable"
    assert exc.value.details["status_code"] == 502
    assert exc.value.details["request_count"] == 2
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [(httpx.ConnectError, "model_connection_error"), (httpx.ReadTimeout, "model_timeout")],
)
async def test_model_connection_and_timeout_have_distinct_codes(
    monkeypatch,
    failure,
    expected_code,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise failure("request failed")

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            max_retries=0,
        )
    )
    with pytest.raises(ModelServiceError) as exc:
        await provider._json_call("system", "user", response_schema={"type": "object"})
    assert exc.value.code == expected_code


@pytest.mark.asyncio
async def test_long_analysis_uses_small_model_requests(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="local-model")
    )
    requests: list[dict] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        requests.append(payload)
        return AnalysisResult(title="测试", one_liner="测试摘要").model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(
            id=index,
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text="一段字幕",
        )
        for index in range(161)
    ]
    result = await provider.analyze(segments, [], [], {})

    assert result.title == "测试"
    assert [len(request["transcript"]) for request in requests[:-1]] == [80, 80, 1]
    assert len(requests[-1]["partial_analyses"]) == 3


@pytest.mark.asyncio
async def test_many_analysis_parts_merge_hierarchically_and_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    calls: list[dict] = []
    checkpoints: dict = {}

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        calls.append(payload)
        return AnalysisResult(
            title="测试",
            one_liner="摘要",
            knowledge_atoms=[
                {
                    "id": "inference",
                    "statement": "推断",
                    "provenance": "ai_inference",
                    "quote": None,
                }
            ],
        ).model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(id=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text="字幕")
        for i in range(321)
    ]
    first = await provider.analyze(segments, [], [], {}, checkpoints=checkpoints)
    assert first.title == "测试"
    assert len([x for x in calls if "transcript" in x]) == 5
    assert all(len(x["partial_analyses"]) <= 3 for x in calls if "partial_analyses" in x)
    assert len(checkpoints) == len(calls)
    calls.clear()
    second = await provider.analyze(segments, [], [], {}, checkpoints=checkpoints)
    assert second.title == first.title
    assert calls == []


def test_merge_keeps_timeline_and_claims_from_all_partials() -> None:
    partials = [
        AnalysisResult(
            title="测试",
            chapters=[
                {
                    "start_ms": index * 1000,
                    "end_ms": (index + 1) * 1000,
                    "title": f"主题 {index}",
                    "summary": f"内容 {index}",
                    "key_points": [f"要点 {index}"],
                    "evidence": [
                        {"timestamp_ms": index * 1000, "quote": f"原话 {index}"}
                    ],
                }
            ],
            knowledge_atoms=[
                {"id": "same-id", "statement": f"主张 {index}", "quote": f"原话 {index}"}
            ],
        ).model_dump(mode="json")
        for index in range(22)
    ]
    merged = _preserve_partial_coverage(AnalysisResult(title="总览"), partials)
    assert len(merged.chapters) == 11
    assert merged.chapters[0].start_ms == 0
    assert merged.chapters[-1].end_ms == 22000
    assert all(len(chapter.evidence) == 2 for chapter in merged.chapters)
    assert len(merged.knowledge_atoms) == 22
    assert len({atom.id for atom in merged.knowledge_atoms}) == 22


@pytest.mark.asyncio
async def test_merge_rejects_citations_absent_from_partial_results(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    checkpoints: dict = {}

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        quote = "虚构引文" if "partial_analyses" in payload else "真实引文"
        return AnalysisResult(
            title="测试",
            one_liner="摘要",
            chapters=[
                {
                    "start_ms": 0,
                    "title": "开始",
                    "summary": "摘要",
                    "evidence": [{"timestamp_ms": 0, "quote": quote, "evidence_type": "audio"}],
                }
            ],
        ).model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(id=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text="真实引文")
        for i in range(81)
    ]
    with pytest.raises(ValueError, match="汇总证据必须继承"):
        await provider.analyze(segments, [], [], {}, checkpoints=checkpoints)
    assert len(checkpoints) == 2


@pytest.mark.asyncio
async def test_transcript_correction_omits_already_resolved_and_noop_review_issues(
    monkeypatch,
) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="local-model")
    )

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        identifiers = [item["id"] for item in payload["segments"]]
        result = {
            "segments": [
                {"id": item["id"], "text": "请做一个落地页设计" if index == 0 else item["text"]}
                for index, item in enumerate(payload["segments"])
            ],
            "review_issues": [
                {"segment_id": identifiers[0], "reason": "同音字错误", "suggestions": ["落地页"]},
                {
                    "segment_id": identifiers[1],
                    "reason": "口语表达不清，保留原意",
                    "suggestions": ["再看看"],
                },
                {
                    "segment_id": identifiers[2],
                    "reason": "专有名词可能有误",
                    "suggestions": ["Claude"],
                },
            ],
        }
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = [
        TranscriptSegment(id=1, start_ms=0, end_ms=1000, text="请做一个落地液设计"),
        TranscriptSegment(id=2, start_ms=1000, end_ms=2000, text="再看看"),
        TranscriptSegment(id=3, start_ms=2000, end_ms=3000, text="Cloud"),
    ]
    corrected, issues = await provider.correct_transcript(segments, [])

    assert corrected[0].text == "请做一个落地页设计"
    assert issues == []


@pytest.mark.asyncio
async def test_provider_can_request_strict_json_schema_for_omlx(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"segments":[],"review_issues":[]}'}}]},
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
            response_format="json_schema",
            enable_thinking=False,
            thinking_budget=0,
            max_output_tokens=4096,
        )
    )

    schema = _analysis_response_schema()
    result = await provider._json_call("analyze work", "input", response_schema=schema)

    assert result == {"segments": [], "review_issues": []}
    assert requests[0].headers["authorization"] == "Bearer omlx-local-key"
    body = json.loads(requests[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["enable_thinking"] is False
    assert body["thinking_budget"] == 0
    assert body["max_tokens"] == 4096


def test_provider_rejects_json_array_when_object_is_required() -> None:
    with pytest.raises(ValueError, match="JSON 对象"):
        _parse_json_content("[]")


def test_analysis_response_schema_is_openai_strict_compatible() -> None:
    schema = _analysis_response_schema()
    assert schema.get("type") == "object"
    assert schema.get("additionalProperties") is False
    assert set(schema.get("required") or []) == set(schema.get("properties") or {})
    assert "content_card" in schema["properties"]
    assert "takeaways" in schema["properties"]
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)

    forbidden = {
        "const",
        "default",
        "discriminator",
        "title",
        "$defs",
        "definitions",
        "maxLength",
        "minLength",
        "maxItems",
        "minItems",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
    }
    content_card = schema["properties"]["content_card"]
    assert "anyOf" in content_card
    assert len(content_card["anyOf"]) >= 2
    assert "discriminator" not in content_card

    def walk(node: object, *, under_properties: bool = False) -> None:
        if isinstance(node, dict):
            assert "$ref" not in node
            # Property names may literally be "title"; only forbid schema keywords.
            if not under_properties:
                for key in forbidden:
                    assert key not in node, key
            if "properties" in node:
                assert set(node.get("required") or []) == set(node["properties"])
                assert node.get("additionalProperties") is False
            if "anyOf" in node:
                variants = node["anyOf"]
                assert not (
                    len(variants) == 2
                    and any(
                        isinstance(value, dict) and value.get("type") == "null"
                        for value in variants
                    )
                ), "nullable anyOf-of-2 must be collapsed to type:[T,null]"
            for key, value in node.items():
                walk(value, under_properties=(key == "properties"))
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    # Discriminator kind fields become enum, not const.
    kinds = [
        variant["properties"]["kind"]
        for variant in content_card["anyOf"]
        if isinstance(variant, dict) and "properties" in variant
    ]
    assert kinds and all("const" not in kind for kind in kinds)
    assert {"tutorial", "explanation", "other"} <= {
        (kind.get("enum") or [None])[0] for kind in kinds
    }


@pytest.mark.asyncio
async def test_json_schema_400_falls_back_to_json_object(monkeypatch) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                text='{"error":{"message":"Invalid parameter: response_format schema"}}',
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"segments":[],"review_issues":[]}'}}]},
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
            response_format="json_schema",
        )
    )

    result = await provider._json_call(
        "analyze work", "input", response_schema=_analysis_response_schema()
    )

    assert result == {"segments": [], "review_issues": []}
    assert len(requests) == 2
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[1]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_json_schema_400_falls_back_without_response_format_substring(
    monkeypatch,
) -> None:
    """Backends may reject strict schema without mentioning response_format."""
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400,
                text='{"error":{"message":"strict mode does not support this keyword"}}',
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"segments":[],"review_issues":[]}'}}]},
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
            response_format="json_schema",
        )
    )

    result = await provider._json_call(
        "analyze work", "input", response_schema=_analysis_response_schema()
    )

    assert result == {"segments": [], "review_issues": []}
    assert len(requests) == 2
    assert requests[1]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_json_schema_fallback_can_remove_unsupported_response_format(monkeypatch) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) < 3:
            return httpx.Response(400, text="unsupported response_format")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            response_format="json_schema",
            max_retries=0,
        )
    )
    result = await provider._json_call(
        "system", "user", response_schema=_analysis_response_schema()
    )
    assert result == {"ok": True}
    assert [body.get("response_format", {}).get("type") for body in requests] == [
        "json_schema",
        "json_object",
        None,
    ]


@pytest.mark.asyncio
async def test_invalid_analysis_result_is_repaired_within_model_retry(monkeypatch) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        result = {
            "analysis_version": 2,
            "title": "测试作品",
            "one_liner": "字" * (121 if len(requests) == 1 else 20),
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(result)}}]},
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model", max_retries=1)
    )
    result = await provider._json_call(
        "system",
        "user",
        response_schema=_analysis_response_schema(),
        response_validator=AnalysisResult.model_validate,
    )
    assert len(result["one_liner"]) == 20
    assert len(requests) == 2
    assert len(requests[1]["messages"]) == 3
    assert requests[1]["messages"][-1]["role"] == "user"
    assert "校验错误" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_json_schema_non_schema_400_does_not_fallback(monkeypatch) -> None:
    """Unrelated 400s (e.g. bad max_tokens) must not silently degrade format."""
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            400,
            text='{"error":{"message":"max_tokens is too large for this model"}}',
        )

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
            response_format="json_schema",
            max_retries=0,
        )
    )

    with pytest.raises(ExternalToolError):
        await provider._json_call(
            "analyze work", "input", response_schema=_analysis_response_schema()
        )

    assert len(requests) == 1
    assert requests[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_json_schema_empty_400_does_not_fallback(monkeypatch) -> None:
    """Blank 400 bodies are too ambiguous to treat as schema rejection."""
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(400, text="")

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "douyin_wiki.adapters.llm.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "omlx-local-key")
    provider = OpenAICompatibleProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
            response_format="json_schema",
            max_retries=0,
        )
    )

    with pytest.raises(ExternalToolError):
        await provider._json_call(
            "analyze work", "input", response_schema=_analysis_response_schema()
        )

    assert len(requests) == 1
    assert requests[0]["response_format"]["type"] == "json_schema"


def test_vault_rejects_internal_symlinks_on_write_and_scan(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    writer = VaultWriter(vault_root)
    writer.initialize(initialize_git=False)

    sources = vault_root / "wiki" / ".data" / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    real = sources / "111111111111.md"
    real.write_text("---\ntype: source\n---\n", encoding="utf-8")
    link = sources / "222222222222.md"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="符号链接"):
        writer._atomic_write(link, "hijack\n")

    with pytest.raises(ValueError, match="符号链接"):
        writer._safe_relative_path("wiki/.data/sources/222222222222.md", field="source_page")

    loaded = writer.load_entries()
    assert loaded == []
    assert any("符号链接" in item["error"] for item in writer.last_entry_load_errors)
