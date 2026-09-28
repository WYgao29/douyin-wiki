from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from douyin_wiki.adapters.media_models import (
    BackendUnavailableError,
    RapidOCREngine,
    SelectedOCR,
    SelectedTranscriber,
    SenseVoiceTranscriber,
)
from douyin_wiki.config import MediaSettings
from douyin_wiki.errors import ExternalToolError
from douyin_wiki.models import TranscriptSegment
from douyin_wiki.review import detect_review_issues


def test_sensevoice_maps_vad_offsets_and_cleans_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(read=lambda *_args, **_kwargs: (np.zeros(32_000), 16_000)),
    )
    engine = SenseVoiceTranscriber(MediaSettings())
    engine._vad = SimpleNamespace(
        generate=lambda **_kwargs: [{"value": [[0, 900], [1000, 1900], [2000, 2200]]}]
    )
    engine._asr = SimpleNamespace(
        generate=lambda **_kwargs: [{"text": "<|zh|><|NEUTRAL|>这不对。"}]
    )
    result = engine._transcribe_sync(Path("unused.wav"))
    assert [(s.id, s.start_ms, s.end_ms, s.text) for s in result] == [
        (0, 0, 900, "这不对。"),
        (1, 1000, 1900, "这不对。"),
    ]
    assert all(s.confidence is None and s.avg_logprob is None for s in result)


def test_sensevoice_loads_models_without_remote_code(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_auto_model(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=fake_auto_model))
    engine = SenseVoiceTranscriber(MediaSettings(asr_device="cpu"))
    engine._ensure_ready()
    assert len(calls) == 2
    assert all(call["trust_remote_code"] is False for call in calls)
    assert calls[0]["model_revision"] == engine.settings.vad_model_revision
    assert calls[1]["model_revision"] == engine.settings.asr_model_revision


def test_rapidocr_keeps_frame_location_and_multiline_text(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    engine = RapidOCREngine()
    engine._engine = lambda _path: SimpleNamespace(txts=["第一行", "第二行"], scores=[0.8, 0.9])
    result = engine._recognize_sync([(5000, frame)])
    assert len(result) == 1
    assert result[0].text == "第一行\n第二行"
    assert result[0].source_index == result[0].timestamp_ms == 5000
    assert result[0].image_path == str(frame)
    assert result[0].confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_auto_asr_falls_back_only_before_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    selector = SelectedTranscriber(MediaSettings())

    async def unavailable() -> None:
        raise BackendUnavailableError("model unavailable")

    async def whisper(_audio: Path, _output: Path) -> list[TranscriptSegment]:
        return [TranscriptSegment(id=0, start_ms=0, end_ms=10, text="ok")]

    monkeypatch.setattr(selector.sensevoice, "ensure_ready", unavailable)
    monkeypatch.setattr(selector.whisper, "transcribe", whisper)
    assert (await selector.transcribe(Path("audio.wav"), Path("out")))[0].text == "ok"
    assert selector.provenance["provider"] == "whisper"
    assert "fallback_reason" in selector.provenance

    async def available() -> None:
        return None

    async def failed(_audio: Path, _output: Path) -> list[TranscriptSegment]:
        raise ExternalToolError("inference failed")

    monkeypatch.setattr(selector.sensevoice, "ensure_ready", available)
    monkeypatch.setattr(selector.sensevoice, "transcribe", failed)
    with pytest.raises(ExternalToolError, match="inference failed"):
        await selector.transcribe(Path("audio.wav"), Path("out"))


@pytest.mark.asyncio
async def test_auto_ocr_fallback_and_explicit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    selector = SelectedOCR(MediaSettings(), Path("vision.swift"))

    async def unavailable() -> None:
        raise BackendUnavailableError("model unavailable")

    async def vision(_frames):
        return []

    monkeypatch.setattr(selector.rapidocr, "ensure_ready", unavailable)
    monkeypatch.setattr(selector.vision, "recognize", vision)
    assert await selector.recognize([(1, Path("frame.jpg"))]) == []
    assert selector.provenance["provider"] == "vision"
    strict = SelectedOCR(MediaSettings(ocr_provider="rapidocr"), Path("vision.swift"))
    monkeypatch.setattr(strict.rapidocr, "ensure_ready", unavailable)
    with pytest.raises(BackendUnavailableError):
        await strict.recognize([(1, Path("frame.jpg"))])


@pytest.mark.parametrize("text", ["AI工具", "5折。", "1000元", "普通句子"])
def test_unknown_sensevoice_confidence_is_not_a_review_reason(text) -> None:
    segments = [TranscriptSegment(id=2, start_ms=0, end_ms=1000, text=text)]
    assert detect_review_issues(segments) == []

def test_selectors_construct_backends_lazily() -> None:
    asr = SelectedTranscriber(MediaSettings(asr_provider="whisper"))
    assert asr._whisper is None and asr._sensevoice is None
    _ = asr.whisper
    assert asr._whisper is not None and asr._sensevoice is None

    ocr = SelectedOCR(MediaSettings(ocr_provider="vision"), Path("vision.swift"))
    assert ocr._rapidocr is None and ocr._vision is None
    _ = ocr.vision
    assert ocr._vision is not None and ocr._rapidocr is None

