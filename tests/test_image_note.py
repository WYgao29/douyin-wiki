from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.adapters.image_note import (
    _best_image_url,
    _dedupe_image_urls,
    _dom_text_metadata,
    _find_aweme_detail,
    _metadata_from_aweme,
    _page_auth_blocked,
    _usable_auth_cookies,
)
from douyin_wiki.adapters.llm import AnalysisProvider
from douyin_wiki.adapters.share import ResolvedShare
from douyin_wiki.config import AppConfig, EmbeddingSettings, LLMSettings
from douyin_wiki.errors import BrowserAuthRequiredError, LivePhotoUnsupportedError
from douyin_wiki.models import (
    AnalysisMode,
    AnalysisResult,
    InspirationInput,
    JobStatus,
    KnowledgeAtom,
    OCRObservation,
    ReviewIssue,
    SourceKind,
    TranscriptSegment,
    VideoMetadata,
)
from douyin_wiki.service import DouyinWikiService
from douyin_wiki.worker import Worker

WORK_ID = "7674987897195870714"


def test_aweme_detail_must_match_requested_work() -> None:
    payload = {
        "recommendations": [
            {"aweme_id": "9999999999999999999", "images": [{"url_list": ["wrong"]}]}
        ],
        "aweme_detail": {"aweme_id": WORK_ID, "images": [{"url_list": ["right"]}]},
    }
    detail = _find_aweme_detail(payload, expected_work_id=WORK_ID)
    assert detail and detail["aweme_id"] == WORK_ID
    assert _find_aweme_detail(payload, expected_work_id="1111111111111111111") is None


class NoteResolver:
    async def resolve(self, share_text: str) -> ResolvedShare:
        return ResolvedShare(
            original_url="https://v.douyin.com/oH4K0gee_Ok/",
            canonical_url=f"https://www.douyin.com/note/{WORK_ID}",
            video_id=WORK_ID,
            redirect_chain=(
                "https://v.douyin.com/oH4K0gee_Ok/",
                f"https://www.douyin.com/note/{WORK_ID}",
            ),
            source_kind=SourceKind.IMAGE_NOTE,
        )


class NoteDownloader:
    def __init__(self, *, auth_error: bool = False) -> None:
        self.calls = 0
        self.auth_error = auth_error

    async def download(self, url: str, work_id: str, target_dir: Path) -> VideoMetadata:
        self.calls += 1
        if self.auth_error:
            raise BrowserAuthRequiredError("需要登录")
        target_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for index in range(1, 4):
            image = target_dir / f"{index:03d}.webp"
            image.write_bytes(f"image-{index}".encode())
            images.append(str(image))
        return VideoMetadata(
            video_id=work_id,
            original_url=url,
            canonical_url=url,
            title="本地模型量化怎么选",
            author="暖热",
            published_at=datetime(2026, 7, 2, tzinfo=UTC),
            description="FP16、Q8、Q4 怎么选？根据显存和质量需求选择。",
            post_text="FP16、Q8、Q4 怎么选？根据显存和质量需求选择。",
            source_kind=SourceKind.IMAGE_NOTE,
            image_paths=images,
            thumbnail_path=images[0],
            thumbnail_kind="image_note_first_image",
            music_metadata={"title": "背景音乐", "author": "作者"},
        )


class BlockingNoteDownloader(NoteDownloader):
    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, url: str, work_id: str, target_dir: Path) -> VideoMetadata:
        self.entered += 1
        if self.entered == 1:
            self.started.set()
            await self.release.wait()
        return await super().download(url, work_id, target_dir)


class InspectingAuthGuidanceLauncher:
    def __init__(self, database) -> None:
        self.database = database
        self.calls: list[tuple[str, str, JobStatus]] = []

    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        persisted = self.database.get_job(trigger_job_id)
        self.calls.append((scope, trigger_job_id, persisted.status))
        return True


class NoteOCR:
    def __init__(self, *, low_confidence: bool = False) -> None:
        self.low_confidence = low_confidence

    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        return [
            OCRObservation(
                timestamp_ms=index,
                text=("Q4 需要 8GB 显存" if index == 2 else f"第 {index} 页说明"),
                confidence=0.3 if self.low_confidence and index == 2 else 0.99,
                image_path=str(path),
            )
            for index, path in frames
        ]


class VideoOnlyAdapterMustNotRun:
    def __getattr__(self, name: str):
        raise AssertionError(f"image-note flow invoked video adapter: {name}")


class NoteAnalysis(AnalysisProvider):
    configured = True
    name = "fake-note"
    model = "fake-note-model"

    async def correct_transcript(
        self, segments: list[TranscriptSegment], ocr: list[OCRObservation]
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]:
        raise AssertionError("image-note flow must not correct a transcript")

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict,
    ) -> AnalysisResult:
        assert segments == []
        assert metadata["source_kind"] == "image_note"
        assert "image_paths" not in metadata and "thumbnail_path" not in metadata
        assert metadata["images"] == [
            {"image_index": 1},
            {"image_index": 2},
            {"image_index": 3},
        ]
        second_page = next(item.text for item in ocr if item.image_index == 2)
        return AnalysisResult(
            title="本地模型量化怎么选",
            one_liner="按显存、速度和质量需求在 FP16、Q8、Q4 之间选择。",
            relevance_to_inspiration="与本地模型部署灵感直接相关。",
            takeaways=["FP16 质量高", "Q8 更均衡", "Q4 更省显存"],
            content_type="explanation",
            content_card={
                "kind": "explanation",
                "question": "量化格式怎么选",
                "concepts": ["FP16", "Q8", "Q4"],
                "mechanism": ["位宽越低通常越省显存"],
                "examples": [],
            },
            knowledge_atoms=[
                KnowledgeAtom(
                    id="format-choice",
                    statement="Q4 更适合显存紧张的本地部署",
                    atom_type="recommendation",
                    provenance="image_ocr",
                    image_index=2,
                    quote=second_page,
                )
            ],
            tags=["本地模型", "量化"],
        )


class CountingNoteAnalysis(NoteAnalysis):
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict,
    ) -> AnalysisResult:
        self.calls += 1
        return await super().analyze(segments, ocr, inspirations, metadata)


def make_service(
    tmp_path: Path,
    *,
    mode: AnalysisMode = AnalysisMode.PROVIDER,
    note_downloader: NoteDownloader | None = None,
    ocr: NoteOCR | None = None,
) -> DouyinWikiService:
    config = AppConfig(
        vault_path=tmp_path / "vault",
        analysis_mode=mode,
        llm=LLMSettings(enabled=False),
        embeddings=EmbeddingSettings(provider="hash", fallback_dimensions=64),
    )
    service = DouyinWikiService(
        config,
        resolver=NoteResolver(),
        downloader=VideoOnlyAdapterMustNotRun(),
        image_note_downloader=note_downloader or NoteDownloader(),
        media=VideoOnlyAdapterMustNotRun(),
        transcriber=VideoOnlyAdapterMustNotRun(),
        ocr=ocr or NoteOCR(),
        analysis=NoteAnalysis(),
        embeddings=EmbeddingService(config.embeddings),
    )
    service.initialize(initialize_git=False)
    return service


@pytest.mark.asyncio
async def test_image_note_pipeline_skips_video_tools_and_writes_ordered_images(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    service.capture_douyin(
        "3.05 复制打开抖音 https://v.douyin.com/oH4K0gee_Ok/",
        [InspirationInput(text="本地模型量化选择")],
    )
    completed = await Worker(service).run_once()

    assert completed.status == JobStatus.COMPLETED
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    assert entry.retention.value == "keep"
    assert "transcript_raw" not in data
    assert "transcript_corrected" not in data
    assert [item["image_index"] for item in data["ocr"]] == [1, 2, 3]
    assert data["metadata"]["music_metadata"]["title"] == "背景音乐"

    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    raw = (service.config.vault_path / entry.raw_path).read_text(encoding="utf-8")
    assert source.count("![抖音图文第 1 张]") == 1
    assert source.count("![抖音图文第 2 张]") == 1
    assert source.count("![抖音图文第 3 张]") == 1
    assert source.index("抖音图文第 1 张") < source.index("抖音图文第 2 张")
    assert "第 2 张" in source and "[00:00]" not in source
    assert "打开原作品" in source
    assert "原始逐字稿" not in raw and "校正逐字稿" not in raw
    assert "逐图 OCR" in raw
    assert "raw/images/" in (service.config.vault_path / ".gitignore").read_text()

    evidence = service.search_knowledge("显存紧张的本地部署")
    assert evidence
    assert any(item.image_index == 2 and item.timestamp_ms is None for item in evidence)
    service.run_maintenance(apply=True)
    assert all(
        (service.config.vault_path / path).is_file() for path in data["metadata"]["image_paths"]
    )


@pytest.mark.asyncio
async def test_image_note_gateway_skips_transcript_correction(tmp_path: Path) -> None:
    service = make_service(tmp_path, mode=AnalysisMode.GATEWAY)
    job = service.capture_douyin(
        "https://v.douyin.com/oH4K0gee_Ok/",
        [InspirationInput(text="本地模型量化选择")],
    )
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert paused.result["phase"] == "analysis"
    context = service.get_analysis_context(job.id)
    assert context["metadata"]["source_kind"] == "image_note"
    assert context["transcript_raw"] == []

    service.submit_gateway_analysis(
        job.id,
        {
            "title": "图文分析",
            "one_liner": "图文总结",
            "takeaways": ["FP16", "Q8", "Q4"],
            "content_type": "other",
            "content_card": {"kind": "other", "notes": ["量化选择"]},
            "knowledge_atoms": [
                {
                    "id": "q4",
                    "statement": "Q4 更省显存",
                    "provenance": "image_ocr",
                    "image_index": 2,
                }
            ],
        },
        producer="hermes",
    )
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_duplicate_note_reuses_images_but_reacquires_missing_file(tmp_path: Path) -> None:
    downloader = NoteDownloader()
    service = make_service(tmp_path, note_downloader=downloader)
    service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/", [InspirationInput(text="灵感一")])
    first = await Worker(service).run_once()
    assert first.status == JobStatus.COMPLETED

    service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/", [InspirationInput(text="灵感二")])
    duplicate = await Worker(service).run_once()
    assert duplicate.result["duplicate"] is True
    assert downloader.calls == 1

    entry = service.database.get_entry(first.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    (service.config.vault_path / data["metadata"]["image_paths"][1]).unlink()
    service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/")
    reacquired = await Worker(service).run_once()
    assert reacquired.result["reacquired"] is True
    assert downloader.calls == 2
    assert [item.text for item in service.database.get_entry(entry.id).inspirations] == [
        "灵感一",
        "灵感二",
    ]


@pytest.mark.asyncio
async def test_concurrent_first_capture_serializes_image_note_pipeline_and_merges_inspirations(
    tmp_path: Path,
) -> None:
    downloader = BlockingNoteDownloader()
    service = make_service(tmp_path, note_downloader=downloader)
    analysis = CountingNoteAnalysis()
    service.analysis = analysis
    first_job = service.capture_douyin(
        "https://v.douyin.com/oH4K0gee_Ok/", [InspirationInput(text="一")]
    )
    second_job = service.capture_douyin(
        "https://v.douyin.com/oH4K0gee_Ok/", [InspirationInput(text="二")]
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
async def test_image_note_auth_and_low_confidence_review(tmp_path: Path) -> None:
    auth_downloader = NoteDownloader(auth_error=True)
    service = make_service(tmp_path / "auth", note_downloader=auth_downloader)
    launcher = InspectingAuthGuidanceLauncher(service.database)
    service.auth_guidance_launcher = launcher
    job = service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.NEEDS_AUTH
    assert paused.result["next_command"] == "douyin-wiki auth douyin"
    assert launcher.calls == [("image_note", job.id, JobStatus.NEEDS_AUTH)]
    auth_downloader.auth_error = False
    assert service.retry_job(job.id).status == JobStatus.QUEUED
    assert (await Worker(service).run_once()).status == JobStatus.COMPLETED

    review_service = make_service(tmp_path / "review", ocr=NoteOCR(low_confidence=True))
    review_job = review_service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/")
    review = await Worker(review_service).run_once()
    assert review.status == JobStatus.NEEDS_REVIEW
    issue = review_service.get_job(review_job.id).result["review_issues"][0]
    assert issue["image_index"] == 2
    review_service.resolve_review(review_job.id, {issue["id"]: "Q4 需要 6GB 显存"})
    completed = await Worker(review_service).run_once()
    assert completed.status == JobStatus.COMPLETED
    data = review_service.database.get_entry_data(completed.result["entry_id"])
    assert data["ocr"][1]["text"] == "Q4 需要 6GB 显存"


def test_image_note_auth_check_rejects_expired_cookie_and_challenge() -> None:
    cookies = [
        {
            "name": "sessionid",
            "domain": ".douyin.com",
            "expires": 1_800_000_000,
            "value": "must-not-be-returned",
        },
        {
            "name": "sid_guard",
            "domain": ".douyin.com",
            "expires": 1,
            "value": "expired",
        },
    ]
    usable = _usable_auth_cookies(cookies, now=1_700_000_000)
    assert [cookie["name"] for cookie in usable] == ["sessionid"]
    assert _usable_auth_cookies(cookies, now=1_900_000_000) == []
    assert _page_auth_blocked("请登录后查看", 200) is True
    assert _page_auth_blocked("抖音首页", 403) is True
    assert _page_auth_blocked("抖音首页", 200) is False
    assert _page_auth_blocked("正文内容" * 300 + "请登录支持我们的活动", 200) is False
    assert _page_auth_blocked("普通页面", 200, "https://passport.douyin.com/login") is True


def test_structured_note_parser_preserves_order_music_and_rejects_live_photo() -> None:
    detail = {
        "aweme_id": WORK_ID,
        "desc": "量化格式说明",
        "author": {"nickname": "暖热"},
        "create_time": 1782921600,
        "images": [
            {"url_list": ["https://a.example/1.webp?x=1"]},
            {"url_list": ["https://a.example/2.webp?x=1"]},
        ],
        "music": {"title": "音乐", "author": "作者", "play_url": "not-stored"},
    }
    metadata, urls = _metadata_from_aweme(
        detail, url=f"https://www.douyin.com/note/{WORK_ID}", work_id=WORK_ID
    )
    assert urls == [
        "https://a.example/1.webp?x=1",
        "https://a.example/2.webp?x=1",
    ]
    assert metadata.music_metadata == {"title": "音乐", "author": "作者"}
    title, post_text, author, published_at = _dom_text_metadata(
        "下载本地模型FP16、Q8、Q4怎么选？正文被截断 - 暖热于20260817发布在抖音，已经收获4个喜欢",
        [
            "下载本地模型FP16、Q8、Q4怎么选？",
            "下载本地模型FP16、Q8、Q4怎么选？这里是完整正文和参数说明。",
        ],
        "错误的推荐账号",
    )
    assert title == "下载本地模型FP16、Q8、Q4怎么选？"
    assert post_text.endswith("这里是完整正文和参数说明。")
    assert author == "暖热"
    assert published_at and published_at.date().isoformat() == "2026-08-17"
    assert (
        _best_image_url(
            {
                "thumbnail": {
                    "width": 300,
                    "height": 400,
                    "url_list": ["https://a.example/thumb.webp"],
                },
                "display_image": {
                    "width": 1200,
                    "height": 1600,
                    "url_list": ["https://a.example/original.webp"],
                },
            }
        )
        == "https://a.example/original.webp"
    )
    assert _dedupe_image_urls(
        [
            "https://a.example/1.webp?signature=old",
            "https://a.example/1.webp?signature=fresh",
            "https://a.example/2.webp?signature=fresh",
        ]
    ) == [
        "https://a.example/1.webp?signature=old",
        "https://a.example/2.webp?signature=fresh",
    ]

    detail["images"][0]["live_photo"] = {"video_url": "https://a.example/live.mp4"}
    with pytest.raises(LivePhotoUnsupportedError):
        _metadata_from_aweme(detail, url=f"https://www.douyin.com/note/{WORK_ID}", work_id=WORK_ID)
