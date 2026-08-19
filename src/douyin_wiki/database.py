from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import EntryNotFoundError, JobStateError
from .models import (
    CaptureRequest,
    EntryRecord,
    InspirationInput,
    JobEvent,
    JobRecord,
    JobStatus,
    ReminderCandidate,
    RetentionPolicy,
    ReviewIssue,
)
from .time_utils import iso_now, parse_datetime, utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'capture',
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    request_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    locked_at TEXT,
    lock_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    acknowledged_at TEXT,
    superseded_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_delivery
ON job_events(acknowledged_at, superseded_at, id);

CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    original_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    media_status TEXT NOT NULL DEFAULT 'present',
    retention TEXT NOT NULL DEFAULT 'temporary',
    media_expires_at TEXT,
    summary TEXT NOT NULL DEFAULT '',
    purposes_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_video_id ON entries(video_id);
CREATE INDEX IF NOT EXISTS idx_entries_expiry ON entries(retention, media_status, media_expires_at);

CREATE TABLE IF NOT EXISTS purposes (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purposes_entry ON purposes(entry_id);

CREATE TABLE IF NOT EXISTS review_issues (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_job ON review_issues(job_id, status);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    system_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_entry ON reminders(entry_id);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp_ms INTEGER,
    image_index INTEGER,
    purposes_text TEXT NOT NULL DEFAULT '',
    tags_text TEXT NOT NULL DEFAULT '',
    embedding_json TEXT,
    stale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_entry ON chunks(entry_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    entry_id UNINDEXED,
    text,
    purposes,
    tags,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS relations (
    source_entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    target_entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_entry_id, target_entry_id, relation_type)
);

CREATE TABLE IF NOT EXISTS maintenance_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

PRAGMA user_version=5;
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            chunk_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()
            }
            if "image_index" not in chunk_columns:
                conn.execute("ALTER TABLE chunks ADD COLUMN image_index INTEGER")
            job_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "lock_owner" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN lock_owner TEXT")
            if "lease_expires_at" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT")
            event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(job_events)").fetchall()
            }
            if "superseded_at" not in event_columns:
                conn.execute("ALTER TABLE job_events ADD COLUMN superseded_at TEXT")
            self._retire_undeliverable_events_conn(conn)
            conn.execute("PRAGMA user_version=5")

    def _retire_undeliverable_events_conn(self, conn: sqlite3.Connection) -> None:
        """Hide legacy route-less events and obsolete events from the delivery queue."""
        now = iso_now()
        rows = conn.execute(
            """SELECT e.id, e.job_id, j.request_json
               FROM job_events e JOIN jobs j ON j.id=e.job_id
               WHERE e.acknowledged_at IS NULL AND e.superseded_at IS NULL
               ORDER BY e.id DESC"""
        ).fetchall()
        latest_routed_job: set[str] = set()
        retire: list[int] = []
        for row in rows:
            request = CaptureRequest.model_validate_json(row["request_json"])
            if request.gateway_context is None or row["job_id"] in latest_routed_job:
                retire.append(row["id"])
            else:
                latest_routed_job.add(row["job_id"])
        if retire:
            conn.executemany(
                "UPDATE job_events SET superseded_at=? WHERE id=?",
                [(now, event_id) for event_id in retire],
            )

    def create_job(self, request: CaptureRequest) -> JobRecord:
        job_id = uuid.uuid4().hex
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, status, progress, request_json, artifacts_json, result_json,
                    created_at, updated_at)
                   VALUES (?, 'capture', ?, 0, ?, '{}', '{}', ?, ?)""",
                (job_id, JobStatus.QUEUED.value, request.model_dump_json(), now, now),
            )
        return self.get_job(job_id)

    def create_reanalysis_job(
        self,
        entry: EntryRecord,
        *,
        force: bool = False,
        gateway_context: Any = None,
        data: dict[str, Any] | None = None,
    ) -> JobRecord:
        """Queue analysis-only work while reusing immutable captured evidence."""
        request = CaptureRequest(
            share_text=entry.original_url,
            inspirations=entry.inspirations,
            gateway_context=gateway_context,
        )
        source = data or self.get_entry_data(entry.id)
        artifacts = {
            "reanalyze_entry_id": entry.id,
            "force": force,
            "metadata": source.get("metadata", {}),
            "transcript_raw": source.get("transcript_raw", []),
            "transcript_corrected": source.get("transcript_corrected")
            or source.get("transcript_raw", []),
            "ocr": source.get("ocr", []),
        }
        job_id = uuid.uuid4().hex
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, status, progress, request_json, artifacts_json, result_json,
                    created_at, updated_at)
                   VALUES (?, 'reanalyze', ?, 0, ?, ?, '{}', ?, ?)""",
                (
                    job_id,
                    JobStatus.QUEUED.value,
                    request.model_dump_json(),
                    json.dumps(artifacts, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobStateError(f"job not found: {job_id}")
        return self._job_from_row(row)

    def list_jobs(self, status: JobStatus | None = None, limit: int = 50) -> list[JobRecord]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._job_from_row(row) for row in rows]

    def claim_next_job(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 180,
    ) -> JobRecord | None:
        owner = worker_id or f"adhoc-{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM jobs WHERE status=? AND locked_at IS NULL
                   ORDER BY created_at LIMIT 1""",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            now_value = utc_now()
            now = now_value.isoformat()
            lease_expires_at = (now_value + timedelta(seconds=max(30, lease_seconds))).isoformat()
            changed = conn.execute(
                """UPDATE jobs SET locked_at=?, lock_owner=?, lease_expires_at=?, updated_at=?
                   WHERE id=? AND locked_at IS NULL AND status=?""",
                (
                    now,
                    owner,
                    lease_expires_at,
                    now,
                    row["id"],
                    JobStatus.QUEUED.value,
                ),
            ).rowcount
            if not changed:
                return None
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return self._job_from_row(row)

    def renew_job_lease(self, job_id: str, worker_id: str, *, lease_seconds: int = 180) -> bool:
        expires = (utc_now() + timedelta(seconds=max(30, lease_seconds))).isoformat()
        with self.connect() as conn:
            changed = conn.execute(
                """UPDATE jobs SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND lock_owner=? AND locked_at IS NOT NULL""",
                (expires, iso_now(), job_id, worker_id),
            ).rowcount
        return bool(changed)

    def recover_expired_jobs(self, now: datetime | None = None) -> int:
        """Requeue only abandoned in-flight jobs whose worker lease has expired."""
        timestamp = (now or datetime.now(UTC)).isoformat()
        recoverable = (
            JobStatus.QUEUED.value,
            JobStatus.RESOLVING.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.EXTRACTING.value,
            JobStatus.TRANSCRIBING.value,
            JobStatus.ANALYZING.value,
        )
        placeholders = ",".join("?" for _ in recoverable)
        with self.connect() as conn:
            result = conn.execute(
                f"""UPDATE jobs
                    SET status=?, locked_at=NULL, lock_owner=NULL, lease_expires_at=NULL,
                        updated_at=?
                    WHERE status IN ({placeholders})
                      AND locked_at IS NOT NULL
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (JobStatus.QUEUED.value, iso_now(), *recoverable, timestamp),
            )
        return result.rowcount

    def unlock_all_jobs(self) -> int:
        """Compatibility escape hatch. Runtime recovery must use recover_expired_jobs()."""
        with self.connect() as conn:
            result = conn.execute(
                """UPDATE jobs SET locked_at=NULL, lock_owner=NULL, lease_expires_at=NULL
                   WHERE locked_at IS NOT NULL"""
            )
        return result.rowcount

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        artifacts: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        unlock: bool = False,
    ) -> JobRecord:
        current = self.get_job(job_id)
        assignments = ["updated_at=?"]
        values: list[Any] = [iso_now()]
        if status is not None:
            assignments.append("status=?")
            values.append(status.value)
        if progress is not None:
            assignments.append("progress=?")
            values.append(max(0.0, min(1.0, progress)))
        if artifacts is not None:
            merged = {**current.artifacts, **artifacts}
            assignments.append("artifacts_json=?")
            values.append(json.dumps(merged, ensure_ascii=False))
        if result is not None:
            assignments.append("result_json=?")
            values.append(json.dumps(result, ensure_ascii=False))
        if error_code is not None:
            assignments.append("error_code=?")
            values.append(error_code)
        if error_message is not None:
            assignments.append("error_message=?")
            values.append(error_message)
        if unlock:
            assignments.extend(["locked_at=NULL", "lock_owner=NULL", "lease_expires_at=NULL"])
        values.append(job_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values)
            if (
                status is not None
                and status != current.status
                and current.request.gateway_context is not None
                and status
                in {
                    JobStatus.AWAITING_AGENT_ANALYSIS,
                    JobStatus.NEEDS_AUTH,
                    JobStatus.NEEDS_REVIEW,
                    JobStatus.WAITING_CONFIRMATION,
                    JobStatus.COMPLETED,
                    JobStatus.COMPLETED_WITH_WARNINGS,
                    JobStatus.FAILED,
                }
            ):
                event_result = result if result is not None else current.result
                event_time = iso_now()
                conn.execute(
                    """UPDATE job_events SET superseded_at=?
                       WHERE job_id=? AND acknowledged_at IS NULL AND superseded_at IS NULL""",
                    (event_time, job_id),
                )
                conn.execute(
                    """INSERT INTO job_events(job_id, status, result_json, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        job_id,
                        status.value,
                        json.dumps(event_result, ensure_ascii=False),
                        event_time,
                    ),
                )
        return self.get_job(job_id)

    def requeue_job(self, job_id: str, *, artifacts: dict[str, Any] | None = None) -> JobRecord:
        job = self.get_job(job_id)
        if job.status not in {
            JobStatus.AWAITING_AGENT_ANALYSIS,
            JobStatus.NEEDS_AUTH,
            JobStatus.NEEDS_REVIEW,
            JobStatus.WAITING_CONFIRMATION,
            JobStatus.FAILED,
        }:
            raise JobStateError(f"job {job_id} cannot be requeued from {job.status}")
        return self.update_job(
            job_id,
            status=JobStatus.QUEUED,
            artifacts=artifacts,
            error_code="",
            error_message="",
            unlock=True,
        )

    def list_job_events(
        self,
        *,
        after_event_id: int = 0,
        unacknowledged_only: bool = True,
        limit: int = 50,
    ) -> list[JobEvent]:
        query = "SELECT * FROM job_events WHERE id>?"
        params: list[Any] = [after_event_id]
        if unacknowledged_only:
            query += " AND acknowledged_at IS NULL AND superseded_at IS NULL"
        query += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            jobs = {
                row["id"]: row
                for row in conn.execute("SELECT id, request_json FROM jobs").fetchall()
            }
        events: list[JobEvent] = []
        for row in rows:
            request = CaptureRequest.model_validate_json(jobs[row["job_id"]]["request_json"])
            events.append(
                JobEvent(
                    id=row["id"],
                    job_id=row["job_id"],
                    status=JobStatus(row["status"]),
                    gateway_context=request.gateway_context,
                    result=json.loads(row["result_json"]),
                    created_at=parse_datetime(row["created_at"]),
                    acknowledged_at=parse_datetime(row["acknowledged_at"])
                    if row["acknowledged_at"]
                    else None,
                    superseded_at=parse_datetime(row["superseded_at"])
                    if row["superseded_at"]
                    else None,
                )
            )
        return events

    def acknowledge_job_event(self, event_id: int) -> JobEvent:
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM job_events WHERE id=?", (event_id,)).fetchone()
            if exists is None:
                raise JobStateError(f"job event not found: {event_id}")
            conn.execute(
                """UPDATE job_events SET acknowledged_at=?
                   WHERE id=? AND acknowledged_at IS NULL""",
                (iso_now(), event_id),
            )
        return self.get_job_event(event_id)

    def get_job_event(self, event_id: int) -> JobEvent:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise JobStateError(f"job event not found: {event_id}")
            request_row = conn.execute(
                "SELECT request_json FROM jobs WHERE id=?", (row["job_id"],)
            ).fetchone()
        request = CaptureRequest.model_validate_json(request_row["request_json"])
        return JobEvent(
            id=row["id"],
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            gateway_context=request.gateway_context,
            result=json.loads(row["result_json"]),
            created_at=parse_datetime(row["created_at"]),
            acknowledged_at=parse_datetime(row["acknowledged_at"])
            if row["acknowledged_at"]
            else None,
            superseded_at=parse_datetime(row["superseded_at"]) if row["superseded_at"] else None,
        )

    def emit_job_event(
        self, job_id: str, status: JobStatus, result: dict[str, Any]
    ) -> JobEvent | None:
        job = self.get_job(job_id)
        if job.request.gateway_context is None:
            return None
        with self.connect() as conn:
            event_time = iso_now()
            conn.execute(
                """UPDATE job_events SET superseded_at=?
                   WHERE job_id=? AND acknowledged_at IS NULL AND superseded_at IS NULL""",
                (event_time, job_id),
            )
            cursor = conn.execute(
                """INSERT INTO job_events(job_id, status, result_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (job_id, status.value, json.dumps(result, ensure_ascii=False), event_time),
            )
            event_id = cursor.lastrowid
        if event_id is None:  # pragma: no cover - SQLite always provides this value
            raise JobStateError("job event could not be created")
        return self.get_job_event(event_id)

    def find_entry_by_video_id(self, video_id: str) -> EntryRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM entries WHERE video_id=?", (video_id,)).fetchone()
        return self._entry_from_row(row) if row else None

    def get_entry(self, entry_id: str) -> EntryRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise EntryNotFoundError(f"entry not found: {entry_id}")
        return self._entry_from_row(row)

    def get_entry_data(self, entry_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise EntryNotFoundError(f"entry not found: {entry_id}")
        return json.loads(row["data_json"])

    def list_entries(self) -> list[EntryRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM entries ORDER BY created_at DESC").fetchall()
        return [self._entry_from_row(row) for row in rows]

    def upsert_entry(self, entry: EntryRecord, data: dict[str, Any]) -> EntryRecord:
        values = self._entry_values(entry, data)
        with self.connect() as conn:
            self._upsert_entry_conn(conn, values)
        return self.get_entry(entry.id)

    @staticmethod
    def _entry_values(entry: EntryRecord, data: dict[str, Any]) -> tuple[Any, ...]:
        return (
            entry.id,
            entry.video_id,
            entry.title,
            entry.original_url,
            entry.canonical_url,
            entry.raw_path,
            entry.source_path,
            entry.status,
            entry.media_status,
            entry.retention.value,
            entry.media_expires_at.isoformat() if entry.media_expires_at else None,
            entry.summary,
            json.dumps(
                [item.model_dump(mode="json") for item in entry.inspirations],
                ensure_ascii=False,
            ),
            json.dumps(entry.tags, ensure_ascii=False),
            json.dumps(data, ensure_ascii=False),
            entry.created_at.isoformat(),
            entry.updated_at.isoformat(),
        )

    @staticmethod
    def _upsert_entry_conn(conn: sqlite3.Connection, values: tuple[Any, ...]) -> None:
        conn.execute(
            """INSERT INTO entries
                (id, video_id, title, original_url, canonical_url, raw_path, source_path, status,
                 media_status, retention, media_expires_at, summary,
                 purposes_json, tags_json, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title, canonical_url=excluded.canonical_url,
                  status=excluded.status, media_status=excluded.media_status,
                  retention=excluded.retention, media_expires_at=excluded.media_expires_at,
                  summary=excluded.summary,
                  purposes_json=excluded.purposes_json, tags_json=excluded.tags_json,
                  data_json=excluded.data_json, updated_at=excluded.updated_at""",
            values,
        )

    def persist_entry_bundle(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        chunks: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reminders: list[ReminderCandidate],
    ) -> EntryRecord:
        """Atomically replace one entry and every rebuildable SQLite projection."""
        now = iso_now()
        with self.connect() as conn:
            self._upsert_entry_conn(conn, self._entry_values(entry, data))
            old_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE entry_id=?", (entry.id,)
                ).fetchall()
            ]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
            conn.execute("DELETE FROM chunks WHERE entry_id=?", (entry.id,))
            for chunk in chunks:
                chunk_id = chunk.get("id") or uuid.uuid4().hex
                embedding = chunk.get("embedding")
                conn.execute(
                    """INSERT INTO chunks
                       (id, entry_id, kind, text, timestamp_ms, image_index,
                        purposes_text, tags_text, embedding_json, stale, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        entry.id,
                        chunk.get("kind", "text"),
                        chunk["text"],
                        chunk.get("timestamp_ms"),
                        chunk.get("image_index"),
                        chunk.get("purposes_text", ""),
                        chunk.get("tags_text", ""),
                        json.dumps(embedding) if embedding is not None else None,
                        int(chunk.get("stale", False)),
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO chunks_fts(chunk_id, entry_id, text, purposes, tags)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        entry.id,
                        chunk["text"],
                        chunk.get("purposes_text", ""),
                        chunk.get("tags_text", ""),
                    ),
                )
            conn.execute("DELETE FROM relations WHERE source_entry_id=?", (entry.id,))
            conn.executemany(
                """INSERT OR REPLACE INTO relations
                   (source_entry_id, target_entry_id, relation_type, reason, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        entry.id,
                        relation["target_entry_id"],
                        relation.get("relation_type", "related"),
                        relation.get("reason", "语义相关"),
                        float(relation.get("confidence", 0)),
                        now,
                    )
                    for relation in relations
                ],
            )
            conn.execute(
                "DELETE FROM reminders WHERE entry_id=? AND status='candidate'", (entry.id,)
            )
            created_ids = {
                ReminderCandidate.model_validate_json(row["data_json"]).id
                for row in conn.execute(
                    "SELECT data_json FROM reminders WHERE entry_id=? AND status='created'",
                    (entry.id,),
                ).fetchall()
            }
            conn.executemany(
                """INSERT INTO reminders
                   (id, entry_id, data_json, status, system_id, updated_at)
                   VALUES (?, ?, ?, 'candidate', NULL, ?)""",
                [
                    (
                        self._reminder_storage_id(entry.id, reminder.id),
                        entry.id,
                        reminder.model_dump_json(),
                        now,
                    )
                    for reminder in reminders
                    if reminder.id not in created_ids
                ],
            )
        return self.get_entry(entry.id)

    def clear_knowledge_cache(self) -> None:
        """Remove only rebuildable knowledge projections; keep jobs and maintenance history."""
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM entries")
            conn.execute("DELETE FROM index_metadata")

    def get_index_metadata(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM index_metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_index_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO index_metadata(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_at=excluded.updated_at""",
                (key, value, iso_now()),
            )

    def add_inspiration(self, entry_id: str, inspiration: InspirationInput) -> EntryRecord:
        entry = self.get_entry(entry_id)
        if inspiration in entry.inspirations:
            return entry
        inspiration_id = uuid.uuid4().hex
        now = iso_now()
        inspirations = [*entry.inspirations, inspiration]
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO purposes(id, entry_id, data_json, created_at) VALUES (?, ?, ?, ?)",
                (inspiration_id, entry_id, inspiration.model_dump_json(), now),
            )
            conn.execute(
                "UPDATE entries SET purposes_json=?, updated_at=? WHERE id=?",
                (
                    json.dumps(
                        [item.model_dump(mode="json") for item in inspirations],
                        ensure_ascii=False,
                    ),
                    now,
                    entry_id,
                ),
            )
        return self.get_entry(entry_id)

    def add_purpose(self, entry_id: str, purpose: InspirationInput) -> EntryRecord:
        """Deprecated compatibility alias; use add_inspiration."""
        return self.add_inspiration(entry_id, purpose)

    def migrate_inspiration_vocabulary(self) -> int:
        """Rename user-facing JSON keys without changing the legacy SQLite schema."""
        changed = 0
        with self.connect() as conn:
            rows = conn.execute("SELECT id, data_json FROM entries").fetchall()
            for row in rows:
                data = json.loads(row["data_json"])
                row_changed = False
                if "purposes" in data and "inspirations" not in data:
                    data["inspirations"] = data.pop("purposes")
                    row_changed = True
                if analysis := data.get("analysis"):
                    rendered = json.dumps(analysis, ensure_ascii=False)
                    migrated = rendered.replace("用户用途：", "用户灵感：")
                    if migrated != rendered:
                        data["analysis"] = json.loads(migrated)
                        row_changed = True
                if relations := data.get("relations"):
                    rendered = json.dumps(relations, ensure_ascii=False)
                    migrated = rendered.replace("用途、摘要", "灵感、摘要")
                    if migrated != rendered:
                        data["relations"] = json.loads(migrated)
                        row_changed = True
                if row_changed:
                    conn.execute(
                        "UPDATE entries SET data_json=?, updated_at=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), iso_now(), row["id"]),
                    )
                    changed += 1
            conn.execute("UPDATE chunks SET kind='inspiration' WHERE kind='purpose'")
        return changed

    def remove_external_validation(self) -> dict[str, int]:
        changed_entries = 0
        changed_jobs = 0
        with self.connect() as conn:
            entry_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()
            }
            has_verification_column = "verification_status" in entry_columns
            has_fact_checks_table = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_checks'"
                ).fetchone()
                is not None
            )
            rows = conn.execute("SELECT id, data_json FROM entries").fetchall()
            for row in rows:
                data = json.loads(row["data_json"])
                changed = bool(data.pop("fact_checks", None))
                if analysis := data.get("analysis"):
                    rendered = json.dumps(analysis, ensure_ascii=False)
                    migrated = rendered.replace(
                        "具体版本和参数仍处于外部未核验状态。",
                        "具体版本和参数需结合原始配方与实际冲煮进一步确认。",
                    )
                    if migrated != rendered:
                        data["analysis"] = json.loads(migrated)
                        changed = True
                for claim in data.get("analysis", {}).get("claims", []):
                    if "verification_status" in claim:
                        claim.pop("verification_status")
                        changed = True
                if changed:
                    changed_entries += 1
                    conn.execute(
                        "UPDATE entries SET data_json=?, updated_at=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), iso_now(), row["id"]),
                    )
            removed_checks = 0
            if has_fact_checks_table:
                removed_checks = conn.execute("DELETE FROM fact_checks").rowcount
                conn.execute("DROP TABLE fact_checks")
            jobs = conn.execute(
                "SELECT id, status, artifacts_json, result_json FROM jobs"
            ).fetchall()
            for job in jobs:
                artifacts = json.loads(job["artifacts_json"])
                result = json.loads(job["result_json"])
                changed = "pending_fact_checks" in result
                result.pop("pending_fact_checks", None)
                if warnings := result.get("warnings"):
                    filtered = [item for item in warnings if "核验" not in item]
                    if filtered != warnings:
                        result["warnings"] = filtered
                        changed = True
                for claim in artifacts.get("analysis", {}).get("claims", []):
                    if "verification_status" in claim:
                        claim.pop("verification_status")
                        changed = True
                status = job["status"]
                if status == JobStatus.COMPLETED_WITH_WARNINGS.value and not result.get("warnings"):
                    status = JobStatus.COMPLETED.value
                    changed = True
                if changed:
                    conn.execute(
                        """UPDATE jobs SET status=?, artifacts_json=?, result_json=?,
                           updated_at=? WHERE id=?""",
                        (
                            status,
                            json.dumps(artifacts, ensure_ascii=False),
                            json.dumps(result, ensure_ascii=False),
                            iso_now(),
                            job["id"],
                        ),
                    )
                    changed_jobs += 1
            if has_verification_column:
                conn.execute("ALTER TABLE entries DROP COLUMN verification_status")
        return {
            "entries": changed_entries,
            "jobs": changed_jobs,
            "removed_checks": removed_checks,
        }

    def replace_review_issues(self, job_id: str, issues: list[ReviewIssue]) -> None:
        now = iso_now()
        with self.connect() as conn:
            conn.execute("DELETE FROM review_issues WHERE job_id=?", (job_id,))
            conn.executemany(
                """INSERT INTO review_issues(id, job_id, data_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'open', ?, ?)""",
                [
                    (
                        f"{job_id}:{issue.id}",
                        job_id,
                        issue.model_dump_json(),
                        now,
                        now,
                    )
                    for issue in issues
                ],
            )

    def get_review_issues(self, job_id: str, *, open_only: bool = False) -> list[ReviewIssue]:
        query = "SELECT data_json FROM review_issues WHERE job_id=?"
        params: list[Any] = [job_id]
        if open_only:
            query += " AND status='open'"
        query += " ORDER BY created_at"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ReviewIssue.model_validate_json(row["data_json"]) for row in rows]

    def resolve_review_issues(self, job_id: str, resolutions: dict[str, str]) -> list[ReviewIssue]:
        issues = self.get_review_issues(job_id)
        known = {issue.id for issue in issues}
        unknown = set(resolutions) - known
        if unknown:
            raise JobStateError(f"unknown review issue ids: {sorted(unknown)}")
        now = iso_now()
        resolved: list[ReviewIssue] = []
        with self.connect() as conn:
            for issue in issues:
                issue.resolution = resolutions.get(issue.id, issue.raw_text)
                conn.execute(
                    """UPDATE review_issues SET data_json=?, status='resolved', updated_at=?
                       WHERE job_id=? AND (id=? OR id=?)""",
                    (
                        issue.model_dump_json(),
                        now,
                        job_id,
                        issue.id,
                        f"{job_id}:{issue.id}",
                    ),
                )
                resolved.append(issue)
        return resolved

    def replace_reminders(self, entry_id: str, reminders: list[ReminderCandidate]) -> None:
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM reminders WHERE entry_id=? AND status='candidate'", (entry_id,)
            )
            conn.executemany(
                """INSERT OR REPLACE INTO reminders
                   (id, entry_id, data_json, status, system_id, updated_at)
                   VALUES (?, ?, ?, 'candidate', NULL, ?)""",
                [
                    (
                        self._reminder_storage_id(entry_id, reminder.id),
                        entry_id,
                        reminder.model_dump_json(),
                        now,
                    )
                    for reminder in reminders
                ],
            )

    def get_reminder(
        self, entry_id: str, reminder_id: str
    ) -> tuple[ReminderCandidate, str, str | None]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reminders WHERE entry_id=? AND id IN (?, ?)",
                (entry_id, reminder_id, self._reminder_storage_id(entry_id, reminder_id)),
            ).fetchone()
        if row is None:
            raise EntryNotFoundError(f"reminder not found: {reminder_id}")
        return (
            ReminderCandidate.model_validate_json(row["data_json"]),
            row["status"],
            row["system_id"],
        )

    def mark_reminder_created(self, entry_id: str, reminder_id: str, system_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE reminders SET status='created', system_id=?, updated_at=?
                   WHERE entry_id=? AND id IN (?, ?)""",
                (
                    system_id,
                    iso_now(),
                    entry_id,
                    reminder_id,
                    self._reminder_storage_id(entry_id, reminder_id),
                ),
            )

    def claim_reminder_creation(
        self, entry_id: str, reminder_id: str, candidate: ReminderCandidate
    ) -> bool:
        storage_id = self._reminder_storage_id(entry_id, reminder_id)
        stale_before = (utc_now() - timedelta(minutes=10)).isoformat()
        with self.connect() as conn:
            changed = conn.execute(
                """UPDATE reminders SET status='creating', data_json=?, updated_at=?
                   WHERE entry_id=? AND id IN (?, ?)
                     AND (status='candidate' OR (status='creating' AND updated_at<=?))""",
                (
                    candidate.model_dump_json(),
                    iso_now(),
                    entry_id,
                    reminder_id,
                    storage_id,
                    stale_before,
                ),
            ).rowcount
            if changed:
                return True
            row = conn.execute(
                "SELECT status FROM reminders WHERE entry_id=? AND id IN (?, ?)",
                (entry_id, reminder_id, storage_id),
            ).fetchone()
        if row and row["status"] == "created":
            return False
        raise JobStateError("该提醒正在由另一个进程创建，请稍后重试")

    def release_reminder_creation(self, entry_id: str, reminder_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE reminders SET status='candidate', updated_at=?
                   WHERE entry_id=? AND id IN (?, ?) AND status='creating'""",
                (
                    iso_now(),
                    entry_id,
                    reminder_id,
                    self._reminder_storage_id(entry_id, reminder_id),
                ),
            )

    @staticmethod
    def _reminder_storage_id(entry_id: str, reminder_id: str) -> str:
        """Namespace model-provided reminder IDs without changing the public ID."""
        return f"{entry_id}:{reminder_id}"

    def replace_chunks(self, entry_id: str, chunks: list[dict[str, Any]]) -> None:
        now = iso_now()
        with self.connect() as conn:
            old_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE entry_id=?", (entry_id,)
                ).fetchall()
            ]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
            conn.execute("DELETE FROM chunks WHERE entry_id=?", (entry_id,))
            for chunk in chunks:
                chunk_id = chunk.get("id") or uuid.uuid4().hex
                embedding = chunk.get("embedding")
                conn.execute(
                    """INSERT INTO chunks
                       (id, entry_id, kind, text, timestamp_ms, image_index,
                        purposes_text, tags_text, embedding_json, stale, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        entry_id,
                        chunk.get("kind", "text"),
                        chunk["text"],
                        chunk.get("timestamp_ms"),
                        chunk.get("image_index"),
                        chunk.get("purposes_text", ""),
                        chunk.get("tags_text", ""),
                        json.dumps(embedding) if embedding is not None else None,
                        int(chunk.get("stale", False)),
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO chunks_fts(chunk_id, entry_id, text, purposes, tags)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        chunk_id,
                        entry_id,
                        chunk["text"],
                        chunk.get("purposes_text", ""),
                        chunk.get("tags_text", ""),
                    ),
                )

    def fetch_chunks(self, *, include_stale: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM chunks"
        if not include_stale:
            query += " WHERE stale=0"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]

    def entry_chunk_count(self, entry_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM chunks WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return int(row["count"])

    def fts_search(
        self, query: str, *, include_stale: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        stale_clause = "" if include_stale else "AND c.stale=0"
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    f"""SELECT c.*, bm25(chunks_fts, 0, 0, 1.0, 1.6, 0.8) AS rank
                        FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id
                        WHERE chunks_fts MATCH ? {stale_clause}
                        ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                escaped = query.replace("%", "\\%").replace("_", "\\_")
                rows = conn.execute(
                    f"""SELECT c.*, 0 AS rank FROM chunks c
                        WHERE (c.text LIKE ? ESCAPE '\\' OR c.purposes_text LIKE ? ESCAPE '\\')
                        {stale_clause} LIMIT ?""",
                    (f"%{escaped}%", f"%{escaped}%", limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def replace_relations(self, source_entry_id: str, relations: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM relations WHERE source_entry_id=?", (source_entry_id,))
            conn.executemany(
                """INSERT OR REPLACE INTO relations
                   (source_entry_id, target_entry_id, relation_type, reason, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        source_entry_id,
                        relation["target_entry_id"],
                        relation.get("relation_type", "related"),
                        relation.get("reason", "语义相关"),
                        float(relation.get("confidence", 0)),
                        iso_now(),
                    )
                    for relation in relations
                ],
            )

    def get_relations(self, entry_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.*, e.title AS target_title, e.source_path AS target_source_path
                   FROM relations r JOIN entries e ON e.id=r.target_entry_id
                   WHERE r.source_entry_id=? ORDER BY r.confidence DESC""",
                (entry_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def entries_with_expired_media(self, now: datetime | None = None) -> list[EntryRecord]:
        timestamp = (now or utc_now()).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entries WHERE retention='temporary' AND media_status='present'
                   AND media_expires_at IS NOT NULL AND media_expires_at <= ?""",
                (timestamp,),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def mark_media_removed(self, entry_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE entries SET media_status='removed', updated_at=? WHERE id=?",
                (iso_now(), entry_id),
            )

    def mark_stale_claims(self, now: datetime | None = None) -> tuple[int, list[str]]:
        current = now or utc_now()
        changed = 0
        entry_ids: list[str] = []
        with self.connect() as conn:
            rows = conn.execute("SELECT id, data_json FROM entries").fetchall()
            for row in rows:
                data = json.loads(row["data_json"])
                analysis = data.get("analysis", {})
                claims = analysis.get("claims", [])
                atoms = analysis.get("knowledge_atoms", [])
                changed_keys: set[str] = set()
                for item in [*claims, *atoms]:
                    valid_until = parse_datetime(item.get("valid_until"))
                    review_after = parse_datetime(item.get("review_after"))
                    expiry = min(
                        (value for value in (valid_until, review_after) if value is not None),
                        default=None,
                    )
                    if expiry and expiry <= current and not item.get("stale"):
                        item["stale"] = True
                        changed_keys.add(str(item.get("id") or item.get("text") or id(item)))
                if changed_keys:
                    changed += len(changed_keys)
                    entry_ids.append(row["id"])
                    for claim in claims:
                        if claim.get("stale"):
                            conn.execute(
                                """UPDATE chunks SET stale=1
                                   WHERE entry_id=? AND kind='claim' AND text=?""",
                                (row["id"], claim.get("text", "")),
                            )
                    for atom in atoms:
                        if atom.get("stale"):
                            conn.execute(
                                "UPDATE chunks SET stale=1 WHERE id=?",
                                (f"{row['id']}:atom:{atom.get('id')}",),
                            )
                    conn.execute(
                        "UPDATE entries SET data_json=?, updated_at=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), iso_now(), row["id"]),
                    )
        return changed, entry_ids

    def record_maintenance(self, kind: str, report: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO maintenance_runs(id, kind, report_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (run_id, kind, json.dumps(report, ensure_ascii=False), iso_now()),
            )
        return run_id

    def last_maintenance_at(self, kind: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT created_at FROM maintenance_runs
                   WHERE kind=? ORDER BY created_at DESC LIMIT 1""",
                (kind,),
            ).fetchone()
        return parse_datetime(row["created_at"]) if row else None

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            kind=row["kind"],
            status=JobStatus(row["status"]),
            progress=row["progress"],
            request=CaptureRequest.model_validate_json(row["request_json"]),
            artifacts=json.loads(row["artifacts_json"]),
            result=json.loads(row["result_json"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> EntryRecord:
        return EntryRecord(
            id=row["id"],
            video_id=row["video_id"],
            title=row["title"],
            original_url=row["original_url"],
            canonical_url=row["canonical_url"],
            raw_path=row["raw_path"],
            source_path=row["source_path"],
            status=row["status"],
            media_status=row["media_status"],
            retention=RetentionPolicy(row["retention"]),
            media_expires_at=parse_datetime(row["media_expires_at"]),
            summary=row["summary"],
            inspirations=[
                InspirationInput.model_validate(item) for item in json.loads(row["purposes_json"])
            ],
            tags=json.loads(row["tags_json"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )
