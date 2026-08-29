from __future__ import annotations

import copy
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.adapters.llm import OpenAICompatibleProvider
from douyin_wiki.config import EmbeddingSettings, LLMSettings
from douyin_wiki.errors import CookieRequiredError, ExternalToolError, JobStateError
from douyin_wiki.models import (
    AnalysisMode,
    CaptureOptions,
    GatewayContext,
    InspirationInput,
    JobStatus,
    TranscriptCorrection,
)
from douyin_wiki.worker import Worker

from .conftest import FakeDownloader, FakeTranscriber


class CookieExpiredDownloader:
    async def download(self, url: str, video_id: str, target_dir: Path):
        raise CookieRequiredError("需要更新浏览器 cookie")


class BrokenOCR:
    async def recognize(self, frames):
        raise ExternalToolError("synthetic OCR failure")


@pytest.mark.asyncio
async def test_gateway_agent_handoff_completes_and_emits_routed_events(service) -> None:
    service.config.analysis_mode = AnalysisMode.GATEWAY
    job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        [InspirationInput(text="建立个人财经信息源筛选标准")],
        gateway_context=GatewayContext(
            gateway="hermes",
            channel="telegram",
            conversation_id="chat-42",
            reply_target="user-7",
        ),
    )

    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert paused.result["phase"] == "transcript_correction"
    context = service.get_analysis_context(job.id)
    assert context["gateway_context"]["conversation_id"] == "chat-42"
    assert context["inspirations_verbatim"][0]["text"] == "建立个人财经信息源筛选标准"
    assert context["analysis_schema"]["title"] == "AnalysisResultV2"
    assert "chapters" in context["analysis_schema"]["properties"]
    assert "key_moments" not in context["analysis_schema"]["properties"]

    corrected = service.submit_transcript_correction(
        job.id,
        [TranscriptCorrection(id=0, text="离职以后，我取关了很多财经媒体。")],
        producer="hermes",
        model="gateway-model",
    )
    assert corrected.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert corrected.result["phase"] == "analysis"
    queued = service.submit_gateway_analysis(
        job.id,
        {
            "title": "离职后如何筛选财经媒体",
            "summary": "优先保留能够提供一手证据的信息源。",
            "core_points": ["信息源质量比数量重要"],
            "tags": ["财经", "信息筛选"],
            "ai_judgment": "值得保存。",
        },
        producer="hermes",
        model="gateway-model",
    )
    assert queued.status == JobStatus.QUEUED

    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    data = service.database.get_entry_data(completed.result["entry_id"])
    assert data["transcript_raw"][0]["text"] == "离职以后我取关了很多财经媒体。"
    assert data["transcript_corrected"][0]["text"] == "离职以后，我取关了很多财经媒体。"
    assert data["provider"] == "agent:hermes"
    assert data["model"] == "gateway-model"

    events = service.list_job_events()
    assert [event.status for event in events] == [JobStatus.COMPLETED]
    assert all(event.gateway_context.gateway == "hermes" for event in events)
    history = service.list_job_events(unacknowledged_only=False)
    assert [event.status for event in history] == [
        JobStatus.AWAITING_AGENT_ANALYSIS,
        JobStatus.AWAITING_AGENT_ANALYSIS,
        JobStatus.COMPLETED,
    ]
    assert history[0].superseded_at is not None
    assert history[1].superseded_at is not None
    acknowledged = service.acknowledge_job_event(events[0].id)
    assert acknowledged.acknowledged_at is not None
    assert service.acknowledge_job_event(events[0].id).acknowledged_at is not None
    assert events[0].id not in {event.id for event in service.list_job_events()}


@pytest.mark.asyncio
async def test_jobs_without_gateway_route_do_not_create_delivery_events(service) -> None:
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.id == job.id
    assert completed.status == JobStatus.COMPLETED
    assert service.list_job_events() == []


@pytest.mark.asyncio
async def test_provider_mode_does_not_silently_fall_back_when_unconfigured(service) -> None:
    service.analysis = OpenAICompatibleProvider(service.config.llm)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    failed = await Worker(service).run_once()
    assert failed.id == job.id
    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "model_not_configured"
    assert service.database.list_entries() == []


@pytest.mark.asyncio
async def test_ocr_failure_is_visible_as_completed_warning(service) -> None:
    service.ocr = BrokenOCR()
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED_WITH_WARNINGS
    assert "OCR 未完成" in completed.result["warnings"][0]


@pytest.mark.asyncio
async def test_end_to_end_capture_writes_vault_and_searches(service) -> None:
    inspiration = InspirationInput(
        text="建立个人财经信息源筛选标准",
        quote="筛选信息源要看它能否提供一手证据",
        start_ms=5000,
        end_ms=9000,
    )
    job = service.capture_douyin(
        "6.43 复制打开抖音 https://v.douyin.com/uvHsRpXIn8s/",
        [inspiration],
    )
    completed = await Worker(service).run_once()
    assert completed.id == job.id
    assert completed.status == JobStatus.COMPLETED
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    raw = service.config.vault_path / entry.raw_path
    source = service.config.vault_path / entry.source_path
    assert raw.exists() and source.exists()
    assert "建立个人财经信息源筛选标准" in raw.read_text(encoding="utf-8")
    assert "https://v.douyin.com/uvHsRpXIn8s/" in source.read_text(encoding="utf-8")
    assert "author: Nee霓公子" in source.read_text(encoding="utf-8")
    source_text = source.read_text(encoding="utf-8")
    assert "status: 正常" in source_text
    assert "media_status: 已保留" in source_text
    assert "media_retention: 临时保留" in source_text
    assert "cover_image: raw/covers/7672717300746907078.jpg" in source_text
    assert "cover_kind: fallback" in source_text
    assert "analysis_version: 2" in source_text
    assert "machine_data_path: wiki/.data/sources/7672717300746907078.md" in source_text
    assert "![抖音视频封面](../../raw/covers/7672717300746907078.jpg)" in source_text
    assert "## 一句话" in source_text
    assert "## 关键主张" not in source_text
    assert "核验" not in source_text
    assert "外部来源" not in source_text
    assert "verification_status" not in source_text
    assert (service.config.vault_path / "raw" / "covers" / "7672717300746907078.jpg").is_file()
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / "7672717300746907078.md"
    assert machine.is_file()
    assert "knowledge_atoms:" in machine.read_text(encoding="utf-8")
    assert "[00:05]" in raw.read_text(encoding="utf-8")

    evidence = service.search_knowledge("如何筛选财经信息源")
    assert evidence
    assert evidence[0].original_url == "https://v.douyin.com/uvHsRpXIn8s/"
    assert any(item.timestamp_ms == 5000 for item in evidence)


@pytest.mark.asyncio
async def test_duplicate_video_appends_inspiration_without_redownload(service) -> None:
    downloader = service.downloader
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="灵感一")])
    first = await Worker(service).run_once()
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="灵感二")])
    second = await Worker(service).run_once()
    assert second.result["duplicate"] is True
    assert downloader.calls == 1
    entry = service.database.get_entry(first.result["entry_id"])
    assert [item.text for item in entry.inspirations] == ["灵感一", "灵感二"]


@pytest.mark.asyncio
async def test_backfill_video_cover_for_legacy_entry(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    cover = service.config.vault_path / data.pop("cover_path")
    cover.unlink()
    service.database.upsert_entry(entry, data)
    service.vault.refresh_source(entry, data)

    report = service.backfill_video_covers()
    assert report["updated_entries"] == [entry.id]
    migrated = service.database.get_entry_data(entry.id)
    assert migrated["cover_path"] == f"raw/covers/{entry.video_id}.jpg"
    assert (service.config.vault_path / migrated["cover_path"]).is_file()
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    assert "![抖音视频封面]" in source


@pytest.mark.asyncio
async def test_remove_external_validation_migrates_legacy_storage(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    data = service.database.get_entry_data(entry_id)
    data["fact_checks"] = {"claim-1": {"verdict": "supported"}}
    data["analysis"]["claims"][0]["verification_status"] = "verified"
    data["analysis"]["risks"] = ["具体版本和参数仍处于外部未核验状态。"]
    service.database.upsert_entry(entry, data)

    with service.database.connect() as conn:
        conn.execute(
            "ALTER TABLE entries ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'pending'"
        )
        conn.execute(
            """CREATE TABLE fact_checks (
                claim_id TEXT NOT NULL,
                entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(entry_id, claim_id)
            )"""
        )
        conn.execute(
            "INSERT INTO fact_checks VALUES (?, ?, ?, ?)",
            ("claim-1", entry_id, "{}", datetime.now(UTC).isoformat()),
        )
        conn.execute(
            """UPDATE jobs SET status='completed_with_warnings',
               result_json=? WHERE id=?""",
            (
                '{"pending_fact_checks":["claim-1"],"warnings":["关键主张尚未核验"]}',
                completed.id,
            ),
        )

    report = service.remove_external_validation()
    assert report["removed_checks"] == 1
    migrated = service.database.get_entry_data(entry_id)
    assert "fact_checks" not in migrated
    assert "verification_status" not in migrated["analysis"]["claims"][0]
    assert migrated["analysis"]["risks"] == ["具体版本和参数需结合原始配方与实际冲煮进一步确认。"]
    assert service.get_job(completed.id).status == JobStatus.COMPLETED
    with service.database.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_checks'"
        ).fetchone()
    assert "verification_status" not in columns
    assert table is None


@pytest.mark.asyncio
async def test_low_confidence_review_pauses_and_resumes(service) -> None:
    service.transcriber = FakeTranscriber(low_confidence=True)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.NEEDS_REVIEW
    issues = service.get_job(job.id).result["review_issues"]
    assert issues[0]["id"] == "asr-1"
    service.resolve_review(job.id, {"asr-1": "10号会打五折。"})
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    data = service.database.get_entry_data(completed.result["entry_id"])
    assert data["transcript_corrected"][1]["text"] == "10号会打五折。"


@pytest.mark.asyncio
async def test_long_video_requires_confirmation(service) -> None:
    service.downloader = FakeDownloader(duration=31 * 60)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    service.approve_job(job.id)
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_two_hour_limit_can_be_overridden(service) -> None:
    service.downloader = FakeDownloader(duration=121 * 60)
    service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        options=CaptureOptions(approve_cloud_analysis=True),
    )
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    assert paused.result["reason"] == "video_too_long"
    service.approve_job(paused.id)
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_long_job_can_continue_from_checkpoint(service) -> None:
    service.downloader = FakeDownloader(duration=121 * 60)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.WAITING_CONFIRMATION
    assert "metadata" in paused.artifacts
    retried = service.approve_job(job.id)
    assert retried.status == JobStatus.QUEUED
    assert retried.artifacts["metadata"] == paused.artifacts["metadata"]


@pytest.mark.asyncio
async def test_video_cookie_failure_enters_needs_auth_and_can_retry(service) -> None:
    service.downloader = CookieExpiredDownloader()
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    paused = await Worker(service).run_once()
    assert paused.status == JobStatus.NEEDS_AUTH
    assert paused.error_code == "cookie_required"
    assert paused.result["auth_scope"] == "video"
    assert paused.result["next_command"] == "douyin-wiki auth video"
    assert paused.result["retry_command"].endswith(job.id)
    service.downloader = FakeDownloader()
    assert service.retry_job(job.id).status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_duplicate_reacquires_removed_media_and_keeps_stable_page(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="初始灵感")])
    first = await Worker(service).run_once()
    entry = service.database.get_entry(first.result["entry_id"])
    service.database.mark_media_removed(entry.id)

    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/", [InspirationInput(text="追加灵感")])
    second = await Worker(service).run_once()
    updated = service.database.get_entry(entry.id)
    assert second.result["reacquired"] is True
    assert service.downloader.calls == 2
    assert updated.media_status == "present"
    assert updated.raw_path == entry.raw_path
    assert updated.source_path == entry.source_path
    source = (service.config.vault_path / updated.source_path).read_text(encoding="utf-8")
    assert "初始灵感" in source and "追加灵感" in source


@pytest.mark.asyncio
async def test_agent_can_submit_reanalysis_with_provenance(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    data = service.database.get_entry_data(entry_id)
    analysis = data["analysis"]
    analysis["summary"] = "Agent 补充后的摘要。"
    analysis["title"] = "Agent 重分析标题"
    entry = service.submit_analysis(entry_id, analysis, producer="codex", model="agent-model")
    updated = service.database.get_entry_data(entry_id)
    assert entry.summary == "Agent 补充后的摘要。"
    assert updated["provider"] == "agent:codex"
    assert updated["model"] == "agent-model"
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    assert "Agent 补充后的摘要" in source


@pytest.mark.asyncio
async def test_contradiction_keeps_both_sources_and_creates_relation(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    data = service.database.get_entry_data(entry_id)

    target = entry.model_copy(
        update={
            "id": "dy-prior-video",
            "video_id": "prior-video",
            "title": "既有相反观点",
            "original_url": "https://www.douyin.com/video/prior-video",
            "canonical_url": "https://www.douyin.com/video/prior-video",
            "raw_path": "raw/prior.md",
            "source_path": "wiki/sources/prior.md",
        }
    )
    target_data = copy.deepcopy(data)
    target_data["analysis"]["title"] = target.title
    target_data["analysis"]["claims"][0]["id"] = "prior-claim"
    target_data["analysis"]["claims"][0]["text"] = "高质量信息源不需要提供一手证据"
    service.database.upsert_entry(target, target_data)
    service.indexer.index_entry(target, target_data)

    analysis = copy.deepcopy(data["analysis"])
    analysis["contradictions"] = [
        {
            "id": "conflict-1",
            "claim_id": "claim-1",
            "conflicts_with_entry_id": target.id,
            "conflicts_with_claim_id": "prior-claim",
            "reason": "两条主张对一手证据的必要性结论相反",
            "confidence": 0.96,
            "status": "open",
        }
    ]
    service.submit_analysis(entry.id, analysis, producer="codex")
    relations = service.database.get_relations(entry.id)
    assert any(item["relation_type"] == "contradiction" for item in relations)
    machine = (
        service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    ).read_text(encoding="utf-8")
    assert "contradictions:" in machine
    assert "既有相反观点" in machine
    assert "两条主张对一手证据的必要性结论相反" in machine


@pytest.mark.asyncio
async def test_maintenance_marks_expired_claim_stale(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    data["analysis"]["claims"][0]["valid_until"] = "2020-01-01T00:00:00+00:00"
    service.database.upsert_entry(entry, data)
    service.indexer.index_entry(entry, data)
    report = service.run_maintenance(apply=True)
    assert report["stale_chunks"] == 1
    updated = service.database.get_entry_data(entry.id)
    assert updated["analysis"]["claims"][0]["stale"] is True
    machine = (
        service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    ).read_text(encoding="utf-8")
    assert "stale: true" in machine


@pytest.mark.asyncio
async def test_maintenance_honors_review_after_without_double_counting(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    for field in ("claims", "knowledge_atoms"):
        data["analysis"][field][0]["review_after"] = "2020-01-01T00:00:00+00:00"
    service.database.upsert_entry(entry, data)
    report = service.run_maintenance(apply=True)
    assert report["stale_chunks"] == 1
    updated = service.database.get_entry_data(entry.id)["analysis"]
    assert updated["claims"][0]["stale"] is True
    assert updated["knowledge_atoms"][0]["stale"] is True


@pytest.mark.asyncio
async def test_confirm_reminder_requires_absolute_time(service, fake_reminders) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    with pytest.raises(JobStateError, match="confirmed=true"):
        service.confirm_reminder(
            entry_id,
            "reminder-1",
            due_at="2026-09-01T09:00:00+08:00",
        )
    result = service.confirm_reminder(
        entry_id,
        "reminder-1",
        due_at="2026-09-01T09:00:00+08:00",
        confirmed=True,
    )
    assert result["system_id"] == "system-reminder-1"
    assert fake_reminders.created
    repeated = service.confirm_reminder(entry_id, "reminder-1", confirmed=True)
    assert repeated["system_id"] == "system-reminder-1"
    assert repeated["candidate"]["due_at"] == "2026-09-01T09:00:00+08:00"
    assert len(fake_reminders.created) == 1
    service.rebuild_database_from_vault(apply=True)
    rebuilt = service.confirm_reminder(entry_id, "reminder-1", confirmed=True)
    assert rebuilt["system_id"] == "system-reminder-1"
    assert len(fake_reminders.created) == 1


@pytest.mark.asyncio
async def test_two_character_chinese_search_uses_lexical_index(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    data["analysis"]["one_liner"] = "今天教你苹果手机拍照技巧"
    service.database.upsert_entry(entry, data)
    service.indexer.index_entry(entry, data)

    assert service.search_knowledge("苹果")[0].entry_id == entry.id
    assert service.search_knowledge("拍照")[0].entry_id == entry.id
    assert service.search_knowledge("手机技巧")[0].entry_id == entry.id

    substring_rows = service.database.fts_search(
        '("不匹配")', raw_query="手机拍照"
    )
    assert substring_rows[0]["match_kind"] == "exact_substring"
    assert substring_rows[0]["rank"] is None
    assert substring_rows[0]["match_quality"] < 1


@pytest.mark.asyncio
async def test_rebuild_reports_bad_entry_without_clearing_database(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    entry = service.database.get_entry(entry_id)
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    machine.write_text("---\ntype: broken\n---\n", encoding="utf-8")

    report = service.rebuild_database_from_vault(apply=False)
    assert report["entry_errors"]
    with pytest.raises(JobStateError, match="entry_errors"):
        service.rebuild_database_from_vault(apply=True)
    assert service.database.get_entry(entry_id).id == entry_id


@pytest.mark.asyncio
async def test_embedding_signature_change_rebuilds_all_cached_vectors(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    replacement = EmbeddingService(EmbeddingSettings(provider="hash", fallback_dimensions=32))
    service.embeddings = replacement
    service.indexer.embeddings = replacement
    service.searcher.embeddings = replacement
    assert service.search_knowledge("一手证据")
    rows = service.database.fetch_chunks(include_stale=True)
    assert rows
    assert {len(json.loads(row["embedding_json"])) for row in rows} == {32}
    assert service.database.get_index_metadata("embedding_signature").endswith("dim=32")


@pytest.mark.asyncio
async def test_maintenance_moves_expired_media_recoverably(service, monkeypatch, tmp_path) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    expired = entry.model_copy(update={"media_expires_at": datetime.now(UTC) - timedelta(days=1)})
    data = service.database.get_entry_data(entry.id)
    service.database.upsert_entry(expired, data)
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_send2trash(value: str) -> None:
        shutil.move(value, trash / Path(value).name)

    monkeypatch.setattr("douyin_wiki.service.send2trash", fake_send2trash)
    report = service.run_maintenance(apply=True)
    assert entry.id in report["media_removed"]
    assert service.database.get_entry(entry.id).media_status == "removed"
    assert (trash / entry.video_id).exists()
    assert (service.config.vault_path / "raw" / "covers" / f"{entry.video_id}.jpg").is_file()


def test_worker_catches_up_missed_weekly_maintenance(service) -> None:
    worker = Worker(service)
    now = datetime(2026, 8, 18, 10, tzinfo=UTC)
    first = worker.run_due_maintenance(now)
    second = worker.run_due_maintenance(now)
    assert first is not None and first["dry_run"] is False
    assert second is None


def test_worker_detects_source_change_before_claiming_new_jobs(service) -> None:
    worker = Worker(
        service,
        loaded_signature="loaded-code",
        signature_provider=lambda: "updated-code",
    )
    assert worker.source_changed() is True


@pytest.mark.asyncio
async def test_worker_continues_when_catch_up_maintenance_fails(
    service, monkeypatch, capsys
) -> None:
    worker = Worker(
        service,
        loaded_signature="loaded-code",
        signature_provider=lambda: "updated-code",
    )

    def fail_maintenance():
        raise ValueError("broken maintenance")

    monkeypatch.setattr(worker, "run_due_maintenance", fail_maintenance)
    await worker.run_forever()

    captured = capsys.readouterr()
    assert "Worker 将继续处理采集任务" in captured.err


def test_embedding_model_failure_uses_deterministic_fallback(monkeypatch) -> None:
    class BrokenSentenceTransformer:
        def __init__(self, _: str) -> None:
            raise ValueError("broken model cache")

    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        BrokenSentenceTransformer,
    )
    embeddings = EmbeddingService(
        EmbeddingSettings(provider="sentence-transformers", fallback_dimensions=32)
    )

    first = embeddings.embed(["本地检索"])[0]
    second = embeddings.embed(["本地检索"])[0]

    assert first == second
    assert len(first) == 32
    assert embeddings.provider_name == "char-ngram-fallback"
    assert embeddings.last_error == "ValueError: broken model cache"


def test_loaded_embedding_model_failure_never_persists_fallback_vectors() -> None:
    class FlakyModel:
        calls = 0

        def get_sentence_embedding_dimension(self) -> int:
            return 3

        def encode(self, texts, **_):
            self.calls += 1
            if self.calls == 1:
                return [[1.0, 0.0, 0.0] for _ in texts]
            raise RuntimeError("temporary model failure")

    embeddings = EmbeddingService(EmbeddingSettings(provider="sentence-transformers"))
    embeddings._model = FlakyModel()
    embeddings._load_attempted = True
    embeddings._dimensions = 3
    embeddings.provider_name = "sentence-transformers:test"

    assert embeddings.embed(["first"], persistent=True) == [[1.0, 0.0, 0.0]]
    with pytest.raises(ExternalToolError, match="未写入不兼容向量"):
        embeddings.embed(["second"], persistent=True)
    assert embeddings.provider_name == "sentence-transformers:test"
    assert embeddings.embed(["query"]) == [[0.0, 0.0, 0.0]]


def test_provider_mode_allows_loopback_endpoint_without_api_key(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.adapters.llm.get_secret", lambda _: "")
    provider = OpenAICompatibleProvider(
        LLMSettings(base_url="http://localhost:11434/v1", model="local-model")
    )

    assert provider.configured is True
    assert provider.api_key == ""
