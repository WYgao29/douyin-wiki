"""User-facing job, auth, and system presentation for the Web primary entry."""

from __future__ import annotations

import re
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
    "available": ("unverified", "已检测到 Cookie，待联网确认"),
    "unverified": ("unverified", "已检测到 Cookie，待联网确认"),
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
    "video": "用来下载视频。和抖音账号授权不是同一件事。",
    "douyin": "用来清点收藏、博主主页和图文。和视频下载不是同一件事。",
}

_CHECK_LOOP_MARKERS = (
    "请点击「检查状态」完成验证；这与抖音账号授权无关。",
    "请点「检查状态」联网确认；与抖音账号授权相互独立。",
    "请点击「检查状态」",
    "请点「检查状态」",
    "点击「检查状态」",
    "待联网确认",
)

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


def user_dismissed(job: JobRecord) -> bool:
    return bool((job.artifacts or {}).get("user_dismissed"))


_GENERIC_SUBJECTS = {"抖音视频", "抖音作品", "抖音博主", "抖音图文", "未命名"}
# Douyin captions append #话题 tokens. Drop them before the length cut so a tag
# is not what the row title gets truncated into.
_HASHTAG_RE = re.compile(r"#[^\s#]+#?")
_KIND_PREFIX = {
    "capture": "单条采集",
    "creator_import": "博主批量",
    "favorites_import": "抖音收藏",
    "reanalyze": "重新分析",
    "media_restore": "媒体恢复",
}


def _clean_subject(value: Any, *, limit: int = 36) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(_HASHTAG_RE.sub(" ", value).split())
    if not text or text in _GENERIC_SUBJECTS:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _share_caption(share_text: str) -> str:
    for raw in str(share_text or "").splitlines():
        text = " ".join(raw.split())
        if not text or text.startswith(("http://", "https://")):
            continue
        if "douyin.com" in text and " " not in text:
            continue
        subject = _clean_subject(text)
        if subject:
            return subject
    return ""


def job_display_title(job: JobRecord, *, hints: dict[str, str] | None = None) -> str:
    """Human title for a job row: kind plus the object name when we know it."""
    hints = hints or {}
    prefix = _KIND_PREFIX.get(job.kind) or JOB_KIND_LABELS.get(job.kind, job.kind)
    result = job.result or {}
    artifacts = job.artifacts or {}
    subject = ""
    if job.kind == "creator_import":
        subject = _clean_subject(
            result.get("creator_name") or artifacts.get("creator_name") or hints.get("creator_name")
        )
        if not subject and job.status == JobStatus.FAILED:
            subject = "链接未能识别博主"
    elif job.kind == "favorites_import":
        subject = _clean_subject(
            result.get("nickname") or artifacts.get("nickname") or hints.get("nickname")
        )
    else:
        metadata = artifacts.get("metadata") if isinstance(artifacts.get("metadata"), dict) else {}
        for candidate in (
            result.get("title"),
            result.get("entry_title"),
            metadata.get("title"),
            hints.get("work_title"),
            hints.get("entry_title"),
            result.get("summary"),
            hints.get("entry_summary"),
            _share_caption(job.request.share_text),
        ):
            subject = _clean_subject(candidate)
            if subject:
                break
    if not subject or subject == prefix:
        return prefix
    return f"{prefix} · {subject}"


def child_job_ids(job: JobRecord) -> list[str]:
    raw = job.result.get("child_job_ids") or []
    return [str(item) for item in raw if item]


def requires_user_action(job: JobRecord) -> bool:
    if user_dismissed(job):
        return False
    if job.status in USER_ACTION_STATUSES:
        return True
    return job.status == JobStatus.FAILED


def job_is_in_progress(job: JobRecord) -> bool:
    """Machine is working. Excludes finished jobs, operator todos, and Gateway waits."""
    return job.status in RUNNING_STATUSES and not requires_user_action(job)


def retryable(job: JobRecord) -> bool:
    if job.kind == "media_restore":
        return job.status in {JobStatus.FAILED, JobStatus.NEEDS_AUTH}
    return job.status in {JobStatus.FAILED, JobStatus.NEEDS_AUTH, JobStatus.NEEDS_REVIEW}


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
            "code": "retry",
            "label": "按新模型校对策略重试",
            "href": f"/jobs/{job.id}",
            "endpoint": f"/api/jobs/{job.id}/retry",
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
    if (
        job.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
        and child_job_ids(job)
    ):
        return {
            "code": "view_children",
            "label": "查看子任务",
            "href": f"/jobs/{job.id}",
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
    if job.status == JobStatus.FAILED and job.kind == "creator_import":
        reason = job.error_message or "任务失败，可在确认原因后重试。"
        return f"{reason} 下一步：换一条博主主页链接，或到「导入博主」重新粘贴。"
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
        return "历史任务停在人工复核；可按当前模型校对策略重新处理。"
    if job.status == JobStatus.NEEDS_SELECTION:
        return "清点已完成，请选择要导入的作品并确认。"
    if job.status == JobStatus.AWAITING_AGENT_ANALYSIS:
        if analysis_mode == AnalysisMode.GATEWAY.value:
            return "当前为 Gateway 模式。网页不会自动分析，需要外部 Agent 接续。后台不会自动完成 AI 整理。"
        if analysis_mode == AnalysisMode.LOCAL.value:
            return "本地模式正在等待整理，不会调用外部模型。"
        return "等待后台模型接口继续整理。"
    if job.status == JobStatus.ANALYZING:
        correction = (job.artifacts.get("analysis_progress") or {}).get("phase") == "correction"
        if correction:
            if job.artifacts["analysis_progress"].get("waiting_for_resource"):
                return "等待模型校正资源。"
            return "后台模型正在校正逐字稿。"
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
        "stage_label": (
            "LLM 校正"
            if job.status == JobStatus.ANALYZING
            and (job.artifacts.get("analysis_progress") or {}).get("phase") == "correction"
            else USER_STAGES[stage]
        ),
        "progress": job.progress,
        "message_for_user": message_for_user(job, analysis_mode=analysis_mode),
        "next_action": next_action_for(job, analysis_mode=analysis_mode),
        "display_title": job_display_title(job),
        "retryable": retryable(job),
        "requires_user_action": requires_user_action(job),
        "dismissed": user_dismissed(job),
        "updated_at": beijing_iso(job.updated_at),
        "updated_display": format_beijing(job.updated_at),
        "created_at": beijing_iso(job.created_at),
        "created_display": format_beijing(job.created_at),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": job.result,
        "analysis_progress": job.artifacts.get("analysis_progress"),
        "llm_stats": job.artifacts.get("llm_stats"),
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


def _strip_check_loop(message: str) -> str:
    text = message or ""
    for marker in _CHECK_LOOP_MARKERS:
        text = text.replace(marker, "")
    return " ".join(text.split()).strip(" 。")


def _video_conclusion(state: str) -> tuple[str, str]:
    if state == "authorizing":
        return "authorizing", "授权中"
    if state == "ready":
        return "authorized", "可以下载"
    if state in {"needs_login", "expired", "missing"}:
        return "needs_login", "需要重新登录"
    return "unconfirmed", "暂时无法确认"


def _account_conclusion(state: str) -> tuple[str, str]:
    if state == "authorizing":
        return "authorizing", "授权中"
    if state == "ready":
        return "authorized", "已授权"
    if state in {"needs_login", "expired", "missing"}:
        return "needs_login", "需要重新登录"
    if state in {"available", "unverified", "unavailable", "error"}:
        return "unconfirmed", "暂时无法确认"
    return AUTH_USER_STATES.get(state, ("check_failed", "检查失败"))


def _unconfirmed_copy(message: str) -> str:
    text = _strip_check_loop(message)
    if text.startswith("暂时无法确认"):
        body = text
    else:
        body = f"暂时无法确认。原因：{text or '这次探测没有得出结论'}。"
    if "下一步" not in body:
        body = f"{body.rstrip('。')}。下一步：稍后重试；若页面要求登录，再重新授权。"
    return body


def cookie_source_label(channel: str, cookie_source: str | None = None) -> str:
    if channel == "video":
        source = (cookie_source or "").lower()
        if "playwright" in source or "browser_profile" in source or "profile" in source:
            return "专用 Playwright Profile（主下载/CDN 路径；抖库不会读取或显示 Cookie 内容）"
        if source:
            return "系统浏览器 Cookie（仅作 yt-dlp 最后回退；抖库不会读取或显示 Cookie 内容）"
        return "专用 Playwright Profile（主路径）；系统 Chrome Cookie 仅作 yt-dlp 回退"
    return "抖库专用浏览器配置（账号/收藏等通道，与视频下载主路径相互独立）"


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
    machine_state = "authorizing" if authorizing else str(check.get("state") or "error")
    if channel == "video":
        user_code, user_label = _video_conclusion(machine_state)
    else:
        user_code, user_label = _account_conclusion(machine_state)
    timestamp = checked_at or datetime.now(UTC)
    raw_message = _strip_check_loop(str(check.get("message") or ""))
    message = _unconfirmed_copy(raw_message) if user_code == "unconfirmed" else raw_message
    detail = _strip_check_loop(str(check.get("detail") or ""))
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
        "message": message,
        "detail": detail,
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
