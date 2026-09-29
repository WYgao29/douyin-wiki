from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from douyin_wiki.errors import JobLeaseLostError
from douyin_wiki.models import CaptureRequest, InspirationInput, JobStatus
from douyin_wiki.web_auth import WebAuthManager
from douyin_wiki.webapp.catalog import LibraryCatalog
from douyin_wiki.webapp.rendering import render_article
from douyin_wiki.worker import Worker
from tests.conftest import FakeDownloader


@pytest.mark.asyncio
async def test_media_restore_preserves_new_analysis_and_inspiration(service) -> None:
    service.capture_douyin("https://www.douyin.com/video/7672717300746907078")
    captured = await Worker(service).run_once()
    entry_id = captured.result["entry_id"]
    service.database.mark_media_removed(entry_id)
    restore = service.set_entry_favorite(entry_id, True)["restore_job"]
    started, release = asyncio.Event(), asyncio.Event()

    class PausedDownloader(FakeDownloader):
        async def download(self, *args, **kwargs):
            started.set()
            await release.wait()
            return await super().download(*args, **kwargs)

    service.downloader = PausedDownloader()
    running = asyncio.create_task(Worker(service).run_once())
    await asyncio.wait_for(started.wait(), timeout=5)
    previous = service.database.get_entry_data(entry_id)["analysis"]
    service.submit_analysis(
        entry_id,
        {**previous, "one_liner": "R7_UNIQUE_ANALYSIS", "summary": "R7_UNIQUE_ANALYSIS"},
        producer="regression-test",
    )
    service.add_inspiration(entry_id, InspirationInput(text="R7_UNIQUE_INSPIRATION"))
    release.set()
    completed = await running

    assert completed.id == restore.id and completed.status == JobStatus.COMPLETED
    entry = service.database.get_entry(entry_id)
    data = service.database.get_entry_data(entry_id)
    assert entry.summary == data["analysis"]["one_liner"] == "R7_UNIQUE_ANALYSIS"
    assert [item.text for item in entry.inspirations] == ["R7_UNIQUE_INSPIRATION"]
    assert data["inspirations"][0]["text"] == "R7_UNIQUE_INSPIRATION"
    assert Path(data["metadata"]["media_path"]).is_file()
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    assert "R7_UNIQUE_ANALYSIS" in source
    assert "R7_UNIQUE_INSPIRATION" in source
    chunks = service.database.fetch_chunks(entry_ids=[entry_id])
    assert any("R7_UNIQUE_ANALYSIS" in chunk["text"] for chunk in chunks)


@pytest.mark.asyncio
async def test_media_restore_lost_lease_does_not_publish(service, monkeypatch) -> None:
    service.capture_douyin("https://www.douyin.com/video/7672717300746907078")
    captured = await Worker(service).run_once()
    entry_id = captured.result["entry_id"]
    service.database.mark_media_removed(entry_id)
    restore = service.set_entry_favorite(entry_id, True)["restore_job"]
    before = service.database.get_entry(entry_id)
    source_path = service.config.vault_path / before.source_path
    source_before = source_path.read_bytes()
    data_before = service.database.get_entry_data(entry_id)
    chunks_before = service.database.fetch_chunks(entry_ids=[entry_id])
    publish = service.persist_entry_documents_and_bundle_locked

    def steal(*args, **kwargs):
        with service.database.connect() as conn:
            conn.execute("UPDATE jobs SET lock_owner='replacement' WHERE id=?", (restore.id,))
        return publish(*args, **kwargs)

    monkeypatch.setattr(service, "persist_entry_documents_and_bundle_locked", steal)
    with pytest.raises(JobLeaseLostError):
        await Worker(service).run_once()
    assert service.database.get_entry(entry_id) == before
    assert service.database.get_entry_data(entry_id) == data_before
    assert service.database.fetch_chunks(entry_ids=[entry_id]) == chunks_before
    assert source_path.read_bytes() == source_before


@pytest.mark.asyncio
async def test_web_auth_replaces_orphan_but_reuses_live_executor(service, monkeypatch) -> None:
    orphan = service.database.create_auth_session(
        channel="douyin", scope="image_note", stage="waiting_login"
    )
    wait = asyncio.Event()

    async def hold(self, session_id, *, timeout_seconds):
        self._database().update_auth_session(session_id, stage="waiting_login")
        await wait.wait()
        return self._database().get_auth_session(session_id)

    monkeypatch.setattr(WebAuthManager, "_run", hold)
    first = WebAuthManager(service)
    second = WebAuthManager(service)
    created = await first.start("douyin")
    assert created["id"] != orphan["id"]
    assert service.database.get_auth_session(orphan["id"])["stage"] == "cancelled"
    assert (await first.start("douyin"))["id"] == created["id"]
    assert len(first._tasks) == 1
    assert (await second.start("douyin"))["id"] == created["id"]
    assert not second._tasks
    await first.close()
    assert service.database.get_auth_session(created["id"])["stage"] == "cancelled"
    assert not first._tasks
    replacement = await second.start("douyin")
    assert replacement["id"] != created["id"]
    await second.close()


def test_creator_parent_reopens_on_child_retry_and_reaggregates(service) -> None:
    request = CaptureRequest(share_text="https://www.douyin.com/video/7000000000000000001")
    parent = service.database.create_job(request, kind="creator_import")
    child = service.database.create_job(
        request, artifacts={"creator_context": {"parent_job_id": parent.id}}
    )
    service.database.update_job(child.id, status=JobStatus.FAILED, unlock=True)
    service.database.update_job(
        parent.id, status=JobStatus.MONITORING, result={"child_job_ids": [child.id]}, unlock=True
    )
    service.refresh_creator_parent(parent.id)
    assert service.database.get_job(parent.id).status == JobStatus.COMPLETED_WITH_WARNINGS

    service.retry_job(child.id)
    reopened = service.database.get_job(parent.id)
    assert reopened.status == JobStatus.MONITORING
    assert reopened.result["child_status_counts"] == {"queued": 1}
    assert reopened.result["finished_count"] == 0
    assert reopened.result["warning_count"] == 0

    service.database.update_job(child.id, status=JobStatus.COMPLETED, unlock=True)
    service.refresh_creator_parent(parent.id)
    finished = service.database.get_job(parent.id)
    assert finished.status == JobStatus.COMPLETED
    assert finished.result["child_status_counts"] == {"completed": 1}
    assert finished.result["warning_count"] == 0


def test_creator_parent_rejects_stale_child_snapshot(service, monkeypatch) -> None:
    request = CaptureRequest(share_text="https://www.douyin.com/video/7000000000000000001")
    parent = service.database.create_job(request, kind="creator_import")
    child = service.database.create_job(request)
    service.database.update_job(
        parent.id, status=JobStatus.MONITORING, result={"child_job_ids": [child.id]}, unlock=True
    )
    update = service.database.update_job
    changed = False

    def race(job_id, **kwargs):
        nonlocal changed
        if job_id == parent.id and not changed:
            changed = True
            update(child.id, status=JobStatus.COMPLETED, unlock=True)
        return update(job_id, **kwargs)

    monkeypatch.setattr(service.database, "update_job", race)
    service.refresh_creator_parent(parent.id)
    assert service.database.get_job(parent.id).result == {"child_job_ids": [child.id]}
    service.refresh_creator_parent(parent.id)
    assert service.database.get_job(parent.id).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_wikilink_navigation_keeps_unsafe_paths_blocked(service) -> None:
    service.capture_douyin("https://www.douyin.com/video/7672717300746907078")
    captured = await Worker(service).run_once()
    entry = service.database.get_entry(captured.result["entry_id"])
    catalog = LibraryCatalog(service.config.vault_path, service.database)
    catalog.refresh()
    target = Path(entry.source_path).with_suffix("").as_posix()
    rendered = render_article(
        f"[[{target}|关联资料]]\n\n"
        "[绝对路径](/etc/passwd) [无效文章](/articles/unknown) "
        "[越界](../../outside.md) [脚本](javascript:alert)",
        source_path=entry.source_path,
        catalog=catalog,
    )
    assert f'href="/articles/{entry.id}"' in rendered
    assert rendered.count('href="#"') == 4
    assert "javascript:" not in rendered
