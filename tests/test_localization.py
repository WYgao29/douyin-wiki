from __future__ import annotations

import json

from douyin_wiki.localization import (
    add_display_labels,
    localize_for_user,
    parse_creator_decision,
    parse_job_status,
    parse_retention,
)
from douyin_wiki.models import CreatorWorkDecision, JobStatus, RetentionPolicy


def test_user_payload_replaces_status_codes_and_count_keys() -> None:
    payload = {
        "status": "needs_selection",
        "created_at": "2026-08-23T08:40:41.757174+00:00",
        "phase": "analysis",
        "selection": {
            "pending": 29,
            "selected": 0,
            "skipped": 0,
            "imported": 0,
            "total": 29,
        },
        "items": [
            {
                "decision": "pending",
                "availability": "available",
                "media_status": "present",
                "retention": "temporary",
                "stale": False,
                "partial": True,
                "has_more": False,
                "is_new": True,
                "is_pinned": False,
                "custom_flag": True,
            }
        ],
    }

    localized = localize_for_user(payload)

    serialized = json.dumps(localized, ensure_ascii=False)
    assert "pending" not in serialized
    assert localized["status"] == "待选择作品"
    assert localized["phase"] == "内容分析"
    assert localized["selection"]["待入库"] == 29
    assert localized["selection"]["总数"] == 29
    assert localized["items"][0]["decision"] == "待入库"
    assert localized["items"][0]["availability"] == "可用"
    assert localized["items"][0]["stale"] == "当前有效"
    assert localized["items"][0]["partial"] == "清单不完整"
    assert localized["items"][0]["has_more"] == "已全部显示"
    assert localized["items"][0]["is_new"] == "新增作品"
    assert localized["items"][0]["is_pinned"] == "未置顶"
    assert localized["items"][0]["custom_flag"] == "是"
    assert localized["created_at"] == "2026-08-23T16:40:41.757174+08:00"


def test_agent_payload_keeps_codes_but_adds_chinese_labels() -> None:
    payload = add_display_labels(
        {
            "status": "needs_selection",
            "created_at": "2026-08-23T08:40:41.757174Z",
            "selection": {"pending": 29, "selected": 0, "total": 29},
            "work": {
                "decision": "pending",
                "availability": "available",
                "stale": False,
                "partial": True,
                "has_more": False,
                "custom_flag": True,
            },
        }
    )

    assert payload["status"] == "needs_selection"
    assert payload["status_label"] == "待选择作品"
    assert payload["selection_labels"] == {"待入库": 29, "已选入库": 0, "总数": 29}
    assert payload["work"]["decision_label"] == "待入库"
    assert payload["work"]["availability_label"] == "可用"
    assert payload["work"]["stale_label"] == "当前有效"
    assert payload["work"]["partial_label"] == "清单不完整"
    assert payload["work"]["has_more_label"] == "已全部显示"
    assert payload["work"]["custom_flag_label"] == "是"
    assert payload["created_at"] == "2026-08-23T16:40:41.757174+08:00"


def test_status_filters_accept_chinese_and_legacy_codes() -> None:
    assert parse_job_status("已完成") == JobStatus.COMPLETED
    assert parse_job_status("completed") == JobStatus.COMPLETED
    assert parse_creator_decision("已跳过") == CreatorWorkDecision.SKIPPED
    assert parse_creator_decision("未入库") == CreatorWorkDecision.SKIPPED
    assert parse_creator_decision("skipped") == CreatorWorkDecision.SKIPPED
    assert parse_retention("永久保留") == RetentionPolicy.KEEP
    assert parse_retention("keep") == RetentionPolicy.KEEP
