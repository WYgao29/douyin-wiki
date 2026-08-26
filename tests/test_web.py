from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from douyin_wiki.config import AppConfig, load_config
from douyin_wiki.models import EntryRecord, RetentionPolicy
from douyin_wiki.service import DouyinWikiService
from douyin_wiki.webapp.app import create_app
from douyin_wiki.webapp.chat import ChatChunk


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
            "analysis": {
                "analysis_version": 2,
                "title": "测试文章",
                "one_liner": "这是一条本地资料。",
                "takeaways": ["关键结论"],
                "key_moments": [],
                "knowledge_atoms": [],
            }
        },
    )
    return config, service


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
        assert 'aria-label="专辑墙视图"' in page.text
        assert 'aria-label="画廊视图"' not in page.text
        assert "data-theme-select" in page.text
        assert 'id="chat-close"' in page.text
        assert 'id="applied-filters"' in page.text
        assert 'id="command-dialog"' in page.text
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
        assert ".article-body { max-inline-size: 40rem" in stylesheet.text
        assert "::view-transition-group(active-album-cover)" in stylesheet.text
        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert 'uiVersion: "douyin-wiki.ui-version"' in script.text
        assert 'const UI_PREFERENCE_VERSION = "2"' in script.text
        assert 'makeCover(item, "gallery-cover")' in script.text
        assert "makeEditorialCover" not in script.text
        assert "article-cover-frame" not in script.text
        assert "image.width = 900" not in script.text
        assert "image.height = 560" not in script.text
        assert "animatedEntryIds" in script.text
        assert "最近一次用量：${latestAssistant.total_tokens} token" in script.text


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
        assert 'id="settings-main"' in page.text
        assert 'aria-label="显示 API Key"' in page.text
        assert "/static/icons.svg#eye" in page.text
        assert "/static/model-settings.js?v=0.1.1" in page.text
        assert "settings-info-panel" not in page.text


def test_chat_stream_persists_history_and_usage(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
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
