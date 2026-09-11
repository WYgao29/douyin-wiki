from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from douyin_wiki.config import AppConfig, LLMSettings, load_config
from douyin_wiki.errors import EntryNotFoundError
from douyin_wiki.models import EntryRecord, InspirationInput, JobStatus, RetentionPolicy
from douyin_wiki.service import DouyinWikiService
from douyin_wiki.webapp.app import create_app
from douyin_wiki.webapp.chat import ChatChunk, OpenAICompatibleChatProvider


class FakeChatProvider:
    model = "测试模型"
    configured = True

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        assert "当前文章（优先）" in "\n".join(item["content"] for item in messages)
        yield ChatChunk(text="依据文章，")
        yield ChatChunk(
            text="这是测试回答。",
            usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        )


class ConfigurableFakeProvider:
    def __init__(self, model: str, configured: bool) -> None:
        self.model = model
        self.configured = configured

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        assert messages[-1]["content"] == "测试连接"
        yield ChatChunk(
            text="连接成功",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        )


class TopicFakeProvider:
    model = "专题测试模型"
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        self.calls += 1
        joined = "\n".join(item["content"] for item in messages)
        assert "dy-123" in joined
        yield ChatChunk(
            text="核心结论来自测试文章。〔dy-123〕",
            usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        )


def _web_fixture(tmp_path: Path) -> tuple[AppConfig, DouyinWikiService]:
    vault = tmp_path / "vault"
    source_dir = vault / "wiki" / "sources"
    raw_dir = vault / "raw" / "assets" / "123"
    data_dir = vault / "wiki" / ".data" / "sources"
    source_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    (raw_dir / "cover.webp").write_bytes(b"RIFFfakeWEBP")
    (vault / "raw" / "测试记录.md").write_text("原始记录", encoding="utf-8")
    image_dir = vault / "raw" / "images" / "123"
    image_dir.mkdir(parents=True)
    (image_dir / "001.webp").write_bytes(b"RIFFfakeIMAGE")
    cover_dir = vault / "raw" / "covers"
    cover_dir.mkdir(parents=True)
    (cover_dir / "123.jpg").write_bytes(b"fakeJPEG")
    source = source_dir / "测试文章_123.md"
    source.write_text(
        """---
type: source
video_id: '123'
author: 测试博主
published: '2026-08-23T08:00:00+08:00'
captured: '2026-08-23T09:00:00+08:00'
status: 正常
source_kind: video
cover_image: raw/assets/123/cover.webp
content_type: tutorial
tags:
  - 咖啡
inspirations:
  - text: 保留这段方法
---
# 测试文章

![封面](../../raw/assets/123/cover.webp)

## 灵感

- 保留这段方法

## 一句话

这是一条本地资料。

## 核心收获

- 关键结论

<script>alert('x')</script>

## 来源

- [原始采集记录](../../raw/测试记录.md)
- [打开原作品](https://www.douyin.com/video/123)
""",
        encoding="utf-8",
    )
    (data_dir / "123.md").write_text("机器数据，不应展示", encoding="utf-8")
    config = AppConfig(
        vault_path=vault,
        embeddings={"provider": "offline", "fallback_dimensions": 32},
    )
    service = DouyinWikiService(config)
    service.database.initialize()
    now = datetime.now(UTC)
    entry = EntryRecord(
        id="dy-123",
        video_id="123",
        title="测试文章",
        original_url="https://www.douyin.com/video/123",
        canonical_url="https://www.douyin.com/video/123",
        raw_path="raw/测试记录.md",
        source_path="wiki/sources/测试文章_123.md",
        status="active",
        media_status="present",
        retention=RetentionPolicy.KEEP,
        summary="这是一条本地资料。",
        inspirations=[{"text": "保留这段方法"}],
        tags=["咖啡"],
        created_at=now,
        updated_at=now,
    )
    service.database.upsert_entry(
        entry,
        {
            "metadata": {
                "video_id": "123",
                "original_url": "https://www.douyin.com/video/123",
                "canonical_url": "https://www.douyin.com/video/123",
                "title": "测试文章",
                "author": "测试博主",
                "source_kind": "video",
            },
            "analysis": {
                "analysis_version": 2,
                "title": "测试文章",
                "one_liner": "这是一条本地资料。",
                "takeaways": ["关键结论"],
                "chapters": [],
                "knowledge_atoms": [],
            }
        },
    )
    return config, service


def test_catalog_skips_bad_date_and_keeps_valid_articles(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    bad = config.vault_path / "wiki" / "sources" / "坏日期_456.md"
    bad.write_text(
        "---\ntype: source\nvideo_id: '456'\ncaptured_at: not-a-date\n---\n# 坏日期",
        encoding="utf-8",
    )
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        response = client.get("/api/library")
        assert response.status_code == 200
        assert {item["entry_id"] for item in response.json()["items"]} == {"dy-123"}
    assert app.state.catalog.load_errors[0]["path"] == "wiki/sources/坏日期_456.md"
    bad.write_text(
        "---\n"
        "type: source\n"
        "video_id: '456'\n"
        "captured_at: '2026-08-24T09:00:00+08:00'\n"
        "---\n# 修复日期",
        encoding="utf-8",
    )
    items = app.state.catalog.refresh()
    assert {item.entry_id for item in items} == {"dy-123", "dy-456"}
    assert app.state.catalog.load_errors == []


def test_library_article_rendering_and_media_security(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    with TestClient(app) as client:
        library = client.get("/api/library").json()
        assert library["total"] == 1
        assert library["items"][0]["content_type_label"] == "教程"
        assert library["items"][0]["captured_at"].endswith("+08:00")

        article = client.get("/api/articles/dy-123").json()
        assert "关键结论" in article["html"]
        assert "<script" not in article["html"]
        assert "原始采集记录" not in article["html"]
        assert article["html"].count("cover.webp") == 0
        assert client.get("/media/raw/assets/123/cover.webp").status_code == 200
        assert client.get("/media/../state.sqlite3").status_code == 404
        assert "机器数据" not in str(library)

        rejected = client.post(
            "/api/inspirations/confirm",
            json={"entry_id": "dy-123", "text": "网页灵感", "confirmed": False},
        )
        assert rejected.status_code == 400


def test_unmanaged_markdown_is_readable_but_not_database_managed(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    asset_dir = config.vault_path / "raw" / "assets" / "456"
    asset_dir.mkdir(parents=True)
    (asset_dir / "example.webp").write_bytes(b"RIFFfakeWEBP")
    source = config.vault_path / "wiki" / "sources" / "只读文章_456.md"
    source.write_text(
        """---
type: source
video_id: '456'
author: 只读作者
source_kind: image_note
content_type: explanation
---
# 只读文章

## 一句话

这是一篇没有数据库记录的 Markdown 文章。

![示例图](../../raw/assets/456/example.webp)
""",
        encoding="utf-8",
    )
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        library = client.get("/api/library")
        assert library.status_code == 200
        item = next(
            item for item in library.json()["items"] if item["entry_id"] == "dy-456"
        )
        assert item["database_managed"] is False

        article = client.get("/api/articles/dy-456")
        assert article.status_code == 200
        assert article.json()["item"]["database_managed"] is False
        assert "只读文章" in article.json()["html"]
        assert client.get("/media/raw/assets/456/example.webp").status_code == 200

        rejected = client.post(
            "/api/chat/sessions",
            json={"scope": "entry", "context_entry_id": "dy-456"},
        )
        assert rejected.status_code == 409
        assert "只读" in rejected.json()["detail"]
        assert service.database.list_chat_sessions() == []

        managed = next(
            item for item in library.json()["items"] if item["entry_id"] == "dy-123"
        )
        assert managed["database_managed"] is True
        created = client.post(
            "/api/chat/sessions",
            json={"scope": "entry", "context_entry_id": "dy-123"},
        )
        assert created.status_code == 201
        assert created.json()["scope"] == "entry"


def test_web_can_favorite_and_unfavorite_an_article(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        initial = client.get("/api/library").json()["items"][0]
        assert initial["favorite"] is False

        favorited = client.put(
            "/api/articles/dy-123/favorite", json={"favorite": True}
        )
        assert favorited.status_code == 200
        assert favorited.json()["item"]["favorite"] is True
        assert favorited.json()["restore_job"] is None
        assert service.database.get_entry("dy-123").retention == RetentionPolicy.KEEP

        unfavorited = client.put(
            "/api/articles/dy-123/favorite", json={"favorite": False}
        )
        assert unfavorited.status_code == 200
        assert unfavorited.json()["item"]["favorite"] is False
        assert service.database.get_entry("dy-123").retention == RetentionPolicy.TEMPORARY
        assert client.get("/api/library").json()["items"][0]["favorite"] is False


def test_web_favorite_queues_removed_media_restore_and_exposes_job(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    entry = service.database.get_entry("dy-123").model_copy(
        update={"media_status": "removed"}
    )
    service.database.upsert_entry(entry, service.database.get_entry_data(entry.id))
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.put(
            "/api/articles/dy-123/favorite", json={"favorite": True}
        )
        assert response.status_code == 202
        restore_job = response.json()["restore_job"]
        assert restore_job["kind"] == "media_restore"
        assert restore_job["status"] == "queued"

        job = client.get(f"/api/jobs/{restore_job['id']}")
        assert job.status_code == 200
        assert job.json()["id"] == restore_job["id"]
        assert job.json()["status"] == "queued"


def test_web_favorite_returns_404_for_missing_article(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.put(
            "/api/articles/missing/favorite", json={"favorite": True}
        )
    assert response.status_code == 404


def test_web_favorite_returns_committed_state_when_catalog_refresh_fails(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    def broken_refresh():
        raise OSError("catalog unavailable")

    monkeypatch.setattr(app.state.catalog, "refresh", broken_refresh)
    with TestClient(app) as client:
        response = client.put(
            "/api/articles/dy-123/favorite", json={"favorite": True}
        )

    assert response.status_code == 200
    assert response.json()["item"]["favorite"] is True
    assert response.json()["item"]["retention"] == "keep"
    assert "目录刷新失败" in response.json()["warnings"][0]


@pytest.mark.parametrize("status", [JobStatus.NEEDS_AUTH, JobStatus.FAILED])
def test_web_can_retry_media_restore_job(tmp_path: Path, status: JobStatus) -> None:
    config, service = _web_fixture(tmp_path)
    entry = service.database.get_entry("dy-123").model_copy(
        update={"media_status": "removed"}
    )
    service.database.upsert_entry(entry, service.database.get_entry_data(entry.id))
    restore = service.set_entry_favorite(entry.id, True)["restore_job"]
    service.database.update_job(
        restore.id,
        status=status,
        error_code="auth" if status == JobStatus.NEEDS_AUTH else "download_failed",
        error_message="需要重试",
        unlock=True,
    )
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{restore.id}/retry")
        repeated = client.post(f"/api/jobs/{restore.id}/retry")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert repeated.status_code == 202
    assert repeated.json()["id"] == restore.id


def test_web_retry_reuses_replacement_media_restore_job(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    entry = service.database.get_entry("dy-123").model_copy(
        update={"media_status": "removed"}
    )
    service.database.upsert_entry(entry, service.database.get_entry_data(entry.id))
    failed = service.set_entry_favorite(entry.id, True)["restore_job"]
    service.database.update_job(
        failed.id,
        status=JobStatus.FAILED,
        error_code="download_failed",
        error_message="需要重试",
        unlock=True,
    )
    replacement = service.set_entry_favorite(entry.id, True)["restore_job"]
    assert replacement.id != failed.id
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.post(f"/api/jobs/{failed.id}/retry")

    assert response.status_code == 202
    assert response.json()["id"] == replacement.id
    active = [
        job
        for job in service.list_jobs()
        if job.kind == "media_restore" and job.status == JobStatus.QUEUED
    ]
    assert [job.id for job in active] == [replacement.id]


def test_web_ui_uses_local_accessible_redesign_assets(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert '<main class="library-main" id="main-content"' in page.text
        assert 'aria-label="资料库导航"' in page.text
        assert 'aria-label="抖库 AI 对话"' in page.text
        assert 'data-library-view="list"' in page.text
        assert 'data-library-view="gallery"' in page.text
        assert 'data-section="favorite"' in page.text
        assert "收藏" in page.text
        assert 'aria-label="专辑墙视图"' in page.text
        assert 'aria-label="画廊视图"' not in page.text
        assert "data-theme-select" in page.text
        assert 'id="chat-close"' in page.text
        assert 'id="applied-filters"' in page.text
        assert 'id="command-dialog"' in page.text
        assert 'id="trash-nav"' in page.text
        assert 'id="trash-view"' in page.text
        assert 'id="destructive-dialog"' in page.text
        assert 'id="imports-toggle"' in page.text
        assert 'id="auth-nav"' in page.text
        assert "已保存视图" not in page.text
        assert "全部视频" not in page.text
        assert "全部图文" not in page.text
        assert "导入单条" in page.text
        assert "导入博主" in page.text
        assert "导入收藏" in page.text
        assert 'id="imports-single-view"' in page.text
        assert 'id="single-share"' in page.text
        assert 'id="capture-dialog"' not in page.text
        assert "/static/icons.svg#" in page.text
        assert "cdn." not in page.text

        article_page = client.get("/articles/dy-123")
        assert article_page.status_code == 200
        assert 'data-entry-id="dy-123"' in article_page.text

        icons = client.get("/static/icons.svg")
        assert icons.status_code == 200
        assert '<symbol id="search"' in icons.text
        theme = client.get("/static/theme.js")
        assert theme.status_code == 200
        assert "douyin-wiki.theme" in theme.text
        stylesheet = client.get("/static/app.css")
        assert stylesheet.status_code == 200
        assert "--color-canvas" in stylesheet.text
        assert 'data-theme="dark"' in stylesheet.text
        assert ".gallery-cover" in stylesheet.text
        assert "aspect-ratio: 3 / 4" in stylesheet.text
        assert ".editorial-cover" not in stylesheet.text
        assert ".article-hero.has-cover" not in stylesheet.text
        assert ".article-cover-frame" not in stylesheet.text
        assert ".article-cover" in stylesheet.text
        assert "--font-reading:" in stylesheet.text
        assert ".article-body { max-inline-size: 40rem" in stylesheet.text
        assert "::view-transition-group(active-album-cover)" in stylesheet.text
        assert "view-transition-name: app-sidebar" in stylesheet.text
        assert ".message.is-new" in stylesheet.text
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert 'uiVersion: "douyin-wiki.ui-version"' in script.text
        assert 'const UI_PREFERENCE_VERSION = "2"' in script.text
        assert 'makeCover(item, "gallery-cover")' in script.text
        assert 'makeCover(item, "article-cover")' in script.text
        assert 'hero.querySelector(".article-cover")' in script.text
        assert "makeEditorialCover" not in script.text
        assert "article-cover-frame" not in script.text
        assert "image.width = 900" not in script.text
        assert "image.height = 560" not in script.text
        assert "animatedEntryIds" in script.text
        assert "删除文章" in script.text
        assert "彻底删除" in script.text
        assert "最近一次用量：${latestAssistant.total_tokens} token" in script.text
        assert "function isDatabaseManaged(item)" in script.text
        assert "if (!isDatabaseManaged(item)) return null;" in script.text
        assert "if (state.topicSelectionMode && isDatabaseManaged(item))" in script.text
        assert "if (!isDatabaseManaged(findLibraryItem(entryId))) return;" in script.text
        assert ".filter(isDatabaseManaged)" in script.text
        assert "const articleChatEnabled = isDatabaseManaged(articleItem);" in script.text
        assert "只读 Markdown，发送时使用全库对话" in script.text
        assert "只读 Markdown 文章不能保存灵感" in script.text
        assert "currentArticleItem: null" in script.text
        assert "state.currentArticleItem = data.item;" in script.text
        assert "state.currentArticleItem?.entry_id === entryId" in script.text
        assert 'const topicsCreateButton = $("#topics-create-button");' in script.text
        assert "topicsCreateButton.classList.toggle(\"hidden\", !hasManagedItems);" in script.text


def test_web_filter_popover_can_paint_above_sidebar(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        stylesheet = client.get("/static/app.css")
        script = client.get("/static/app.js")

    assert stylesheet.status_code == 200
    assert ".library-sidebar { position: relative; z-index: 0;" in stylesheet.text
    assert ".library-main { position: relative; z-index: 1; min-width: 0;" in stylesheet.text
    assert (
        ".filter-popover { position: fixed; z-index: 50; "
        "top: var(--filter-popover-top"
    ) in stylesheet.text

    assert script.status_code == 200
    assert "function positionFilterPopover()" in script.text


@pytest.mark.parametrize(
    "share_text",
    [
        "https://www.douyin.com/video/7659645255277039717",
        (
            "3.21 复制打开抖音，看看【测试作者的作品】实用技巧 "
            "https://v.douyin.com/AbCdEfG/ 08/31"
        ),
    ],
)
def test_web_capture_queues_link_or_share_text(tmp_path: Path, share_text: str) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.post("/api/captures", json={"share_text": share_text})

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert service.get_job(payload["job_id"]).request.share_text == share_text


def test_web_capture_rejects_text_without_douyin_link(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/captures",
            json={"share_text": "看看这个网页 https://example.com/video/123"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "分享文本中没有找到有效的抖音链接"
    assert service.list_jobs() == []


def test_article_trash_restore_and_permanent_delete(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    source = config.vault_path / "wiki" / "sources" / "测试文章_123.md"
    assets = config.vault_path / "raw" / "assets" / "123"
    images = config.vault_path / "raw" / "images" / "123"
    cover = config.vault_path / "raw" / "covers" / "123.jpg"
    raw_record = config.vault_path / "raw" / "测试记录.md"
    machine = config.vault_path / "wiki" / ".data" / "sources" / "123.md"
    source_before = source.read_bytes()
    machine_before = machine.read_bytes()
    with TestClient(app) as client:
        topic_id = service.create_topic(
            "删除恢复测试", ["dy-123"], goal="验证专题来源恢复"
        )["topic"]["id"]
        session_id = client.post(
            "/api/chat/sessions",
            json={"scope": "entry", "context_entry_id": "dy-123"},
        ).json()["id"]
        rejected = client.request(
            "DELETE", "/api/articles/dy-123", json={"confirmed": False}
        )
        assert rejected.status_code == 400

        deleted = client.request(
            "DELETE", "/api/articles/dy-123", json={"confirmed": True}
        )
        assert deleted.status_code == 200
        trash_id = deleted.json()["trash_id"]
        assert client.get("/api/library").json()["total"] == 0
        assert service.database.list_entries() == []
        assert not source.exists()
        assert not assets.exists()
        assert not images.exists()
        assert not cover.exists()
        assert not raw_record.exists()
        assert not machine.exists()
        assert service.database.get_topic(topic_id).sources == []
        deleted_session = service.database.get_chat_session(session_id)
        assert deleted_session.scope == "library"
        assert deleted_session.context_entry_id is None
        trash = client.get("/api/trash").json()
        assert trash["total"] == 1
        assert trash["items"][0]["title"] == "测试文章"
        assert client.get("/trash").status_code == 200

        restored = client.post(
            f"/api/trash/{trash_id}/restore", json={"confirmed": True}
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["entry_id"] == "dy-123"
        assert client.get("/api/library").json()["total"] == 1
        assert source.is_file()
        assert assets.is_dir()
        assert images.is_dir()
        assert cover.is_file()
        assert raw_record.is_file()
        assert machine.is_file()
        assert source.read_bytes() == source_before
        assert machine.read_bytes() == machine_before
        assert [
            item.entry_id for item in service.database.get_topic(topic_id).sources
        ] == ["dy-123"]
        restored_session = service.database.get_chat_session(session_id)
        assert restored_session.scope == "entry"
        assert restored_session.context_entry_id == "dy-123"
        assert client.get("/api/trash").json()["total"] == 0

        deleted_again = client.request(
            "DELETE", "/api/articles/dy-123", json={"confirmed": True}
        ).json()
        trash_id = deleted_again["trash_id"]
        rejected_purge = client.request(
            "DELETE", f"/api/trash/{trash_id}", json={"confirmed": False}
        )
        assert rejected_purge.status_code == 400
        purged = client.request(
            "DELETE", f"/api/trash/{trash_id}", json={"confirmed": True}
        )
        assert purged.status_code == 200
        assert purged.json()["status"] == "已彻底删除"
        assert not (
            config.vault_path / ".douyin-wiki" / "trash" / "entries" / trash_id
        ).exists()
        assert client.get("/api/trash").json()["total"] == 0
        invalid = client.request(
            "DELETE", "/api/trash/not-a-valid-id", json={"confirmed": True}
        )
        assert invalid.status_code == 400


def test_interrupted_delete_is_rolled_back_on_recovery(tmp_path: Path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    source = config.vault_path / "wiki" / "sources" / "测试文章_123.md"
    original_delete = service.database.delete_entry_projection

    def interrupt_delete(entry_id: str):
        raise SystemExit(entry_id)

    monkeypatch.setattr(service.database, "delete_entry_projection", interrupt_delete)
    with pytest.raises(SystemExit):
        service.trash_entry("dy-123", confirmed=True)

    assert not source.exists()
    assert service.database.get_entry("dy-123").id == "dy-123"
    manifests = list(
        (config.vault_path / ".douyin-wiki" / "trash" / "entries").glob(
            "*/manifest.json"
        )
    )
    assert len(manifests) == 1
    assert '"phase": "files_moved"' in manifests[0].read_text(encoding="utf-8")

    monkeypatch.setattr(service.database, "delete_entry_projection", original_delete)
    report = service.recover_entry_trash_operations()
    assert report["recovered"]
    assert source.is_file()
    assert service.list_trashed_entries() == []


def test_interrupted_restore_is_completed_without_ghost_trash(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    deleted = service.trash_entry("dy-123", confirmed=True)
    original_write = service._write_trash_manifest

    def interrupt_after_database_restore(item_dir: Path, manifest: dict) -> None:
        if manifest.get("phase") == "restored":
            raise SystemExit("模拟恢复完成后的进程中断")
        original_write(item_dir, manifest)

    monkeypatch.setattr(
        service, "_write_trash_manifest", interrupt_after_database_restore
    )
    with pytest.raises(SystemExit):
        service.restore_trashed_entry(deleted["trash_id"], confirmed=True)

    assert service.database.get_entry("dy-123").id == "dy-123"
    assert (config.vault_path / "wiki" / "sources" / "测试文章_123.md").is_file()
    item_dir = (
        config.vault_path
        / ".douyin-wiki"
        / "trash"
        / "entries"
        / deleted["trash_id"]
    )
    assert item_dir.is_dir()

    monkeypatch.setattr(service, "_write_trash_manifest", original_write)
    report = service.recover_entry_trash_operations()
    assert deleted["trash_id"] in report["completed"]
    assert service.list_trashed_entries() == []
    assert not item_dir.exists()


def test_delete_lock_prevents_stale_writer_from_resurrecting_entry(
    tmp_path: Path, monkeypatch
) -> None:
    _config, service = _web_fixture(tmp_path)
    finalizing = threading.Event()
    release = threading.Event()
    writer_started = threading.Event()
    errors: list[Exception] = []
    original_finalize = service._finalize_deleted_entry

    def blocking_finalize(*args, **kwargs):
        finalizing.set()
        assert release.wait(timeout=3)
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(service, "_finalize_deleted_entry", blocking_finalize)

    delete_thread = threading.Thread(
        target=lambda: service.trash_entry("dy-123", confirmed=True), daemon=True
    )

    def add_inspiration() -> None:
        writer_started.set()
        try:
            service.add_inspiration("dy-123", InspirationInput(text="并发灵感"))
        except Exception as exc:
            errors.append(exc)

    delete_thread.start()
    assert finalizing.wait(timeout=3)
    writer_thread = threading.Thread(target=add_inspiration, daemon=True)
    writer_thread.start()
    assert writer_started.wait(timeout=1)
    sleep(0.05)
    assert writer_thread.is_alive()
    release.set()
    delete_thread.join(timeout=3)
    writer_thread.join(timeout=3)

    assert not delete_thread.is_alive()
    assert not writer_thread.is_alive()
    assert any(isinstance(error, EntryNotFoundError) for error in errors)
    with pytest.raises(EntryNotFoundError):
        service.database.get_entry("dy-123")


def test_secondary_delete_and_restore_failures_return_success_with_warning(
    tmp_path: Path, monkeypatch
) -> None:
    _config, service = _web_fixture(tmp_path)
    topic_id = service.create_topic("次要更新失败", ["dy-123"])["topic"]["id"]
    monkeypatch.setattr(
        service.vault,
        "rebuild_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("索引故障")),
    )
    deleted = service.trash_entry("dy-123", confirmed=True)
    assert deleted["warnings"]
    with pytest.raises(EntryNotFoundError):
        service.database.get_entry("dy-123")

    monkeypatch.undo()
    monkeypatch.setattr(
        service,
        "_persist_topic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("专题故障")),
    )
    restored = service.restore_trashed_entry(deleted["trash_id"], confirmed=True)
    assert restored["status"] == "已恢复"
    assert any("专题" in warning for warning in restored["warnings"])
    assert service.database.get_entry("dy-123").id == "dy-123"
    assert service.database.get_topic(topic_id).sources[0].entry_id == "dy-123"
    assert service.list_trashed_entries() == []


def test_web_delete_survives_catalog_refresh_failure(tmp_path: Path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    monkeypatch.setattr(
        app.state.catalog,
        "refresh",
        lambda: (_ for _ in ()).throw(RuntimeError("目录故障")),
    )
    with TestClient(app) as client:
        response = client.request(
            "DELETE", "/api/articles/dy-123", json={"confirmed": True}
        )
    assert response.status_code == 200
    assert any("网页目录刷新失败" in item for item in response.json()["warnings"])
    with pytest.raises(EntryNotFoundError):
        service.database.get_entry("dy-123")


def test_topic_web_flow_strict_chat_artifacts_and_note_confirmation(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    provider = TopicFakeProvider()
    app = create_app(
        config,
        service=service,
        chat_provider=provider,
        start_watcher=False,
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/topics",
            json={
                "title": "测试专题",
                "goal": "验证严格来源",
                "instructions": "只比较资料内的方法",
                "entry_ids": ["dy-123"],
            },
        )
        assert created.status_code == 201
        topic_id = created.json()["topic"]["id"]
        assert client.get(f"/topics/{topic_id}").status_code == 200
        page = client.get("/").text
        assert 'id="topics-nav"' in page
        assert 'id="topic-select-toggle"' in page

        session = client.post(
            "/api/chat/sessions",
            json={"scope": "topic", "context_topic_id": topic_id},
        ).json()
        no_evidence = client.post(
            f"/api/chat/sessions/{session['id']}/messages",
            json={"content": "聪明杯怎么冲？"},
        )
        assert no_evidence.status_code == 200
        assert "当前专题没有相关证据" in no_evidence.text
        assert provider.calls == 0

        artifact = client.post(
            f"/api/topics/{topic_id}/artifacts",
            json={"kind": "decision_brief"},
        )
        assert artifact.status_code == 201
        assert artifact.json()["status_label"] == "当前版本"
        assert artifact.json()["total_tokens"] == 13
        assert provider.calls == 1

        cancelled = client.post(
            f"/api/topics/{topic_id}/notes",
            json={"content": "逐字保存", "confirmed": False},
        )
        assert cancelled.status_code == 400
        saved = client.post(
            f"/api/topics/{topic_id}/notes",
            json={"content": "逐字保存", "confirmed": True},
        )
        assert saved.status_code == 201
        assert saved.json()["content_markdown"] == "逐字保存"


def test_model_settings_page_shares_theme_and_accessible_controls(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    with TestClient(app) as client:
        page = client.get("/settings/model")
        assert page.status_code == 200
        assert "data-theme-select" in page.text
        assert 'id="app-shell"' in page.text
        assert 'id="model-view"' in page.text
        assert 'id="analysis-view"' in page.text
        assert 'aria-label="显示 API Key"' in page.text
        assert "/static/icons.svg#eye" in page.text
        assert "/static/model-settings.js?v=0.2.16" in page.text
        assert "对话模型" in page.text
        assert 'name="analysis-mode"' in page.text
        assert "保存分析方式" in page.text
        assert 'href="/settings/analysis"' in page.text
        assert 'data-route="/settings/model"' in page.text
        assert "settings-info-panel" not in page.text
        assert "settings-shell" not in page.text

        analysis = client.get("/settings/analysis")
        assert analysis.status_code == 200
        assert 'id="app-shell"' in analysis.text
        assert "导入后的分析方式" in analysis.text
        assert 'value="provider"' in analysis.text
        assert "/static/analysis-settings.js?v=0.2.16" in analysis.text
        assert 'href="/settings/model"' in analysis.text
        assert 'data-route="/settings/analysis"' in analysis.text


def test_chat_stream_persists_history_and_usage(tmp_path: Path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    original_add_message = service.database.add_chat_message

    def add_message_off_event_loop(*args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return original_add_message(*args, **kwargs)

    monkeypatch.setattr(service.database, "add_chat_message", add_message_off_event_loop)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    with TestClient(app) as client:
        session = client.post(
            "/api/chat/sessions",
            json={"scope": "entry", "context_entry_id": "dy-123"},
        ).json()
        response = client.post(
            f"/api/chat/sessions/{session['id']}/messages",
            json={"content": "这篇文章讲了什么？"},
        )
        assert response.status_code == 200
        assert "这是测试回答" in response.text
        assert '"total_tokens": 20' in response.text
        assert '"original_url": "https://www.douyin.com/video/123"' in response.text
        history = client.get(f"/api/chat/sessions/{session['id']}").json()
        assert [item["role"] for item in history["messages"]] == ["user", "assistant"]
        assert history["messages"][1]["total_tokens"] == 20
        assert client.delete(f"/api/chat/sessions/{session['id']}").status_code == 200
        assert service.database.list_chat_sessions() == []


def test_local_host_and_same_origin_are_enforced(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, chat_provider=FakeChatProvider(), start_watcher=False)
    with TestClient(app) as client:
        assert client.get("/", headers={"host": "example.com"}).status_code == 400
        blocked = client.post(
            "/api/chat/sessions",
            json={"scope": "library"},
            headers={"origin": "https://evil.example"},
        )
        assert blocked.status_code == 403


def test_creator_sources_are_visible_and_machine_files_are_excluded(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    creator_source = config.vault_path / "creators" / "示例_abcd" / "sources" / "图文_456.md"
    creator_source.parent.mkdir(parents=True)
    creator_source.write_text(
        """---
type: source
video_id: '456'
author: 示例博主
source_kind: image_note
content_type: explanation
inspirations: []
---
# 图文资料

## 灵感

- 未填写；AI 不推测用户灵感。

## 一句话

正文里包含独特检索词“本地模型量化”。
""",
        encoding="utf-8",
    )
    hidden = config.vault_path / "creators" / "示例_abcd" / ".data" / "sources" / "999.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("---\ntype: source\nvideo_id: '999'\n---\n# 不应出现", encoding="utf-8")
    app = create_app(config, service=service, chat_provider=FakeChatProvider(), start_watcher=False)
    with TestClient(app) as client:
        library = client.get("/api/library").json()
        assert {item["entry_id"] for item in library["items"]} == {"dy-123", "dy-456"}
        creator_item = next(item for item in library["items"] if item["entry_id"] == "dy-456")
        assert creator_item["inspirations"] == []
        result = client.get("/api/library", params={"q": "本地模型量化"}).json()
        assert [item["entry_id"] for item in result["items"]] == ["dy-456"]


def test_file_watcher_refreshes_library_without_generating_html(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, chat_provider=FakeChatProvider(), start_watcher=True)
    with TestClient(app) as client:
        assert client.get("/api/library").json()["total"] == 1
        added = config.vault_path / "wiki" / "sources" / "自动刷新_789.md"
        added.write_text(
            "---\ntype: source\nvideo_id: '789'\nauthor: 文件监听\n---\n# 自动刷新文章\n",
            encoding="utf-8",
        )
        deadline = monotonic() + 3
        while monotonic() < deadline:
            if client.get("/api/library").json()["total"] == 2:
                break
            sleep(0.05)
        assert client.get("/api/library").json()["total"] == 2
        assert not list(config.vault_path.rglob("*.html"))


def test_model_settings_save_to_keychain_without_changing_gateway_mode(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    config_path = tmp_path / "config.toml"
    secrets: dict[str, str] = {}

    monkeypatch.setattr(
        "douyin_wiki.webapp.app.get_secret",
        lambda account: secrets.get(account, ""),
    )
    monkeypatch.setattr(
        "douyin_wiki.webapp.app.store_secret",
        lambda account, value: secrets.__setitem__(account, value),
    )

    def factory(settings):
        return ConfigurableFakeProvider(
            settings.model,
            bool(settings.model and secrets.get(settings.api_key_env)),
        )

    app = create_app(
        config,
        config_path=config_path,
        service=service,
        chat_provider_factory=factory,
        start_watcher=False,
    )
    with TestClient(app) as client:
        page = client.get("/settings/model")
        assert page.status_code == 200
        assert "API Key" in page.text
        before = client.get("/api/settings/model").json()
        assert before["configured"] is False
        assert before["api_key_required"] is True
        assert before["analysis_mode_label"] == "网关 Agent"

        invalid = client.post(
            "/api/settings/model",
            json={"base_url": "file:///tmp/model", "model": "bad", "api_key": "secret"},
        )
        assert invalid.status_code == 422
        saved = client.post(
            "/api/settings/model",
            json={
                "base_url": "https://models.example/v1/",
                "model": "example/model-1",
                "api_key": "super-secret-value",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert saved.json()["analysis_mode"] == "gateway"
        assert "super-secret-value" not in saved.text

        persisted = load_config(config_path)
        assert persisted.analysis_mode.value == "gateway"
        assert persisted.llm.base_url == "https://models.example/v1"
        assert persisted.llm.model == "example/model-1"
        assert "super-secret-value" not in config_path.read_text(encoding="utf-8")

        status = client.get("/api/settings/model")
        assert status.json()["api_key_configured"] is True
        assert "super-secret-value" not in status.text
        tested = client.post("/api/settings/model/test", json={})
        assert tested.status_code == 200
        assert tested.json()["usage"]["total_tokens"] == 7


def test_web_can_switch_analysis_mode_without_silent_change(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr("douyin_wiki.webapp.app.get_secret", lambda account: "")
    app = create_app(
        config,
        config_path=config_path,
        service=service,
        start_watcher=False,
    )
    with TestClient(app) as client:
        before = client.get("/api/settings/model").json()
        assert before["analysis_mode"] == "gateway"
        assert {item["value"] for item in before["analysis_modes"]} == {
            "gateway",
            "provider",
            "local",
        }
        switched = client.post("/api/settings/analysis-mode", json={"mode": "provider"})
        assert switched.status_code == 200
        assert switched.json()["analysis_mode"] == "provider"
        assert switched.json()["web_can_complete"] is True
        assert load_config(config_path).analysis_mode.value == "provider"
        saved_model = client.post(
            "/api/settings/model",
            json={
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "local-test",
            },
        )
        assert saved_model.status_code == 200
        assert saved_model.json()["analysis_mode"] == "provider"
        rejected = client.post("/api/settings/analysis-mode", json={"mode": "cloud"})
        assert rejected.status_code == 422


def test_model_settings_rejects_remote_http_before_secret_and_saves_loopback(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    config_path = tmp_path / "config.toml"
    secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "douyin_wiki.webapp.app.get_secret",
        lambda account: secrets.get(account, ""),
    )
    monkeypatch.setattr(
        "douyin_wiki.webapp.app.store_secret",
        lambda account, value: secrets.__setitem__(account, value),
    )
    app = create_app(
        config,
        config_path=config_path,
        service=service,
        chat_provider_factory=lambda settings: ConfigurableFakeProvider(settings.model, True),
        start_watcher=False,
    )

    with TestClient(app) as client:
        rejected = client.post(
            "/api/settings/model",
            json={
                "base_url": "http://models.example/v1",
                "model": "remote-model",
                "api_key": "remote-secret",
            },
        )
        assert rejected.status_code == 422
        assert secrets == {}

        saved = client.post(
            "/api/settings/model",
            json={
                "base_url": "http://127.0.0.1:1234/v1/",
                "model": "local-model",
                "api_key": None,
            },
        )
        assert saved.status_code == 200

    assert load_config(config_path).llm.base_url == "http://127.0.0.1:1234/v1"
    assert secrets == {}


def test_model_settings_validation_does_not_echo_credential_url(
    tmp_path: Path,
) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(
        config,
        service=service,
        chat_provider=FakeChatProvider(),
        start_watcher=False,
    )
    credential_url = "https://user:secret@models.example/v1"

    with TestClient(app) as client:
        rejected = client.post(
            "/api/settings/model",
            json={
                "base_url": credential_url,
                "model": "remote-model",
                "api_key": "body-secret",
            },
        )

    assert rejected.status_code == 422
    assert credential_url not in rejected.text
    assert "user:secret" not in rejected.text
    assert "body-secret" not in rejected.text


def test_loopback_model_endpoint_does_not_require_api_key(tmp_path: Path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    local_config = config.model_copy(
        update={
            "llm": config.llm.model_copy(
                update={"base_url": "http://127.0.0.1:1234/v1", "model": "local-model"}
            )
        }
    )
    monkeypatch.setattr("douyin_wiki.webapp.app.get_secret", lambda _: "")
    monkeypatch.setattr("douyin_wiki.webapp.chat.get_secret", lambda _: "")
    app = create_app(local_config, service=service, start_watcher=False)

    with TestClient(app) as client:
        status = client.get("/api/settings/model").json()
        assert status["configured"] is True
        assert status["api_key_required"] is False
        assert status["api_key_source"] == "本机接口无需密钥"


def test_loopback_model_provider_forwards_configured_api_key(monkeypatch) -> None:
    monkeypatch.setattr("douyin_wiki.webapp.chat.get_secret", lambda _: "omlx-local-key")

    provider = OpenAICompatibleChatProvider(
        LLMSettings(
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen3.6-35B-A3B-4bit",
        )
    )

    assert provider.configured is True
    assert provider.api_key == "omlx-local-key"


def test_loopback_model_status_reports_configured_api_key_source(
    tmp_path: Path, monkeypatch
) -> None:
    config, service = _web_fixture(tmp_path)
    local_config = config.model_copy(
        update={
            "llm": config.llm.model_copy(
                update={"base_url": "http://127.0.0.1:8000/v1", "model": "local-model"}
            )
        }
    )
    monkeypatch.setattr("douyin_wiki.webapp.app.get_secret", lambda _: "omlx-local-key")
    monkeypatch.setattr("douyin_wiki.webapp.chat.get_secret", lambda _: "omlx-local-key")
    app = create_app(local_config, service=service, start_watcher=False)

    with TestClient(app) as client:
        status = client.get("/api/settings/model").json()
        assert status["api_key_configured"] is True
        assert status["api_key_required"] is False
        assert status["api_key_source"] == "macOS Keychain"


def test_switching_cloud_endpoint_requires_a_new_provider_key(tmp_path: Path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    config_path = tmp_path / "config.toml"
    secrets = {config.llm.api_key_env: "old-provider-key"}
    monkeypatch.setattr(
        "douyin_wiki.webapp.app.get_secret", lambda account: secrets.get(account, "")
    )
    monkeypatch.setattr(
        "douyin_wiki.webapp.app.store_secret",
        lambda account, value: secrets.__setitem__(account, value),
    )
    app = create_app(config, config_path=config_path, service=service, start_watcher=False)

    with TestClient(app) as client:
        rejected = client.post(
            "/api/settings/model",
            json={
                "base_url": "https://different-provider.example/v1",
                "model": "different-model",
                "api_key": None,
            },
        )
        assert rejected.status_code == 400
        assert "重新输入" in rejected.json()["detail"]
