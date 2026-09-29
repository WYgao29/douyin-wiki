from __future__ import annotations

from datetime import UTC, datetime, timedelta

from douyin_wiki.models import (
    CaptureRequest,
    JobRecord,
    JobStatus,
)
from douyin_wiki.operation import (
    job_display_title,
    job_is_in_progress,
    looks_secret_key,
    message_for_user,
    next_action_for,
    present_job,
    requires_user_action,
    sanitize_public_payload,
    stage_for_status,
    worker_is_fresh,
)


def _job(status: JobStatus, **kwargs) -> JobRecord:
    now = datetime.now(UTC)
    payload = {
        "id": "a" * 32,
        "kind": "capture",
        "status": status,
        "progress": 0.4,
        "request": CaptureRequest(share_text="https://v.douyin.com/abc/"),
        "artifacts": {},
        "result": {},
        "created_at": now,
        "updated_at": now,
    }
    payload.update(kwargs)
    return JobRecord.model_validate(payload)


def test_status_maps_to_stable_user_stages() -> None:
    assert stage_for_status(JobStatus.QUEUED) == "prepare"
    assert stage_for_status(JobStatus.INVENTORYING) == "read_source"
    assert stage_for_status(JobStatus.DOWNLOADING) == "download"
    assert stage_for_status(JobStatus.TRANSCRIBING) == "extract"
    assert stage_for_status(JobStatus.ANALYZING) == "analyze"
    assert stage_for_status(JobStatus.COMPLETED) == "done"


def test_gateway_waiting_is_not_described_as_automatic_analysis() -> None:
    job = _job(JobStatus.AWAITING_AGENT_ANALYSIS)
    message = message_for_user(job, analysis_mode="gateway")
    presented = present_job(job, analysis_mode="gateway")
    assert "后台不会自动完成" in message
    assert "网页不会自动分析" in message
    assert "外部 Agent" in message
    assert presented["next_action"]["code"] == "wait_gateway"
    assert "正在自动" not in message


def test_job_presentation_exposes_model_progress_and_stats() -> None:
    progress = {
        "phase": "analysis",
        "completed_chunks": 3,
        "total_chunks": 11,
        "last_response_at": "2026-09-27T00:00:00+00:00",
    }
    stats = {"successful_calls": 3, "retry_count": 1}
    job = _job(
        JobStatus.ANALYZING,
        artifacts={
            "analysis_progress": progress,
            "llm_stats": stats,
        },
    )
    presented = present_job(job, analysis_mode="provider")
    assert presented["analysis_progress"] == progress
    assert presented["llm_stats"] == stats
    assert "analysis_evidence_audit" not in presented


def test_sanitize_public_payload_drops_secret_fields_but_keeps_token_hint() -> None:
    payload = sanitize_public_payload(
        {
            "token_hint": "可能消耗 Token",
            "cookie": "secret-cookie",
            "api_key": "sk-test",
            "nested": {"password": "x", "ok": True},
        }
    )
    assert payload["token_hint"] == "可能消耗 Token"
    assert "cookie" not in payload
    assert "api_key" not in payload
    assert "password" not in payload["nested"]
    assert payload["nested"]["ok"] is True
    assert looks_secret_key("cookie")
    assert not looks_secret_key("token_hint")


def test_display_title_names_the_creator_or_explains_a_failed_link() -> None:
    named = _job(
        JobStatus.NEEDS_SELECTION,
        kind="creator_import",
        result={"creator_name": "叫我舒老师"},
    )
    failed = _job(JobStatus.FAILED, kind="creator_import", result={})
    favorites = _job(
        JobStatus.NEEDS_SELECTION,
        kind="favorites_import",
        result={"nickname": "小明"},
    )
    assert job_display_title(named) == "博主批量 · 叫我舒老师"
    assert job_display_title(failed) == "博主批量 · 链接未能识别博主"
    assert job_display_title(favorites) == "抖音收藏 · 小明"
    assert present_job(named, analysis_mode="provider")["display_title"] == "博主批量 · 叫我舒老师"


def test_display_title_uses_work_title_summary_or_share_caption() -> None:
    titled = _job(
        JobStatus.COMPLETED,
        artifacts={"metadata": {"title": "春季穿搭分享"}},
        result={"summary": "一条摘要"},
    )
    generic = _job(
        JobStatus.COMPLETED,
        artifacts={"metadata": {"title": "抖音视频"}},
        result={"summary": "真正的作品摘要"},
    )
    long_summary = "一二三四五六七八九十" * 4
    summarized = _job(JobStatus.COMPLETED, result={"summary": long_summary})
    caption = _job(
        JobStatus.QUEUED,
        request=CaptureRequest(share_text="春季穿搭\nhttps://v.douyin.com/abc/"),
    )
    bare = _job(JobStatus.QUEUED)
    assert job_display_title(titled) == "单条采集 · 春季穿搭分享"
    assert job_display_title(generic) == "单条采集 · 真正的作品摘要"
    subject = job_display_title(summarized).split(" · ", 1)[1]
    assert len(subject) == 36
    assert subject.endswith("…")
    assert job_display_title(caption) == "单条采集 · 春季穿搭"
    assert job_display_title(bare) == "单条采集"
    assert job_display_title(bare, hints={"entry_title": "资料标题"}) == "单条采集 · 资料标题"


def test_display_title_strips_hashtags_before_truncation() -> None:
    tagged = _job(
        JobStatus.COMPLETED,
        artifacts={"metadata": {"title": "春季穿搭分享 #ai新星计划 #穿搭"}},
    )
    assert job_display_title(tagged) == "单条采集 · 春季穿搭分享"
    raw = ("一二三四五六七八九十" * 2) + " #ai新星计划 " + ("二三四五六七八九" * 4)
    summarized = _job(JobStatus.COMPLETED, result={"summary": raw})
    subject = job_display_title(summarized).split(" · ", 1)[1]
    assert "#" not in subject
    assert "ai新星计划" not in subject
    assert len(subject) == 36
    assert subject.endswith("…")
    assert subject.startswith("一二三四五六七八九十")
    only_tags = _job(
        JobStatus.COMPLETED,
        artifacts={"metadata": {"title": "#ai新星计划 #穿搭"}},
        result={"summary": "真正的作品摘要"},
    )
    assert job_display_title(only_tags) == "单条采集 · 真正的作品摘要"
    caption = _job(
        JobStatus.QUEUED,
        request=CaptureRequest(share_text="#ai新星计划\n春季穿搭 #ootd\nhttps://v.douyin.com/abc/"),
    )
    assert job_display_title(caption) == "单条采集 · 春季穿搭"


def test_dismissed_failure_is_not_waiting_on_the_user() -> None:
    dismissed = _job(JobStatus.FAILED, artifacts={"user_dismissed": True})
    failed = _job(JobStatus.FAILED)
    selecting = _job(JobStatus.NEEDS_SELECTION, kind="creator_import")
    assert requires_user_action(dismissed) is False
    assert present_job(dismissed, analysis_mode="provider")["dismissed"] is True
    assert requires_user_action(failed) is True
    assert requires_user_action(selecting) is True


def test_worker_heartbeat_freshness_window() -> None:
    now = datetime.now(UTC)
    assert worker_is_fresh(now, now=now)
    assert not worker_is_fresh(now - timedelta(seconds=120), now=now)
    assert not worker_is_fresh(None, now=now)


def test_in_progress_is_machine_work_only() -> None:
    assert job_is_in_progress(_job(JobStatus.QUEUED))
    assert job_is_in_progress(_job(JobStatus.INVENTORYING))
    assert job_is_in_progress(_job(JobStatus.DOWNLOADING))
    assert job_is_in_progress(_job(JobStatus.ANALYZING))
    assert not job_is_in_progress(_job(JobStatus.NEEDS_SELECTION))
    assert not job_is_in_progress(_job(JobStatus.FAILED))
    assert not job_is_in_progress(_job(JobStatus.AWAITING_AGENT_ANALYSIS))
    assert not job_is_in_progress(_job(JobStatus.COMPLETED))
    assert not job_is_in_progress(_job(JobStatus.COMPLETED_WITH_WARNINGS))


def test_completed_parent_without_an_entry_points_at_children() -> None:
    parent = _job(
        JobStatus.COMPLETED_WITH_WARNINGS,
        result={"child_job_ids": ["b" * 32], "warnings": []},
    )
    action = next_action_for(parent, analysis_mode="provider")
    assert action["code"] == "view_children"
    assert action["label"] == "查看子任务"
    with_entry = _job(
        JobStatus.COMPLETED,
        result={"entry_id": "entry-1", "child_job_ids": ["b" * 32]},
    )
    assert next_action_for(with_entry, analysis_mode="provider")["code"] == "open_entry"


def test_failed_creator_message_names_the_next_step() -> None:
    job = _job(JobStatus.FAILED, kind="creator_import", error_message="无法解析")
    message = message_for_user(job, analysis_mode="provider")
    assert message.startswith("无法解析")
    assert "导入博主" in message
    assert "重新粘贴" in message
