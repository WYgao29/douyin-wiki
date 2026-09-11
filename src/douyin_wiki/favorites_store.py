"""Durable favorites runs; these tables are operational history, not knowledge cache."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .adapters.share import extract_douyin_url, extract_video_id
from .database import Database
from .errors import BrowserAuthRequiredError, InvalidShareTextError, JobStateError
from .favorites_models import FavoriteInventory
from .models import CaptureRequest, JobStatus
from .time_utils import iso_now

FAVORITES_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites_runs (
    parent_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    options_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    confirmed INTEGER NOT NULL DEFAULT 0,
    accept_partial INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS favorites_items (
    parent_id TEXT NOT NULL REFERENCES favorites_runs(parent_id) ON DELETE CASCADE,
    work_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    selected INTEGER NOT NULL,
    child_id TEXT REFERENCES jobs(id),
    entry_id TEXT,
    PRIMARY KEY(parent_id, work_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_items_child ON favorites_items(child_id);
"""

TERMINAL = {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED}


def eligible(work: dict, options: dict) -> bool:
    type_allowed = work["source_kind"] == "video" or (
        work["source_kind"] == "image_note" and options.get("include_images")
    )
    folder_allowed = not options.get("folder_ids") or bool(
        set(options["folder_ids"]).intersection(work.get("folder_ids", []))
    )
    return bool(work.get("available", True) and type_allowed and folder_allowed)


def capture_keys(request: CaptureRequest, artifacts: dict) -> list[str]:
    keys = [request.share_text]
    try:
        if work_id := extract_video_id(extract_douyin_url(request.share_text)):
            keys.append(work_id)
    except InvalidShareTextError:
        pass
    for value in (
        artifacts.get("resolved", {}).get("video_id"),
        artifacts.get("metadata", {}).get("video_id"),
        artifacts.get("favorites_context", {}).get("work_id"),
    ):
        if value:
            keys.append(str(value))
    return keys


class FavoritesStore:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _run(conn: sqlite3.Connection, job_id: str) -> dict:
        row = conn.execute(
            """SELECT f.*, j.status FROM favorites_runs f JOIN jobs j ON j.id=f.parent_id
               WHERE f.parent_id=? AND j.kind='favorites_import'""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobStateError("该任务不是收藏导入任务")
        return {
            "options": json.loads(row["options_json"]),
            "snapshot": json.loads(row["snapshot_json"]),
            "confirmed": bool(row["confirmed"]),
            "accept_partial": bool(row["accept_partial"]),
            "status": row["status"],
        }

    def load(self, job_id: str) -> dict:
        with self.database.connect() as conn:
            return self._run(conn, job_id)

    def create(self, request: CaptureRequest, options: dict) -> str:
        job_id, now = uuid.uuid4().hex, iso_now()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO jobs(id,kind,status,progress,request_json,artifacts_json,
                   result_json,created_at,updated_at)
                   VALUES (?,'favorites_import',?,0,?,?,?, ?,?)""",
                (
                    job_id,
                    JobStatus.QUEUED.value,
                    request.model_dump_json(),
                    json.dumps({"favorites_options": options}),
                    "{}",
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO favorites_runs(parent_id,options_json) VALUES (?,?)",
                (job_id, json.dumps(options)),
            )
        return job_id

    def previous_work_ids(self, job_id: str) -> set[str]:
        current = self.load(job_id)
        folder_ids = current["options"].get("folder_ids")
        for other_id in self.parent_ids(limit=50):
            if other_id == job_id:
                continue
            other = self.load(other_id)
            if other["options"].get("directory_only"):
                continue
            if other["options"].get("folder_ids") != folder_ids:
                continue
            return {row["work_id"] for row in self.rows(other_id)}
        return set()

    def seed_from_previous(self, job_id: str, *, folder_ids=None) -> int:
        source_id = None
        for other_id in self.parent_ids(limit=50):
            if other_id == job_id:
                continue
            other = self.load(other_id)
            if other["options"].get("directory_only"):
                continue
            if other["options"].get("folder_ids") != folder_ids:
                continue
            source_id = other_id
            break
        if source_id is None:
            return 0
        rows = self.rows(source_id)
        if not rows:
            return 0
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index, row in enumerate(rows, start=1):
                conn.execute(
                    """INSERT OR IGNORE INTO favorites_items
                       (parent_id,work_id,position,data_json,selected)
                       VALUES (?,?,?,?,?)""",
                    (job_id, row["work_id"], index, row["data_json"], int(row["selected"])),
                )
            snapshot = json.loads(
                conn.execute(
                    "SELECT snapshot_json FROM favorites_runs WHERE parent_id=?",
                    (source_id,),
                ).fetchone()["snapshot_json"]
                or "{}"
            )
            conn.execute(
                "UPDATE favorites_runs SET snapshot_json=? WHERE parent_id=?",
                (json.dumps(snapshot, ensure_ascii=False), job_id),
            )
        return len(rows)

    def checkpoint(self, job_id: str, snapshot: FavoriteInventory) -> None:
        payload = snapshot.model_dump(mode="json")
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.database.assert_job_claim(conn, job_id)
            run = self._run(conn, job_id)
            previous = run["snapshot"].get("account_id")
            if previous and previous != snapshot.account_id:
                raise BrowserAuthRequiredError("专用浏览器账号已变化，请登录原账号或新建清点任务")
            if run["confirmed"]:
                raise JobStateError("已提交的收藏清单不能重新清点")
            works = payload.pop("works")
            wanted = set(run["options"].get("folder_ids") or [])
            if wanted:
                scoped = [w for w in works if wanted.intersection(w.get("folder_ids", []))]
                directory_ids = {f["id"] for f in payload.get("folders", [])}
                if len(scoped) != len(works) or not wanted.issubset(directory_ids):
                    payload["complete"] = False
                    payload["warnings"] = list(
                        dict.fromkeys(
                            [
                                *payload.get("warnings", []),
                                "部分作品或收藏夹无法确认属于所选范围，已排除范围外作品",
                            ]
                        )
                    )
                # Explicit contrary evidence invalidates earlier membership; unseen rows survive.
                for work in works:
                    if not wanted.intersection(work.get("folder_ids", [])):
                        conn.execute(
                            "UPDATE favorites_items SET data_json=?,selected=0 "
                            "WHERE parent_id=? AND work_id=?",
                            (json.dumps(work, ensure_ascii=False), job_id, work["work_id"]),
                        )
                works = scoped
            # Keep checkpointed works: visibility on resume is not a deletion signal.
            position = conn.execute(
                "SELECT COALESCE(MAX(position),0) FROM favorites_items WHERE parent_id=?", (job_id,)
            ).fetchone()[0]
            if not run["options"].get("directory_only"):
                for work in works:
                    position += 1
                    conn.execute(
                        """INSERT INTO favorites_items
                           (parent_id,work_id,position,data_json,selected)
                           VALUES (?,?,?,?,?) ON CONFLICT(parent_id,work_id) DO UPDATE
                           SET data_json=excluded.data_json""",
                        (
                            job_id,
                            work["work_id"],
                            position,
                            json.dumps(work, ensure_ascii=False),
                            int(eligible(work, run["options"])),
                        ),
                    )
            conn.execute(
                "UPDATE favorites_runs SET snapshot_json=? WHERE parent_id=?",
                (json.dumps(payload, ensure_ascii=False), job_id),
            )

    def rows(self, job_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM favorites_items WHERE parent_id=? ORDER BY position,work_id",
                (job_id,),
            ).fetchall()
        return [{**dict(r), "work": json.loads(r["data_json"])} for r in rows]

    def select(self, job_id: str, *, selected: bool, work_ids=None, folder_id=None) -> None:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._run(conn, job_id)
            if run["confirmed"] or run["status"] != JobStatus.NEEDS_SELECTION.value:
                raise JobStateError("只有待选择作品的收藏任务可以修改选择")
            rows = conn.execute(
                "SELECT work_id,data_json FROM favorites_items WHERE parent_id=?", (job_id,)
            ).fetchall()
            if folder_id and folder_id not in {f["id"] for f in run["snapshot"].get("folders", [])}:
                raise JobStateError("收藏夹不属于当前清单")
            if work_ids is not None and not set(work_ids).issubset({r["work_id"] for r in rows}):
                raise JobStateError("作品不属于当前清单")
            wanted = set(work_ids) if work_ids is not None else None
            targets = []
            for row in rows:
                work = json.loads(row["data_json"])
                if wanted is not None and row["work_id"] not in wanted:
                    continue
                if folder_id and folder_id not in work.get("folder_ids", []):
                    continue
                if selected and not eligible(work, run["options"]):
                    if wanted is not None:
                        raise JobStateError("该作品类型未选择、不支持或不可用")
                    continue
                targets.append((int(selected), job_id, row["work_id"]))
            conn.executemany(
                "UPDATE favorites_items SET selected=? WHERE parent_id=? AND work_id=?", targets
            )

    def confirm(self, job_id: str, *, accept_partial: bool, entry_intact) -> None:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._run(conn, job_id)
            if run["options"].get("directory_only"):
                raise JobStateError("收藏夹目录任务不能提交作品")
            if run["confirmed"]:
                return
            if run["status"] != JobStatus.NEEDS_SELECTION.value:
                raise JobStateError("清点结束后才能提交收藏作品")
            if not run["snapshot"].get("complete") and not accept_partial:
                raise JobStateError("清单未完整读取；仅导入已发现作品需显式 accept_partial=true")
            parent = conn.execute("SELECT request_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            request = CaptureRequest.model_validate_json(parent["request_json"])
            active = {}
            for row in conn.execute("SELECT * FROM jobs WHERE kind='capture' ORDER BY created_at"):
                if JobStatus(row["status"]) in TERMINAL:
                    continue
                child_request = CaptureRequest.model_validate_json(row["request_json"])
                artifacts = json.loads(row["artifacts_json"])
                for key in capture_keys(child_request, artifacts):
                    active.setdefault(key, row["id"])
            rows = conn.execute(
                "SELECT * FROM favorites_items WHERE parent_id=? AND selected=1 ORDER BY position",
                (job_id,),
            ).fetchall()
            for row in rows:
                work = json.loads(row["data_json"])
                if not eligible(work, run["options"]):
                    continue
                existing = conn.execute(
                    "SELECT * FROM entries WHERE video_id=?", (row["work_id"],)
                ).fetchone()
                if existing and entry_intact(existing):
                    conn.execute(
                        "UPDATE favorites_items SET entry_id=? WHERE parent_id=? AND work_id=?",
                        (existing["id"], job_id, row["work_id"]),
                    )
                    continue
                child_id = active.get(row["work_id"]) or active.get(work["canonical_url"])
                if not child_id:
                    child_id, now = uuid.uuid4().hex, iso_now()
                    child_request = CaptureRequest(
                        share_text=work["canonical_url"],
                        options=request.options,
                        gateway_context=request.gateway_context,
                    )
                    context = {
                        "parent_job_id": job_id,
                        "work_id": row["work_id"],
                        "batch_silent": True,
                    }
                    conn.execute(
                        """INSERT INTO jobs(id,kind,status,request_json,artifacts_json,
                           result_json,created_at,updated_at)
                   VALUES (?,'capture',?,?,?,'{}',?,?)""",
                        (
                            child_id,
                            JobStatus.QUEUED.value,
                            child_request.model_dump_json(),
                            json.dumps({"favorites_context": context}),
                            now,
                            now,
                        ),
                    )
                    active[row["work_id"]] = child_id
                conn.execute(
                    "UPDATE favorites_items SET child_id=? WHERE parent_id=? AND work_id=?",
                    (child_id, job_id, row["work_id"]),
                )
            conn.execute(
                "UPDATE favorites_runs SET confirmed=1,accept_partial=? WHERE parent_id=?",
                (int(accept_partial), job_id),
            )

    def parent_ids(
        self, *, child_id: str | None = None, limit: int | None = None, needs_refresh: bool = False
    ) -> list[str]:
        with self.database.connect() as conn:
            if child_id:
                rows = conn.execute(
                    "SELECT DISTINCT parent_id FROM favorites_items WHERE child_id=?", (child_id,)
                ).fetchall()
            else:
                sql = "SELECT f.parent_id FROM favorites_runs f JOIN jobs j ON j.id=f.parent_id"
                if needs_refresh:
                    sql += (
                        " WHERE f.confirmed=1 AND (j.status NOT IN "
                        "('completed','completed_with_warnings','failed') OR EXISTS ("
                        "SELECT 1 FROM favorites_items i JOIN jobs c ON c.id=i.child_id "
                        "WHERE i.parent_id=f.parent_id AND (c.updated_at>j.updated_at "
                        "OR c.status NOT IN ('completed','completed_with_warnings','failed'))))"
                    )
                sql += " ORDER BY j.created_at DESC"
                rows = conn.execute(
                    sql + (" LIMIT ?" if limit is not None else ""),
                    (limit,) if limit is not None else (),
                ).fetchall()
        return [r["parent_id"] for r in rows]
