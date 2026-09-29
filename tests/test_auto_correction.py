import json

import pytest

from douyin_wiki.adapters.llm import OpenAICompatibleProvider
from douyin_wiki.config import LLMSettings
from douyin_wiki.models import AnalysisMode, JobStatus, ReviewIssue, TranscriptSegment
from douyin_wiki.operation import next_action_for, retryable
from douyin_wiki.worker import Worker


@pytest.mark.asyncio
async def test_split_correction_receives_bounded_adjacent_context(monkeypatch):
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test", max_output_tokens=256)
    )
    source = "".join(f"这是第{index}句的完整内容。" for index in range(30))
    offset = 0

    async def respond(system, user, *, response_schema, response_validator):
        nonlocal offset
        assert response_schema["required"] == ["segments"]
        payload = json.loads(user)
        for piece in payload["segments"]:
            assert piece["context_before"] == source[:offset][-40:]
            offset += len(piece["text"])
            assert piece["context_after"] == source[offset:][:40]
        result = {"segments": [{"id": p["id"], "text": p["text"]}
                               for p in payload["segments"]]}
        response_validator(result)
        return result

    monkeypatch.setattr(provider, "_json_call", respond)
    corrected, issues = await provider.correct_transcript(
        [TranscriptSegment(id=9, start_ms=0, end_ms=10000, text=source)], []
    )
    assert offset == len(source)
    assert corrected[0].text == source
    assert issues == []


@pytest.mark.asyncio
async def test_correction_edits_survive_vault_roundtrip(service, monkeypatch):
    calls = 0

    async def correct(segments, ocr):
        nonlocal calls
        calls += 1
        return [s.model_copy(update={"text": s.text.replace("离职以后", "离职之后")})
                for s in segments], []

    monkeypatch.setattr(service.analysis, "correct_transcript", correct)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    assert completed.result["warnings"] == []
    assert calls == 1
    data = service.database.get_entry_data(completed.result["entry_id"])
    assert data["transcript_edits"]
    entry, restored = service.vault.load_entries()[0]
    assert restored["transcript_edits"] == data["transcript_edits"]
    assert restored["transcript_raw"] == data["transcript_raw"]
    assert restored["transcript_corrected"] == data["transcript_corrected"]
    assert "模型校正" in (service.config.vault_path / entry.raw_path).read_text()
    assert service.database.get_review_issues(job.id) == []


@pytest.mark.asyncio
async def test_legacy_review_explicit_retry_archives_and_recorrects(service):
    service.config.analysis_mode = AnalysisMode.GATEWAY
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    await Worker(service).run_once()
    issue = ReviewIssue(id="old-0", start_ms=0, end_ms=1000,
                        raw_text="旧疑点", reason="旧规则")
    service.database.replace_review_issues(job.id, [issue])
    old = service.database.update_job(
        job.id, status=JobStatus.NEEDS_REVIEW,
        artifacts={"transcript_corrected": [{"id": 0, "text": "错误旧稿"}],
                   "analysis": {"title": "旧分析"}, "llm_checkpoints": {"old": {}}},
    )
    assert retryable(old)
    assert next_action_for(old, analysis_mode="gateway")["code"] == "retry"
    queued = service.retry_job(job.id)
    assert queued.status == JobStatus.QUEUED
    assert service.database.get_review_issues(job.id) == []
    archive = queued.artifacts["previous_review_attempts"][0]
    assert archive["review_issues"][0]["id"] == "old-0"
    assert archive["transcript_corrected"][0]["text"] == "错误旧稿"
    for key in ("transcript_corrected", "analysis", "llm_checkpoints"):
        assert key not in queued.artifacts
    waiting = await Worker(service).run_once()
    assert waiting.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert waiting.result["phase"] == "transcript_correction"
