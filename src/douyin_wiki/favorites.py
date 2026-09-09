"""Favorites inventory and explicit dispatch into the existing capture pipeline."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING

from .errors import JobStateError
from .favorites_store import TERMINAL, FavoritesStore, capture_keys, eligible
from .models import CaptureRequest, JobStatus

if TYPE_CHECKING:
    from .service import DouyinWikiService

FAVORITES_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"


class FavoritesService:
    def __init__(self, core: DouyinWikiService, adapter):
        self.core = core
        self.database = core.database
        self.adapter = adapter
        self.store = FavoritesStore(core.database)

    def start(
        self, *, folder_ids=None, include_images=False, directory_only=False, gateway_context=None
    ):
        if folder_ids is not None:
            if not folder_ids or any(not isinstance(v, str) or not v.strip() for v in folder_ids):
                raise ValueError("指定收藏夹时必须提供非空的收藏夹 ID 列表")
            folder_ids = list(dict.fromkeys(folder_ids))
        if directory_only and folder_ids:
            raise ValueError("读取目录不能同时指定收藏夹")
        options = {
            "folder_ids": folder_ids,
            "include_images": bool(include_images),
            "directory_only": bool(directory_only),
        }
        job_id = self.store.create(
            CaptureRequest(share_text=FAVORITES_URL, gateway_context=gateway_context), options
        )
        return self.database.get_job(job_id)

    async def process(self, job):
        run = self.store.load(job.id)
        if run["confirmed"]:
            return self.refresh(job.id)
        self.database.update_job(job.id, status=JobStatus.INVENTORYING, progress=0)

        async def checkpoint(snapshot):
            await asyncio.to_thread(self.store.checkpoint, job.id, snapshot)

        snapshot = await self.adapter.inventory(
            folder_ids=run["options"].get("folder_ids"),
            directory_only=run["options"].get("directory_only", False),
            expected_account_id=run["snapshot"].get("account_id"),
            on_checkpoint=checkpoint,
        )
        await checkpoint(snapshot)
        directory_only = run["options"].get("directory_only")
        if directory_only:
            status = (
                JobStatus.COMPLETED
                if snapshot.folders_complete
                else JobStatus.COMPLETED_WITH_WARNINGS
            )
        else:
            status = JobStatus.NEEDS_SELECTION
        data = self.get(job.id)
        return self.database.update_job(
            job.id,
            status=status,
            progress=1 if directory_only else 0,
            result={
                "summary": data["summary"],
                "complete": data["complete"],
                "folders_complete": data["folders_complete"],
                "warnings": data["warnings"],
            },
            unlock=True,
        )

    def _items(self, job_id, run):
        rows = self.store.rows(job_id)
        with self.database.connect() as conn:
            entries = {
                r["video_id"]: r
                for r in conn.execute(
                    "SELECT e.* FROM entries e JOIN favorites_items f ON e.video_id=f.work_id "
                    "WHERE f.parent_id=?",
                    (job_id,),
                )
            }
            active = {}
            # Include globally queued captures so the preview agrees with dispatch-time dedupe.
            for r in conn.execute(
                "SELECT * FROM jobs WHERE kind='capture' AND status NOT IN (?,?,?) "
                "AND ?=0 ORDER BY created_at",
                (
                    JobStatus.COMPLETED.value,
                    JobStatus.COMPLETED_WITH_WARNINGS.value,
                    JobStatus.FAILED.value,
                    int(run["confirmed"]),
                ),
            ):
                child = self.database._job_from_row(r)
                if child.status not in TERMINAL:
                    for key in capture_keys(child.request, child.artifacts):
                        active.setdefault(key, child)
            children = {
                r["id"]: self.database._job_from_row(r)
                for r in conn.execute(
                    """SELECT j.* FROM jobs j JOIN favorites_items f ON f.child_id=j.id
                       WHERE f.parent_id=?""",
                    (job_id,),
                )
            }
        items = []
        for row in rows:
            work = row["work"]
            child = children.get(row["child_id"])
            entry = entries.get(row["work_id"])
            entry_id = None
            if entry is not None and self._entry_intact(entry):
                entry_id = entry["id"]
            state = "pending"
            can_import = eligible(work, run["options"])
            if work["source_kind"] == "article":
                state = "unsupported"
            elif not work.get("available", True):
                state = "unavailable"
            elif child is not None:
                if child.status == JobStatus.FAILED:
                    state = "failed"
                elif child.status in TERMINAL:
                    state = "completed"
                    entry_id = child.result.get("entry_id") or entry_id
                else:
                    state = "active"
            elif entry_id:
                state = "imported"
            elif not can_import or not row["selected"]:
                state = "excluded"
            elif not run["confirmed"]:
                child = active.get(row["work_id"]) or active.get(work["canonical_url"])
                if child:
                    state = "active"
            item = {
                **work,
                "selected": bool(row["selected"]) and can_import,
                "disposition": state,
                "job_id": child.id if child else None,
                "entry_id": entry_id,
                "error_message": child.error_message if child else None,
                "job_status": child.status.value if child else None,
                "job_updated_at": child.updated_at.isoformat() if child else None,
            }
            items.append(item)
        return items

    def _entry_intact(self, row) -> bool:
        return self.core._entry_documents_intact(self.database._entry_from_row(row))

    @staticmethod
    def _summary(items, options):
        counts = Counter(i["disposition"] for i in items)
        result = {
            name: counts[name]
            for name in (
                "imported",
                "active",
                "completed",
                "failed",
                "excluded",
                "unsupported",
                "unavailable",
            )
        }
        result.update(
            discovered=len(items),
            eligible=sum(eligible(i, options) for i in items),
            selected=sum(i["selected"] and i["disposition"] != "imported" for i in items),
        )
        result["child_status_counts"] = dict(
            Counter(i["job_status"] for i in items if i["job_id"] and i["selected"])
        )
        return result

    def get(self, job_id: str, *, page=1, limit=50, folder_id=None, query=""):
        if page < 1 or not 1 <= limit <= 1000:
            raise ValueError("page 必须大于零，limit 必须在 1 到 1000 之间")
        run = self.store.load(job_id)
        job = self.database.get_job(job_id)
        items = self._items(job_id, run)
        summary = self._summary(items, run["options"])
        if run["confirmed"]:
            job = self._refresh_with(job_id, run, items, summary)
        filtered = [
            i
            for i in items
            if (not folder_id or folder_id in i.get("folder_ids", []))
            and (not query or query.casefold() in (i["title"] + " " + i["author"]).casefold())
        ]
        snapshot = run["snapshot"]
        start = (page - 1) * limit
        return {
            "job_id": job_id,
            "status": job.status.value,
            "progress": job.progress,
            **run["options"],
            "account_id": snapshot.get("account_id"),
            "nickname": snapshot.get("nickname", ""),
            "complete": snapshot.get("complete", False),
            "folders_complete": snapshot.get("folders_complete", False),
            "warnings": snapshot.get("warnings", []),
            "folders": snapshot.get("folders", []),
            "items": filtered[start : start + limit],
            "total": len(filtered),
            "page": page,
            "limit": limit,
            "has_more": start + limit < len(filtered),
            "summary": summary,
            "analysis_mode": self.core.config.analysis_mode.value,
            "error_message": job.error_message,
            "error_code": job.error_code,
            "auth_scope": job.result.get("auth_scope"),
            "confirmed": run["confirmed"],
        }

    def history(self, *, limit=50):
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        return [self.get(job_id, limit=1) for job_id in self.store.parent_ids(limit=limit)]

    def select(self, job_id: str, *, selected: bool, work_ids=None, folder_id=None):
        self.store.select(job_id, selected=selected, work_ids=work_ids, folder_id=folder_id)
        return self.get(job_id)

    def confirm(self, job_id: str, *, accept_partial=False):
        self.store.confirm(job_id, accept_partial=accept_partial, entry_intact=self._entry_intact)
        return self.refresh(job_id)

    def _refresh_with(self, job_id, run, items, summary):
        current = self.database.get_job(job_id)
        if not run["confirmed"]:
            return current
        children = [i for i in items if i["job_id"] and i["selected"]]
        finished = sum(i["disposition"] in {"failed", "completed"} for i in children)
        warnings = (
            not run["snapshot"].get("complete")
            or not run["snapshot"].get("folders_complete")
            or bool(run["snapshot"].get("warnings"))
            or summary["failed"] > 0
            or any(i["job_status"] == JobStatus.COMPLETED_WITH_WARNINGS.value for i in children)
        )
        if finished < len(children):
            status = JobStatus.MONITORING
            progress = finished / len(children)
        else:
            status = JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED
            progress = 1
        result = {
            "summary": summary,
            "child_status_counts": summary["child_status_counts"],
            "complete": run["snapshot"].get("complete", False),
            "warnings": run["snapshot"].get("warnings", []),
            "accept_partial": run["accept_partial"],
            "child_job_ids": [i["job_id"] for i in children],
        }
        if current.status == status and current.result == result and current.progress == progress:
            return current
        return self.database.update_job(
            job_id,
            status=status,
            progress=progress,
            result=result,
            unlock=True,
            expected_child_updates={i["job_id"]: i["job_updated_at"] for i in children},
        )

    def refresh(self, job_id: str):
        run = self.store.load(job_id)
        items = self._items(job_id, run)
        return self._refresh_with(job_id, run, items, self._summary(items, run["options"]))

    def refresh_all(self) -> None:
        for job_id in self.store.parent_ids(needs_refresh=True):
            self.refresh(job_id)

    def refresh_for_child(self, child_id: str) -> None:
        for job_id in self.store.parent_ids(child_id=child_id):
            self.refresh(job_id)

    def retry_failed(self, job_id: str):
        run = self.store.load(job_id)
        if not run["confirmed"]:
            return self.core.retry_job(job_id)
        for row in self.store.rows(job_id):
            if (
                row["child_id"]
                and self.database.get_job(row["child_id"]).status == JobStatus.FAILED
            ):
                try:
                    self.core.retry_job(row["child_id"])
                except JobStateError:
                    if self.database.get_job(row["child_id"]).status == JobStatus.FAILED:
                        raise
        return self.refresh(job_id)
