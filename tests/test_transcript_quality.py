from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from douyin_wiki import mcp_server
from douyin_wiki.mcp_server import mcp
from douyin_wiki.models import AnalysisMode, JobStatus, ReviewIssue, TranscriptSegment
from douyin_wiki.review import detect_review_issues, transcript_confidence_info
from douyin_wiki.webapp.app import create_app
from douyin_wiki.worker import Worker


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [AnalysisMode.PROVIDER, AnalysisMode.GATEWAY])
@pytest.mark.parametrize("has_issue", [False, True])
async def test_model_correction_notes_do_not_block(
    service, monkeypatch, mode, has_issue
):
    source = TranscriptSegment(id=0, start_ms=0, end_ms=9000, text="AI课程售价2000元。")
    issue = ReviewIssue(
        id="price-conflict",
        start_ms=0,
        end_ms=9000,
        raw_text=source.text,
        reason="语音识别为2000元，对应字幕为200元，请回听核对。",
        suggestions=["AI课程售价200元。", "AI课程售价2000元。"],
    )

    async def transcribe(*_args):
        return [source]

    async def correct(*_args):
        return [source], [issue] if has_issue else []

    service.transcriber = SimpleNamespace(
        transcribe=transcribe, provenance={"provider": "sensevoice", "model": "test"}
    )
    service.config.analysis_mode = mode
    monkeypatch.setattr(service.analysis, "correct_transcript", correct)
    monkeypatch.setattr(mcp_server, "_SERVICE", service)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    outcome = await Worker(service).run_once()
    if mode == AnalysisMode.GATEWAY:
        assert outcome.status == JobStatus.AWAITING_AGENT_ANALYSIS
        context = mcp_server.get_analysis_context(job.id)
        assert context["media_provenance"]["asr"]["confidence_status"] == "unavailable"
        await mcp.call_tool(
            "submit_transcript_correction",
            {
                "job_id": job.id,
                "corrections": [{"id": 0, "text": source.text}],
                "review_issues": [issue.model_dump(mode="json")] if has_issue else [],
                "producer": "test-agent",
            },
        )
        outcome = service.get_job(job.id)
        await mcp.call_tool(
            "submit_gateway_analysis",
            {"job_id": job.id, "analysis": {"title": "课程介绍"}, "producer": "test-agent"},
        )
        outcome = await Worker(service).run_once()

    assert outcome.status == JobStatus.COMPLETED
    assert service.database.get_review_issues(job.id) == []
    assert [item["id"] for item in outcome.artifacts["correction_notes"]] == (
        [issue.id] if has_issue else []
    )
    info = outcome.artifacts["media_provenance"]["asr"]
    assert info["confidence_status"] == "unavailable"
    assert "不代表识别质量低" in info["confidence_note"]
    assert outcome.artifacts["transcript_raw"][0]["confidence"] is None
    assert outcome.result["warnings"] == []
    assert service.database.get_entry(outcome.result["entry_id"])
    # The same availability message reaches the Web API and MCP payload.
    app = create_app(service.config, service=service, start_watcher=False)
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["status"] == outcome.status.value
    assert detail["media_provenance"]["asr"] == info
    assert mcp_server.get_job(job.id)["artifacts"]["media_provenance"]["asr"] == info


@pytest.mark.parametrize("scores", [{"confidence": 0.2}, {"avg_logprob": -1.5}])
def test_actual_low_scores_still_require_review(scores):
    segment = TranscriptSegment(id=0, start_ms=0, end_ms=1000, text="售价2000元。", **scores)
    issues = detect_review_issues([segment])
    assert len(issues) == 1
    assert "低置信转录" in issues[0].reason
    assert transcript_confidence_info([segment])["confidence_status"] == "available"


def test_partial_and_empty_score_availability():
    unknown = TranscriptSegment(id=0, start_ms=0, end_ms=1000, text="AI")
    scored = unknown.model_copy(update={"id": 1, "avg_logprob": -0.2})
    assert transcript_confidence_info([]) == {}
    assert transcript_confidence_info([unknown, scored])["confidence_status"] == "partial"
