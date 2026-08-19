from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.adapters.llm import AnalysisProvider
from douyin_wiki.adapters.share import ResolvedShare
from douyin_wiki.config import AppConfig, EmbeddingSettings, LLMSettings, MediaSettings
from douyin_wiki.models import (
    AnalysisMode,
    AnalysisResult,
    Claim,
    Entity,
    InspirationInput,
    OCRObservation,
    ReminderCandidate,
    ReviewIssue,
    TranscriptSegment,
    VideoMetadata,
)
from douyin_wiki.service import DouyinWikiService


class FakeResolver:
    async def resolve(self, share_text: str) -> ResolvedShare:
        video_id = "7672717300746907078"
        return ResolvedShare(
            original_url="https://v.douyin.com/uvHsRpXIn8s/",
            canonical_url=f"https://www.douyin.com/video/{video_id}",
            video_id=video_id,
            redirect_chain=(
                "https://v.douyin.com/uvHsRpXIn8s/",
                f"https://www.douyin.com/video/{video_id}",
            ),
        )


class FakeDownloader:
    def __init__(self, duration: float = 90) -> None:
        self.duration = duration
        self.calls = 0

    async def download(self, url: str, video_id: str, target_dir: Path) -> VideoMetadata:
        self.calls += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        video = target_dir / "original.mp4"
        video.write_bytes(b"fake video")
        thumbnail = target_dir / "original.jpeg"
        thumbnail.write_bytes(b"fake thumbnail")
        return VideoMetadata(
            video_id=video_id,
            original_url=url,
            canonical_url=url,
            title="离职后如何筛选财经媒体",
            author="Nee霓公子",
            published_at=datetime(2026, 8, 18, tzinfo=UTC),
            duration_seconds=self.duration,
            description="财经媒体筛选方法",
            media_path=str(video),
            thumbnail_path=str(thumbnail),
        )


class FakeMediaProcessor:
    async def probe_duration(self, video_path: Path) -> float:
        return 90

    async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
        audio_path.write_bytes(b"fake audio")
        return audio_path

    async def extract_frames(
        self,
        video_path: Path,
        frames_dir: Path,
        *,
        duration_seconds: float,
        interval_seconds: int,
        scene_threshold: float,
        max_frames: int,
    ) -> list[tuple[int, Path]]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame = frames_dir / "frame-0000000000.jpg"
        frame.write_bytes(b"fake frame")
        return [(0, frame)]


class FakeTranscriber:
    def __init__(self, *, low_confidence: bool = False) -> None:
        self.low_confidence = low_confidence

    async def transcribe(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        return [
            TranscriptSegment(
                id=0,
                start_ms=0,
                end_ms=5000,
                text="离职以后我取关了很多财经媒体。",
                confidence=0.95,
                avg_logprob=-0.1,
            ),
            TranscriptSegment(
                id=1,
                start_ms=5000,
                end_ms=9000,
                text=(
                    "10号会打5折。" if self.low_confidence else "筛选信息源要看它能否提供一手证据。"
                ),
                confidence=0.2 if self.low_confidence else 0.94,
                avg_logprob=-1.5 if self.low_confidence else -0.2,
            ),
        ]


class FakeOCR:
    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        return [OCRObservation(timestamp_ms=0, text="财经媒体筛选", confidence=0.99)]


class FakeAnalysisProvider(AnalysisProvider):
    configured = True
    name = "fake"
    model = "fake-model"

    async def correct_transcript(
        self, segments: list[TranscriptSegment], ocr: list[OCRObservation]
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]:
        return segments, []

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict,
    ) -> AnalysisResult:
        source_quote = (
            segments[-1].text.rstrip("。") if segments else "筛选信息源要看它能否提供一手证据"
        )
        return AnalysisResult(
            title="离职后如何筛选财经媒体",
            summary="减少低价值财经媒体，优先保留能够提供一手证据的信息源。",
            core_points=["信息源质量比数量重要"],
            evidence=["作者说明自己主动取关大量财经媒体"],
            steps=["识别信息源", "检查是否提供一手证据"],
            applicable_scenarios=[inspiration.text for inspiration in inspirations],
            risks=["这是作者个人经验，不代表所有场景"],
            actions=["整理当前关注的信息源"],
            tags=["财经", "信息筛选"],
            concepts=["信息源筛选"],
            entities=[Entity(name="Nee霓公子", kind="creator")],
            claims=[
                Claim(
                    id="claim-1",
                    text="高质量信息源应提供一手证据",
                    source_quote=source_quote,
                    start_ms=5000,
                )
            ],
            reminders=[
                ReminderCandidate(
                    id="reminder-1",
                    title="检查财经信息源",
                    due_at=None,
                    reason="视频提出了可执行的整理动作",
                    source_quote=source_quote,
                    confidence=0.7,
                    needs_clarification=True,
                )
            ],
            ai_judgment="值得保存，适合用于建立个人信息源筛选标准。",
        )


class FakeReminderAdapter:
    def __init__(self) -> None:
        self.created = []

    def create(self, candidate: ReminderCandidate, *, source_url: str) -> str:
        self.created.append((candidate, source_url))
        return "system-reminder-1"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        vault_path=tmp_path / "vault",
        analysis_mode=AnalysisMode.PROVIDER,
        llm=LLMSettings(enabled=False),
        embeddings=EmbeddingSettings(provider="hash", fallback_dimensions=128),
        media=MediaSettings(
            retention_days=30,
            cloud_confirmation_minutes=30,
            max_duration_minutes=120,
            frame_interval_seconds=10,
            max_frames=5,
        ),
    )


@pytest.fixture
def fake_reminders() -> FakeReminderAdapter:
    return FakeReminderAdapter()


@pytest.fixture
def service(config: AppConfig, fake_reminders: FakeReminderAdapter) -> DouyinWikiService:
    instance = DouyinWikiService(
        config,
        resolver=FakeResolver(),
        downloader=FakeDownloader(),
        media=FakeMediaProcessor(),
        transcriber=FakeTranscriber(),
        ocr=FakeOCR(),
        analysis=FakeAnalysisProvider(),
        embeddings=EmbeddingService(config.embeddings),
        reminders=fake_reminders,
    )
    instance.initialize(initialize_git=False)
    return instance
