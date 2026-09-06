from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from douyin_wiki.database import Database
from douyin_wiki.errors import JobLeaseLostError
from douyin_wiki.models import (
    CaptureRequest,
    CreatorRecord,
    CreatorWorkAvailability,
    CreatorWorkDecision,
    CreatorWorkRecord,
    EntryRecord,
    GatewayContext,
    InspirationInput,
    JobStatus,
    ResearchTopic,
    RetentionPolicy,
    SourceKind,
    TopicArtifact,
    TopicSource,
)


def test_queue_claim_and_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    job = database.create_job(
        CaptureRequest(
            share_text="https://v.douyin.com/test/",
            inspirations=[InspirationInput(text="测试")],
        )
    )
    claimed = database.claim_next_job()
    assert claimed and claimed.id == job.id
    assert database.claim_next_job() is None
    assert database.recover_expired_jobs(datetime.now(UTC) + timedelta(minutes=10)) == 1
    assert database.claim_next_job() is not None


def test_replace_knowledge_cache_rolls_back_every_projection_on_restore_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    now = datetime.now(UTC)

    old_entry = EntryRecord(
        id="dy-111111111111",
        video_id="111111111111",
        title="旧资料",
        original_url="https://www.douyin.com/video/111111111111",
        canonical_url="https://www.douyin.com/video/111111111111",
        raw_path="raw/old.md",
        source_path="wiki/sources/old.md",
        status="active",
        media_status="present",
        retention=RetentionPolicy.KEEP,
        created_at=now,
        updated_at=now,
    )
    old_data = {
        "analysis": {
            "analysis_version": 2,
            "title": "旧资料",
            "one_liner": "旧摘要",
            "relevance_to_inspiration": "旧关联",
            "takeaways": ["旧要点"],
            "content_type": "other",
            "content_card": {"kind": "other"},
            "tags": [],
        }
    }
    database.persist_entry_bundle(
        old_entry,
        old_data,
        [{"id": "old-chunk", "kind": "summary", "text": "旧片段", "embedding": [1.0]}],
        [],
        [],
    )
    creator = CreatorRecord(
        id="dyc-old",
        sec_uid="old-sec-uid-123456",
        canonical_url="https://www.douyin.com/user/old-sec-uid-123456",
        original_url="https://v.douyin.com/old/",
        nickname="旧博主",
        folder_path="creators/旧博主",
        created_at=now,
        updated_at=now,
    )
    work = CreatorWorkRecord(
        creator_id=creator.id,
        work_id="111111111111",
        source_kind=SourceKind.VIDEO,
        canonical_url=old_entry.canonical_url,
        original_url=old_entry.original_url,
        title=old_entry.title,
        decision=CreatorWorkDecision.IMPORTED,
        availability=CreatorWorkAvailability.AVAILABLE,
        entry_id=old_entry.id,
        first_seen_at=now,
        last_seen_at=now,
    )
    database.restore_creator_bundle(creator, [work])
    topic = ResearchTopic(
        id="topic-old",
        title="旧专题",
        sources=[
            TopicSource(
                entry_id=old_entry.id,
                position=1,
                source_revision=now,
            )
        ],
        source_revision="old-revision",
        created_at=now,
        updated_at=now,
    )
    artifact = TopicArtifact(
        id="artifact-old",
        topic_id=topic.id,
        kind="note",
        title="旧成果",
        content_markdown="旧内容",
        source_revision="old-revision",
        created_at=now,
        updated_at=now,
        user_authored=True,
    )
    database.restore_topic_bundle(topic, [artifact])
    database.set_index_metadata("embedding_signature", "old-signature")
    job = database.create_job(
        CaptureRequest(
            share_text="https://www.douyin.com/video/111111111111",
            gateway_context=GatewayContext(gateway="test"),
        )
    )
    database.update_job(job.id, status=JobStatus.COMPLETED)
    database.create_creator_run_items(job.id, creator.id, [work.work_id])
    database.attach_creator_child_job(creator.id, work.work_id, job.id)
    session = database.create_chat_session(
        title="保留对话", scope="topic", context_topic_id=topic.id
    )
    database.add_chat_message(session.id, "user", "保留这条消息")
    database.record_maintenance("weekly", {"status": "保留"})

    with database.connect() as conn:
        before = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "entries",
                "chunks",
                "creators",
                "creator_works",
                "research_topics",
                "topic_sources",
                "topic_artifacts",
                "index_metadata",
                "jobs",
                "job_events",
                "creator_run_items",
                "web_chat_sessions",
                "web_chat_messages",
                "maintenance_runs",
            )
        }

    replacement = old_entry.model_copy(update={"id": "dy-222222222222", "video_id": "222222222222"})
    replacement_data = {**old_data, "analysis": {**old_data["analysis"], "title": "新资料"}}
    replacement_topic = topic.model_copy(
        update={
            "sources": [
                TopicSource(
                    entry_id=replacement.id,
                    position=1,
                    source_revision=now,
                )
            ]
        }
    )

    def fail_restore(*args, **kwargs):
        raise RuntimeError("restore failed")

    monkeypatch.setattr(database, "_restore_creator_bundle_conn", fail_restore)
    with pytest.raises(RuntimeError, match="restore failed"):
        database.replace_knowledge_cache(
            entries=[
                (
                    replacement,
                    replacement_data,
                    [{"id": "new-chunk", "kind": "summary", "text": "新片段", "embedding": [1.0]}],
                    [],
                    [],
                )
            ],
            creators=[(creator, [work])],
            topics=[(replacement_topic, [artifact])],
            embedding_signature="new-signature",
        )

    with database.connect() as conn:
        after = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in before
        }
    assert after == before

    monkeypatch.undo()
    database.replace_knowledge_cache(
        entries=[
            (
                replacement,
                replacement_data,
                [{"id": "new-chunk", "kind": "summary", "text": "新片段", "embedding": [1.0]}],
                [],
                [],
            )
        ],
        creators=[(creator, [work])],
        topics=[(replacement_topic, [artifact])],
        embedding_signature="new-signature",
    )
    with database.connect() as conn:
        preserved_after_success = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "jobs",
                "job_events",
                "creator_run_items",
                "web_chat_sessions",
                "web_chat_messages",
                "maintenance_runs",
            )
        }
    assert preserved_after_success == {
        table: before[table]
        for table in preserved_after_success
    }
    with database.connect() as conn:
        restored_work = conn.execute(
            "SELECT entry_id, last_job_id FROM creator_works WHERE creator_id=? AND work_id=?",
            (creator.id, work.work_id),
        ).fetchone()
    assert restored_work["entry_id"] is None
    assert restored_work["last_job_id"] == job.id


def test_list_jobs_accepts_none_limit_for_all_matching_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    for index in range(55):
        job = database.create_job(
            CaptureRequest(share_text=f"https://v.douyin.com/test-{index}/")
        )
        database.update_job(
            job.id,
            status=JobStatus.NEEDS_AUTH,
            result={"auth_scope": "video"},
        )

    jobs = database.list_jobs(status=JobStatus.NEEDS_AUTH, limit=None)

    assert len(jobs) == 55


def test_recovery_requeues_job_after_mid_stage_crash(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    job = database.create_job(CaptureRequest(share_text="https://v.douyin.com/test/"))
    assert database.claim_next_job(worker_id="worker-a", lease_seconds=60)
    database.update_job(job.id, status=JobStatus.DOWNLOADING)

    assert database.recover_expired_jobs(datetime.now(UTC) + timedelta(minutes=2)) == 1
    recovered = database.claim_next_job(worker_id="worker-b")
    assert recovered and recovered.id == job.id


def test_live_worker_lease_is_not_stolen(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    job = database.create_job(CaptureRequest(share_text="https://v.douyin.com/test/"))
    assert database.claim_next_job(worker_id="worker-a", lease_seconds=300)
    database.update_job(job.id, status=JobStatus.TRANSCRIBING)

    assert database.recover_expired_jobs(datetime.now(UTC)) == 0
    assert database.claim_next_job(worker_id="worker-b") is None


def test_legacy_purposes_payload_migrates_to_inspirations() -> None:
    request = CaptureRequest.model_validate(
        {
            "share_text": "https://v.douyin.com/test/",
            "purposes": [{"text": "旧字段内容"}],
        }
    )
    payload = request.model_dump(mode="json")
    assert request.inspirations[0].text == "旧字段内容"
    assert payload["inspirations"][0]["text"] == "旧字段内容"
    assert "purposes" not in payload


def test_initialize_migrates_chunks_with_image_index(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE chunks (
                id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp_ms INTEGER,
                purposes_text TEXT NOT NULL DEFAULT '',
                tags_text TEXT NOT NULL DEFAULT '',
                embedding_json TEXT,
                stale INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute("PRAGMA user_version=2")
    database = Database(path)
    database.initialize()
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(chunks)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "image_index" in columns
    assert version == 10


def test_initialize_adds_favorite_to_existing_entries_table(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE entries (
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
            INSERT INTO entries (
                id, video_id, title, original_url, canonical_url, raw_path, source_path,
                created_at, updated_at
            ) VALUES (
                'dy-legacy', 'legacy', '旧资料', 'https://example.com/legacy',
                'https://example.com/legacy', 'raw/legacy.md', 'wiki/sources/legacy.md',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'
            );
            PRAGMA user_version=9;
            """
        )
    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        favorite = connection.execute(
            "SELECT favorite FROM entries WHERE id='dy-legacy'"
        ).fetchone()[0]

    assert "favorite" in columns
    assert version == 10
    assert favorite == 0


def test_entry_favorite_round_trips_through_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    now = datetime.now(UTC)
    entry = EntryRecord(
        id="dy-123456789012",
        video_id="123456789012",
        title="收藏测试",
        original_url="https://www.douyin.com/video/123456789012",
        canonical_url="https://www.douyin.com/video/123456789012",
        raw_path="raw/收藏测试.md",
        source_path="wiki/sources/收藏测试.md",
        status="active",
        media_status="present",
        retention=RetentionPolicy.KEEP,
        favorite=True,
        created_at=now,
        updated_at=now,
    )

    database.upsert_entry(entry, {})

    assert database.get_entry(entry.id).favorite is True


def test_expired_media_query_never_returns_favorite_entry(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    now = datetime.now(UTC)
    entry = EntryRecord(
        id="dy-inconsistent",
        video_id="inconsistent",
        title="收藏保护",
        original_url="https://example.com/inconsistent",
        canonical_url="https://example.com/inconsistent",
        raw_path="raw/inconsistent.md",
        source_path="wiki/sources/inconsistent.md",
        status="active",
        media_status="present",
        retention=RetentionPolicy.TEMPORARY,
        favorite=True,
        media_expires_at=now - timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    database.upsert_entry(entry, {})

    assert database.entries_with_expired_media(now) == []


def test_stale_worker_cannot_update_or_unlock_new_owner(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    job = database.create_job(CaptureRequest(share_text="https://v.douyin.com/test/"))
    assert database.claim_next_job(worker_id="worker-a", lease_seconds=30)
    assert database.recover_expired_jobs(datetime.now(UTC) + timedelta(minutes=1)) == 1
    claimed = database.claim_next_job(worker_id="worker-b", lease_seconds=300)
    assert claimed and claimed.id == job.id

    with database.claimed_job_updates(job.id, "worker-a"), pytest.raises(JobLeaseLostError):
        database.update_job(job.id, status=JobStatus.COMPLETED, unlock=True)

    current = database.get_job(job.id)
    assert current.status == JobStatus.QUEUED
    with database.connect() as connection:
        owner = connection.execute("SELECT lock_owner FROM jobs WHERE id=?", (job.id,)).fetchone()
    assert owner["lock_owner"] == "worker-b"
