import json
import sqlite3

import pytest

from douyin_wiki.adapters.llm import ModelOutputError, OpenAICompatibleProvider
from douyin_wiki.errors import JobLeaseLostError
from douyin_wiki.models import AnalysisResult, JobStatus, TranscriptSegment
from douyin_wiki.worker import Worker


@pytest.mark.asyncio
async def test_lost_lease_cannot_publish_entry(service, monkeypatch):
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    original = service.persist_entry_documents_and_bundle

    def steal(*args, **kwargs):
        with service.database.connect() as conn:
            conn.execute("UPDATE jobs SET lock_owner='replacement' WHERE id=?", (job.id,))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "persist_entry_documents_and_bundle", steal)
    with pytest.raises(JobLeaseLostError):
        await Worker(service).run_once()
    assert service.database.list_entries() == []
    assert service.vault.load_entries() == []


@pytest.mark.asyncio
async def test_publication_holds_database_writer_lock(service, monkeypatch):
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    original = service.write_entry_documents
    checked = False

    def publish(*args, **kwargs):
        nonlocal checked
        # Another connection cannot recover or claim the task after validation.
        conn = sqlite3.connect(service.database.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute("BEGIN IMMEDIATE")
        finally:
            conn.close()
        checked = True
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "write_entry_documents", publish)
    done = await Worker(service).run_once()
    assert checked
    assert done.status == JobStatus.COMPLETED
    assert len(service.database.list_entries()) == 1


@pytest.mark.asyncio
async def test_reanalysis_retry_reuses_successful_chunks(service, monkeypatch):
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    initial = await Worker(service).run_once()
    job = service.reanalyze_entry(initial.result["entry_id"], force=True)
    segments = [
        TranscriptSegment(id=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text="字")
        .model_dump(mode="json") for i in range(81)
    ]
    service.database.update_job(job.id, artifacts={"transcript_corrected": segments})
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    service.config.llm.enabled = True
    service.config.llm.base_url = "http://127.0.0.1:8000/v1"
    service.config.llm.model = "test"
    provider = OpenAICompatibleProvider(service.config.llm)
    calls = []
    fail = True

    async def respond(system, user, **kwargs):
        payload = json.loads(user)
        ids = [x["source_segment_id"] for x in payload.get("transcript", [])]
        calls.append(ids)
        if fail and 80 in ids:
            raise ModelOutputError("forced limit")
        return AnalysisResult(title="成功分析").model_dump(mode="json")

    monkeypatch.setattr(provider, "_json_call", respond)
    service.analysis = provider
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
    assert service.database.get_entry(initial.result["entry_id"]).title == "成功分析"
