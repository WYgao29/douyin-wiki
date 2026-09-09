from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Collection, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import EntryNotFoundError, JobLeaseLostError, JobStateError
from .models import (
    CaptureRequest,
    ChatMessage,
    ChatSession,
    Citation,
    CreatorInventoryItem,
    CreatorInventoryWork,
    CreatorProfile,
    CreatorRecord,
    CreatorWorkAvailability,
    CreatorWorkDecision,
    CreatorWorkRecord,
    EntryRecord,
    InspirationInput,
    JobEvent,
    JobRecord,
    JobStatus,
    ReminderCandidate,
    ResearchTopic,
    RetentionPolicy,
    ReviewIssue,
    SourceKind,
    SourceRevision,
    TopicArtifact,
    TopicSource,
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
    favorite INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS research_topics (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_topics_updated
ON research_topics(updated_at DESC);

CREATE TABLE IF NOT EXISTS topic_sources (
    topic_id TEXT NOT NULL REFERENCES research_topics(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    source_revision TEXT NOT NULL,
    PRIMARY KEY(topic_id, entry_id),
    UNIQUE(topic_id, position)
);
CREATE INDEX IF NOT EXISTS idx_topic_sources_scope
ON topic_sources(topic_id, enabled, position);

CREATE TABLE IF NOT EXISTS topic_artifacts (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES research_topics(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    source_revisions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'current' CHECK(status IN ('current', 'needs_update')),
    model TEXT,
    prompt_version TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    user_authored INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_artifacts_topic
ON topic_artifacts(topic_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS creators (
    id TEXT PRIMARY KEY,
    sec_uid TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    original_url TEXT NOT NULL,
    nickname TEXT NOT NULL,
    folder_path TEXT NOT NULL UNIQUE,
    uid TEXT,
    unique_id TEXT,
    signature TEXT NOT NULL DEFAULT '',
    avatar_path TEXT,
    inspirations_json TEXT NOT NULL DEFAULT '[]',
    reported_work_count INTEGER,
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS creator_works (
    creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    work_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    original_url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    duration_seconds REAL,
    thumbnail_path TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT 'pending',
    availability TEXT NOT NULL DEFAULT 'available',
    missing_sync_count INTEGER NOT NULL DEFAULT 0,
    entry_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
    last_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(creator_id, work_id)
);
CREATE INDEX IF NOT EXISTS idx_creator_works_decision
ON creator_works(creator_id, decision, availability);

CREATE TABLE IF NOT EXISTS creator_run_items (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    creator_id TEXT NOT NULL REFERENCES creators(id) ON DELETE CASCADE,
    work_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    is_new INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(job_id, work_id),
    UNIQUE(job_id, ordinal),
    FOREIGN KEY(creator_id, work_id) REFERENCES creator_works(creator_id, work_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_creator_run_items_job ON creator_run_items(job_id, ordinal);

CREATE TABLE IF NOT EXISTS web_chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('library', 'entry', 'topic')),
    context_entry_id TEXT,
    context_topic_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_chat_sessions_updated
ON web_chat_sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS web_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES web_chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_chat_messages_session
ON web_chat_messages(session_id, id);

"""


_LEXICAL_PARTS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[\w]+", re.UNICODE)
_ACTIVE_CLAIM: ContextVar[tuple[str, str] | None] = ContextVar("active_job_claim", default=None)


def lexical_tokens(value: str) -> list[str]:
    """Return deterministic FTS tokens, including CJK unigrams and bigrams."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    for match in _LEXICAL_PARTS.finditer(normalized):
        part = match.group(0)
        if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", part):
            tokens.extend(part)
            tokens.extend(part[index : index + 2] for index in range(len(part) - 1))
        else:
            tokens.append(part)
    return list(dict.fromkeys(token for token in tokens if token))


def lexical_document(value: str) -> str:
    return " ".join(lexical_tokens(value))


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

    @contextmanager
    def claimed_job_updates(self, job_id: str, worker_id: str) -> Iterable[None]:
        """Fence updates made while one worker owns a claimed job."""
        token = _ACTIVE_CLAIM.set((job_id, worker_id))
        try:
            yield
        finally:
            _ACTIVE_CLAIM.reset(token)

    def assert_job_claim(self, conn: sqlite3.Connection, job_id: str) -> None:
        active = _ACTIVE_CLAIM.get()
        if active and active[0] == job_id:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE id=? AND lock_owner=? AND locked_at IS NOT NULL "
                "AND lease_expires_at>?", (job_id, active[1], utc_now().isoformat())
            ).fetchone()
            if row is None:
                raise JobLeaseLostError("收藏清点任务的 Worker 租约已失效")

    def initialize(self) -> None:
        from .favorites_store import FAVORITES_SCHEMA

        with self.connect() as conn:
            previous_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            conn.executescript(SCHEMA)
            conn.executescript(FAVORITES_SCHEMA)
            self._migrate_chat_sessions_for_topics(conn)
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
            entry_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()
            }
            if "favorite" not in entry_columns:
                conn.execute("ALTER TABLE entries ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_favorite "
                "ON entries(favorite, updated_at DESC)"
            )
            if previous_version < 9:
                self._rebuild_fts_conn(conn)
            self._retire_undeliverable_events_conn(conn)
            conn.execute("PRAGMA user_version=10")

    @staticmethod
    def _rebuild_fts_conn(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM chunks_fts")
        rows = conn.execute(
            "SELECT id, entry_id, text, purposes_text, tags_text FROM chunks"
        ).fetchall()
        conn.executemany(
            """INSERT INTO chunks_fts(chunk_id, entry_id, text, purposes, tags)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    row["id"],
                    row["entry_id"],
                    lexical_document(row["text"]),
                    lexical_document(row["purposes_text"]),
                    lexical_document(row["tags_text"]),
                )
                for row in rows
            ],
        )

    @staticmethod
    def _migrate_chat_sessions_for_topics(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='web_chat_sessions'"
        ).fetchone()
        columns = {
            item["name"]
            for item in conn.execute("PRAGMA table_info(web_chat_sessions)").fetchall()
        }
        table_sql = str(row["sql"] or "") if row else ""
        if (
            row
            and "'topic'" in table_sql
            and "context_topic_id" in columns
            and "REFERENCES research_topics" not in table_sql
        ):
            return
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(
                """
                ALTER TABLE web_chat_messages RENAME TO web_chat_messages_legacy;
                ALTER TABLE web_chat_sessions RENAME TO web_chat_sessions_legacy;
                CREATE TABLE web_chat_sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('library', 'entry', 'topic')),
                    context_entry_id TEXT,
                    context_topic_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO web_chat_sessions
                    (id, title, scope, context_entry_id, context_topic_id, created_at, updated_at)
                SELECT id, title, scope, context_entry_id, NULL, created_at, updated_at
                FROM web_chat_sessions_legacy;
                CREATE TABLE web_chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES web_chat_sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    created_at TEXT NOT NULL
                );
                INSERT INTO web_chat_messages
                SELECT * FROM web_chat_messages_legacy;
                DROP TABLE web_chat_messages_legacy;
                DROP TABLE web_chat_sessions_legacy;
                CREATE INDEX idx_web_chat_sessions_updated
                ON web_chat_sessions(updated_at DESC);
                CREATE INDEX idx_web_chat_messages_session
                ON web_chat_messages(session_id, id);
                """
            )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

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

    def create_job(
        self,
        request: CaptureRequest,
        *,
        kind: str = "capture",
        artifacts: dict[str, Any] | None = None,
        status: JobStatus = JobStatus.QUEUED,
        progress: float = 0,
    ) -> JobRecord:
        job_id = uuid.uuid4().hex
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, status, progress, request_json, artifacts_json, result_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
                (
                    job_id,
                    kind,
                    status.value,
                    max(0.0, min(1.0, progress)),
                    request.model_dump_json(),
                    json.dumps(artifacts or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def get_or_create_active_job(
        self,
        request: CaptureRequest,
        *,
        kind: str,
        artifacts: dict[str, Any],
        match_artifact: str,
    ) -> JobRecord:
        """Atomically reuse a matching unfinished job or create it."""
        terminal_statuses = (
            JobStatus.COMPLETED.value,
            JobStatus.COMPLETED_WITH_WARNINGS.value,
            JobStatus.FAILED.value,
        )
        job_id = uuid.uuid4().hex
        now = iso_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE kind=? AND status NOT IN (?, ?, ?)
                   ORDER BY created_at DESC""",
                (kind, *terminal_statuses),
            ).fetchall()
            expected = artifacts.get(match_artifact)
            for row in rows:
                existing_artifacts = json.loads(row["artifacts_json"])
                if existing_artifacts.get(match_artifact) == expected:
                    return self._job_from_row(row)
            conn.execute(
                """INSERT INTO jobs
                   (id, kind, status, progress, request_json, artifacts_json, result_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, '{}', ?, ?)""",
                (
                    job_id,
                    kind,
                    JobStatus.QUEUED.value,
                    request.model_dump_json(),
                    json.dumps(artifacts, ensure_ascii=False),
                    now,
                    now,
                ),
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

    def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int | None = 50,
    ) -> list[JobRecord]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status.value)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
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
        current = utc_now()
        expires = (current + timedelta(seconds=max(30, lease_seconds))).isoformat()
        with self.connect() as conn:
            changed = conn.execute(
                """UPDATE jobs SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND lock_owner=? AND locked_at IS NOT NULL
                     AND lease_expires_at>?""",
                (expires, current.isoformat(), job_id, worker_id, current.isoformat()),
            ).rowcount
        return bool(changed)

    def recover_expired_jobs(self, now: datetime | None = None) -> int:
        """Requeue only abandoned in-flight jobs whose worker lease has expired."""
        timestamp = (now or datetime.now(UTC)).isoformat()
        recoverable = (
            JobStatus.QUEUED.value,
            JobStatus.RESOLVING.value,
            JobStatus.INVENTORYING.value,
            JobStatus.DISPATCHING.value,
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
        expected_child_updates: dict[str, str] | None = None,
    ) -> JobRecord:
        active_claim = _ACTIVE_CLAIM.get()
        expected_owner = active_claim[1] if active_claim and active_claim[0] == job_id else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current_row is None:
                raise JobStateError(f"job not found: {job_id}")
            current = self._job_from_row(current_row)
            if expected_child_updates is not None:
                # Check the aggregation snapshot under the same write lock as publication.
                for child_id, observed_at in expected_child_updates.items():
                    child = conn.execute(
                        "SELECT updated_at FROM jobs WHERE id=?", (child_id,)
                    ).fetchone()
                    if child is None or child["updated_at"] != observed_at:
                        return current
            assignments = ["updated_at=?"]
            values: list[Any] = [iso_now()]
            if status is not None:
                assignments.append("status=?")
                values.append(status.value)
            if progress is not None:
                assignments.append("progress=?")
                values.append(max(0.0, min(1.0, progress)))
            merged_artifacts = current.artifacts
            if artifacts is not None:
                merged_artifacts = {**current.artifacts, **artifacts}
                assignments.append("artifacts_json=?")
                values.append(json.dumps(merged_artifacts, ensure_ascii=False))
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
            where = "id=?"
            values.append(job_id)
            if expected_owner is not None:
                where += " AND lock_owner=? AND locked_at IS NOT NULL AND lease_expires_at>?"
                values.extend([expected_owner, utc_now().isoformat()])
            changed = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE {where}", values
            ).rowcount
            if expected_owner is not None and not changed:
                raise JobLeaseLostError(f"任务 {job_id} 的 Worker 租约已失效")
            if (
                status is not None
                and status != current.status
                and current.request.gateway_context is not None
                and status
                in {
                    JobStatus.AWAITING_AGENT_ANALYSIS,
                    JobStatus.NEEDS_AUTH,
                    JobStatus.NEEDS_REVIEW,
                    JobStatus.NEEDS_SELECTION,
                    JobStatus.WAITING_CONFIRMATION,
                    JobStatus.COMPLETED,
                    JobStatus.COMPLETED_WITH_WARNINGS,
                    JobStatus.FAILED,
                }
            ):
                event_result = dict(result if result is not None else current.result)
                if favorites_context := merged_artifacts.get("favorites_context"):
                    event_result["favorites_context"] = favorites_context
                if creator_context := merged_artifacts.get("creator_context"):
                    event_result["creator_context"] = creator_context
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
            updated_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_from_row(updated_row)

    def requeue_job(self, job_id: str, *, artifacts: dict[str, Any] | None = None) -> JobRecord:
        job = self.get_job(job_id)
        if job.status not in {
            JobStatus.AWAITING_AGENT_ANALYSIS,
            JobStatus.NEEDS_AUTH,
            JobStatus.NEEDS_REVIEW,
            JobStatus.NEEDS_SELECTION,
            JobStatus.WAITING_CONFIRMATION,
            JobStatus.FAILED,
        }:
            raise JobStateError(f"任务 {job_id} 当前状态不允许重新排队")
        return self.update_job(
            job_id,
            status=JobStatus.QUEUED,
            artifacts=artifacts,
            error_code="",
            error_message="",
            unlock=True,
        )

    def requeue_job_deduplicated(
        self, job_id: str, *, match_artifact: str
    ) -> JobRecord:
        """Atomically retry a job unless an equivalent active job already exists."""
        terminal_statuses = {
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_WARNINGS,
            JobStatus.FAILED,
        }
        retryable_statuses = {JobStatus.FAILED, JobStatus.NEEDS_AUTH}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if current_row is None:
                raise JobStateError(f"job not found: {job_id}")
            current = self._job_from_row(current_row)
            if current.status not in retryable_statuses:
                if current.status not in terminal_statuses:
                    return current
                raise JobStateError(f"任务 {job_id} 当前状态不允许重新排队")
            expected = current.artifacts.get(match_artifact)
            rows = conn.execute(
                """SELECT * FROM jobs
                   WHERE id<>? AND kind=? AND status NOT IN (?, ?, ?)
                   ORDER BY created_at DESC""",
                (
                    job_id,
                    current.kind,
                    JobStatus.COMPLETED.value,
                    JobStatus.COMPLETED_WITH_WARNINGS.value,
                    JobStatus.FAILED.value,
                ),
            ).fetchall()
            for row in rows:
                if json.loads(row["artifacts_json"]).get(match_artifact) == expected:
                    return self._job_from_row(row)
            conn.execute(
                """UPDATE jobs
                   SET status=?, error_code='', error_message='', locked_at=NULL,
                       lock_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=?""",
                (JobStatus.QUEUED.value, iso_now(), job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_from_row(updated)

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
        event_result = dict(result)
        if favorites_context := job.artifacts.get("favorites_context"):
            event_result["favorites_context"] = favorites_context
        if creator_context := job.artifacts.get("creator_context"):
            event_result["creator_context"] = creator_context
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
                (job_id, status.value, json.dumps(event_result, ensure_ascii=False), event_time),
            )
            event_id = cursor.lastrowid
        if event_id is None:  # pragma: no cover - SQLite always provides this value
            raise JobStateError("job event could not be created")
        return self.get_job_event(event_id)

    def find_entry_by_video_id(self, video_id: str) -> EntryRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM entries WHERE video_id=?", (video_id,)).fetchone()
        return self._entry_from_row(row) if row else None

    def upsert_creator(
        self,
        profile: CreatorProfile,
        *,
        creator_id: str,
        folder_path: str,
        inspirations: list[InspirationInput] | None = None,
    ) -> CreatorRecord:
        now = iso_now()
        existing = self.find_creator_by_sec_uid(profile.sec_uid)
        merged_inspirations = list(existing.inspirations) if existing else []
        seen = {item.model_dump_json() for item in merged_inspirations}
        for inspiration in inspirations or []:
            if inspiration.model_dump_json() not in seen:
                merged_inspirations.append(inspiration)
                seen.add(inspiration.model_dump_json())
        created_at = existing.created_at.isoformat() if existing else now
        stable_folder = existing.folder_path if existing else folder_path
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO creators
                   (id, sec_uid, canonical_url, original_url, nickname, folder_path, uid,
                    unique_id, signature, avatar_path, inspirations_json, reported_work_count,
                    last_synced_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(sec_uid) DO UPDATE SET
                     canonical_url=excluded.canonical_url,
                     nickname=excluded.nickname,
                     uid=COALESCE(excluded.uid, creators.uid),
                     unique_id=COALESCE(excluded.unique_id, creators.unique_id),
                     signature=excluded.signature,
                     avatar_path=COALESCE(excluded.avatar_path, creators.avatar_path),
                     inspirations_json=excluded.inspirations_json,
                     reported_work_count=excluded.reported_work_count,
                     updated_at=excluded.updated_at""",
                (
                    existing.id if existing else creator_id,
                    profile.sec_uid,
                    profile.canonical_url,
                    existing.original_url if existing else profile.original_url,
                    profile.nickname,
                    stable_folder,
                    profile.uid,
                    profile.unique_id,
                    profile.signature,
                    profile.avatar_path,
                    json.dumps(
                        [item.model_dump(mode="json") for item in merged_inspirations],
                        ensure_ascii=False,
                    ),
                    profile.reported_work_count,
                    (
                        existing.last_synced_at.isoformat()
                        if existing and existing.last_synced_at
                        else None
                    ),
                    created_at,
                    now,
                ),
            )
        return self.find_creator_by_sec_uid(profile.sec_uid)  # type: ignore[return-value]

    def get_creator(self, creator_id: str) -> CreatorRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
        if row is None:
            raise JobStateError(f"creator not found: {creator_id}")
        return self._creator_from_row(row)

    def find_creator_by_sec_uid(self, sec_uid: str) -> CreatorRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM creators WHERE sec_uid=?", (sec_uid,)).fetchone()
        return self._creator_from_row(row) if row else None

    def find_creator_for_work(self, work_id: str) -> CreatorRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT c.* FROM creators c JOIN creator_works w ON w.creator_id=c.id
                   WHERE w.work_id=? LIMIT 1""",
                (work_id,),
            ).fetchone()
        return self._creator_from_row(row) if row else None

    def list_creators(self) -> list[CreatorRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM creators ORDER BY updated_at DESC").fetchall()
        return [self._creator_from_row(row) for row in rows]

    def has_unfinished_creator_jobs(self) -> bool:
        terminal = (
            JobStatus.COMPLETED.value,
            JobStatus.COMPLETED_WITH_WARNINGS.value,
            JobStatus.FAILED.value,
        )
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM jobs
                   WHERE kind='creator_import' AND status NOT IN (?, ?, ?) LIMIT 1""",
                terminal,
            ).fetchone()
        return row is not None

    def record_creator_inventory(
        self,
        job_id: str,
        creator_id: str,
        works: list[CreatorInventoryWork],
        *,
        complete: bool,
        sync: bool,
    ) -> dict[str, Any]:
        now = iso_now()
        seen_ids = {work.work_id for work in works}
        new_ids: list[str] = []
        changed_ids: list[str] = []
        missing_ids: list[str] = []
        with self.connect() as conn:
            existing_rows = {
                row["work_id"]: row
                for row in conn.execute(
                    "SELECT * FROM creator_works WHERE creator_id=?", (creator_id,)
                ).fetchall()
            }
            conn.execute("DELETE FROM creator_run_items WHERE job_id=?", (job_id,))
            ordinal = 0
            for work in works:
                previous = existing_rows.get(work.work_id)
                entry_row = conn.execute(
                    "SELECT id FROM entries WHERE video_id=?", (work.work_id,)
                ).fetchone()
                entry_id = (
                    entry_row["id"] if entry_row else (previous["entry_id"] if previous else None)
                )
                if entry_id:
                    decision = CreatorWorkDecision.IMPORTED.value
                elif previous:
                    decision = previous["decision"]
                else:
                    decision = CreatorWorkDecision.PENDING.value
                    new_ids.append(work.work_id)
                if previous and any(
                    (
                        previous["title"] != work.title,
                        previous["source_kind"] != work.source_kind.value,
                        previous["canonical_url"] != work.canonical_url,
                    )
                ):
                    changed_ids.append(work.work_id)
                conn.execute(
                    """INSERT INTO creator_works
                       (creator_id, work_id, source_kind, canonical_url, original_url, title,
                        published_at, duration_seconds, thumbnail_path, is_pinned, decision,
                        availability, missing_sync_count, entry_id, last_job_id,
                        first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', 0, ?, ?, ?, ?)
                       ON CONFLICT(creator_id, work_id) DO UPDATE SET
                         source_kind=excluded.source_kind,
                         canonical_url=excluded.canonical_url,
                         title=excluded.title,
                         published_at=COALESCE(excluded.published_at, creator_works.published_at),
                         duration_seconds=COALESCE(
                           excluded.duration_seconds, creator_works.duration_seconds),
                         thumbnail_path=COALESCE(
                           excluded.thumbnail_path, creator_works.thumbnail_path),
                         is_pinned=excluded.is_pinned,
                         decision=excluded.decision,
                         availability='available', missing_sync_count=0,
                         entry_id=COALESCE(excluded.entry_id, creator_works.entry_id),
                         last_seen_at=excluded.last_seen_at""",
                    (
                        creator_id,
                        work.work_id,
                        work.source_kind.value,
                        work.canonical_url,
                        work.original_url,
                        work.title,
                        work.published_at.isoformat() if work.published_at else None,
                        work.duration_seconds,
                        work.thumbnail_path,
                        int(work.is_pinned),
                        decision,
                        entry_id,
                        previous["last_job_id"] if previous else None,
                        previous["first_seen_at"] if previous else now,
                        now,
                    ),
                )
                include = not sync or previous is None
                if include:
                    ordinal += 1
                    conn.execute(
                        """INSERT INTO creator_run_items
                           (job_id, creator_id, work_id, ordinal, is_new)
                           VALUES (?, ?, ?, ?, ?)""",
                        (job_id, creator_id, work.work_id, ordinal, int(previous is None)),
                    )
            if complete:
                for work_id, previous in existing_rows.items():
                    if work_id in seen_ids:
                        continue
                    missing_count = int(previous["missing_sync_count"] or 0) + 1
                    availability = (
                        CreatorWorkAvailability.SOURCE_UNAVAILABLE.value
                        if missing_count >= 2
                        else CreatorWorkAvailability.POSSIBLY_UNAVAILABLE.value
                    )
                    conn.execute(
                        """UPDATE creator_works SET missing_sync_count=?, availability=?
                           WHERE creator_id=? AND work_id=?""",
                        (missing_count, availability, creator_id, work_id),
                    )
                    missing_ids.append(work_id)
            conn.execute(
                "UPDATE creators SET last_synced_at=?, updated_at=? WHERE id=?",
                (now, now, creator_id),
            )
        return {
            "new_work_ids": new_ids,
            "changed_work_ids": changed_ids,
            "missing_work_ids": missing_ids,
            "inventory_count": len(works),
            "selection_count": ordinal,
        }

    def list_creator_works(self, creator_id: str) -> list[CreatorWorkRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM creator_works WHERE creator_id=?
                   ORDER BY is_pinned DESC, published_at DESC, first_seen_at DESC""",
                (creator_id,),
            ).fetchall()
        return [self._creator_work_from_row(row) for row in rows]

    def get_creator_work(self, creator_id: str, work_id: str) -> CreatorWorkRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM creator_works WHERE creator_id=? AND work_id=?",
                (creator_id, work_id),
            ).fetchone()
        if row is None:
            raise JobStateError(f"creator work not found: {creator_id}/{work_id}")
        return self._creator_work_from_row(row)

    def register_creator_work(
        self, creator_id: str, work: CreatorInventoryWork
    ) -> CreatorWorkRecord:
        """Remember a work found through direct capture without creating a selection run."""
        now = iso_now()
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM creator_works WHERE creator_id=? AND work_id=?",
                (creator_id, work.work_id),
            ).fetchone()
            entry = conn.execute(
                "SELECT id FROM entries WHERE video_id=?", (work.work_id,)
            ).fetchone()
            decision = (
                CreatorWorkDecision.IMPORTED.value
                if entry
                else (previous["decision"] if previous else CreatorWorkDecision.PENDING.value)
            )
            conn.execute(
                """INSERT INTO creator_works
                   (creator_id, work_id, source_kind, canonical_url, original_url, title,
                    published_at, duration_seconds, thumbnail_path, is_pinned, decision,
                    availability, missing_sync_count, entry_id, last_job_id,
                    first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', 0, ?, NULL, ?, ?)
                   ON CONFLICT(creator_id, work_id) DO UPDATE SET
                     source_kind=excluded.source_kind,
                     canonical_url=excluded.canonical_url,
                     original_url=excluded.original_url,
                     title=excluded.title,
                     published_at=COALESCE(excluded.published_at, creator_works.published_at),
                     duration_seconds=COALESCE(
                       excluded.duration_seconds, creator_works.duration_seconds),
                     thumbnail_path=COALESCE(
                       excluded.thumbnail_path, creator_works.thumbnail_path),
                     availability='available', missing_sync_count=0,
                     entry_id=COALESCE(excluded.entry_id, creator_works.entry_id),
                     last_seen_at=excluded.last_seen_at""",
                (
                    creator_id,
                    work.work_id,
                    work.source_kind.value,
                    work.canonical_url,
                    work.original_url,
                    work.title,
                    work.published_at.isoformat() if work.published_at else None,
                    work.duration_seconds,
                    work.thumbnail_path,
                    int(work.is_pinned),
                    decision,
                    entry["id"] if entry else None,
                    previous["first_seen_at"] if previous else now,
                    now,
                ),
            )
        return self.get_creator_work(creator_id, work.work_id)

    def list_creator_inventory(
        self,
        job_id: str,
        *,
        offset: int = 0,
        limit: int = 10,
        decision: CreatorWorkDecision | None = None,
        source_kind: SourceKind | None = None,
        query: str | None = None,
    ) -> tuple[list[CreatorInventoryItem], int]:
        clauses = ["r.job_id=?"]
        params: list[Any] = [job_id]
        if decision:
            clauses.append("w.decision=?")
            params.append(decision.value)
        if source_kind:
            clauses.append("w.source_kind=?")
            params.append(source_kind.value)
        if query:
            clauses.append("w.title LIKE ?")
            params.append(f"%{query}%")
        where = " AND ".join(clauses)
        with self.connect() as conn:
            total = conn.execute(
                f"""SELECT COUNT(*) AS count FROM creator_run_items r
                    JOIN creator_works w ON w.creator_id=r.creator_id AND w.work_id=r.work_id
                    WHERE {where}""",
                params,
            ).fetchone()["count"]
            rows = conn.execute(
                f"""SELECT r.job_id, r.ordinal, r.is_new, w.*
                    FROM creator_run_items r
                    JOIN creator_works w ON w.creator_id=r.creator_id AND w.work_id=r.work_id
                    WHERE {where} ORDER BY r.ordinal LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        return [
            CreatorInventoryItem(
                job_id=row["job_id"],
                ordinal=row["ordinal"],
                is_new=bool(row["is_new"]),
                work=self._creator_work_from_row(row),
            )
            for row in rows
        ], int(total)

    def set_creator_run_selection(
        self,
        job_id: str,
        decision: CreatorWorkDecision,
        *,
        ordinals: list[int] | None = None,
        work_ids: list[str] | None = None,
    ) -> int:
        if decision not in {CreatorWorkDecision.SELECTED, CreatorWorkDecision.SKIPPED}:
            raise JobStateError("作品只能标记为“已选入库”或“未入库”")
        if (ordinals is not None or work_ids is not None) and not (ordinals or work_ids):
            return 0
        clauses = ["job_id=?"]
        params: list[Any] = [job_id]
        if ordinals:
            placeholders = ",".join("?" for _ in ordinals)
            clauses.append(f"ordinal IN ({placeholders})")
            params.extend(ordinals)
        if work_ids:
            placeholders = ",".join("?" for _ in work_ids)
            clauses.append(f"work_id IN ({placeholders})")
            params.extend(work_ids)
        if ordinals is None and work_ids is None:
            clauses.append("1=1")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT creator_id, work_id FROM creator_run_items WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            for row in rows:
                current = conn.execute(
                    """SELECT decision FROM creator_works
                       WHERE creator_id=? AND work_id=?""",
                    (row["creator_id"], row["work_id"]),
                ).fetchone()
                if current and current["decision"] != CreatorWorkDecision.IMPORTED.value:
                    conn.execute(
                        """UPDATE creator_works SET decision=?
                           WHERE creator_id=? AND work_id=?""",
                        (decision.value, row["creator_id"], row["work_id"]),
                    )
        return len(rows)

    def create_creator_run_items(self, job_id: str, creator_id: str, work_ids: list[str]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM creator_run_items WHERE job_id=?", (job_id,))
            for ordinal, work_id in enumerate(work_ids, start=1):
                exists = conn.execute(
                    """SELECT 1 FROM creator_works
                       WHERE creator_id=? AND work_id=?""",
                    (creator_id, work_id),
                ).fetchone()
                if exists is None:
                    raise JobStateError(f"creator work not found: {creator_id}/{work_id}")
                conn.execute(
                    """INSERT INTO creator_run_items
                       (job_id, creator_id, work_id, ordinal, is_new)
                       VALUES (?, ?, ?, ?, 0)""",
                    (job_id, creator_id, work_id, ordinal),
                )

    def creator_inventory_summary(self, job_id: str) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT w.decision, COUNT(*) AS count FROM creator_run_items r
                   JOIN creator_works w ON w.creator_id=r.creator_id AND w.work_id=r.work_id
                   WHERE r.job_id=? GROUP BY w.decision""",
                (job_id,),
            ).fetchall()
        values = {item.value: 0 for item in CreatorWorkDecision}
        values.update({row["decision"]: int(row["count"]) for row in rows})
        values["total"] = sum(int(row["count"]) for row in rows)
        return values

    def attach_creator_child_job(self, creator_id: str, work_id: str, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE creator_works SET last_job_id=?
                   WHERE creator_id=? AND work_id=?""",
                (job_id, creator_id, work_id),
            )

    def mark_creator_work_imported(self, creator_id: str, work_id: str, entry_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE creator_works
                   SET decision='imported', entry_id=?, availability='available'
                   WHERE creator_id=? AND work_id=?""",
                (entry_id, creator_id, work_id),
            )

    def restore_creator_bundle(
        self, creator: CreatorRecord, works: list[CreatorWorkRecord]
    ) -> None:
        """Restore the rebuildable creator projection from a tracked sidecar."""
        with self.connect() as conn:
            self._restore_creator_bundle_conn(conn, creator, works)

    def _restore_creator_bundle_conn(
        self,
        conn: sqlite3.Connection,
        creator: CreatorRecord,
        works: list[CreatorWorkRecord],
    ) -> None:
        """Restore one creator and its works using the caller's connection."""
        conn.execute(
            """INSERT INTO creators
               (id, sec_uid, canonical_url, original_url, nickname, folder_path, uid,
                unique_id, signature, avatar_path, inspirations_json, reported_work_count,
                last_synced_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 sec_uid=excluded.sec_uid, canonical_url=excluded.canonical_url,
                 original_url=excluded.original_url, nickname=excluded.nickname,
                 folder_path=excluded.folder_path, uid=excluded.uid,
                 unique_id=excluded.unique_id, signature=excluded.signature,
                 avatar_path=excluded.avatar_path,
                 inspirations_json=excluded.inspirations_json,
                 reported_work_count=excluded.reported_work_count,
                 last_synced_at=excluded.last_synced_at, created_at=excluded.created_at,
                 updated_at=excluded.updated_at""",
            (
                creator.id,
                creator.sec_uid,
                creator.canonical_url,
                creator.original_url,
                creator.nickname,
                creator.folder_path,
                creator.uid,
                creator.unique_id,
                creator.signature,
                creator.avatar_path,
                json.dumps(
                    [item.model_dump(mode="json") for item in creator.inspirations],
                    ensure_ascii=False,
                ),
                creator.reported_work_count,
                creator.last_synced_at.isoformat() if creator.last_synced_at else None,
                creator.created_at.isoformat(),
                creator.updated_at.isoformat(),
            ),
        )
        for work in works:
            entry_id = work.entry_id
            if (
                entry_id
                and not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone()
            ):
                entry_id = None
            conn.execute(
                """INSERT INTO creator_works
                   (creator_id, work_id, source_kind, canonical_url, original_url, title,
                    published_at, duration_seconds, thumbnail_path, is_pinned, decision,
                    availability, missing_sync_count, entry_id, last_job_id,
                    first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(creator_id, work_id) DO UPDATE SET
                     source_kind=excluded.source_kind, canonical_url=excluded.canonical_url,
                     original_url=excluded.original_url, title=excluded.title,
                     published_at=excluded.published_at, duration_seconds=excluded.duration_seconds,
                     thumbnail_path=excluded.thumbnail_path, is_pinned=excluded.is_pinned,
                     decision=excluded.decision, availability=excluded.availability,
                     missing_sync_count=excluded.missing_sync_count, entry_id=excluded.entry_id,
                     last_job_id=NULL, first_seen_at=excluded.first_seen_at,
                     last_seen_at=excluded.last_seen_at""",
                (
                    creator.id,
                    work.work_id,
                    work.source_kind.value,
                    work.canonical_url,
                    work.original_url,
                    work.title,
                    work.published_at.isoformat() if work.published_at else None,
                    work.duration_seconds,
                    work.thumbnail_path,
                    int(work.is_pinned),
                    work.decision.value,
                    work.availability.value,
                    work.missing_sync_count,
                    entry_id,
                    work.first_seen_at.isoformat(),
                    work.last_seen_at.isoformat(),
                ),
            )

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

    def snapshot_entry_dependencies(self, entry_id: str) -> dict[str, Any]:
        """Capture dependent projections before the entry files are moved."""
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
                raise EntryNotFoundError(f"entry not found: {entry_id}")
            topic_ids = [
                row["topic_id"]
                for row in conn.execute(
                    "SELECT topic_id FROM topic_sources WHERE entry_id=? ORDER BY topic_id",
                    (entry_id,),
                ).fetchall()
            ]
            return {
                "topics": {
                    topic_id: [
                        dict(row)
                        for row in conn.execute(
                            """SELECT entry_id, position, enabled, source_revision
                               FROM topic_sources WHERE topic_id=? ORDER BY position""",
                            (topic_id,),
                        ).fetchall()
                    ]
                    for topic_id in topic_ids
                },
                "creator_works": [
                    dict(row)
                    for row in conn.execute(
                        """SELECT creator_id, work_id, decision, entry_id
                           FROM creator_works WHERE entry_id=?""",
                        (entry_id,),
                    ).fetchall()
                ],
                "reminders": [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM reminders WHERE entry_id=?", (entry_id,)
                    ).fetchall()
                ],
                "relations": [
                    dict(row)
                    for row in conn.execute(
                        """SELECT * FROM relations
                           WHERE source_entry_id=? OR target_entry_id=?""",
                        (entry_id, entry_id),
                    ).fetchall()
                ],
                "purposes": [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM purposes WHERE entry_id=?", (entry_id,)
                    ).fetchall()
                ],
                "chat_session_ids": [
                    row["id"]
                    for row in conn.execute(
                        """SELECT id FROM web_chat_sessions
                           WHERE scope='entry' AND context_entry_id=?""",
                        (entry_id,),
                    ).fetchall()
                ],
            }

    def delete_entry_projection(self, entry_id: str) -> dict[str, Any]:
        """Remove one rebuildable entry projection and return data needed for restoration."""
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
                raise EntryNotFoundError(f"entry not found: {entry_id}")
            topic_ids = [
                row["topic_id"]
                for row in conn.execute(
                    "SELECT topic_id FROM topic_sources WHERE entry_id=? ORDER BY topic_id",
                    (entry_id,),
                ).fetchall()
            ]
            topic_sources: dict[str, list[dict[str, Any]]] = {}
            for topic_id in topic_ids:
                topic_sources[topic_id] = [
                    dict(row)
                    for row in conn.execute(
                        """SELECT entry_id, position, enabled, source_revision
                           FROM topic_sources WHERE topic_id=? ORDER BY position""",
                        (topic_id,),
                    ).fetchall()
                ]
            creator_works = [
                dict(row)
                for row in conn.execute(
                    """SELECT creator_id, work_id, decision, entry_id
                       FROM creator_works WHERE entry_id=?""",
                    (entry_id,),
                ).fetchall()
            ]
            reminders = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM reminders WHERE entry_id=?", (entry_id,)
                ).fetchall()
            ]
            relations = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM relations
                       WHERE source_entry_id=? OR target_entry_id=?""",
                    (entry_id, entry_id),
                ).fetchall()
            ]
            chat_session_ids = [
                row["id"]
                for row in conn.execute(
                    """SELECT id FROM web_chat_sessions
                       WHERE scope='entry' AND context_entry_id=?""",
                    (entry_id,),
                ).fetchall()
            ]
            purposes = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM purposes WHERE entry_id=?", (entry_id,)
                ).fetchall()
            ]
            chunk_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE entry_id=?", (entry_id,)
                ).fetchall()
            ]
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                conn.execute(
                    f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids
                )
            conn.execute(
                """UPDATE creator_works
                   SET decision='skipped', entry_id=NULL
                   WHERE entry_id=?""",
                (entry_id,),
            )
            conn.execute(
                """UPDATE web_chat_sessions
                   SET scope='library', context_entry_id=NULL, updated_at=?
                   WHERE scope='entry' AND context_entry_id=?""",
                (iso_now(), entry_id),
            )
            conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        return {
            "topics": topic_sources,
            "creator_works": creator_works,
            "reminders": reminders,
            "relations": relations,
            "purposes": purposes,
            "chat_session_ids": chat_session_ids,
        }

    def restore_entry_dependencies(
        self, entry_id: str, snapshot: dict[str, Any]
    ) -> dict[str, list[str]]:
        """Restore non-document projections after an entry has been recreated."""
        restored_creators: list[str] = []
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
                raise EntryNotFoundError(f"entry not found: {entry_id}")
            for item in snapshot.get("creator_works", []):
                cursor = conn.execute(
                    """UPDATE creator_works SET decision='imported', entry_id=?
                       WHERE creator_id=? AND work_id=?""",
                    (entry_id, item.get("creator_id"), item.get("work_id")),
                )
                if cursor.rowcount:
                    restored_creators.append(str(item.get("creator_id")))
            for item in snapshot.get("reminders", []):
                conn.execute(
                    """INSERT OR REPLACE INTO reminders
                       (id, entry_id, data_json, status, system_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item["id"],
                        entry_id,
                        item["data_json"],
                        item["status"],
                        item.get("system_id"),
                        item["updated_at"],
                    ),
                )
            for item in snapshot.get("relations", []):
                source_id = str(item.get("source_entry_id") or "")
                target_id = str(item.get("target_entry_id") or "")
                if not source_id or not target_id:
                    continue
                if not all(
                    conn.execute("SELECT 1 FROM entries WHERE id=?", (value,)).fetchone()
                    for value in (source_id, target_id)
                ):
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO relations
                       (source_entry_id, target_entry_id, relation_type, reason,
                        confidence, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        target_id,
                        item["relation_type"],
                        item["reason"],
                        item["confidence"],
                        item["created_at"],
                    ),
                )
            for item in snapshot.get("purposes", []):
                conn.execute(
                    """INSERT OR REPLACE INTO purposes
                       (id, entry_id, data_json, created_at) VALUES (?, ?, ?, ?)""",
                    (item["id"], entry_id, item["data_json"], item["created_at"]),
                )
            session_ids = [str(value) for value in snapshot.get("chat_session_ids", [])]
            for session_id in session_ids:
                conn.execute(
                    """UPDATE web_chat_sessions
                       SET scope='entry', context_entry_id=?, updated_at=?
                       WHERE id=? AND scope='library' AND context_entry_id IS NULL""",
                    (entry_id, iso_now(), session_id),
                )
        return {"creator_ids": list(dict.fromkeys(restored_creators))}

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
            int(entry.favorite),
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
                 media_status, retention, favorite, media_expires_at, summary,
                 purposes_json, tags_json, data_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title, canonical_url=excluded.canonical_url,
                  status=excluded.status, media_status=excluded.media_status,
                  retention=excluded.retention, favorite=excluded.favorite,
                  media_expires_at=excluded.media_expires_at,
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
        with self.connect() as conn:
            self._persist_entry_bundle_conn(conn, entry, data, chunks, relations, reminders)
        return self.get_entry(entry.id)

    def _persist_entry_bundle_conn(
        self,
        conn: sqlite3.Connection,
        entry: EntryRecord,
        data: dict[str, Any],
        chunks: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reminders: list[ReminderCandidate],
    ) -> None:
        """Persist one complete entry projection using the caller's connection."""
        now = iso_now()
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
                    lexical_document(chunk["text"]),
                    lexical_document(chunk.get("purposes_text", "")),
                    lexical_document(chunk.get("tags_text", "")),
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
        conn.execute("DELETE FROM reminders WHERE entry_id=? AND status='candidate'", (entry.id,))
        created_ids = {
            ReminderCandidate.model_validate_json(row["data_json"]).id
            for row in conn.execute(
                "SELECT data_json FROM reminders WHERE entry_id=? AND status='created'",
                (entry.id,),
            ).fetchall()
        }
        persisted_states = {
            str(item.get("id")): item
            for item in data.get("reminder_states", [])
            if isinstance(item, dict) and item.get("status") == "created"
        }
        conn.executemany(
            """INSERT INTO reminders
               (id, entry_id, data_json, status, system_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    self._reminder_storage_id(entry.id, reminder.id),
                    entry.id,
                    reminder.model_dump_json(),
                    "created" if reminder.id in persisted_states else "candidate",
                    persisted_states.get(reminder.id, {}).get("system_id"),
                    now,
                )
                for reminder in reminders
                if reminder.id not in created_ids
            ],
        )

    def replace_knowledge_cache(
        self,
        *,
        entries: list[
            tuple[
                EntryRecord,
                dict[str, Any],
                list[dict[str, Any]],
                list[dict[str, Any]],
                list[ReminderCandidate],
            ]
        ],
        creators: list[tuple[CreatorRecord, list[CreatorWorkRecord]]],
        topics: list[tuple[ResearchTopic, list[TopicArtifact]]],
        embedding_signature: str,
    ) -> None:
        """Replace every rebuildable projection in one SQLite transaction."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_items = [
                dict(row) for row in conn.execute("SELECT * FROM creator_run_items").fetchall()
            ]
            last_job_links = [
                dict(row)
                for row in conn.execute(
                    """SELECT creator_id, work_id, last_job_id
                       FROM creator_works WHERE last_job_id IS NOT NULL"""
                ).fetchall()
            ]
            self._clear_knowledge_cache_conn(conn, include_creators=True)
            for entry, data, chunks, relations, reminders in entries:
                self._persist_entry_bundle_conn(
                    conn, entry, data, chunks, relations, reminders
                )
            for creator, works in creators:
                self._restore_creator_bundle_conn(conn, creator, works)
            for topic, artifacts in topics:
                self._restore_topic_bundle_conn(conn, topic, artifacts)

            for item in run_items:
                valid = (
                    conn.execute("SELECT 1 FROM jobs WHERE id=?", (item["job_id"],)).fetchone()
                    and conn.execute(
                        "SELECT 1 FROM creator_works WHERE creator_id=? AND work_id=?",
                        (item["creator_id"], item["work_id"]),
                    ).fetchone()
                )
                if not valid:
                    continue
                conn.execute(
                    """INSERT INTO creator_run_items
                       (job_id, creator_id, work_id, ordinal, is_new)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        item["job_id"],
                        item["creator_id"],
                        item["work_id"],
                        item["ordinal"],
                        item["is_new"],
                    ),
                )
            for item in last_job_links:
                if (
                    conn.execute(
                        "SELECT 1 FROM jobs WHERE id=?", (item["last_job_id"],)
                    ).fetchone()
                    is None
                ):
                    continue
                if (
                    conn.execute(
                        "SELECT 1 FROM creator_works WHERE creator_id=? AND work_id=?",
                        (item["creator_id"], item["work_id"]),
                    ).fetchone()
                    is None
                ):
                    continue
                conn.execute(
                    """UPDATE creator_works SET last_job_id=?
                       WHERE creator_id=? AND work_id=?""",
                    (item["last_job_id"], item["creator_id"], item["work_id"]),
                )
            self._set_index_metadata_conn(conn, "embedding_signature", embedding_signature)

    def clear_knowledge_cache(self, *, include_creators: bool = False) -> None:
        """Remove only rebuildable knowledge projections; keep jobs and maintenance history."""
        with self.connect() as conn:
            self._clear_knowledge_cache_conn(conn, include_creators=include_creators)

    @staticmethod
    def _clear_knowledge_cache_conn(
        conn: sqlite3.Connection, *, include_creators: bool = False
    ) -> None:
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM topic_artifacts")
        conn.execute("DELETE FROM research_topics")
        conn.execute("DELETE FROM entries")
        if include_creators:
            conn.execute("DELETE FROM creators")
        conn.execute("DELETE FROM index_metadata")

    def get_index_metadata(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM index_metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_index_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            self._set_index_metadata_conn(conn, key, value)

    @staticmethod
    def _set_index_metadata_conn(
        conn: sqlite3.Connection, key: str, value: str
    ) -> None:
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
            row = conn.execute(
                "SELECT data_json FROM reminders WHERE entry_id=? AND id IN (?, ?)",
                (entry_id, reminder_id, self._reminder_storage_id(entry_id, reminder_id)),
            ).fetchone()
            if row is None:
                raise EntryNotFoundError(f"reminder not found: {reminder_id}")
            candidate = ReminderCandidate.model_validate_json(row["data_json"])
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
            entry_row = conn.execute(
                "SELECT data_json FROM entries WHERE id=?", (entry_id,)
            ).fetchone()
            if entry_row is None:
                raise EntryNotFoundError(f"entry not found: {entry_id}")
            data = json.loads(entry_row["data_json"])
            states = [
                item
                for item in data.get("reminder_states", [])
                if isinstance(item, dict) and str(item.get("id")) != candidate.id
            ]
            states.append(
                {
                    "id": candidate.id,
                    "status": "created",
                    "system_id": system_id,
                }
            )
            data["reminder_states"] = states
            conn.execute(
                "UPDATE entries SET data_json=?, updated_at=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), iso_now(), entry_id),
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
                        lexical_document(chunk["text"]),
                        lexical_document(chunk.get("purposes_text", "")),
                        lexical_document(chunk.get("tags_text", "")),
                    ),
                )

    def fetch_chunks(
        self,
        *,
        include_stale: bool = False,
        entry_ids: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        if entry_ids is not None and not entry_ids:
            return []
        query = "SELECT * FROM chunks"
        clauses: list[str] = []
        values: list[Any] = []
        if not include_stale:
            clauses.append("stale=0")
        if entry_ids is not None:
            placeholders = ",".join("?" for _ in entry_ids)
            clauses.append(f"entry_id IN ({placeholders})")
            values.extend(entry_ids)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def entry_chunk_count(self, entry_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM chunks WHERE entry_id=?", (entry_id,)
            ).fetchone()
        return int(row["count"])

    def fts_search(
        self,
        query: str,
        *,
        relaxed_query: str | None = None,
        raw_query: str | None = None,
        include_stale: bool = False,
        limit: int = 50,
        entry_ids: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        if entry_ids is not None and not entry_ids:
            return []
        conditions = [] if include_stale else ["c.stale=0"]
        filter_values: list[Any] = []
        if entry_ids is not None:
            placeholders = ",".join("?" for _ in entry_ids)
            conditions.append(f"c.entry_id IN ({placeholders})")
            filter_values.extend(entry_ids)
        filter_clause = "" if not conditions else "AND " + " AND ".join(conditions)
        rows: list[dict[str, Any]] = []
        with self.connect() as conn:
            def append_fts(match_query: str, match_kind: str, quality: float) -> None:
                try:
                    matched = conn.execute(
                        f"""SELECT c.*, bm25(chunks_fts, 0, 0, 1.0, 1.6, 0.8) AS rank
                            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.chunk_id
                            WHERE chunks_fts MATCH ? {filter_clause}
                            ORDER BY rank LIMIT ?""",
                        (match_query, *filter_values, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return
                seen = {row["id"] for row in rows}
                rows.extend(
                    {
                        **dict(row),
                        "match_kind": match_kind,
                        "match_quality": quality,
                    }
                    for row in matched
                    if row["id"] not in seen
                )

            append_fts(query, "fts_strict", 1.0)
            exact_query = (raw_query if raw_query is not None else query).strip()
            if exact_query:
                escaped = exact_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like_conditions = [
                    "(c.text LIKE ? ESCAPE '\\' OR c.purposes_text LIKE ? ESCAPE '\\')"
                ]
                if not include_stale:
                    like_conditions.append("c.stale=0")
                if entry_ids is not None:
                    placeholders = ",".join("?" for _ in entry_ids)
                    like_conditions.append(f"c.entry_id IN ({placeholders})")
                like_rows = conn.execute(
                    f"""SELECT c.* FROM chunks c
                        WHERE {' AND '.join(like_conditions)} LIMIT ?""",
                    (f"%{escaped}%", f"%{escaped}%", *filter_values, limit),
                ).fetchall()
                seen = {row["id"] for row in rows}
                rows.extend(
                    {
                        **dict(row),
                        "rank": None,
                        "match_kind": "exact_substring",
                        "match_quality": 0.95,
                    }
                    for row in like_rows
                    if row["id"] not in seen
                )
            if relaxed_query:
                append_fts(relaxed_query, "fts_relaxed", 0.72)
        return rows[:limit]

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

    def create_topic(
        self,
        *,
        topic_id: str,
        title: str,
        goal: str = "",
        instructions: str = "",
        source_revision: str,
    ) -> ResearchTopic:
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO research_topics
                   (id, title, goal, instructions, source_revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (topic_id, title, goal, instructions, source_revision, now, now),
            )
        return self.get_topic(topic_id)

    def get_topic(self, topic_id: str) -> ResearchTopic:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_topics WHERE id=?", (topic_id,)
            ).fetchone()
            sources = conn.execute(
                """SELECT s.*, e.title
                   FROM topic_sources s JOIN entries e ON e.id=s.entry_id
                   WHERE s.topic_id=? ORDER BY s.position""",
                (topic_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"专题不存在：{topic_id}")
        return self._topic_from_rows(row, sources)

    def list_topics(self) -> list[ResearchTopic]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_topics ORDER BY updated_at DESC"
            ).fetchall()
        return [self.get_topic(row["id"]) for row in rows]

    def set_topic_sources(
        self,
        topic_id: str,
        sources: list[tuple[str, bool, str]],
        *,
        source_revision: str,
    ) -> ResearchTopic:
        self.get_topic(topic_id)
        if len({entry_id for entry_id, _, _ in sources}) != len(sources):
            raise ValueError("专题来源不能重复")
        with self.connect() as conn:
            for entry_id, _, _ in sources:
                if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
                    raise EntryNotFoundError(f"entry not found: {entry_id}")
            conn.execute("DELETE FROM topic_sources WHERE topic_id=?", (topic_id,))
            conn.executemany(
                """INSERT INTO topic_sources
                   (topic_id, entry_id, position, enabled, source_revision)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (topic_id, entry_id, position, int(enabled), revision)
                    for position, (entry_id, enabled, revision) in enumerate(sources, start=1)
                ],
            )
            now = iso_now()
            conn.execute(
                """UPDATE research_topics SET source_revision=?, updated_at=? WHERE id=?""",
                (source_revision, now, topic_id),
            )
            conn.execute(
                """UPDATE topic_artifacts SET status='needs_update', updated_at=?
                   WHERE topic_id=? AND user_authored=0 AND source_revision<>?""",
                (now, topic_id, source_revision),
            )
        return self.get_topic(topic_id)

    def update_topic_revision(
        self,
        topic_id: str,
        *,
        source_revision: str,
        source_versions: dict[str, str],
    ) -> ResearchTopic:
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                "UPDATE research_topics SET source_revision=?, updated_at=? WHERE id=?",
                (source_revision, now, topic_id),
            )
            conn.executemany(
                """UPDATE topic_sources SET source_revision=?
                   WHERE topic_id=? AND entry_id=?""",
                [(revision, topic_id, entry_id) for entry_id, revision in source_versions.items()],
            )
            conn.execute(
                """UPDATE topic_artifacts SET status='needs_update', updated_at=?
                   WHERE topic_id=? AND user_authored=0 AND source_revision<>?""",
                (now, topic_id, source_revision),
            )
        return self.get_topic(topic_id)

    def enabled_topic_entry_ids(self, topic_id: str) -> list[str]:
        self.get_topic(topic_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT entry_id FROM topic_sources
                   WHERE topic_id=? AND enabled=1 ORDER BY position""",
                (topic_id,),
            ).fetchall()
        return [row["entry_id"] for row in rows]

    def save_topic_artifact(self, artifact: TopicArtifact) -> TopicArtifact:
        with self.connect() as conn:
            self._save_topic_artifact_conn(conn, artifact)
        return self.get_topic_artifact(artifact.id)

    @staticmethod
    def _save_topic_artifact_conn(
        conn: sqlite3.Connection, artifact: TopicArtifact
    ) -> None:
        conn.execute(
            """INSERT INTO topic_artifacts
               (id, topic_id, kind, title, content_markdown, source_revision,
                source_revisions_json, status, model, prompt_version, prompt_tokens,
                completion_tokens, total_tokens, user_authored, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, content_markdown=excluded.content_markdown,
                source_revision=excluded.source_revision,
                source_revisions_json=excluded.source_revisions_json,
                status=excluded.status, model=excluded.model,
                prompt_version=excluded.prompt_version,
                prompt_tokens=excluded.prompt_tokens,
                completion_tokens=excluded.completion_tokens,
                total_tokens=excluded.total_tokens, updated_at=excluded.updated_at""",
            (
                artifact.id,
                artifact.topic_id,
                artifact.kind,
                artifact.title,
                artifact.content_markdown,
                artifact.source_revision,
                json.dumps(
                    [item.model_dump(mode="json") for item in artifact.source_revisions],
                    ensure_ascii=False,
                    default=str,
                ),
                artifact.status,
                artifact.model,
                artifact.prompt_version,
                artifact.prompt_tokens,
                artifact.completion_tokens,
                artifact.total_tokens,
                int(artifact.user_authored),
                artifact.created_at.isoformat(),
                artifact.updated_at.isoformat(),
            ),
        )

    def restore_topic_bundle(
        self, topic: ResearchTopic, artifacts: list[TopicArtifact]
    ) -> ResearchTopic:
        """Restore a topic projection after its entry rows have been rebuilt."""
        with self.connect() as conn:
            self._restore_topic_bundle_conn(conn, topic, artifacts)
        return self.get_topic(topic.id)

    def _restore_topic_bundle_conn(
        self,
        conn: sqlite3.Connection,
        topic: ResearchTopic,
        artifacts: list[TopicArtifact],
    ) -> None:
        """Restore one topic, its sources, and artifacts using one connection."""
        conn.execute(
            """INSERT INTO research_topics
               (id, title, goal, instructions, source_revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, goal=excluded.goal,
               instructions=excluded.instructions, source_revision=excluded.source_revision,
               created_at=excluded.created_at, updated_at=excluded.updated_at""",
            (
                topic.id,
                topic.title,
                topic.goal,
                topic.instructions,
                topic.source_revision,
                topic.created_at.isoformat(),
                topic.updated_at.isoformat(),
            ),
        )
        conn.execute("DELETE FROM topic_sources WHERE topic_id=?", (topic.id,))
        valid_sources = [
            source
            for source in topic.sources
            if conn.execute(
                "SELECT 1 FROM entries WHERE id=?", (source.entry_id,)
            ).fetchone()
        ]
        conn.executemany(
            """INSERT INTO topic_sources
               (topic_id, entry_id, position, enabled, source_revision)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    topic.id,
                    source.entry_id,
                    position,
                    int(source.enabled),
                    source.source_revision.isoformat(),
                )
                for position, source in enumerate(valid_sources, start=1)
            ],
        )
        for artifact in artifacts:
            self._save_topic_artifact_conn(conn, artifact)

    def get_topic_artifact(self, artifact_id: str) -> TopicArtifact:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM topic_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"专题成果不存在：{artifact_id}")
        return self._topic_artifact_from_row(row)

    def list_topic_artifacts(self, topic_id: str) -> list[TopicArtifact]:
        self.get_topic(topic_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM topic_artifacts WHERE topic_id=?
                   ORDER BY created_at DESC""",
                (topic_id,),
            ).fetchall()
        return [self._topic_artifact_from_row(row) for row in rows]

    def create_chat_session(
        self,
        *,
        title: str = "新对话",
        scope: str = "library",
        context_entry_id: str | None = None,
        context_topic_id: str | None = None,
    ) -> ChatSession:
        session_id = uuid.uuid4().hex
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO web_chat_sessions
                   (id, title, scope, context_entry_id, context_topic_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, title, scope, context_entry_id, context_topic_id, now, now),
            )
        return self.get_chat_session(session_id)

    def get_chat_session(self, session_id: str) -> ChatSession:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM web_chat_sessions WHERE id=?", (session_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"对话不存在：{session_id}")
        return self._chat_session_from_row(row)

    def list_chat_sessions(self) -> list[ChatSession]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM web_chat_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [self._chat_session_from_row(row) for row in rows]

    def update_chat_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        scope: str | None = None,
        context_entry_id: str | None = None,
        context_topic_id: str | None = None,
        update_context: bool = False,
    ) -> ChatSession:
        current = self.get_chat_session(session_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE web_chat_sessions
                   SET title=?, scope=?, context_entry_id=?, context_topic_id=?, updated_at=?
                   WHERE id=?""",
                (
                    title if title is not None else current.title,
                    scope if scope is not None else current.scope,
                    context_entry_id if update_context else current.context_entry_id,
                    context_topic_id if update_context else current.context_topic_id,
                    iso_now(),
                    session_id,
                ),
            )
        return self.get_chat_session(session_id)

    def delete_chat_session(self, session_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM web_chat_sessions WHERE id=?", (session_id,))
        return cursor.rowcount > 0

    def add_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        citations: list[Citation] | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> ChatMessage:
        now = iso_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO web_chat_messages
                   (session_id, role, content, citations_json, model,
                    prompt_tokens, completion_tokens, total_tokens, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(
                        [item.model_dump(mode="json") for item in (citations or [])],
                        ensure_ascii=False,
                    ),
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    now,
                ),
            )
            message_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE web_chat_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            row = conn.execute(
                "SELECT * FROM web_chat_messages WHERE id=?", (message_id,)
            ).fetchone()
        return self._chat_message_from_row(row)

    def list_chat_messages(self, session_id: str, *, limit: int = 200) -> list[ChatMessage]:
        self.get_chat_session(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM web_chat_messages WHERE session_id=?
                       ORDER BY id DESC LIMIT ?
                   ) ORDER BY id""",
                (session_id, limit),
            ).fetchall()
        return [self._chat_message_from_row(row) for row in rows]

    def entries_with_expired_media(self, now: datetime | None = None) -> list[EntryRecord]:
        timestamp = (now or utc_now()).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM entries WHERE favorite=0
                   AND retention='temporary' AND media_status='present'
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
            favorite=bool(row["favorite"]),
            media_expires_at=parse_datetime(row["media_expires_at"]),
            summary=row["summary"],
            inspirations=[
                InspirationInput.model_validate(item) for item in json.loads(row["purposes_json"])
            ],
            tags=json.loads(row["tags_json"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _creator_from_row(row: sqlite3.Row) -> CreatorRecord:
        return CreatorRecord(
            id=row["id"],
            sec_uid=row["sec_uid"],
            canonical_url=row["canonical_url"],
            original_url=row["original_url"],
            nickname=row["nickname"],
            folder_path=row["folder_path"],
            uid=row["uid"],
            unique_id=row["unique_id"],
            signature=row["signature"],
            avatar_path=row["avatar_path"],
            inspirations=[
                InspirationInput.model_validate(item)
                for item in json.loads(row["inspirations_json"])
            ],
            reported_work_count=row["reported_work_count"],
            last_synced_at=parse_datetime(row["last_synced_at"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _creator_work_from_row(row: sqlite3.Row) -> CreatorWorkRecord:
        return CreatorWorkRecord(
            creator_id=row["creator_id"],
            work_id=row["work_id"],
            source_kind=SourceKind(row["source_kind"]),
            canonical_url=row["canonical_url"],
            original_url=row["original_url"],
            title=row["title"],
            published_at=parse_datetime(row["published_at"]),
            duration_seconds=row["duration_seconds"],
            thumbnail_path=row["thumbnail_path"],
            is_pinned=bool(row["is_pinned"]),
            decision=CreatorWorkDecision(row["decision"]),
            availability=CreatorWorkAvailability(row["availability"]),
            missing_sync_count=int(row["missing_sync_count"] or 0),
            entry_id=row["entry_id"],
            last_job_id=row["last_job_id"],
            first_seen_at=parse_datetime(row["first_seen_at"]),
            last_seen_at=parse_datetime(row["last_seen_at"]),
        )

    @staticmethod
    def _topic_from_rows(
        row: sqlite3.Row, source_rows: Iterable[sqlite3.Row]
    ) -> ResearchTopic:
        return ResearchTopic(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            instructions=row["instructions"],
            sources=[
                TopicSource(
                    entry_id=source["entry_id"],
                    position=source["position"],
                    enabled=bool(source["enabled"]),
                    source_revision=parse_datetime(source["source_revision"]),
                    title=source["title"],
                )
                for source in source_rows
            ],
            source_revision=row["source_revision"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _topic_artifact_from_row(row: sqlite3.Row) -> TopicArtifact:
        return TopicArtifact(
            id=row["id"],
            topic_id=row["topic_id"],
            kind=row["kind"],
            title=row["title"],
            content_markdown=row["content_markdown"],
            source_revision=row["source_revision"],
            source_revisions=[
                SourceRevision.model_validate(item)
                for item in json.loads(row["source_revisions_json"])
            ],
            status=row["status"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            user_authored=bool(row["user_authored"]),
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _chat_session_from_row(row: sqlite3.Row) -> ChatSession:
        return ChatSession(
            id=row["id"],
            title=row["title"],
            scope=row["scope"],
            context_entry_id=row["context_entry_id"],
            context_topic_id=row["context_topic_id"],
            created_at=parse_datetime(row["created_at"]),
            updated_at=parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _chat_message_from_row(row: sqlite3.Row) -> ChatMessage:
        return ChatMessage(
            id=row["id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            citations=[Citation.model_validate(item) for item in json.loads(row["citations_json"])],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            created_at=parse_datetime(row["created_at"]),
        )
