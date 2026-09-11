"""User-facing job, auth, and system presentation for the Web primary entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .localization import ANALYSIS_MODE_LABELS, JOB_KIND_LABELS, JOB_STATUS_LABELS, label_status
from .models import AnalysisMode, JobRecord, JobStatus
from .time_utils import beijing_iso, format_beijing

USER_STAGES: dict[str, str] = {
    "prepare": "准备",
    "read_source": "读取来源",
    "download": "下载内容",
    "extract": "本地提取",
    "analyze": "AI 整理",
    "write": "写入知识库",
    "done": "完成",
}

STATUS_TO_STAGE: dict[JobStatus, str] = {
    JobStatus.QUEUED: "prepare",
    JobStatus.RESOLVING: "read_source",
    JobStatus.INVENTORYING: "read_source",
    JobStatus.NEEDS_SELECTION: "read_source",
    JobStatus.DISPATCHING: "prepare",
    JobStatus.MONITORING: "download",
    JobStatus.DOWNLOADING: "download",
    JobStatus.EXTRACTING: "extract",
    JobStatus.TRANSCRIBING: "extract",
    JobStatus.AWAITING_AGENT_ANALYSIS: "analyze",
    JobStatus.ANALYZING: "analyze",
    JobStatus.NEEDS_AUTH: "download",
    JobStatus.NEEDS_REVIEW: "analyze",
    JobStatus.WAITING_CONFIRMATION: "download",
    JobStatus.COMPLETED: "done",
    JobStatus.COMPLETED_WITH_WARNINGS: "done",
    JobStatus.FAILED: "done",
}

USER_ACTION_STATUSES = {
    JobStatus.NEEDS_AUTH,
    JobStatus.NEEDS_REVIEW,
    JobStatus.WAITING_CONFIRMATION,
    JobStatus.NEEDS_SELECTION,
}

RUNNING_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RESOLVING,
    JobStatus.INVENTORYING,
    JobStatus.DISPATCHING,
    JobStatus.MONITORING,
    JobStatus.DOWNLOADING,
    JobStatus.EXTRACTING,
    JobStatus.TRANSCRIBING,
    JobStatus.ANALYZING,
}

TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.COMPLETED_WITH_WARNINGS,
    JobStatus.FAILED,
}

AUTH_USER_STATES: dict[str, tuple[str, str]] = {
    "missing": ("unauthorized", "未授权"),
    "available": ("unverified", "可能有效但未完成服务器验证"),
    "unverified": ("unverified", "可能有效但未完成服务器验证"),
    "ready": ("authorized", "已授权"),
    "expired": ("expired", "授权已过期"),
    "needs_login": ("needs_login", "需要重新登录"),
    "unavailable": ("check_failed", "检查失败"),
    "error": ("check_failed", "检查失败"),
    "authorizing": ("authorizing", "授权中"),
}

CHANNEL_LABELS = {
    "video": "视频下载授权",
    "douyin": "抖音账号授权",
}

CHANNEL_PURPOSES = {
    "video": "访问抖音视频并由 yt-dlp 下载原片。使用本机浏览器的登录状态，与专用浏览器无关。",
    "douyin": (
        "收藏清点、博主主页清点和静态图文采集。"
        "使用抖库专用浏览器配置，登录成功不代表视频下载已授权。"
    ),
}

ANALYSIS_MODE_WEB_COPY = {
    "provider": "后台模型接口自动整理",
    "local": "本地模式，不调用外部模型",
    "gateway": "等待外部 Agent 接续",
}

AUTH_SESSION_STAGES = {
    "queued": "准备开始",
    "launching": "正在打开浏览器",
    "waiting_login": "请在本机浏览器完成登录",
    "verifying": "正在验证授权",
    "succeeded": "授权成功",
    "cancelled": "已取消",
    "failed": "授权失败",
    "timeout": "授权超时",
    "profile_locked": "专用浏览器正被占用",
    "account_changed": "登录账号已变化",
}

SECRET_KEY_NAMES = {
    "cookie",
    "cookies",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "secret",
    "access_token",
    "refresh_token",
    "local_storage",
    "localstorage",
    "authorization",
}


def stage_for_status(status: JobStatus, *, auth_scope: str | None = None) -> str:
    if status == JobStatus.NEEDS_AUTH and auth_scope in {"creator", "favorites", "image_note"}:
        return "read_source"
    if status == JobStatus.COMPLETED_WITH_WARNINGS:
        return "done"
    return STATUS_TO_STAGE.get(status, "prepare")


def parent_job_id(job: JobRecord) -> str | None:
    artifacts = job.artifacts or {}
    for key in ("creator_context", "favorites_context"):
        value = artifacts.get(key) or {}
        parent = value.get("parent_job_id")
        if parent:
            return str(parent)
    return None


def child_job_ids(job: JobRecord) -> list[str]:
    raw = job.result.get("child_job_ids") or []
    return [str(item) for item in raw if item]


def requires_user_action(job: JobRecord) -> bool:
    if job.status in USER_ACTION_STATUSES:
        return True
    return job.status == JobStatus.FAILED


def retryable(job: JobRecord) -> bool:
    if job.kind == "media_restore":
        return job.status in {JobStatus.FAILED, JobStatus.NEEDS_AUTH}
    return job.status in {JobStatus.FAILED, JobStatus.NEEDS_AUTH}


def _auth_scope(job: JobRecord) -> str | None:
    return job.result.get("auth_scope") or job.artifacts.get("auth_scope")


def _entry_id(job: JobRecord) -> str | None:
    return job.result.get("entry_id") or job.artifacts.get("reanalyze_entry_id")


def next_action_for(
    job: JobRecord,
    *,
    analysis_mode: str,
) -> dict[str, Any] | None:
    scope = _auth_scope(job)
    if job.status == JobStatus.NEEDS_AUTH:
        channel = "video" if scope == "video" else "douyin"
        label = "立即授权视频下载" if channel == "video" else "立即授权抖音账号"
        return {
            "code": f"authorize_{channel}",
            "label": label,
            "href": "/settings/auth",
            "channel": channel,
        }
    if job.status == JobStatus.WAITING_CONFIRMATION:
        return {
            "code": "approve_long_video",
            "label": "批准继续处理长视频",
            "href": f"/jobs/{job.id}",
            "endpoint": f"/api/jobs/{job.id}/approve",
        }
    if job.status == JobStatus.NEEDS_REVIEW:
        return {
            "code": "review",
            "label": "处理人工复核疑点",
            "href": f"/jobs/{job.id}",
            "endpoint": f"/api/jobs/{job.id}/review",
        }
    if job.status == JobStatus.NEEDS_SELECTION:
        href = (
            f"/imports/favorites?job={job.id}"
            if job.kind == "favorites_import"
            else f"/imports/creators?job={job.id}"
        )
        return {
            "code": "select_works",
            "label": "选择并确认导入作品",
            "href": href,
        }
    if job.status == JobStatus.FAILED:
        return {
            "code": "retry",
            "label": "重试失败任务",
            "href": f"/jobs/{job.id}",
            "endpoint": f"/api/jobs/{job.id}/retry",
        }
    if job.status == JobStatus.AWAITING_AGENT_ANALYSIS:
        if analysis_mode == AnalysisMode.GATEWAY.value:
            return {
                "code": "wait_gateway",
                "label": "等待外部 Agent 接续",
                "href": f"/jobs/{job.id}",
            }
        return {
            "code": "wait_worker",
            "label": "等待后台继续整理",
            "href": f"/jobs/{job.id}",
        }
    entry_id = _entry_id(job)
    if job.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS} and entry_id:
        return {
            "code": "open_entry",
            "label": "打开知识资料",
            "href": f"/articles/{entry_id}",
        }
    if job.status in RUNNING_STATUSES:
        return {
            "code": "wait_worker",
            "label": "查看任务进度",
            "href": f"/jobs/{job.id}",
        }
    return None


def message_for_user(
    job: JobRecord,
    *,
    analysis_mode: str,
) -> str:
    if job.error_message and job.status in {JobStatus.FAILED, JobStatus.NEEDS_AUTH}:
        return job.error_message
    if job.status == JobStatus.NEEDS_AUTH:
        scope = _auth_scope(job)
        if scope == "video":
            return "视频下载授权已失效或尚未完成，任务已暂停。"
        return "抖音账号授权已失效或尚未完成，任务已暂停。"
    if job.status == JobStatus.WAITING_CONFIRMATION:
        return "该视频较长，需要你明确批准后才会继续下载或分析。"
    if job.status == JobStatus.NEEDS_REVIEW:
        return "本地提取完成，但有低置信疑点需要你复核。"
    if job.status == JobStatus.NEEDS_SELECTION:
        return "清点已完成，请选择要导入的作品并确认。"
    if job.status == JobStatus.AWAITING_AGENT_ANALYSIS:
        if analysis_mode == AnalysisMode.GATEWAY.value:
            return "当前为 Gateway 模式，等待外部 Agent 接续分析。后台不会自动完成 AI 整理。"
        if analysis_mode == AnalysisMode.LOCAL.value:
            return "本地模式正在等待整理，不会调用外部模型。"
        return "等待后台模型接口继续整理。"
    if job.status == JobStatus.ANALYZING:
        if analysis_mode == AnalysisMode.GATEWAY.value:
            return "外部 Agent 正在提交分析。后台不会在 Gateway 模式下自行完成整理。"
        if analysis_mode == AnalysisMode.LOCAL.value:
            return "本地模式正在整理，不调用外部模型。"
        return "后台模型接口正在整理。"
    if job.status == JobStatus.COMPLETED:
        return "已写入知识库。"
    if job.status == JobStatus.COMPLETED_WITH_WARNINGS:
        return "已结束，但有需要留意的提示。"
    if job.status == JobStatus.FAILED:
        return job.error_message or "任务失败，可在确认原因后重试。"
    return JOB_STATUS_LABELS.get(job.status.value, job.status.value)


def present_job(
    job: JobRecord,
    *,
    analysis_mode: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage = stage_for_status(job.status, auth_scope=_auth_scope(job))
    payload = {
        "id": job.id,
        "kind": job.kind,
        "kind_label": JOB_KIND_LABELS.get(job.kind, job.kind),
        "status": job.status.value,
        "state": job.status.value,
        "state_label": label_status(job.status),
        "stage": stage,
        "stage_label": USER_STAGES[stage],
        "progress": job.progress,
        "message_for_user": message_for_user(job, analysis_mode=analysis_mode),
        "next_action": next_action_for(job, analysis_mode=analysis_mode),
        "retryable": retryable(job),
        "requires_user_action": requires_user_action(job),
        "updated_at": beijing_iso(job.updated_at),
        "updated_display": format_beijing(job.updated_at),
        "created_at": beijing_iso(job.created_at),
        "created_display": format_beijing(job.created_at),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": job.result,
        "parent_job_id": parent_job_id(job),
        "child_job_ids": child_job_ids(job),
        "entry_id": _entry_id(job),
        "auth_scope": _auth_scope(job),
        "analysis_mode": analysis_mode,
        "analysis_mode_label": ANALYSIS_MODE_WEB_COPY.get(
            analysis_mode, ANALYSIS_MODE_LABELS.get(analysis_mode, analysis_mode)
        ),
        "share_text": job.request.share_text,
        "inspirations": [item.model_dump(mode="json") for item in job.request.inspirations],
        "options": job.request.options.model_dump(mode="json"),
    }
    if extra:
        payload.update(extra)
    return sanitize_public_payload(payload)


def cookie_source_label(channel: str, cookie_source: str | None = None) -> str:
    if channel == "video":
        return "本机日常浏览器的登录 Cookie（抖库不会读取或显示 Cookie 内容）"
    return "抖库专用浏览器配置（与视频下载通道相互独立）"


def present_auth_check(
    check: dict[str, Any],
    *,
    channel: str,
    session_stage: str | None = None,
    affected_job_count: int = 0,
    checked_at: datetime | None = None,
    account_hint: str | None = None,
) -> dict[str, Any]:
    authorizing = session_stage in {"queued", "launching", "waiting_login", "verifying"}
    machine_state = "authorizing" if authorizing else check.get("state") or "error"
    user_code, user_label = AUTH_USER_STATES.get(str(machine_state), ("check_failed", "检查失败"))
    timestamp = checked_at or datetime.now(UTC)
    payload = {
        "channel": channel,
        "channel_label": CHANNEL_LABELS[channel],
        "purpose": CHANNEL_PURPOSES[channel],
        "state": machine_state,
        "user_state": user_code,
        "user_state_label": user_label,
        "ok": bool(check.get("ok")),
        "server_verified": bool(check.get("server_verified")),
        "cookie_source_label": cookie_source_label(channel, check.get("cookie_source")),
        "account_hint": account_hint or "",
        "message": check.get("message") or "",
        "checked_at": beijing_iso(timestamp),
        "checked_display": format_beijing(timestamp),
        "affected_job_count": affected_job_count,
        "session_stage": session_stage,
        "session_stage_label": AUTH_SESSION_STAGES.get(session_stage or "", ""),
    }
    return sanitize_public_payload(payload)


def analysis_presentation(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "mode_label": ANALYSIS_MODE_LABELS.get(mode, mode),
        "web_copy": ANALYSIS_MODE_WEB_COPY.get(mode, mode),
        "web_can_complete": mode != AnalysisMode.GATEWAY.value,
        "token_hint": (
            "可能调用外部模型并消耗 Token"
            if mode == AnalysisMode.PROVIDER.value
            else "不调用外部模型"
            if mode == AnalysisMode.LOCAL.value
            else "Web 不能单独完成分析，需要外部 Agent 接续"
        ),
    }


def worker_is_fresh(heartbeat_at: datetime | None, *, now: datetime | None = None) -> bool:
    if heartbeat_at is None:
        return False
    current = now or datetime.now(UTC)
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
    return current - heartbeat_at <= timedelta(seconds=90)


SAFE_COOKIE_KEYS = {"cookie_values_exposed", "cookie_source_label"}


def looks_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in SAFE_COOKIE_KEYS:
        return False
    if normalized in SECRET_KEY_NAMES:
        return True
    parts = set(normalized.split("_"))
    return bool(parts & {"password", "passwd", "secret", "apikey"}) or (
        "cookie" in parts and normalized not in SAFE_COOKIE_KEYS
    )


def sanitize_public_payload(value: Any) -> Any:
    """Drop secret-looking fields from anything returned to Web clients."""
    if isinstance(value, dict):
        return {
            str(key): sanitize_public_payload(item)
            for key, item in value.items()
            if not looks_secret_key(str(key))
        }
    if isinstance(value, list):
        return [sanitize_public_payload(item) for item in value]
    return value
