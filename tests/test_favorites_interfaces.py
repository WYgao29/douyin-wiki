from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from douyin_wiki import cli, mcp_server
from douyin_wiki.auth_guidance import MacOSDialog, auth_channel
from douyin_wiki.errors import JobStateError
from douyin_wiki.localization import add_display_labels
from douyin_wiki.models import JobStatus
from douyin_wiki.webapp.app import create_app


class FakeFavoritesService:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.confirmed: list[tuple[str, bool]] = []
        self.selections: list[dict] = []

    def start(self, **options):
        self.started.append(options)
        return SimpleNamespace(id="favorites-1", status=JobStatus.QUEUED)

    def history(self, *, limit: int = 50):
        return [
            {
                "job_id": "favorites-1",
                "status": "needs_selection",
                "progress": 1.0,
                "summary": {"discovered": 2, "selected": 1},
            }
        ][:limit]

    def get(self, job_id: str, **filters):
        if job_id == "capture-1":
            raise JobStateError("任务不是收藏导入任务")
        return {
            "job_id": job_id,
            "status": "needs_selection",
            "progress": 1.0,
            "directory_only": False,
            "account_id": "account-1",
            "nickname": "测试账号",
            "complete": True,
            "folders_complete": True,
            "warnings": [],
            "folders": [{"id": "folder-1", "name": "测试夹", "reported_count": 2}],
            "items": [
                {
                    "work_id": "7000000000000000001",
                    "canonical_url": "https://www.douyin.com/video/7000000000000000001",
                    "title": "测试作品",
                    "author": "测试作者",
                    "source_kind": "video",
                    "folder_ids": ["folder-1"],
                    "selected": True,
                    "disposition": "pending",
                    "job_id": None,
                    "entry_id": None,
                    "error_message": None,
                }
            ],
            "total": 1,
            "page": filters.get("page", 1),
            "limit": filters.get("limit", 50),
            "has_more": False,
            "summary": {
                "discovered": 1,
                "eligible": 1,
                "selected": 1,
                "imported": 0,
                "active": 0,
                "completed": 0,
                "failed": 0,
                "excluded": 0,
                "unsupported": 0,
                "unavailable": 0,
                "child_status_counts": {},
            },
            "analysis_mode": "gateway",
        }

    def select(self, job_id: str, **selection):
        self.selections.append({"job_id": job_id, **selection})
        return self.get(job_id)

    def confirm(self, job_id: str, *, accept_partial: bool = False):
        if job_id == "capture-1":
            raise JobStateError("任务不是收藏导入任务")
        self.confirmed.append((job_id, accept_partial))
        return SimpleNamespace(id=job_id, status=JobStatus.DISPATCHING)

    def retry_failed(self, job_id: str):
        return SimpleNamespace(id=job_id, status=JobStatus.MONITORING)


def _client(config, service, favorites: FakeFavoritesService):
    service.favorites = favorites
    return TestClient(create_app(config, service=service, start_watcher=False))


def test_web_scan_only_creates_inventory_job(config, service) -> None:
    favorites = FakeFavoritesService()
    with _client(config, service, favorites) as client:
        response = client.post(
            "/api/favorites/imports",
            json={"folder_ids": ["folder-1"], "include_images": True},
        )

    assert response.status_code == 202
    assert response.json() == {"job_id": "favorites-1", "status": "queued"}
    assert favorites.started == [
        {
            "folder_ids": ["folder-1"],
            "include_images": True,
            "directory_only": False,
            "update_mode": "incremental",
        }
    ]
    assert favorites.confirmed == []


def test_web_history_and_resume_inventory(config, service) -> None:
    favorites = FakeFavoritesService()
    with _client(config, service, favorites) as client:
        history = client.get("/api/favorites/imports?limit=1")
        detail = client.get(
            "/api/favorites/imports/favorites-1?page=2&limit=25&folder_id=folder-1&query=%E6%B5%8B%E8%AF%95"
        )

    assert history.status_code == 200
    assert history.json()[0]["job_id"] == "favorites-1"
    assert history.json()[0]["status_label"] == "待选择作品"
    assert detail.status_code == 200
    assert detail.json()["page"] == 2
    assert detail.json()["limit"] == 25


def test_web_rejects_invalid_favorites_bounds(config, service) -> None:
    favorites = FakeFavoritesService()
    with _client(config, service, favorites) as client:
        page = client.get("/api/favorites/imports/favorites-1?page=0")
        limit = client.get("/api/favorites/imports?limit=201")

    assert page.status_code == 422
    assert limit.status_code == 422


def test_web_selection_applies_to_explicit_scope(config, service) -> None:
    favorites = FakeFavoritesService()
    with _client(config, service, favorites) as client:
        response = client.post(
            "/api/favorites/imports/favorites-1/selection",
            json={"selected": False, "folder_id": "folder-1"},
        )

    assert response.status_code == 200
    assert favorites.selections == [
        {
            "job_id": "favorites-1",
            "selected": False,
            "work_ids": None,
            "folder_id": "folder-1",
        }
    ]


def test_web_requires_explicit_confirmation_and_maps_conflicts(config, service) -> None:
    favorites = FakeFavoritesService()
    with _client(config, service, favorites) as client:
        before = client.get("/api/favorites/imports/favorites-1")
        confirmed = client.post(
            "/api/favorites/imports/favorites-1/confirm",
            json={"accept_partial": True},
        )
        conflict = client.post(
            "/api/favorites/imports/capture-1/confirm",
            json={"accept_partial": False},
        )

    assert before.status_code == 200
    assert favorites.confirmed == [("favorites-1", True)]
    assert confirmed.status_code == 202
    assert confirmed.json() == {"job_id": "favorites-1", "status": "dispatching"}
    assert conflict.status_code == 409


def test_cli_favorites_commands_emit_json(monkeypatch) -> None:
    favorites = FakeFavoritesService()
    monkeypatch.setattr(cli, "_service", lambda _: SimpleNamespace(favorites=favorites))
    runner = CliRunner()

    scan = runner.invoke(
        cli.app,
        [
            "favorites",
            "scan",
            "--folder-id",
            "folder-1",
            "--include-images",
            "--gateway",
            "openclaw",
            "--conversation-id",
            "conversation-1",
        ],
    )
    selection = runner.invoke(
        cli.app,
        ["favorites", "select", "favorites-1", "--exclude", "--work-id", "7000000000000000001"],
    )

    assert scan.exit_code == 0
    assert json.loads(scan.stdout)["status"] == "待处理"
    assert favorites.started[0]["gateway_context"].gateway == "openclaw"
    assert selection.exit_code == 0
    assert favorites.selections[-1]["selected"] is False


def test_mcp_favorites_inventory_is_separate_from_confirmation(monkeypatch) -> None:
    favorites = FakeFavoritesService()
    monkeypatch.setattr(mcp_server, "_SERVICE", SimpleNamespace(favorites=favorites))

    scanned = mcp_server.scan_favorites(folder_ids=["folder-1"], include_images=False)
    detail = mcp_server.get_favorites_import("favorites-1", page=1, limit=10)

    assert scanned["status"] == "queued"
    assert detail["summary"]["selected"] == 1
    assert favorites.confirmed == []


def test_favorites_auth_uses_douyin_channel_and_dialog_count() -> None:
    calls = []

    def runner(command, **_):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="confirmed\n")

    assert auth_channel("favorites") == "douyin"
    assert MacOSDialog(runner=runner).confirm(
        "douyin", {"image_note": 2, "creator": 3, "favorites": 4}
    )
    assert calls[0][-3:] == ["图文、博主与收藏", "9", "图文 2 个，博主 3 个，收藏 4 个"]


def test_favorites_payload_adds_chinese_labels_without_replacing_codes() -> None:
    payload = add_display_labels(
        {
            "kind": "favorites_import",
            "scope": "favorites",
            "source_kind": "article",
            "disposition": "unsupported",
            "summary": {"selected": 2, "unsupported": 1, "awaiting_agent_analysis": 1},
        }
    )

    assert payload["kind"] == "favorites_import"
    assert payload["kind_label"] == "收藏批量导入"
    assert payload["scope_label"] == "收藏清点"
    assert payload["source_kind_label"] == "文章"
    assert payload["disposition_label"] == "暂不支持"
    assert payload["summary_labels"] == {"已选择": 2, "暂不支持": 1, "待 AI 处理": 1}
