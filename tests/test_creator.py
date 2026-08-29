from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from douyin_wiki.adapters.creator import _collect_post_payload, creator_id_for
from douyin_wiki.adapters.embeddings import EmbeddingService
from douyin_wiki.adapters.share import ResolvedShare, extract_creator_sec_uid
from douyin_wiki.models import (
    CreatorInventoryResult,
    CreatorInventoryWork,
    CreatorProfile,
    CreatorWorkDecision,
    JobStatus,
    SourceKind,
)
from douyin_wiki.service import DouyinWikiService
from tests.conftest import (
    FakeAnalysisProvider,
    FakeDownloader,
    FakeMediaProcessor,
    FakeOCR,
    FakeTranscriber,
)


class DirectResolver:
    async def resolve(self, share_text: str) -> ResolvedShare:
        work_id = share_text.rstrip("/").rsplit("/", 1)[-1]
        kind = SourceKind.IMAGE_NOTE if "/note/" in share_text else SourceKind.VIDEO
        route = "note" if kind == SourceKind.IMAGE_NOTE else "video"
        return ResolvedShare(
            original_url=share_text,
            canonical_url=f"https://www.douyin.com/{route}/{work_id}",
            video_id=work_id,
            redirect_chain=(share_text,),
            source_kind=kind,
        )


class FakeCreatorAdapter:
    def __init__(self, inventory: CreatorInventoryResult) -> None:
        self.inventory_result = inventory
        self.calls = 0

    async def inventory(self, source_text: str, target_dir: Path) -> CreatorInventoryResult:
        self.calls += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        updated = []
        for index, work in enumerate(self.inventory_result.works, start=1):
            preview = target_dir / f"{index:03d}-{work.work_id}.jpg"
            preview.write_bytes(b"preview")
            updated.append(work.model_copy(update={"thumbnail_path": str(preview)}))
        return self.inventory_result.model_copy(update={"works": updated})


class CreatorAwareDownloader(FakeDownloader):
    def __init__(self, sec_uid: str) -> None:
        super().__init__()
        self.sec_uid = sec_uid

    async def download(self, url: str, video_id: str, target_dir: Path):
        metadata = await super().download(url, video_id, target_dir)
        return metadata.model_copy(update={"creator_sec_uid": self.sec_uid})


def inventory_result(*work_ids: str, complete: bool = True) -> CreatorInventoryResult:
    sec_uid = "MS4wLjABAAAAFRvCbB_XVtImbxhAlIDbZXm9e297ylWjBNJb4jydJzaZG23JjbtKHz78jLjA0UC-"
    return CreatorInventoryResult(
        profile=CreatorProfile(
            sec_uid=sec_uid,
            canonical_url=f"https://www.douyin.com/user/{sec_uid}",
            original_url="https://v.douyin.com/rrcucI9W-e8/",
            nickname="元气山海FDE学院",
            unique_id="73878956869",
            signature="企业 AI 部署",
            reported_work_count=len(work_ids),
        ),
        works=[
            CreatorInventoryWork(
                work_id=work_id,
                source_kind=SourceKind.VIDEO,
                canonical_url=f"https://www.douyin.com/video/{work_id}",
                original_url=f"https://www.douyin.com/video/{work_id}",
                title=f"作品 {index}",
                published_at=datetime(2026, 8, 23 - index, tzinfo=UTC),
                duration_seconds=60,
            )
            for index, work_id in enumerate(work_ids, start=1)
        ],
        complete=complete,
        reported_count=len(work_ids),
    )


@pytest.fixture
def creator_service(config, fake_reminders):
    adapter = FakeCreatorAdapter(inventory_result("7676706580084141163", "7676383353574510070"))
    downloader = FakeDownloader()
    service = DouyinWikiService(
        config,
        resolver=DirectResolver(),
        downloader=downloader,
        creator_adapter=adapter,
        media=FakeMediaProcessor(),
        transcriber=FakeTranscriber(),
        ocr=FakeOCR(),
        analysis=FakeAnalysisProvider(),
        embeddings=EmbeddingService(config.embeddings),
        reminders=fake_reminders,
    )
    service.initialize(initialize_git=False)
    return service, adapter, downloader


def test_extract_creator_identity_from_profile_url() -> None:
    sec_uid = "MS4wLjABAAAAFRvCbB_XVtImbxhAlIDbZXm9e297ylWjBNJb4jydJzaZG23JjbtKHz78jLjA0UC-"
    profile_url = f"https://www.douyin.com/user/{sec_uid}?from_tab_name=main"
    assert extract_creator_sec_uid(profile_url) == sec_uid
    assert creator_id_for(sec_uid).startswith("dyc-")


def test_collect_mixed_creator_post_payload() -> None:
    works, has_more = _collect_post_payload(
        {
            "aweme_list": [
                {
                    "aweme_id": "7676706580084141163",
                    "desc": "企业 AI 部署",
                    "video": {"duration": 87_000, "cover": {"url_list": ["https://x/a"]}},
                },
                {
                    "aweme_id": "7674987897195870714",
                    "desc": "本地模型选择",
                    "images": [{"url_list": ["https://x/b"]}],
                },
                {
                    "aweme_id": "7674987897195870715",
                    "desc": "五秒短视频",
                    "video": {"duration": 5_000},
                },
            ],
            "has_more": 0,
        }
    )
    assert [item.source_kind for item in works] == [
        SourceKind.VIDEO,
        SourceKind.IMAGE_NOTE,
        SourceKind.VIDEO,
    ]
    assert works[0].duration_seconds == 87
    assert works[2].duration_seconds == 5
    assert has_more is False


@pytest.mark.asyncio
async def test_creator_inventory_selection_and_isolated_vault(creator_service) -> None:
    service, adapter, downloader = creator_service
    job = service.capture_douyin_creator("8.92 复制打开抖音 https://v.douyin.com/rrcucI9W-e8/")
    claimed = service.database.claim_next_job()
    result = await service.process_claimed_job(claimed)
    assert result.status == JobStatus.NEEDS_SELECTION
    assert adapter.calls == 1
    assert downloader.calls == 0

    inventory = service.get_creator_inventory(job.id)
    assert inventory["total"] == 2
    assert [item["ordinal"] for item in inventory["items"]] == [1, 2]
    empty = service.set_creator_work_selection(
        job.id, CreatorWorkDecision.SELECTED, ordinals=[]
    )
    assert empty["changed"] == 0
    assert empty["selection"]["pending"] == 2
    with pytest.raises(Exception, match="仍有未决定作品"):
        service.confirm_creator_import(job.id)

    service.set_creator_work_selection(job.id, CreatorWorkDecision.SELECTED, ordinals=[1])
    service.set_creator_work_selection(job.id, CreatorWorkDecision.SKIPPED, ordinals=[2])
    parent = service.confirm_creator_import(job.id)
    assert parent.status == JobStatus.MONITORING
    assert len(parent.result["child_job_ids"]) == 1

    child = service.database.claim_next_job()
    completed = await service.process_claimed_job(child)
    assert completed.status == JobStatus.COMPLETED
    parent = service.get_job(job.id)
    assert parent.status == JobStatus.COMPLETED
    assert parent.result["selection"] == {
        "pending": 0,
        "selected": 0,
        "skipped": 1,
        "imported": 1,
        "total": 2,
    }

    entry = service.database.get_entry(completed.result["entry_id"])
    assert entry.source_path.startswith("creators/元气山海FDE学院_")
    assert "/sources/" in entry.source_path
    assert "/raw/records/" in entry.raw_path
    assert not list((service.config.vault_path / "wiki" / "sources").glob("*.md"))
    creator = service.list_creators()[0]
    creator_root = service.config.vault_path / creator["folder_path"]
    assert (creator_root / "index.md").is_file()
    assert (creator_root / ".data" / "sources" / f"{entry.video_id}.md").is_file()
    assert list((creator_root / "raw" / "covers").glob("*.jpg"))
    creator_index = (creator_root / "index.md").read_text(encoding="utf-8")
    assert "## 待入库（0）" in creator_index
    assert "## 已入库（1）" in creator_index
    assert "## 未入库（1）" in creator_index
    assert "暂无待入库作品" in creator_index
    assert "北京时间" in creator_index
    assert "+00:00" not in creator_index

    service.database.clear_knowledge_cache(include_creators=True)
    rebuilt = service.rebuild_database_from_vault(apply=True)
    assert rebuilt["creator_count"] == 1
    assert service.database.get_creator(creator["id"]).nickname == "元气山海FDE学院"
    restored = service.database.get_creator_work(creator["id"], entry.video_id)
    assert restored.decision == CreatorWorkDecision.IMPORTED
    assert restored.entry_id == entry.id


@pytest.mark.asyncio
async def test_creator_inventory_returns_all_works_by_default(creator_service) -> None:
    service, adapter, _ = creator_service
    work_ids = [str(7_677_000_000_000_000_000 + index) for index in range(12)]
    adapter.inventory_result = inventory_result(*work_ids)
    job = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    await service.process_claimed_job(service.database.claim_next_job())

    inventory = service.get_creator_inventory(job.id)
    assert inventory["display_mode"] == "all"
    assert len(inventory["items"]) == 12
    assert inventory["has_more"] is False

    page = service.get_creator_inventory(job.id, page=2, limit=5)
    assert page["display_mode"] == "paginated"
    assert len(page["items"]) == 5
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_manual_sync_only_lists_new_works(creator_service) -> None:
    service, adapter, _ = creator_service
    initial = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    result = await service.process_claimed_job(service.database.claim_next_job())
    service.set_creator_work_selection(initial.id, CreatorWorkDecision.SKIPPED, ordinals=[1, 2])
    service.confirm_creator_import(initial.id)
    creator_id = result.result["creator_id"]

    adapter.inventory_result = inventory_result(
        "7676706580084141163", "7676383353574510070", "7677077378468957668"
    )
    sync = service.sync_creator(creator_id)
    assert adapter.calls == 1
    sync_result = await service.process_claimed_job(service.database.claim_next_job())
    assert adapter.calls == 2
    assert sync_result.status == JobStatus.NEEDS_SELECTION
    inventory = service.get_creator_inventory(sync.id)
    assert inventory["total"] == 1
    assert inventory["items"][0]["work"]["work_id"] == "7677077378468957668"


@pytest.mark.asyncio
async def test_manual_sync_marks_missing_twice_and_restores_work(creator_service) -> None:
    service, adapter, _ = creator_service
    initial = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    inventoried = await service.process_claimed_job(service.database.claim_next_job())
    service.set_creator_work_selection(initial.id, CreatorWorkDecision.SKIPPED, ordinals=[1, 2])
    service.confirm_creator_import(initial.id)
    creator_id = inventoried.result["creator_id"]
    missing_id = "7676383353574510070"

    adapter.inventory_result = inventory_result("7676706580084141163")
    for expected in ("possibly_unavailable", "source_unavailable"):
        service.sync_creator(creator_id)
        await service.process_claimed_job(service.database.claim_next_job())
        assert (
            service.database.get_creator_work(creator_id, missing_id).availability.value == expected
        )

    adapter.inventory_result = inventory_result("7676706580084141163", missing_id)
    service.sync_creator(creator_id)
    await service.process_claimed_job(service.database.claim_next_job())
    restored = service.database.get_creator_work(creator_id, missing_id)
    assert restored.availability.value == "available"
    assert restored.missing_sync_count == 0


@pytest.mark.asyncio
async def test_partial_inventory_requires_explicit_acceptance(creator_service) -> None:
    service, adapter, _ = creator_service
    adapter.inventory_result = inventory_result("7676706580084141163", complete=False)
    job = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    await service.process_claimed_job(service.database.claim_next_job())
    service.set_creator_work_selection(job.id, CreatorWorkDecision.SKIPPED, ordinals=[1])
    with pytest.raises(Exception, match="清单不完整"):
        service.confirm_creator_import(job.id)
    completed = service.confirm_creator_import(job.id, accept_partial=True)
    assert completed.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_empty_partial_inventory_does_not_claim_no_new_works(creator_service) -> None:
    service, adapter, _ = creator_service
    adapter.inventory_result = inventory_result(complete=False)
    job = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    result = await service.process_claimed_job(service.database.claim_next_job())
    assert result.status == JobStatus.NEEDS_SELECTION
    with pytest.raises(Exception, match="清单不完整"):
        service.confirm_creator_import(job.id)
    assert service.confirm_creator_import(job.id, accept_partial=True).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_direct_capture_of_known_creator_uses_creator_folder(creator_service) -> None:
    service, _, _ = creator_service
    initial = service.capture_douyin_creator("https://www.douyin.com/user/example-sec-uid")
    inventoried = await service.process_claimed_job(service.database.claim_next_job())
    service.set_creator_work_selection(initial.id, CreatorWorkDecision.SKIPPED, ordinals=[1, 2])
    service.confirm_creator_import(initial.id)
    creator = service.database.get_creator(inventoried.result["creator_id"])

    service.downloader = CreatorAwareDownloader(creator.sec_uid)
    work_id = "7677999999999999999"
    service.capture_douyin(f"https://www.douyin.com/video/{work_id}")
    completed = await service.process_claimed_job(service.database.claim_next_job())

    assert completed.status == JobStatus.COMPLETED
    entry = service.database.get_entry(completed.result["entry_id"])
    assert entry.source_path.startswith(f"{creator.folder_path}/sources/")
    assert not (service.config.vault_path / "raw" / "assets" / work_id).exists()
    work = service.database.get_creator_work(creator.id, work_id)
    assert work.decision == CreatorWorkDecision.IMPORTED
