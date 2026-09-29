import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from douyin_wiki.adapters.llm import ModelOutputError, OpenAICompatibleProvider
from douyin_wiki.errors import JobStateError
from douyin_wiki.models import AnalysisMode, JobStatus, TranscriptCorrection, TranscriptSegment
from douyin_wiki.webapp.app import create_app
from douyin_wiki.worker import Worker


def test_delayed_retry_does_not_clear_active_claim(service, monkeypatch):
    db = service.database
    job = service.capture_douyin("test")
    db.update_job(job.id, status=JobStatus.FAILED)
    ready, release = threading.Event(), threading.Event()
    original = db.update_job
    errors = []

    def delayed(*args, **kwargs):
        if threading.current_thread().name == "delayed-retry":
            ready.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    def slow():
        try:
            db.requeue_job(job.id)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(db, "update_job", delayed)
    thread = threading.Thread(target=slow, name="delayed-retry")
    thread.start()
    try:
        assert ready.wait(10)
        db.requeue_job(job.id)
        assert db.claim_next_job(worker_id="active-worker") is not None
        original(job.id, status=JobStatus.ANALYZING)
        before = db.get_job(job.id)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], JobStateError)
    assert db.get_job(job.id) == before
    with db.connect() as conn:
        owner = conn.execute(
            "SELECT lock_owner FROM jobs WHERE id=?", (job.id,)
        ).fetchone()[0]
        assert owner == "active-worker"


@pytest.mark.asyncio
async def test_analysis_limit_fails_without_publishing(service, monkeypatch):
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    service.config.llm.enabled = True
    service.config.llm.base_url = "http://127.0.0.1:8000/v1"
    service.config.llm.model = "test"
    provider = OpenAICompatibleProvider(service.config.llm)

    async def transcribe(*args):
        return [TranscriptSegment(id=0, start_ms=0, end_ms=1000, text="字")]

    async def correct(segments, *args, **kwargs):
        return segments, []

    async def limited(*args, **kwargs):
        raise ModelOutputError("forced limit")

    monkeypatch.setattr(provider, "correct_transcript", correct)
    monkeypatch.setattr(provider, "_analysis_call", limited)
    service.analysis = provider
    service.transcriber = SimpleNamespace(transcribe=transcribe)
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    done = await Worker(service).run_once()
    assert done.status == JobStatus.FAILED
    assert done.error_code == "model_output_limit"
    assert "entry_id" not in done.result
    assert "analysis" not in done.artifacts
    assert service.vault.load_entries() == []


def test_model_save_switches_provider_and_requests_reload(service, monkeypatch, tmp_path):
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    monkeypatch.setattr("douyin_wiki.webapp.app.get_secret", lambda _: "")
    service.config.analysis_mode = AnalysisMode.PROVIDER
    service.config.llm.enabled = True
    service.config.llm.base_url = "http://127.0.0.1:8000/v1"
    service.config.llm.model = "old-model"
    service.analysis = OpenAICompatibleProvider(service.config.llm)
    app = create_app(
        service.config, config_path=tmp_path / "settings.toml", service=service,
        start_watcher=False,
        chat_provider_factory=lambda c: SimpleNamespace(model=c.model, configured=True),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/settings/model", headers={"Origin": "http://testserver"},
            json={"base_url": "http://127.0.0.1:9000/v1", "model": "new-model"},
        )
    assert response.status_code == 200
    assert service.analysis.model == "new-model"
    assert service.analysis.settings.base_url == "http://127.0.0.1:9000/v1"
    assert response.json()["worker_reload"]
    assert Worker(service).reload_requested()


@pytest.mark.asyncio
async def test_gateway_correction_cannot_overwrite_analysis_phase(service):
    service.config.analysis_mode = AnalysisMode.GATEWAY
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    waiting = await Worker(service).run_once()
    corrections = [TranscriptCorrection(id=s["id"], text=s["text"])
                   for s in waiting.artifacts["transcript_raw"]]
    corrected = service.submit_transcript_correction(job.id, corrections, producer="test")
    with pytest.raises(JobStateError, match="分析阶段"):
        service.submit_transcript_correction(job.id, corrections, producer="late-agent")
    assert service.database.get_job(job.id) == corrected
    with pytest.raises(JobStateError, match="其他操作更新"):
        service.database.requeue_job(job.id, expected_updated_at=waiting.updated_at,
                                     artifacts={"analysis": {"title": "stale"}})
    assert service.database.get_job(job.id) == corrected


def test_gateway_empty_correction_only_allowed_for_empty_transcript(service):
    job = service.capture_douyin("test")
    service.database.update_job(
        job.id, status=JobStatus.AWAITING_AGENT_ANALYSIS,
        artifacts={"transcript_raw": [], "metadata": {"source_kind": "video"}},
        result={"phase": "transcript_correction"},
    )
    corrected = service.submit_transcript_correction(job.id, [], producer="test")
    assert corrected.artifacts["transcript_corrected"] == []
    assert corrected.result["phase"] == "analysis"
