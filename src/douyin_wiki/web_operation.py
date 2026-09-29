"""Service-side operations used by the Web primary entry."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from .config import llm_is_configured
from .errors import EntryNotFoundError, JobStateError
from .models import CaptureOptions, InspirationInput, JobRecord, JobStatus, RetentionPolicy
from .operation import (
    RUNNING_STATUSES,
    TERMINAL_STATUSES,
    USER_ACTION_STATUSES,
    analysis_presentation,
    job_display_title,
    job_is_in_progress,
    parent_job_id,
    present_auth_check,
    present_job,
    sanitize_public_payload,
    worker_is_fresh,
)
from .operation import requires_user_action as job_needs_user
from .secrets import get_secret
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
        merged = dict(extra or {})
        merged["display_title"] = job_display_title(job, hints=self._title_hints(job))
        return present_job(job, analysis_mode=self.analysis_mode, extra=merged)

    def dismiss_job(self, job_id: str):
        job = self.core.database.get_job(job_id)
        if job.status != JobStatus.FAILED:
            raise JobStateError("只有失败任务可以不再提醒")
        if job.artifacts.get("user_dismissed"):
            return job
        return self.core.database.update_job(job_id, artifacts={"user_dismissed": True})

    def _title_hints(self, job: JobRecord) -> dict[str, str]:
        hints: dict[str, str] = {}
        artifacts = job.artifacts or {}
        result = job.result or {}
        if job.kind == "favorites_import":
            try:
                snapshot = self.core.favorites.store.load(job.id).get("snapshot") or {}
            except JobStateError:
                snapshot = {}
            nickname = snapshot.get("nickname")
            if isinstance(nickname, str) and nickname.strip():
                hints["nickname"] = nickname.strip()
        if job.kind == "creator_import":
            creator_id = str(result.get("creator_id") or artifacts.get("creator_id") or "")
            if creator_id:
                try:
                    creator = self.core.database.get_creator(creator_id)
                except JobStateError:
                    creator = None
                if creator and creator.nickname:
                    hints["creator_name"] = creator.nickname
        context = artifacts.get("creator_context")
        if isinstance(context, dict) and context.get("id") and context.get("work_id"):
            try:
                work = self.core.database.get_creator_work(
                    str(context["id"]), str(context["work_id"])
                )
            except JobStateError:
                work = None
            if work and work.title:
                hints["work_title"] = work.title
        entry_id = (
            result.get("entry_id")
            or artifacts.get("entry_id")
            or artifacts.get("reanalyze_entry_id")
        )
        if entry_id:
            try:
                entry = self.core.database.get_entry(str(entry_id))
            except EntryNotFoundError:
                entry = None
            if entry is not None:
                if entry.title:
                    hints["entry_title"] = entry.title
                if entry.summary:
                    hints["entry_summary"] = entry.summary
        return hints

    def list_jobs(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        requires_user_action: bool | None = None,
        parents_only: bool = True,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= limit <= 200:
            raise ValueError("page 必须大于零，limit 必须在 1 到 200 之间")
        in_progress = status in {"in_progress", "进行中"}
        if status and not in_progress:
            try:
                JobStatus(status)
            except ValueError as exc:
                raise ValueError("不支持的任务状态") from exc
        jobs = self.core.list_jobs(limit=None)
        if parents_only:
            jobs = [job for job in jobs if parent_job_id(job) is None]
        if kind:
            jobs = [job for job in jobs if job.kind == kind]
        if in_progress:
            jobs = [job for job in jobs if job_is_in_progress(job)]
        elif status == JobStatus.COMPLETED.value:
            done = {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
            jobs = [job for job in jobs if job.status in done]
        elif status:
            jobs = [job for job in jobs if job.status.value == status]
        if requires_user_action is True:
            jobs = [job for job in jobs if job_needs_user(job)]
        elif requires_user_action is False:
            jobs = [job for job in jobs if not job_needs_user(job)]
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
        extra["analysis_evidence_audit"] = job.artifacts.get("analysis_evidence_audit", [])
        extra["media_provenance"] = job.artifacts.get("media_provenance", {})
        extra["children"] = children
        extra["child_stats"] = self._child_stats(children)
        imported_ids: list[str] = []
        child_warnings: list[str] = []
        own_warnings = [
            str(item).strip()
            for item in (job.result or {}).get("warnings") or []
            if str(item).strip()
        ]
        for child in children:
            entry_id = child.get("entry_id")
            if entry_id and entry_id not in imported_ids:
                imported_ids.append(str(entry_id))
            for warning in (child.get("result") or {}).get("warnings") or []:
                text = str(warning).strip()
                if text and text not in own_warnings and text not in child_warnings:
                    child_warnings.append(text)
        if imported_ids:
            extra["imported_entry_ids"] = imported_ids
        if child_warnings:
            extra["result"] = {
                **(job.result or {}),
                "warnings": [*own_warnings, *child_warnings],
                "warnings_from_children": not own_warnings,
            }
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
        counts["requires_user_action"] = self.list_jobs(requires_user_action=True, limit=1)["total"]
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
        elif video["user_state"] == "unverified":
            alerts.append(
                {
                    "code": "video_auth_unverified",
                    "message": (
                        "视频下载授权已检测到 Cookie，但尚未联网确认。"
                        "采集前请到授权状态页点击「检查状态」。"
                        "这与抖音账号授权相互独立。"
                    ),
                    "actions": [
                        {"label": "检查授权状态", "href": "/settings/auth"},
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

    def _video_probe_url(self) -> str | None:
        """Pick a recent capture URL so refresh can run CDN/yt-dlp verification."""
        url_re = re.compile(r"https?://[^\s]+(?:douyin|iesdouyin)\.com/[^\s]+")
        for job in self.core.list_jobs(limit=80):
            if getattr(job, "kind", None) not in {"capture", "creator_import", "favorites_import"}:
                continue
            artifacts = getattr(job, "artifacts", None) or {}
            resolved = artifacts.get("resolved") or {}
            for key in ("canonical_url", "url", "share_url"):
                value = str(resolved.get(key) or "").strip()
                if "douyin.com" in value or "iesdouyin.com" in value:
                    return value
            request = getattr(job, "request", None)
            share = str(getattr(request, "share_text", "") or "")
            match = url_re.search(share)
            if match:
                return match.group(0).rstrip("，。,.")
        return None

    async def auth_status(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            cached = self._auth_cache()
            if cached:
                return self._decorate_auth_status(cached, cached=True)
        import asyncio

        probe_url = self._video_probe_url() if refresh else None
        video, image_note = await asyncio.gather(
            self.core.check_auth_scope("video", video_url=probe_url),
            self.core.check_auth_scope("image_note"),
        )
        if (
            refresh
            and probe_url is None
            and not bool(getattr(video, "server_verified", False))
            and str(getattr(video, "state", "")) in {"available", "unverified"}
        ):
            suffix = (
                "当前库内没有可用来联网探测的作品链接，"
                "请先成功采集一条视频，或使用 CLI："
                "douyin-wiki auth status --video-url <作品链接>。"
            )
            base = str(getattr(video, "message", "") or "已检测到 Cookie")
            video = video.model_copy(update={"message": f"{base}。{suffix}" if not base.endswith("。") else f"{base}{suffix}"})
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
        from douyin_wiki.webapp.app import WEB_VERSION

        heartbeat = self.core.database.get_worker_heartbeat()
        raw_heartbeat = heartbeat.get("at") if heartbeat else None
        heartbeat_at = parse_datetime(raw_heartbeat) if raw_heartbeat else None
        running = worker_is_fresh(heartbeat_at)
        counts = self.job_counts()
        last_maintenance = self.core.database.last_maintenance_at("weekly")
        asr = self.core.config.media.asr_provider
        ocr = self.core.config.media.ocr_provider
        asr_labels = {
            "auto": "自动（优先 SenseVoice，否则 Whisper）",
            "sensevoice": "SenseVoice",
            "whisper": "Whisper",
        }
        ocr_labels = {
            "auto": "自动（优先 RapidOCR，否则苹果 Vision）",
            "rapidocr": "RapidOCR",
            "vision": "苹果 Vision",
        }
        return sanitize_public_payload(
            {
                "web": {
                    "version": WEB_VERSION,
                    "running": True,
                    "bind": f"{self.core.config.web.host}:{self.core.config.web.port}",
                },
                "media": {
                    "asr_provider": asr,
                    "asr_label": asr_labels.get(asr, asr),
                    "ocr_provider": ocr,
                    "ocr_label": ocr_labels.get(ocr, ocr),
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

    async def model_health(self) -> dict[str, str]:
        if self.analysis_mode != "provider":
            return {"status": "inactive", "message": "当前分析方式不使用后台模型"}
        settings = self.core.config.llm
        api_key = get_secret(settings.api_key_env)
        if not llm_is_configured(settings, api_key):
            return {"status": "unconfigured", "message": "后台模型尚未配置"}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(
                    f"{settings.base_url.rstrip('/')}/models",
                    headers=headers,
                )
            if response.status_code == 200:
                return {"status": "ready", "message": "模型服务可连接"}
            if response.status_code in {401, 403}:
                return {"status": "auth", "message": "模型服务拒绝授权"}
            if response.status_code in {404, 405}:
                return {"status": "unknown", "message": "模型接口未提供状态检查"}
            return {"status": "unavailable", "message": f"模型服务返回 HTTP {response.status_code}"}
        except httpx.HTTPError:
            return {"status": "unavailable", "message": "无法连接模型服务"}

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
