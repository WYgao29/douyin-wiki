from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from .models import CreatorWorkDecision, JobStatus, RetentionPolicy
from .time_utils import user_times_to_beijing

JOB_STATUS_LABELS = {
    "queued": "待处理",
    "resolving": "正在解析",
    "inventorying": "正在清点作品",
    "needs_selection": "待选择作品",
    "dispatching": "正在创建采集任务",
    "monitoring": "正在处理所选作品",
    "downloading": "正在下载",
    "extracting": "正在提取内容",
    "transcribing": "正在转录",
    "awaiting_agent_analysis": "待 AI 处理",
    "needs_auth": "需要登录授权",
    "needs_review": "需要人工复核",
    "waiting_confirmation": "等待用户确认",
    "analyzing": "正在分析",
    "completed": "已完成",
    "completed_with_warnings": "已完成（有提示）",
    "failed": "失败",
}

CREATOR_DECISION_LABELS = {
    "pending": "待入库",
    "selected": "已选入库",
    "skipped": "未入库",
    "imported": "已入库",
}

CREATOR_AVAILABILITY_LABELS = {
    "available": "可用",
    "possibly_unavailable": "可能已不可用",
    "source_unavailable": "来源已不可用",
}

AUTH_STATE_LABELS = {
    "ready": "已就绪",
    "available": "可用",
    "unverified": "尚未联网验证",
    "missing": "未找到授权信息",
    "expired": "已过期",
    "needs_login": "需要登录",
    "unavailable": "不可用",
    "error": "检查失败",
}

ENTRY_STATUS_LABELS = {
    "active": "正常",
    "stale": "已过期",
    "raw": "原始记录",
}

MEDIA_STATUS_LABELS = {
    "present": "已保留",
    "removed": "已清理",
}

RETENTION_LABELS = {
    "temporary": "临时保留",
    "keep": "永久保留",
    "discard": "处理后清理",
}

REMINDER_STATUS_LABELS = {
    "candidate": "待确认",
    "creating": "正在创建",
    "created": "已创建",
}

REVIEW_STATUS_LABELS = {
    "open": "待处理",
    "resolved": "已解决",
}

GENERAL_STATUS_LABELS = {
    "initialized": "初始化完成",
    "configured": "配置完成",
    "idle": "当前无任务",
    "migrated": "迁移完成",
    "removed": "已移除",
    "created": "已创建",
    "rebuilt": "重建完成",
    "current": "当前版本",
    "needs_update": "需要更新",
}

PHASE_LABELS = {
    "transcript_correction": "逐字稿校正",
    "analysis": "内容分析",
    "human_review": "人工复核",
    "image_ocr_review": "图片文字复核",
}

SOURCE_KIND_LABELS = {"video": "视频", "image_note": "图文"}
ANALYSIS_MODE_LABELS = {"gateway": "网关 Agent", "provider": "模型接口", "local": "本地模式"}
DISPLAY_MODE_LABELS = {"all": "全部", "paginated": "分页"}
JOB_KIND_LABELS = {
    "capture": "单条采集",
    "creator_import": "博主批量采集",
    "reanalyze": "重新分析",
    "overview": "专题总览",
    "comparison": "跨来源对比表",
    "evidence_map": "证据地图",
    "consensus": "共识与分歧",
    "decision_brief": "决策简报",
    "faq": "专题 FAQ",
    "note": "专题笔记",
}
AUTH_SCOPE_LABELS = {
    "video": "视频下载",
    "image_note": "图文采集",
    "creator": "博主主页",
    "library": "整个资料库",
    "entry": "单篇文章",
    "topic": "专题",
}

BOOLEAN_STATE_LABELS = {
    "enabled": {True: "已启用", False: "未启用"},
    "server_verified": {True: "已联网验证", False: "尚未联网验证"},
    "allow_long": {True: "已允许超长视频", False: "未允许超长视频"},
    "approve_cloud_analysis": {True: "已允许云端分析", False: "未允许云端分析"},
    "ai_analysis_approved": {True: "已允许 AI 分析", False: "未允许 AI 分析"},
    "cloud_analysis_approved": {True: "已允许云端分析", False: "未允许云端分析"},
    "stale": {True: "已过期", False: "当前有效"},
    "partial": {True: "清单不完整", False: "清单完整"},
    "inventory_partial": {True: "清单不完整", False: "清单完整"},
    "complete": {True: "清点完整", False: "清点不完整"},
    "has_more": {True: "还有更多", False: "已全部显示"},
    "is_new": {True: "新增作品", False: "已有记录"},
    "is_pinned": {True: "已置顶", False: "未置顶"},
    "ok": {True: "可用", False: "不可用"},
    "needs_clarification": {True: "需要澄清时间", False: "时间明确"},
    "required": {True: "必需", False: "可选"},
    "dry_run": {True: "仅预览", False: "已执行"},
    "review_resolved": {True: "复核已解决", False: "复核未解决"},
    "duplicate": {True: "重复作品", False: "新作品"},
    "skipped": {True: "已跳过", False: "未跳过"},
    "reacquired": {True: "已重新获取媒体", False: "首次获取媒体"},
    "reused_ocr": {True: "已复用文字识别结果", False: "未复用文字识别结果"},
    "live_photo": {True: "动态照片", False: "静态图片"},
    "batch_silent": {True: "批量静默汇总", False: "逐项反馈"},
    "cookie_values_exposed": {True: "已暴露 Cookie 内容", False: "未暴露 Cookie 内容"},
    "force": {True: "强制重跑", False: "常规运行"},
    "accept_partial": {True: "已接受不完整清单", False: "未接受不完整清单"},
}

STATUS_LABELS = {
    **JOB_STATUS_LABELS,
    **ENTRY_STATUS_LABELS,
    **MEDIA_STATUS_LABELS,
    **REMINDER_STATUS_LABELS,
    **REVIEW_STATUS_LABELS,
    **GENERAL_STATUS_LABELS,
}

FIELD_LABELS = {
    "status": STATUS_LABELS,
    "state": AUTH_STATE_LABELS,
    "decision": CREATOR_DECISION_LABELS,
    "availability": CREATOR_AVAILABILITY_LABELS,
    "phase": PHASE_LABELS,
    "retention": RETENTION_LABELS,
    "media_retention": RETENTION_LABELS,
    "media_status": MEDIA_STATUS_LABELS,
    "source_kind": SOURCE_KIND_LABELS,
    "analysis_mode": ANALYSIS_MODE_LABELS,
    "display_mode": DISPLAY_MODE_LABELS,
    "kind": JOB_KIND_LABELS,
    "scope": AUTH_SCOPE_LABELS,
}

COUNT_FIELDS = {"summary", "selection", "counts", "child_status_counts"}
COUNT_LABELS = {
    **JOB_STATUS_LABELS,
    **CREATOR_DECISION_LABELS,
    **CREATOR_AVAILABILITY_LABELS,
    "total": "总数",
}


def label_for(field: str, value: str) -> str:
    return FIELD_LABELS.get(field, {}).get(value, value)


def label_status(value: JobStatus | str) -> str:
    code = value.value if isinstance(value, JobStatus) else str(value)
    return JOB_STATUS_LABELS.get(code, code)


def label_entry_status(value: str) -> str:
    return ENTRY_STATUS_LABELS.get(value, value)


def label_media_status(value: str) -> str:
    return MEDIA_STATUS_LABELS.get(value, value)


def label_retention(value: RetentionPolicy | str) -> str:
    code = value.value if isinstance(value, RetentionPolicy) else str(value)
    return RETENTION_LABELS.get(code, code)


def _reverse(labels: dict[str, str], value: str) -> str:
    normalized = value.strip()
    if normalized in labels:
        return normalized
    for code, label in labels.items():
        if normalized == label:
            return code
    raise ValueError(f"不支持的状态：{value}")


def parse_job_status(value: str) -> JobStatus:
    return JobStatus(_reverse(JOB_STATUS_LABELS, value))


def parse_creator_decision(value: str) -> CreatorWorkDecision:
    legacy_labels = {"待选择": "pending", "已选择": "selected", "已跳过": "skipped"}
    if value.strip() in legacy_labels:
        return CreatorWorkDecision(legacy_labels[value.strip()])
    return CreatorWorkDecision(_reverse(CREATOR_DECISION_LABELS, value))


def parse_entry_status(value: str) -> str:
    return _reverse(ENTRY_STATUS_LABELS, value)


def parse_media_status(value: str) -> str:
    return _reverse(MEDIA_STATUS_LABELS, value)


def parse_retention(value: str) -> RetentionPolicy:
    return RetentionPolicy(_reverse(RETENTION_LABELS, value))


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    return value


def _localized_counts(value: dict[str, Any]) -> dict[str, Any]:
    return {
        COUNT_LABELS.get(str(key), str(key)): localize_for_user(item) for key, item in value.items()
    }


def localize_for_user(value: Any, *, field: str | None = None) -> Any:
    """Replace status codes in a user-facing payload while leaving business data intact."""
    value = _dump(value)
    if isinstance(value, dict):
        localized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in COUNT_FIELDS and isinstance(item, dict):
                localized[key_text] = _localized_counts(item)
            else:
                localized[key_text] = localize_for_user(item, field=key_text)
        return localized
    if isinstance(value, list):
        return [localize_for_user(item) for item in value]
    if isinstance(value, tuple):
        return [localize_for_user(item) for item in value]
    if isinstance(value, bool) and field in BOOLEAN_STATE_LABELS:
        return BOOLEAN_STATE_LABELS[field][value]
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str) and field:
        localized_time = user_times_to_beijing(value, field=field)
        return label_for(field, localized_time)
    return user_times_to_beijing(value, field=field)


def add_display_labels(value: Any, *, field: str | None = None) -> Any:
    """Keep stable machine codes for Agents while adding explicit Chinese display labels."""
    value = _dump(value)
    if isinstance(value, list):
        return [add_display_labels(item, field=field) for item in value]
    if not isinstance(value, dict):
        return user_times_to_beijing(value, field=field)
    result = {str(key): add_display_labels(item, field=str(key)) for key, item in value.items()}
    for field, labels in FIELD_LABELS.items():
        raw = value.get(field)
        if isinstance(raw, str) and raw in labels:
            result[f"{field}_label"] = labels[raw]
    for field in COUNT_FIELDS:
        raw = value.get(field)
        if isinstance(raw, dict):
            result[f"{field}_labels"] = _localized_counts(raw)
    for field, labels in BOOLEAN_STATE_LABELS.items():
        raw = value.get(field)
        if isinstance(raw, bool):
            result[f"{field}_label"] = labels[raw]
    for field, raw in value.items():
        if isinstance(raw, bool) and f"{field}_label" not in result:
            result[f"{field}_label"] = "是" if raw else "否"
    return result
