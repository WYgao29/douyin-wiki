from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from douyin_wiki.database import Database
from douyin_wiki.models import CaptureRequest, InspirationInput, JobStatus


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
    assert version == 8
