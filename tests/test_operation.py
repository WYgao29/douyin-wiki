from __future__ import annotations

from datetime import UTC, datetime, timedelta

from douyin_wiki.models import (
    CaptureRequest,
    JobRecord,
    JobStatus,
)
from douyin_wiki.operation import (
    looks_secret_key,
    message_for_user,
    present_job,
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
    assert presented["next_action"]["code"] == "wait_gateway"
    assert "正在自动" not in message


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


def test_worker_heartbeat_freshness_window() -> None:
    now = datetime.now(UTC)
    assert worker_is_fresh(now, now=now)
    assert not worker_is_fresh(now - timedelta(seconds=120), now=now)
    assert not worker_is_fresh(None, now=now)
