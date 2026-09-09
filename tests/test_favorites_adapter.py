from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from douyin_wiki.adapters.favorites import (
    DouyinFavoritesAdapter,
    _folders_from_dom,
    _merge_works,
    _works_from_dom,
)
from douyin_wiki.config import MediaSettings
from douyin_wiki.errors import BrowserAuthRequiredError
from douyin_wiki.favorites_models import FavoriteWork

WORK_ID = "1234567890123456789"
SECOND_WORK_ID = "2234567890123456789"


def test_work_rejects_external_source() -> None:
    with pytest.raises(ValidationError):
        FavoriteWork(
            work_id=WORK_ID,
            canonical_url=f"https://evil.example/video/{WORK_ID}",
        )


def test_work_normalizes_protocol_relative_note_url() -> None:
    work = FavoriteWork(
        work_id=WORK_ID,
        canonical_url=f"//www.douyin.com/note/{WORK_ID}?previous_page=web_code_link",
        source_kind="image_note",
    )

    assert work.canonical_url == f"https://www.douyin.com/note/{WORK_ID}"


def test_work_rejects_url_with_different_work_id() -> None:
    with pytest.raises(ValidationError):
        FavoriteWork(
            work_id=WORK_ID,
            canonical_url=f"https://www.douyin.com/video/{SECOND_WORK_ID}",
        )


def test_duplicate_work_merges_folder_memberships() -> None:
    merged = _merge_works(
        [
            FavoriteWork(
                work_id=WORK_ID,
                canonical_url=f"https://www.douyin.com/video/{WORK_ID}",
                folder_ids=["1001"],
            ),
            FavoriteWork(
                work_id=WORK_ID,
                canonical_url=f"https://www.douyin.com/video/{WORK_ID}",
                folder_ids=["1002", "1001"],
            ),
        ]
    )

    assert len(merged) == 1
    assert merged[0].folder_ids == ["1001", "1002"]


def test_dom_records_keep_articles_and_exclude_non_list_anchors() -> None:
    works, warnings = _works_from_dom(
        [
            {
                "scope": "favorites-list",
                "href": f"/article/{WORK_ID}?from=collection",
                "title": "一篇长文",
            },
            {
                "scope": "recommendation",
                "href": f"/video/{SECOND_WORK_ID}",
                "title": "页脚推荐",
            },
        ]
    )

    assert [(item.work_id, item.source_kind) for item in works] == [(WORK_ID, "article")]
    assert warnings == []


def test_folder_catalog_never_uses_display_name_as_identity() -> None:
    folders, complete, warnings = _folders_from_dom(
        [
            {"name": "旅行", "href": "/user/self?modal_id=favorite_collection"},
            {"name": "AI", "platform_id": "2002", "reported_count": "3"},
        ],
        terminal=True,
        reported_count=2,
    )

    assert [folder.id for folder in folders] == ["2002"]
    assert complete is False
    assert any("稳定 ID" in warning for warning in warnings)


def test_folder_catalog_count_mismatch_is_incomplete() -> None:
    folders, complete, warnings = _folders_from_dom(
        [{"name": "旅行", "platform_id": "2001", "reported_count": 8}],
        terminal=True,
        reported_count=2,
    )

    assert len(folders) == 1
    assert complete is False
    assert any("目录显示 2 个" in warning for warning in warnings)


class FakeResponse:
    status = 200


class FakeLocator:
    def __init__(self, page: SyntheticFavoritesPage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        return int(self.selector in self.page.visible_selectors)

    async def click(self, **_: Any) -> None:
        self.page.clicked.append(self.selector)

    async def inner_text(self, **_: Any) -> str:
        return self.page.body


class SyntheticFavoritesPage:
    def __init__(
        self,
        *,
        account_id: str = "MS4wLjABAAAA-test-account",
        body: str = "我的抖音主页",
        listings: list[dict[str, Any]] | None = None,
        directory: dict[str, Any] | None = None,
    ) -> None:
        self.url = "https://www.douyin.com/"
        self.body = body
        self.account_id = account_id
        self.listings = listings or []
        self.directory = directory or {
            "records": [],
            "terminal": True,
            "reported_count": 0,
        }
        self.listing_index = 0
        self.clicked: list[str] = []
        self.checkpoint_count = 0
        self.scroll_count = 0
        self.visible_selectors = {
            '[data-e2e="favorite_collection"]',
            '[data-e2e="favorite_folder"]',
            '[data-e2e="video"]',
        }

    async def goto(self, url: str, **_: Any) -> FakeResponse:
        self.url = url
        return FakeResponse()

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def wait_for_timeout(self, _: int) -> None:
        return None

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "douyin-wiki:account" in script:
            return {"account_id": self.account_id, "nickname": "测试账号"}
        if "douyin-wiki:directory" in script:
            return self.directory
        if "douyin-wiki:open-folder" in script:
            return any(record.get("platform_id") == arg for record in self.directory["records"])
        if "douyin-wiki:listing" in script:
            if not self.listings:
                return {"records": [], "terminal": False}
            return self.listings[min(self.listing_index, len(self.listings) - 1)]
        if "douyin-wiki:scroll" in script:
            self.scroll_count += 1
            self.listing_index += 1
            return True
        raise AssertionError("unexpected evaluate script")


@pytest.mark.asyncio
async def test_inventory_raises_auth_required_for_login_wall(tmp_path: Path) -> None:
    adapter = DouyinFavoritesAdapter(MediaSettings(), tmp_path / "profile")
    page = SyntheticFavoritesPage(body="请登录后查看")

    with pytest.raises(BrowserAuthRequiredError):
        await adapter._inventory_page(
            page,
            cookies=[],
            folder_ids=None,
            directory_only=False,
            expected_account_id=None,
            on_checkpoint=None,
        )


@pytest.mark.asyncio
async def test_inventory_rejects_changed_account_on_resume(tmp_path: Path) -> None:
    adapter = DouyinFavoritesAdapter(MediaSettings(), tmp_path / "profile")
    page = SyntheticFavoritesPage(account_id="current-account")

    with pytest.raises(BrowserAuthRequiredError, match="账号已变化"):
        await adapter._inventory_page(
            page,
            cookies=[{"name": "sessionid", "domain": ".douyin.com", "expires": -1}],
            folder_ids=None,
            directory_only=True,
            expected_account_id="original-account",
            on_checkpoint=None,
        )


@pytest.mark.asyncio
async def test_inventory_checkpoints_cumulative_results_before_scrolling(tmp_path: Path) -> None:
    adapter = DouyinFavoritesAdapter(MediaSettings(), tmp_path / "profile")
    page = SyntheticFavoritesPage(
        listings=[
            {
                "records": [
                    {
                        "scope": "favorites-list",
                        "href": f"/video/{WORK_ID}",
                        "title": "第一条",
                    }
                ],
                "terminal": False,
            },
            {
                "records": [
                    {
                        "scope": "favorites-list",
                        "href": f"/video/{SECOND_WORK_ID}",
                        "title": "第二条",
                    }
                ],
                "terminal": True,
            },
        ]
    )
    checkpoints: list[list[str]] = []

    async def checkpoint(snapshot: Any) -> None:
        checkpoints.append([work.work_id for work in snapshot.works])
        page.checkpoint_count += 1

    result = await adapter._inventory_page(
        page,
        cookies=[{"name": "sessionid", "domain": ".douyin.com", "expires": -1}],
        folder_ids=None,
        directory_only=False,
        expected_account_id=None,
        on_checkpoint=checkpoint,
    )

    assert checkpoints == [[WORK_ID], [WORK_ID, SECOND_WORK_ID]]
    assert result.complete is True
    assert page.scroll_count == 1
    assert page.checkpoint_count > page.scroll_count


@pytest.mark.asyncio
async def test_inventory_is_incomplete_when_listing_idles_without_terminal(tmp_path: Path) -> None:
    adapter = DouyinFavoritesAdapter(MediaSettings(), tmp_path / "profile")
    page = SyntheticFavoritesPage(
        listings=[
            {
                "records": [
                    {
                        "scope": "favorites-list",
                        "href": f"/video/{WORK_ID}",
                    }
                ],
                "terminal": False,
            }
        ]
    )

    result = await adapter._inventory_page(
        page,
        cookies=[{"name": "sessionid", "domain": ".douyin.com", "expires": -1}],
        folder_ids=None,
        directory_only=False,
        expected_account_id=None,
        on_checkpoint=None,
    )

    assert result.complete is False
    assert any("分页结束" in warning for warning in result.warnings)


def test_protocol_relative_records_and_article_badge_are_not_importable_notes():
    works, warnings = _works_from_dom(
        [
            {
                "scope": "favorites-list",
                "href": "//www.douyin.com/note/123456",
                "source_kind": "article",
            },
            {"scope": "favorites-list", "href": "/note/234567"},
        ]
    )
    assert [(work.work_id, work.source_kind) for work in works] == [
        ("123456", "article"),
        ("234567", "image_note"),
    ]
    assert not warnings


def test_folder_catalog_ignores_external_links_even_with_platform_id():
    folders, complete, warnings = _folders_from_dom(
        [{"name": "外部链接", "platform_id": "123", "href": "https://example.com/collection/123"}],
        terminal=True,
        reported_count=1,
    )
    assert folders == []
    assert not complete
    assert warnings
