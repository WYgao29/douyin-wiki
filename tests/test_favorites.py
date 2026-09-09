from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from douyin_wiki.errors import BrowserAuthRequiredError, JobStateError
from douyin_wiki.models import CaptureRequest, GatewayContext, JobStatus

A = "7000000000000000001"
B = "7000000000000000002"
C = "7000000000000000003"


def snapshot(*, complete=True, account="test-account", kinds=None):
    from douyin_wiki.favorites_models import FavoriteFolder, FavoriteInventory, FavoriteWork

    kinds = kinds or [(A, "video"), (B, "image_note"), (C, "article")]
    return FavoriteInventory(
        account_id=account,
        nickname="测试用户",
        complete=complete,
        folders_complete=True,
        folders=[FavoriteFolder(id="folder-1", name="测试夹", reported_count=3)],
        works=[
            FavoriteWork(
                work_id=work_id,
                canonical_url=(
                    f"https://www.douyin.com/{'note' if kind == 'image_note' else kind}/{work_id}"
                ),
                title=f"测试作品 {work_id}",
                source_kind=kind,
                folder_ids=["folder-1"],
            )
            for work_id, kind in kinds
        ],
    )


class FakeFavoritesAdapter:
    def __init__(self, result, *, failure=None):
        self.result = result
        self.failure = failure

    async def inventory(self, **kwargs):
        if kwargs.get("on_checkpoint"):
            await kwargs["on_checkpoint"](self.result)
        if self.failure:
            raise self.failure
        return self.result


async def scan(service, result=None, **kwargs):
    assert hasattr(service, "favorites"), "service must expose the new favorites workflow"
    service.favorites.adapter = FakeFavoritesAdapter(result or snapshot())
    parent = service.favorites.start(**kwargs)
    return await service.process_claimed_job(parent)


async def test_inventory_never_queues_media_and_filters_unsupported_types(service):
    parent = await scan(service)
    assert parent.status == JobStatus.NEEDS_SELECTION
    assert [j.kind for j in service.database.list_jobs()] == ["favorites_import"]
    data = service.favorites.get(parent.id)
    assert data["summary"]["discovered"] == 3
    assert data["summary"]["selected"] == 1
    assert data["summary"]["unsupported"] == 1
    assert data["items"][2]["disposition"] == "unsupported"


async def test_confirm_is_explicit_idempotent_and_respects_include_images(service):
    parent = await scan(service, include_images=True)
    first = service.favorites.confirm(parent.id)
    second = service.favorites.confirm(parent.id)
    children = [j for j in service.database.list_jobs() if j.kind == "capture"]
    assert first.id == second.id == parent.id
    assert len(children) == 2
    assert {j.request.share_text for j in children} == {
        f"https://www.douyin.com/video/{A}",
        f"https://www.douyin.com/note/{B}",
    }
    assert all(not j.request.options.allow_long for j in children)


async def test_partial_requires_explicit_acceptance(service):
    parent = await scan(service, snapshot(complete=False))
    with pytest.raises(JobStateError):
        service.favorites.confirm(parent.id)
    assert len(service.database.list_jobs()) == 1
    service.favorites.confirm(parent.id, accept_partial=True)
    assert len(service.database.list_jobs()) == 2
    assert not service.favorites.get(parent.id)["complete"]


async def test_cross_parent_reuse_keeps_original_gateway_context(service):
    original = service.database.create_job(
        CaptureRequest(
            share_text=f"https://www.douyin.com/video/{A}",
            gateway_context=GatewayContext(gateway="original"),
        )
    )
    parents = [await scan(service), await scan(service)]
    for parent in parents:
        service.favorites.confirm(parent.id)
        assert service.favorites.get(parent.id)["items"][0]["job_id"] == original.id
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 1
    assert service.database.get_job(original.id).request.gateway_context.gateway == "original"
    service.database.update_job(original.id, status=JobStatus.NEEDS_REVIEW)
    service.favorites.refresh_all()
    for parent in parents:
        data = service.favorites.get(parent.id)
        assert data["status"] == "monitoring"
        assert data["summary"]["child_status_counts"] == {"needs_review": 1}


async def test_concurrent_confirm_creates_one_child(service):
    parent = await scan(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.favorites.confirm(parent.id), range(2)))
    assert results[0].id == results[1].id
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 1


async def test_selection_is_whole_inventory_and_rejects_unknown_ids(service):
    parent = await scan(service, snapshot(kinds=[(A, "video"), (B, "video")]))
    assert len(service.favorites.get(parent.id, limit=1)["items"]) == 1
    service.favorites.select(parent.id, selected=False)
    assert service.favorites.get(parent.id)["summary"]["selected"] == 0
    with pytest.raises(JobStateError):
        service.favorites.select(parent.id, selected=True, work_ids=[A, "999"])
    assert service.favorites.get(parent.id)["summary"]["selected"] == 0
    service.favorites.select(parent.id, selected=True, work_ids=[B])
    service.favorites.confirm(parent.id)
    children = [j for j in service.database.list_jobs() if j.kind == "capture"]
    assert [j.request.share_text for j in children] == [f"https://www.douyin.com/video/{B}"]


async def test_directory_only_cannot_be_confirmed(service):
    parent = await scan(service, directory_only=True)
    assert parent.status == JobStatus.COMPLETED
    assert service.favorites.get(parent.id)["items"] == []
    with pytest.raises(JobStateError):
        service.favorites.confirm(parent.id)
    assert len(service.database.list_jobs()) == 1


async def test_resume_preserves_checkpoint_and_rejects_account_switch(service):
    assert hasattr(service, "favorites")
    service.favorites.adapter = FakeFavoritesAdapter(
        snapshot(complete=False), failure=BrowserAuthRequiredError("登录失效")
    )
    parent = service.favorites.start()
    failed = await service.process_claimed_job(parent)
    assert failed.status == JobStatus.NEEDS_AUTH
    assert failed.result["auth_scope"] == "favorites"
    assert service.favorites.get(parent.id)["summary"]["discovered"] == 3
    service.favorites.adapter = FakeFavoritesAdapter(snapshot(account="different-account"))
    retried = service.retry_job(parent.id)
    await service.process_claimed_job(retried)
    data = service.favorites.get(parent.id)
    assert data["account_id"] == "test-account"
    assert data["status"] == "needs_auth"


async def test_history_refresh_recovers_missed_child_failure_and_retry(service):
    parent = await scan(service)
    service.favorites.confirm(parent.id)
    child = next(j for j in service.database.list_jobs() if j.kind == "capture")
    service.database.update_job(child.id, status=JobStatus.FAILED, error_message="测试失败")
    data = service.favorites.history()[0]
    assert data["status"] == "completed_with_warnings"
    assert data["summary"]["failed"] == 1
    service.favorites.retry_failed(parent.id)
    assert service.database.get_job(child.id).status == JobStatus.QUEUED
    assert service.favorites.get(parent.id)["status"] == "monitoring"
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 1


async def test_gateway_wait_is_not_completion_and_events_keep_batch_context(service):
    parent = await scan(service, gateway_context=GatewayContext(gateway="test"))
    service.favorites.confirm(parent.id)
    child = next(j for j in service.database.list_jobs() if j.kind == "capture")
    service.database.update_job(child.id, status=JobStatus.AWAITING_AGENT_ANALYSIS)
    data = service.favorites.get(parent.id)
    assert data["summary"]["completed"] == 0
    assert data["status"] == "monitoring"
    events = service.database.list_job_events()
    event = next(e for e in events if e.job_id == child.id)
    assert event.result["favorites_context"]["batch_silent"] is True


def test_start_and_pagination_validate_inputs_without_creating_jobs(service):
    assert hasattr(service, "favorites")
    with pytest.raises(ValueError):
        service.favorites.start(folder_ids=[])
    assert service.database.list_jobs() == []
    parent = service.favorites.start()
    with pytest.raises(ValueError):
        service.favorites.get(parent.id, limit=0)
    with pytest.raises(ValueError):
        service.favorites.get(parent.id, page=0)


async def test_completed_capture_is_skipped_on_rescan_without_changing_user_state(service):
    from douyin_wiki.adapters.share import ResolvedShare
    from douyin_wiki.models import RetentionPolicy
    from douyin_wiki.worker import Worker

    class Resolver:
        async def resolve(self, text):
            return ResolvedShare(
                original_url=text, canonical_url=text, video_id=A, redirect_chain=(text,)
            )

    service.resolver = Resolver()
    service.favorites.adapter = FakeFavoritesAdapter(snapshot(kinds=[(A, "video")]))
    parent = service.favorites.start()
    worker = Worker(service)
    assert (await worker.run_once()).status == JobStatus.NEEDS_SELECTION
    service.favorites.confirm(parent.id)
    result = await worker.run_once()
    assert result.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
    assert service.favorites.get(parent.id)["summary"]["completed"] == 1
    entry_id = result.result["entry_id"]
    service.set_entry_favorite(entry_id, True)
    before = service.database.get_entry(entry_id)
    second = await scan(service, snapshot(kinds=[(A, "video")]))
    assert service.favorites.get(second.id)["summary"]["imported"] == 1
    service.favorites.confirm(second.id)
    after = service.database.get_entry(entry_id)
    assert service.downloader.calls == 1
    assert after.favorite is True and after.retention == RetentionPolicy.KEEP
    assert after.inspirations == before.inspirations
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 1


async def test_dispatch_failure_rolls_back_all_children_and_can_be_resubmitted(service):
    import sqlite3

    parent = await scan(service, snapshot(kinds=[(A, "video"), (B, "video")]))
    with service.database.connect() as conn:
        conn.execute(f"""CREATE TRIGGER simulate_disk_failure BEFORE UPDATE OF child_id
            ON favorites_items WHEN NEW.work_id='{B}'
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        service.favorites.confirm(parent.id)
    assert [j.kind for j in service.database.list_jobs()] == ["favorites_import"]
    assert service.favorites.get(parent.id)["status"] == "needs_selection"
    with service.database.connect() as conn:
        conn.execute("DROP TRIGGER simulate_disk_failure")
    service.favorites.confirm(parent.id)
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 2


async def test_knowledge_cache_rebuild_preserves_favorites_operational_history(service):
    parent = await scan(service)
    service.database.clear_knowledge_cache(include_creators=True)
    service.database.initialize()
    data = service.favorites.get(parent.id)
    assert data["summary"]["discovered"] == 3
    assert data["items"][0]["work_id"] == A
    assert data["status"] == "needs_selection"


def test_expired_worker_cannot_write_favorites_checkpoint(service):
    from douyin_wiki.errors import JobLeaseLostError

    parent = service.favorites.start()
    service.database.claim_next_job(worker_id="old-worker")
    with service.database.connect() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01' WHERE id=?", (parent.id,))
    with (
        service.database.claimed_job_updates(parent.id, "old-worker"),
        pytest.raises(JobLeaseLostError),
    ):
        service.favorites.store.checkpoint(parent.id, snapshot())
    assert service.favorites.get(parent.id)["summary"]["discovered"] == 0


async def test_more_than_5000_selected_works_are_not_silently_truncated(service):
    parent = await scan(
        service, snapshot(kinds=[(str(7100000000000000000 + i), "video") for i in range(5001)])
    )
    service.favorites.confirm(parent.id)
    with service.database.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='capture'").fetchone()[0]
    assert count == 5001
    assert service.favorites.get(parent.id, page=101)["total"] == 5001


async def test_prefixed_share_text_reuses_existing_capture_for_same_work(service):
    child = service.capture_douyin(f"测试分享文案 https://www.douyin.com/video/{A}?from=share")
    parent = await scan(service)
    service.favorites.confirm(parent.id)
    assert service.favorites.get(parent.id)["items"][0]["job_id"] == child.id
    assert len([j for j in service.database.list_jobs() if j.kind == "capture"]) == 1


async def test_requested_folder_never_imports_works_from_another_folder(service):
    parent = await scan(service, folder_ids=["folder-2"])
    service.favorites.confirm(parent.id, accept_partial=True)
    assert not [j for j in service.database.list_jobs() if j.kind == "capture"]


async def test_resume_preserves_manual_exclusions(service):
    parent = await scan(service)
    service.favorites.select(parent.id, selected=False, work_ids=[A])
    service.database.update_job(parent.id, status=JobStatus.FAILED)
    service.favorites.adapter = FakeFavoritesAdapter(snapshot())
    await service.process_claimed_job(service.retry_job(parent.id))
    assert service.favorites.get(parent.id)["summary"]["selected"] == 0


async def test_resumed_scan_deselects_work_explicitly_moved_outside_scope(service):
    parent = await scan(service, folder_ids=["folder-1"])
    changed = snapshot(kinds=[(A, "video")])
    changed.works[0].folder_ids = ["other-folder"]
    service.database.update_job(parent.id, status=JobStatus.FAILED)
    service.favorites.adapter = FakeFavoritesAdapter(changed)
    await service.process_claimed_job(service.retry_job(parent.id))
    service.favorites.confirm(parent.id, accept_partial=True)
    assert not [j for j in service.database.list_jobs() if j.kind == "capture"]


async def test_stale_parent_aggregation_cannot_hide_a_requeued_child(service):
    parent = await scan(service)
    service.favorites.confirm(parent.id)
    child = next(j for j in service.database.list_jobs() if j.kind == "capture")
    service.database.update_job(child.id, status=JobStatus.FAILED)
    stale_run = service.favorites.store.load(parent.id)
    stale_items = service.favorites._items(parent.id, stale_run)
    stale_summary = service.favorites._summary(stale_items, stale_run["options"])
    service.retry_job(child.id)
    service.favorites._refresh_with(parent.id, stale_run, stale_items, stale_summary)
    service.favorites.refresh_all()
    assert service.database.get_job(parent.id).status == JobStatus.MONITORING
    assert service.database.get_job(parent.id).result["child_status_counts"] == {"queued": 1}
