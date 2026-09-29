import json

import pytest
from pydantic import ValidationError

from douyin_wiki.adapters.llm import ModelOutputError, OpenAICompatibleProvider
from douyin_wiki.config import LLMSettings
from douyin_wiki.models import AnalysisResult, JobStatus, OCRObservation, TranscriptCorrection
from douyin_wiki.worker import Worker
from tests.test_image_note import make_service


@pytest.mark.parametrize("text", [" ", "\n\t", "\u3000"])
def test_correction_rejects_whitespace(text):
    with pytest.raises(ValidationError, match="不能仅包含空白"):
        TranscriptCorrection(id=0, text=text)


@pytest.mark.asyncio
async def test_image_analysis_retries_only_unfinished_chunks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(enabled=True, base_url="http://127.0.0.1:8000/v1", model="test")
    )
    service.analysis = provider
    calls = []
    fail = True

    async def recognize(frames):
        return [OCRObservation(image_index=1, text=f"文字{i}", confidence=0.3)
                for i in range(21)]

    async def respond(system, user, **kwargs):
        payload = json.loads(user)
        texts = [item["text"] for item in payload.get("ocr", [])]
        for item in payload.get("ocr", []):
            assert item["confidence"] == 0.3
        calls.append(texts)
        if fail and "文字20" in texts:
            raise ModelOutputError("forced limit")
        return AnalysisResult(title="图文分析").model_dump(mode="json")

    monkeypatch.setattr(service.ocr, "recognize", recognize)
    monkeypatch.setattr(provider, "_json_call", respond)
    job = service.capture_douyin("https://v.douyin.com/oH4K0gee_Ok/")
    first = await Worker(service).run_once()
    assert first.status == JobStatus.FAILED
    assert len(first.artifacts["llm_checkpoints"]) == 1
    assert first.artifacts["analysis_progress"]["completed_chunks"] == 1
    first_chunk = calls[0]
    fail = False
    service.retry_job(job.id)
    done = await Worker(service).run_once()
    assert done.status == JobStatus.COMPLETED
    assert calls.count(first_chunk) == 1
    assert done.artifacts["llm_checkpoints"] == {}
    assert service.database.get_review_issues(job.id) == []
