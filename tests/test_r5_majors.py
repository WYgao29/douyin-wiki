"""R5 Major fixes: sticky analysis_candidate + correction segment ID remapping."""

from __future__ import annotations

import json

import pytest

from douyin_wiki.adapters.llm import (
    OpenAICompatibleProvider,
    _normalize_correction_segments,
)
from douyin_wiki.config import LLMSettings
from douyin_wiki.models import AnalysisResult, JobStatus, TranscriptSegment
from douyin_wiki.worker import Worker


def _segments(count: int) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            id=index,
            start_ms=index * 1000,
            end_ms=(index + 1) * 1000,
            text=f"原片段{index}",
        )
        for index in range(count)
    ]


def test_normalize_correction_accepts_local_ids():
    chunk = _segments(3)
    returned = [
        {"id": 1, "text": "改1"},
        {"id": 0, "text": "改0"},
        {"id": 2, "text": "改2"},
        {"id": 99, "text": "应忽略"},
    ]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "改0"},
        {"id": 1, "text": "改1"},
        {"id": 2, "text": "改2"},
    ]


def test_normalize_correction_remaps_one_based_ids_like_live_max_items():
    # Live failure shape: max_items=80 chunk, model returns 1..80 instead of 0..79.
    chunk = _segments(80)
    returned = [{"id": index + 1, "text": f"校对{index}"} for index in range(80)]
    normalized = _normalize_correction_segments(chunk, returned)
    assert [item["id"] for item in normalized] == list(range(80))
    assert normalized[0]["text"] == "校对0"
    assert normalized[79]["text"] == "校对79"


def test_normalize_correction_ignores_duplicate_and_fills_gaps():
    chunk = _segments(3)
    returned = [
        {"id": 0, "text": "首条"},
        {"id": 0, "text": "重复忽略"},
        {"id": 2, "text": "末条"},
    ]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "首条"},
        {"id": 1, "text": "原片段1"},
        {"id": 2, "text": "末条"},
    ]


def test_normalize_correction_positional_fallback_when_ids_unusable():
    chunk = _segments(2)
    returned = [
        {"id": 900, "text": "按序0"},
        {"id": 901, "text": "按序1"},
    ]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "按序0"},
        {"id": 1, "text": "按序1"},
    ]


def test_normalize_correction_keeps_originals_when_empty():
    chunk = _segments(2)
    assert _normalize_correction_segments(chunk, []) == [
        {"id": 0, "text": "原片段0"},
        {"id": 1, "text": "原片段1"},
    ]


def test_normalize_correction_rejects_non_list():
    with pytest.raises(ValueError, match="缺少 segments"):
        _normalize_correction_segments(_segments(1), {"segments": []})


def test_normalize_correction_full_zero_based_ids():
    chunk = _segments(4)
    returned = [{"id": index, "text": f"改{index}"} for index in range(4)]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "改0"},
        {"id": 1, "text": "改1"},
        {"id": 2, "text": "改2"},
        {"id": 3, "text": "改3"},
    ]


def test_normalize_correction_full_one_based_ids():
    chunk = _segments(4)
    returned = [{"id": index + 1, "text": f"校对{index}"} for index in range(4)]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "校对0"},
        {"id": 1, "text": "校对1"},
        {"id": 2, "text": "校对2"},
        {"id": 3, "text": "校对3"},
    ]


def test_normalize_correction_partial_without_zero_stays_local():
    # R6-1: missing 0 must NOT trigger 1-based remap (would shift FIXED onto index 0).
    chunk = _segments(5)
    returned = [{"id": 1, "text": "FIXED"}]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "原片段0"},
        {"id": 1, "text": "FIXED"},
        {"id": 2, "text": "原片段2"},
        {"id": 3, "text": "原片段3"},
        {"id": 4, "text": "原片段4"},
    ]


def test_normalize_correction_partial_subset_without_zero_stays_local():
    chunk = _segments(5)
    returned = [
        {"id": 1, "text": "改1"},
        {"id": 2, "text": "改2"},
        {"id": 3, "text": "改3"},
    ]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "原片段0"},
        {"id": 1, "text": "改1"},
        {"id": 2, "text": "改2"},
        {"id": 3, "text": "改3"},
        {"id": 4, "text": "原片段4"},
    ]


def test_normalize_correction_duplicates_and_unknown_ids():
    chunk = _segments(3)
    returned = [
        {"id": 1, "text": "首改"},
        {"id": 1, "text": "重复忽略"},
        {"id": 99, "text": "未知忽略"},
        {"id": -1, "text": "越界忽略"},
        {"id": 2, "text": "末改"},
    ]
    assert _normalize_correction_segments(chunk, returned) == [
        {"id": 0, "text": "原片段0"},
        {"id": 1, "text": "首改"},
        {"id": 2, "text": "末改"},
    ]


@pytest.mark.asyncio
async def test_correct_transcript_survives_one_based_chunk_ids(monkeypatch):
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    )
    seen_sizes: list[int] = []

    async def fake_json_call(system, user, *, response_schema, response_validator):
        payload = json.loads(user)
        size = len(payload["segments"])
        seen_sizes.append(size)
        # Emit 1-based ids — the live oMLX failure mode around max_items=80.
        result = {
            "segments": [
                {"id": item["id"] + 1, "text": f"校对后{item['id']}"}
                for item in payload["segments"]
            ]
        }
        response_validator(result)
        assert [row["id"] for row in result["segments"]] == list(range(size))
        return result

    monkeypatch.setattr(provider, "_json_call", fake_json_call)
    segments = _segments(85)
    corrected, issues = await provider.correct_transcript(segments, [])
    assert issues == []
    assert seen_sizes == [80, 5]
    # Model sees per-chunk local ids; first chunk 0..79, second chunk 0..4.
    assert [item.text for item in corrected[:80]] == [f"校对后{index}" for index in range(80)]
    assert [item.text for item in corrected[80:]] == [f"校对后{index}" for index in range(5)]


def test_failed_retry_clears_analysis_candidate(service):
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    candidate = AnalysisResult(
        title="未入库坏候选",
        summary="应在失败重试时被清除",
        core_points=["x"],
        evidence=["y"],
        steps=["z"],
        applicable_scenarios=[],
        risks=[],
        actions=[],
        tags=[],
        concepts=[],
        entities=[],
        claims=[],
        reminders=[],
        ai_judgment="bad",
    ).model_dump(mode="json")
    service.database.update_job(
        job.id,
        status=JobStatus.FAILED,
        error_code="external_tool_error",
        error_message="证据校验失败",
        artifacts={
            "analysis_candidate": candidate,
            "transcript_corrected": [
                {"id": 0, "start_ms": 0, "end_ms": 1000, "text": "保留校正稿"}
            ],
        },
    )
    queued = service.retry_job(job.id)
    assert queued.status == JobStatus.QUEUED
    assert "analysis_candidate" not in queued.artifacts
    assert queued.artifacts["transcript_corrected"][0]["text"] == "保留校正稿"


@pytest.mark.asyncio
async def test_failed_retry_reinvokes_analyze_after_clearing_candidate(service, monkeypatch):
    calls = {"analyze": 0}

    async def analyze(segments, ocr, inspirations, metadata, **kwargs):
        calls["analyze"] += 1
        if calls["analyze"] == 1:
            return AnalysisResult(
                title="sticky-candidate",
                summary="第一次分析候选",
                core_points=["a"],
                evidence=["b"],
                steps=["c"],
                applicable_scenarios=[],
                risks=[],
                actions=[],
                tags=["sticky"],
                concepts=[],
                entities=[],
                claims=[],
                reminders=[],
                ai_judgment="candidate",
            )
        return AnalysisResult(
            title="reanalyzed",
            summary="重试后重新分析",
            core_points=["新"],
            evidence=["新证据"],
            steps=["新步骤"],
            applicable_scenarios=[],
            risks=[],
            actions=[],
            tags=["fresh"],
            concepts=[],
            entities=[],
            claims=[],
            reminders=[],
            ai_judgment="fresh",
        )

    monkeypatch.setattr(service.analysis, "analyze", analyze)

    def boom(analysis, context, audit=None):
        if analysis.title == "sticky-candidate":
            raise ValueError("forced evidence failure")
        return analysis, 0

    monkeypatch.setattr(service, "prune_unverified_analysis_evidence", boom)
    monkeypatch.setattr(service, "validate_analysis_evidence", lambda *args, **kwargs: None)

    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    first = await Worker(service).run_once()
    assert first.status == JobStatus.FAILED
    assert "analysis_candidate" in first.artifacts
    assert first.artifacts["analysis_candidate"]["title"] == "sticky-candidate"
    assert calls["analyze"] == 1

    queued = service.retry_job(job.id)
    assert "analysis_candidate" not in queued.artifacts
    done = await Worker(service).run_once()
    assert done.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
    assert calls["analyze"] == 2
    assert done.artifacts["analysis"]["title"] == "reanalyzed"
    assert "analysis_candidate" not in done.artifacts or done.artifacts.get("analysis")
