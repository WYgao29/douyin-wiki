from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

USER_TIME_FIELDS = {
    "absolute_time",
    "created",
    "published",
    "updated",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_datetime(value: str | date | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_beijing(value: str | date | datetime) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:  # pragma: no cover - protected by the non-optional input type
        raise ValueError("时间不能为空")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def beijing_iso(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    return to_beijing(value).isoformat()


def beijing_date(value: str | date | datetime | None = None) -> str:
    target = utc_now() if value is None else value
    return to_beijing(target).date().isoformat()


def format_beijing(value: str | date | datetime | None) -> str:
    if value is None:
        return ""
    return to_beijing(value).strftime("%Y-%m-%d %H:%M:%S（北京时间）")


def _is_time_field(field: str | None) -> bool:
    if not field:
        return False
    return field in USER_TIME_FIELDS or field.endswith(("_at", "_until", "_after"))


def _convert_time_string(value: str, field: str | None) -> str:
    if not _is_time_field(field) or len(value) < 16:
        return value
    try:
        return beijing_iso(value) or value
    except ValueError:
        return value


def user_times_to_beijing(value: Any, *, field: str | None = None) -> Any:
    """Convert user-facing timestamps recursively while preserving internal UTC storage."""
    if isinstance(value, datetime):
        return beijing_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: user_times_to_beijing(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [user_times_to_beijing(item, field=field) for item in value]
    if isinstance(value, tuple):
        return [user_times_to_beijing(item, field=field) for item in value]
    if isinstance(value, str):
        return _convert_time_string(value, field)
    return value
