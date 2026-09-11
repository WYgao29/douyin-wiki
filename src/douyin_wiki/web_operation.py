"""Service-side operations used by the Web primary entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import JobStateError
from .models import CaptureOptions, InspirationInput, JobStatus, RetentionPolicy
from .operation import (
    RUNNING_STATUSES,
    TERMINAL_STATUSES,
    USER_ACTION_STATUSES,
    analysis_presentation,
    present_auth_check,
    present_job,
    sanitize_public_payload,
    worker_is_fresh,
)
from .setup import doctor
from .time_utils import beijing_iso, format_beijing, parse_datetime
from .web_auth import WebAuthManager


class WebOperationService:
    def __init__(self, core, *, auth_manager: WebAuthManager | None = None) -> None:
        self.core = core
        self.auth = auth_manager or WebAuthManager(core)

    @property
    def analysis_mode(self) -> str:
        return self.core.config.analysis_mode.value

    def present(self, job, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return present_job(job, analysis_mode=self.analysis_mode, extra=extra)

    def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        requires_user_action: bool | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= limit <= 200:
            raise ValueError("page 必须大于零，limit 必须在 1 到 200 之间")
        if status:
            try:
                JobStatus(status)
            except ValueError as exc:
                raise ValueError("不支持的任务状态") from exc
        jobs = self.core.list_jobs(limit=None)
        if kind:
            jobs = [job for job in jobs if job.kind == kind]
        if status:
            jobs = [job for job in jobs if job.status.value == status]
        if requires_user_action is True:
            jobs = [
                job
                for job in jobs
                if job.status in USER_ACTION_STATUSES or job.status == JobStatus.FAILED
            ]
        elif requires_user_action is False:
            jobs = [
                job
                for job in jobs
                if job.status not in USER_ACTION_STATUSES and job.status != JobStatus.FAILED
            ]
        total = len(jobs)
        start = (page - 1) * limit
        sliced = jobs[start : start + limit]
        return {
            "items": [self.present(job) for job in sliced],
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": start + limit < total,
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.core.get_job(job_id)
        timeline = [
            {
                "id": event.id,
                "status": event.status.value,
                "state_label": present_job(
                    job.model_copy(update={"status": event.status}),
                    analysis_mode=self.analysis_mode,
                )["state_label"],
                "result": event.result,
                "created_at": beijing_iso(event.created_at),
                "created_display": format_beijing(event.created_at),
            }
            for event in self.core.database.list_job_timeline(job_id)
        ]
        children = []
        for child_id in job.result.get("child_job_ids") or []:
            try:
                children.append(self.present(self.core.get_job(child_id)))
            except JobStateError:
                continue
        if job.kind == "favorites_import":
            detail = self.core.favorites.get(job_id, page=1, limit=1)
            extra = {
                "batch": {
                    "summary": detail.get("summary"),
                    "complete": detail.get("complete"),
                    "folders_complete": detail.get("folders_complete"),
                    "warnings": detail.get("warnings"),
                    "confirmed": detail.get("confirmed"),
                    "nickname": detail.get("nickname"),
                }
            }
        elif job.kind == "creator_import":
            extra = {
                "batch": {
                    "summary": job.result.get("selection")
                    or self.core.database.creator_inventory_summary(job_id),
                    "partial": bool(job.artifacts.get("inventory_partial")),
                    "creator_id": job.artifacts.get("creator_id"),
                }
            }
        else:
            extra = {}
        extra["timeline"] = timeline
        extra["children"] = children
        extra["child_stats"] = self._child_stats(children)
        if job.status == JobStatus.NEEDS_REVIEW:
            extra["review_issues"] = [
                issue.model_dump(mode="json")
                for issue in self.core.database.get_review_issues(job_id, open_only=True)
            ]
        return self.present(job, extra=extra)

    @staticmethod
    def _child_stats(children: list[dict[str, Any]]) -> dict[str, int]:
        stats = {
            "total": len(children),
            "completed": 0,
            "skipped": 0,
            "running": 0,
            "waiting_user": 0,
            "failed": 0,
            "unavailable": 0,
        }
        for child in children:
            status = child.get("status")
            if status in {"completed", "completed_with_warnings"}:
                stats["completed"] += 1
            elif status == "failed":
                stats["failed"] += 1
            elif status in {item.value for item in USER_ACTION_STATUSES}:
                stats["waiting_user"] += 1
            elif status in {item.value for item in RUNNING_STATUSES}:
                stats["running"] += 1
        return stats

    def job_counts(self) -> dict[str, int]:
        counts = self.core.database.job_status_counts()
        running = sum(counts.get(status.value, 0) for status in RUNNING_STATUSES)
        waiting = sum(counts.get(status.value, 0) for status in USER_ACTION_STATUSES)
        return {
            "running": running,
            "waiting_user": waiting,
            "failed": counts.get(JobStatus.FAILED.value, 0),
            "completed": counts.get(JobStatus.COMPLETED.value, 0)
            + counts.get(JobStatus.COMPLETED_WITH_WARNINGS.value, 0),
            "queued": counts.get(JobStatus.QUEUED.value, 0),
        }

    async def overview(self) -> dict[str, Any]:
        auth = await self.auth_status()
        counts = self.job_counts()
        recent = [
            self.present(job)
            for job in self.core.list_jobs(limit=8)
            if job.kind in {"capture", "creator_import", "favorites_import"}
        ]
        alerts = []
        video = auth["channels"]["video"]
        douyin = auth["channels"]["douyin"]
        if video["affected_job_count"] and video["user_state"] not in {"authorized", "authorizing"}:
            alerts.append(
                {
                    "code": "video_auth",
                    "message": f"视频授权已失效，{video['affected_job_count']} 个任务已暂停。",
                    "actions": [
                        {"label": "立即授权", "href": "/settings/auth"},
                        {
                            "label": f"查看这 {video['affected_job_count']} 个任务",
                            "href": "/jobs?requires_user_action=1&kind=capture",
                        },
                    ],
                }
            )
        douyin_blocked = douyin["user_state"] not in {"authorized", "authorizing"}
        if douyin["affected_job_count"] and douyin_blocked:
            alerts.append(
                {
                    "code": "douyin_auth",
                    "message": f"抖音账号授权已失效，{douyin['affected_job_count']} 个任务已暂停。",
                    "actions": [
                        {"label": "立即授权", "href": "/settings/auth"},
                        {"label": "查看受影响任务", "href": "/jobs?requires_user_action=1"},
                    ],
                }
            )
        health = self.system_health()
        if not health["worker"]["running"]:
            alerts.append(
                {
                    "code": "worker",
                    "message": "后台 Worker 未在运行，新提交的任务会排队等待。",
                    "actions": [{"label": "查看系统状态", "href": "/settings/system"}],
                }
            )
        return sanitize_public_payload(
            {
                "auth": auth,
                "worker": health["worker"],
                "analysis": analysis_presentation(self.analysis_mode),
                "jobs": counts,
                "recent_imports": recent,
                "alerts": alerts,
            }
        )

    def _auth_cache(self) -> dict[str, Any] | None:
        raw = self.core.database.get_index_metadata("web_auth_status_cache")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _store_auth_cache(self, payload: dict[str, Any]) -> None:
        self.core.database.set_index_metadata(
            "web_auth_status_cache", json.dumps(payload, ensure_ascii=False)
        )

    def _decorate_auth_status(self, payload: dict[str, Any], *, cached: bool) -> dict[str, Any]:
        channels = dict(payload.get("channels") or {})
        for name in ("video", "douyin"):
            card = dict(channels.get(name) or {})
            session = self.core.database.latest_auth_session(name)
            card["affected_job_count"] = self.auth.affected_count(name)
            card["session_stage"] = (session or {}).get("stage")
            card["session_stage_label"] = card.get("session_stage_label") or ""
            channels[name] = card
        payload = {**payload, "channels": channels, "cached": cached}
        return sanitize_public_payload(payload)

    async def auth_status(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            cached = self._auth_cache()
            if cached:
                return self._decorate_auth_status(cached, cached=True)
        import asyncio

        video, image_note = await asyncio.gather(
            self.core.check_auth_scope("video"),
            self.core.check_auth_scope("image_note"),
        )
        raw_video = video.model_dump(mode="json")
        raw_douyin = image_note.model_dump(mode="json")
        account_hint = ""
        try:
            history = self.core.favorites.history(limit=1)
            if history:
                account_hint = str(history[0].get("nickname") or "")
        except Exception:
            account_hint = ""
        video_session = self.core.database.latest_auth_session("video")
        douyin_session = self.core.database.latest_auth_session("douyin")
        payload = {
            "channels": {
                "video": present_auth_check(
                    raw_video,
                    channel="video",
                    session_stage=(video_session or {}).get("stage"),
                    affected_job_count=self.auth.affected_count("video"),
                    checked_at=parse_datetime(raw_video.get("checked_at"))
                    if raw_video.get("checked_at")
                    else None,
                ),
                "douyin": present_auth_check(
                    raw_douyin,
                    channel="douyin",
                    session_stage=(douyin_session or {}).get("stage"),
                    affected_job_count=self.auth.affected_count("douyin"),
                    account_hint=account_hint,
                    checked_at=parse_datetime(raw_douyin.get("checked_at"))
                    if raw_douyin.get("checked_at")
                    else None,
                ),
            },
            "scopes": {
                "video": raw_video,
                "image_note": raw_douyin,
                "creator": raw_douyin,
                "favorites": raw_douyin,
            },
            "cookie_values_exposed": False,
        }
        self._store_auth_cache(payload)
        return self._decorate_auth_status(payload, cached=False)

    def capture(
        self,
        share_text: str,
        *,
        inspirations: list[InspirationInput] | None = None,
        retention: RetentionPolicy = RetentionPolicy.TEMPORARY,
        allow_long: bool = False,
        approve_cloud_analysis: bool = False,
    ):
        job = self.core.capture_douyin(
            share_text,
            inspirations=inspirations or [],
            options=CaptureOptions(
                retention=retention,
                allow_long=allow_long,
                approve_cloud_analysis=approve_cloud_analysis,
            ),
        )
        return self.present(self.core.get_job(job.id))

    def system_health(self) -> dict[str, Any]:
        heartbeat = self.core.database.get_worker_heartbeat()
        raw_heartbeat = heartbeat.get("at") if heartbeat else None
        heartbeat_at = parse_datetime(raw_heartbeat) if raw_heartbeat else None
        running = worker_is_fresh(heartbeat_at)
        counts = self.job_counts()
        last_maintenance = self.core.database.last_maintenance_at("weekly")
        return sanitize_public_payload(
            {
                "web": {
                    "version": "0.2.0",
                    "running": True,
                    "bind": f"{self.core.config.web.host}:{self.core.config.web.port}",
                },
                "worker": {
                    "running": running,
                    "last_heartbeat": beijing_iso(heartbeat_at) if heartbeat_at else None,
                    "last_heartbeat_display": (
                        format_beijing(heartbeat_at) if heartbeat_at else "尚无心跳"
                    ),
                    "worker_id": (heartbeat or {}).get("worker_id"),
                    "running_job_id": (heartbeat or {}).get("running_job_id"),
                    "queue_length": counts["queued"],
                    "reload_requested_at": self.core.database.worker_reload_requested_at(),
                },
                "analysis": analysis_presentation(self.analysis_mode),
                "jobs": counts,
                "last_maintenance_at": beijing_iso(last_maintenance) if last_maintenance else None,
                "last_maintenance_display": format_beijing(last_maintenance)
                if last_maintenance
                else "尚未执行",
            }
        )

    def run_doctor(self) -> dict[str, Any]:
        return sanitize_public_payload(doctor(self.core.config))

    def maintenance(self, *, confirmed: bool) -> dict[str, Any]:
        report = self.core.run_maintenance(apply=confirmed)
        return sanitize_public_payload(
            {
                "preview": not confirmed,
                "executed": confirmed,
                "will_delete": ["过期且未永久保留的媒体文件"],
                "will_keep": ["知识资料正文", "任务历史", "聊天记录"],
                "report": report,
            }
        )

    def rebuild(self, *, confirmed: bool) -> dict[str, Any]:
        report = self.core.rebuild_database_from_vault(apply=confirmed)
        return sanitize_public_payload(
            {
                "preview": not confirmed,
                "executed": confirmed,
                "will_delete": ["可重建的 SQLite 知识缓存"] if confirmed else [],
                "will_keep": ["Vault 知识资料", "任务队列", "收藏导入历史", "聊天记录"],
                "report": report,
            }
        )

    def request_worker_reload(self) -> dict[str, Any]:
        self.core.database.request_worker_reload()
        health = self.system_health()
        return {
            "status": "已请求 Worker 在当前循环结束后退出，由 LaunchAgent 重新拉起",
            "worker": health["worker"],
            "note": "若未安装常驻 Worker，需要在终端运行 douyin-wiki worker run 或 service install",
        }

    def storage(self) -> dict[str, Any]:
        vault = self.core.config.vault_path
        database = self.core.config.database_path
        entries = self.core.database.list_entries()
        kept = sum(1 for item in entries if item.retention.value == "keep" or item.favorite)
        return sanitize_public_payload(
            {
                "vault_exists": vault.exists(),
                "database_exists": database.exists(),
                "vault_bytes": _path_size(vault),
                "database_bytes": database.stat().st_size if database.exists() else 0,
                "entry_count": len(entries),
                "kept_media_count": kept,
                "retention_days": self.core.config.media.retention_days,
                "retention_label": f"临时媒体保留 {self.core.config.media.retention_days} 天",
            }
        )

    def delete_import_history(self, job_id: str, *, confirmed: bool) -> dict[str, Any]:
        job = self.core.get_job(job_id)
        if job.kind not in {"favorites_import", "creator_import"}:
            raise JobStateError("只能删除博主或收藏导入批次的展示历史")
        if job.status in RUNNING_STATUSES:
            raise JobStateError("进行中的导入批次不能删除")
        child_ids = list(job.result.get("child_job_ids") or [])
        preview = {
            "job_id": job_id,
            "kind": job.kind,
            "will_delete": ["该批次的展示历史和本地清点清单"],
            "will_keep": ["已入库知识资料", "抖音平台内容", "已创建的采集子任务"],
            "child_job_count": len(child_ids),
        }
        if not confirmed:
            return {"preview": True, "executed": False, **preview}
        if job.kind == "favorites_import":
            with self.core.database.connect() as conn:
                conn.execute("DELETE FROM favorites_items WHERE parent_id=?", (job_id,))
                conn.execute("DELETE FROM favorites_runs WHERE parent_id=?", (job_id,))
        self.core.database.delete_job_record(job_id)
        return {"preview": False, "executed": True, **preview}

    def clear_favorites_cache(self, *, confirmed: bool) -> dict[str, Any]:
        removable: list[str] = []
        for job_id in self.core.favorites.store.parent_ids(limit=200):
            job = self.core.database.get_job(job_id)
            run = self.core.favorites.store.load(job_id)
            if job.status in RUNNING_STATUSES:
                continue
            keep_confirmed = TERMINAL_STATUSES | {JobStatus.NEEDS_SELECTION}
            if run["confirmed"] and job.status not in keep_confirmed:
                continue
            if run["confirmed"]:
                continue
            removable.append(job_id)
        preview = {
            "will_delete": ["未确认的本地收藏目录和作品清单缓存"],
            "will_keep": ["已入库知识资料", "抖音平台收藏", "已确认并仍在执行的导入批次"],
            "run_count": len(removable),
        }
        if not confirmed:
            return {"preview": True, "executed": False, **preview}
        for job_id in removable:
            with self.core.database.connect() as conn:
                conn.execute("DELETE FROM favorites_items WHERE parent_id=?", (job_id,))
                conn.execute("DELETE FROM favorites_runs WHERE parent_id=?", (job_id,))
            self.core.database.delete_job_record(job_id)
        return {"preview": False, "executed": True, **preview}


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total
