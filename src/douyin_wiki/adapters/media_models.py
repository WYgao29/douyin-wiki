"""Optional local ASR and OCR engines and their selection rules."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Protocol

from ..config import MediaSettings
from ..errors import ExternalToolError
from ..models import OCRObservation, TranscriptSegment
from .media import VisionOCR, WhisperTranscriber

logger = logging.getLogger(__name__)
_SENSEVOICE_TAG = re.compile(r"<\|[^|>]+\|>")


class Transcriber(Protocol):
    async def transcribe(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]: ...


class OCREngine(Protocol):
    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]: ...


class BackendUnavailableError(ExternalToolError):
    """The selected engine cannot be initialized before processing input."""


class SenseVoiceTranscriber:
    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._vad = None
        self._asr = None

    def _ensure_ready(self) -> None:
        with self._lock:
            if self._vad is not None and self._asr is not None:
                return
            try:
                import soundfile as sf  # noqa: F401
                import torch
                from funasr import AutoModel
            except ImportError as exc:
                raise BackendUnavailableError(
                    "未安装 SenseVoice 依赖；运行 uv sync --extra asr"
                ) from exc
            device = self.settings.asr_device
            if device == "auto":
                device = "mps" if torch.backends.mps.is_available() else "cpu"
            if device == "mps" and not torch.backends.mps.is_available():
                raise BackendUnavailableError("当前设备不支持 MPS；请改用 asr_device=cpu")
            try:
                vad = AutoModel(
                    model=self.settings.vad_model,
                    model_revision=self.settings.vad_model_revision,
                    trust_remote_code=False,
                    device=device,
                    disable_update=True,
                )
                asr = AutoModel(
                    model=self.settings.asr_model,
                    model_revision=self.settings.asr_model_revision,
                    trust_remote_code=False,
                    device=device,
                    disable_update=True,
                )
            except Exception as exc:
                raise BackendUnavailableError(f"SenseVoice/VAD 模型无法加载：{exc}") from exc
            self._vad, self._asr = vad, asr

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready)

    async def transcribe(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        await self.ensure_ready()
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: Path) -> list[TranscriptSegment]:
        import soundfile as sf

        with self._lock:
            try:
                audio, sample_rate = sf.read(str(audio_path), dtype="float32")
                if audio.ndim != 1 or sample_rate != 16000:
                    raise ExternalToolError("SenseVoice 要求 16 kHz 单声道 WAV")
                duration_ms = round(len(audio) * 1000 / sample_rate)
                vad_result = self._vad.generate(input=str(audio_path))
                if not isinstance(vad_result, list) or not vad_result or not isinstance(
                    vad_result[0], dict
                ):
                    raise ExternalToolError("FSMN-VAD 返回格式错误")
                spans = vad_result[0].get("value")
                if not isinstance(spans, list):
                    raise ExternalToolError("FSMN-VAD 未返回分段列表")
                segments: list[TranscriptSegment] = []
                for span in spans:
                    if not isinstance(span, (list, tuple)) or len(span) != 2:
                        raise ExternalToolError("FSMN-VAD 返回无效时间区间")
                    start_ms = max(0, min(duration_ms, int(span[0])))
                    end_ms = max(0, min(duration_ms, int(span[1])))
                    if end_ms <= start_ms:
                        continue
                    start_sample = round(start_ms * sample_rate / 1000)
                    end_sample = round(end_ms * sample_rate / 1000)
                    result = self._asr.generate(
                        input=audio[start_sample:end_sample],
                        fs=sample_rate,
                        language="zh",
                        use_itn=True,
                        batch_size_s=60,
                    )
                    if (
                        not isinstance(result, list)
                        or not result
                        or not isinstance(result[0], dict)
                    ):
                        raise ExternalToolError("SenseVoice 返回格式错误")
                    text = _SENSEVOICE_TAG.sub("", str(result[0].get("text") or "")).strip()
                    if text:
                        segments.append(
                            TranscriptSegment(
                                id=len(segments), start_ms=start_ms, end_ms=end_ms, text=text
                            )
                        )
                return segments
            except ExternalToolError:
                raise
            except Exception as exc:
                raise ExternalToolError(f"SenseVoice 转录失败：{exc}") from exc


class RapidOCREngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine = None

    def _ensure_ready(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
            try:
                from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
            except ImportError as exc:
                raise BackendUnavailableError("未安装 RapidOCR；运行 uv sync --extra ocr") from exc
            params = {
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Det.model_type": ModelType.SMALL,
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.model_type": ModelType.SMALL,
                "Rec.ocr_version": OCRVersion.PPOCRV6,
                # CoreML emits model-shape errors on the verified macOS runtime.
                # ONNX Runtime CPU processes the same sample without those errors.
                "EngineConfig.onnxruntime.use_coreml": False,
            }
            try:
                self._engine = RapidOCR(params=params)
            except Exception as exc:
                raise BackendUnavailableError(f"RapidOCR small 模型无法加载：{exc}") from exc

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready)

    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        if not frames:
            return []
        await self.ensure_ready()
        return await asyncio.to_thread(self._recognize_sync, frames)

    def _recognize_sync(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        observations: list[OCRObservation] = []
        with self._lock:
            try:
                for source_index, path in frames:
                    result = self._engine(str(path))
                    texts = getattr(result, "txts", None)
                    scores = getattr(result, "scores", None)
                    lines = [
                        str(value).strip()
                        for value in (texts if texts is not None else [])
                        if str(value).strip()
                    ]
                    if not lines:
                        continue
                    confidence = None
                    if scores is not None:
                        values = [float(score) for score in scores]
                        if values and len(values) == len(texts) and all(
                            0 <= value <= 1 for value in values
                        ):
                            confidence = sum(values) / len(values)
                    observations.append(
                        OCRObservation(
                            timestamp_ms=source_index,
                            source_index=source_index,
                            text="\n".join(lines),
                            confidence=confidence,
                            image_path=str(path),
                        )
                    )
            except Exception as exc:
                raise ExternalToolError(f"RapidOCR 识别失败：{exc}") from exc
        return observations


class SelectedTranscriber:
    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings
        self.whisper = WhisperTranscriber(settings)
        self.sensevoice = SenseVoiceTranscriber(settings)
        self.provenance: dict[str, str] = {}

    async def transcribe(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        self.provenance = {}
        provider = self.settings.asr_provider
        if provider != "whisper":
            try:
                await self.sensevoice.ensure_ready()
            except BackendUnavailableError as exc:
                if provider != "auto":
                    raise
                logger.warning("SenseVoice unavailable; using Whisper: %s", exc)
                fallback_reason = str(exc)
            else:
                self.provenance = {"provider": "sensevoice", "model": self.settings.asr_model}
                return await self.sensevoice.transcribe(audio_path, output_dir)
        result = await self.whisper.transcribe(audio_path, output_dir)
        self.provenance = dict(self.whisper.provenance) or {
            "provider": "whisper", "model": self.settings.whisper_model
        }
        if provider == "auto" and "fallback_reason" not in self.provenance:
            self.provenance["fallback_reason"] = fallback_reason
        return result


class SelectedOCR:
    def __init__(self, settings: MediaSettings, vision_script: Path) -> None:
        self.settings = settings
        self.rapidocr = RapidOCREngine()
        self.vision = VisionOCR(vision_script)
        self.provenance: dict[str, str] = {}

    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        self.provenance = {}
        if not frames:
            return []
        provider = self.settings.ocr_provider
        if provider != "vision":
            try:
                await self.rapidocr.ensure_ready()
            except BackendUnavailableError as exc:
                if provider != "auto":
                    raise
                logger.warning("RapidOCR unavailable; using Vision: %s", exc)
                self.provenance = {
                    "provider": "vision", "model": "macOS Vision", "fallback_reason": str(exc)
                }
            else:
                self.provenance = {"provider": "rapidocr", "model": "PP-OCRv6 small"}
                return await self.rapidocr.recognize(frames)
        if not self.provenance:
            self.provenance = {"provider": "vision", "model": "macOS Vision"}
        return await self.vision.recognize(frames)
