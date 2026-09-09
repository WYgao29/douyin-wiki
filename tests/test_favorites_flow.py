"""Real Web-to-store integration with synthetic inventory; never downloads media."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from douyin_wiki.webapp.app import create_app
from tests.test_favorites import A, B, FakeFavoritesAdapter, snapshot


def test_web_inventory_to_selection_to_explicit_queue(config, service):
    service.favorites.adapter = FakeFavoritesAdapter(snapshot())
    with TestClient(create_app(config, service=service, start_watcher=False)) as client:
        submitted = client.post("/api/favorites/imports", json={})
        assert submitted.status_code == 202
        parent_id = submitted.json()["job_id"]
        assert [j.kind for j in service.database.list_jobs()] == ["favorites_import"]
        asyncio.run(service.process_claimed_job(service.database.get_job(parent_id)))
        detail = client.get(f"/api/favorites/imports/{parent_id}").json()
        assert detail["summary"]["discovered"] == 3
        assert detail["summary"]["selected"] == 1
        assert detail["summary"]["unsupported"] == 1
        assert service.downloader.calls == 0
        result = client.post(f"/api/favorites/imports/{parent_id}/confirm", json={})
        assert result.status_code == 202
        client.post(f"/api/favorites/imports/{parent_id}/confirm", json={})
        children = [j for j in service.database.list_jobs() if j.kind == "capture"]
        assert len(children) == 1
        assert children[0].request.share_text == f"https://www.douyin.com/video/{A}"
        assert service.downloader.calls == 0
        assert client.get("/api/favorites/imports").json()[0]["job_id"] == parent_id


def test_web_unproven_inventory_requires_acceptance_and_rejects_unknown_selection(config, service):
    service.favorites.adapter = FakeFavoritesAdapter(snapshot(complete=False))
    with TestClient(create_app(config, service=service, start_watcher=False)) as client:
        parent_id = client.post("/api/favorites/imports", json={}).json()["job_id"]
        asyncio.run(service.process_claimed_job(service.database.get_job(parent_id)))
        result = client.post(
            f"/api/favorites/imports/{parent_id}/selection",
            json={"selected": True, "work_ids": [B]},
        )
        assert result.status_code == 409  # Images were not enabled.
        rejected = client.post(f"/api/favorites/imports/{parent_id}/confirm", json={})
        assert rejected.status_code == 409
        assert len(service.database.list_jobs()) == 1
        accepted = client.post(
            f"/api/favorites/imports/{parent_id}/confirm", json={"accept_partial": True}
        )
        assert accepted.status_code == 202
        assert len(service.database.list_jobs()) == 2
        assert service.downloader.calls == 0
