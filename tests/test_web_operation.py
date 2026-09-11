from __future__ import annotations

from fastapi.testclient import TestClient

from douyin_wiki.models import (
    AuthCheckResult,
    CaptureRequest,
    InspirationInput,
    JobStatus,
    ReviewIssue,
)
from douyin_wiki.webapp.app import create_app
from tests.test_web import _web_fixture


class FakeAuthDownloader:
    def __init__(
        self, state: str = "needs_login", *, ok: bool = False, verified: bool = False
    ) -> None:
        self.state = state
        self.ok = ok
        self.verified = verified
        self.authenticate_calls = 0

    async def check_auth(self, *, video_url: str | None = None) -> AuthCheckResult:
        return AuthCheckResult(
            scope="video",
            state=self.state,  # type: ignore[arg-type]
            ok=self.ok,
            server_verified=self.verified,
            cookie_source="browser cookie store",
            message="fake video auth",
        )

    async def authenticate(self) -> AuthCheckResult:
        self.authenticate_calls += 1
        self.state = "ready"
        self.ok = True
        self.verified = True
        return await self.check_auth()


class FakeDouyinAuth:
    def __init__(self, *, ok: bool = False) -> None:
        self.ok = ok
        self.authenticate_calls = 0

    async def check_auth(self) -> AuthCheckResult:
        return AuthCheckResult(
            scope="image_note",
            state="ready" if self.ok else "needs_login",
            ok=self.ok,
            server_verified=self.ok,
            cookie_source="dedicated-profile",
            message="fake douyin auth",
        )

    async def authenticate(self, *, timeout_seconds: int = 600) -> None:
        self.authenticate_calls += 1
        self.ok = True


def test_capture_options_persist_and_open_job_detail(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/captures",
            json={
                "share_text": "https://v.douyin.com/uvHsRpXIn8s/",
                "inspirations": [
                    {
                        "text": "用来筛选信息源",
                        "quote": "一手证据",
                        "start_ms": 1000,
                        "end_ms": 2000,
                    }
                ],
                "retention": "keep",
                "allow_long": True,
                "approve_cloud_analysis": True,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["options"]["allow_long"] is True
        assert detail["options"]["approve_cloud_analysis"] is True
        assert detail["options"]["retention"] == "keep"
        assert detail["inspirations"][0]["text"] == "用来筛选信息源"
        assert detail["stage"] == "prepare"
        dumped = str(detail).lower()
        assert "sk-" not in dumped and "password" not in dumped
        page = client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "任务中心" in page.text or "任务详情" in page.text


def test_job_list_filters_user_action_and_keeps_compat_fields(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    queued = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    failed = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(
        failed.id,
        status=JobStatus.FAILED,
        error_message="模拟失败",
        unlock=True,
    )
    waiting = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(
        waiting.id,
        status=JobStatus.WAITING_CONFIRMATION,
        unlock=True,
    )
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        listing = client.get("/api/jobs").json()
        assert listing["total"] >= 3
        assert listing["items"][0]["state_label"]
        waiting_list = client.get("/api/jobs", params={"requires_user_action": True}).json()
        ids = {item["id"] for item in waiting_list["items"]}
        assert waiting.id in ids
        assert failed.id in ids
        assert queued.id not in ids
        detail = client.get(f"/api/jobs/{waiting.id}").json()
        assert detail["next_action"]["code"] == "approve_long_video"
        approved = client.post(f"/api/jobs/{waiting.id}/approve")
        assert approved.status_code == 202
        assert approved.json()["status"] == "queued"


def test_video_and_douyin_auth_channels_are_isolated(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    video = FakeAuthDownloader(state="needs_login")
    douyin = FakeDouyinAuth(ok=True)
    service.downloader = video
    service.image_note_downloader = douyin
    service.creator_adapter = douyin
    service.favorites.adapter = douyin
    paused = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(
        paused.id,
        status=JobStatus.NEEDS_AUTH,
        result={"auth_scope": "video"},
        unlock=True,
    )
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        status = client.get("/api/auth/status").json()
        assert status["cookie_values_exposed"] is False
        assert status["channels"]["video"]["user_state"] != "authorized"
        assert status["channels"]["douyin"]["user_state"] == "authorized"
        assert "cookie" not in status["channels"]["video"]
        started = client.post(
            "/api/auth/sessions",
            json={"channel": "video", "trigger_job_id": paused.id},
        )
        assert started.status_code == 202
        session_id = started.json()["id"]
        session = {"stage": started.json()["stage"]}
        for _ in range(30):
            session = client.get(f"/api/auth/sessions/{session_id}").json()
            if session["stage"] in {"succeeded", "failed", "timeout"}:
                break
        assert session["channel"] == "video"
        assert session["stage"] == "succeeded"
        assert video.authenticate_calls >= 1
        retried = client.get(f"/api/jobs/{paused.id}").json()
        assert retried["status"] == "queued"


def test_maintenance_and_rebuild_do_nothing_until_confirmed(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        preview = client.post("/api/system/maintenance", json={"confirmed": False}).json()
        assert preview["preview"] is True
        assert preview["executed"] is False
        rebuild = client.post("/api/system/rebuild", json={"confirmed": False}).json()
        assert rebuild["preview"] is True
        assert rebuild["executed"] is False
        assert config.database_path.exists()
        health = client.get("/api/system/health").json()
        assert health["web"]["running"] is True
        assert "127.0.0.1" in health["web"]["bind"]


def test_delete_import_history_keeps_entries(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    parent = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/user/self?showTab=favorite_collection"),
        kind="favorites_import",
        artifacts={"favorites_options": {"directory_only": False}},
        status=JobStatus.COMPLETED,
        progress=1,
    )
    with service.database.connect() as conn:
        conn.execute(
            "INSERT INTO favorites_runs(parent_id, options_json, confirmed) VALUES (?,?,1)",
            (parent.id, "{\"folder_ids\": null, \"include_images\": false}"),
        )
    entry_id = service.database.list_entries()[0].id
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        preview = client.request(
            "DELETE", f"/api/imports/{parent.id}", json={"confirmed": False}
        ).json()
        assert preview["preview"] is True
        assert "已入库知识资料" in preview["will_keep"]
        deleted = client.request(
            "DELETE", f"/api/imports/{parent.id}", json={"confirmed": True}
        ).json()
        assert deleted["executed"] is True
        assert client.get(f"/api/jobs/{parent.id}").status_code == 404
        assert client.get(f"/api/articles/{entry_id}").status_code == 200


def test_review_and_gateway_payloads(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    job = service.capture_douyin(
        "https://v.douyin.com/uvHsRpXIn8s/",
        inspirations=[InspirationInput(text="核对疑点")],
    )
    service.database.replace_review_issues(
        job.id,
        [
            ReviewIssue(
                id="iss-1",
                start_ms=1000,
                end_ms=2000,
                raw_text="10号会打5折。",
                reason="低置信",
            )
        ],
    )
    service.database.update_job(job.id, status=JobStatus.NEEDS_REVIEW, unlock=True)
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        review = client.get(f"/api/jobs/{job.id}/review").json()
        assert review["issues"][0]["id"] == "iss-1"
        rejected = client.post(
            f"/api/jobs/{job.id}/review", json={"resolutions": {"other": "x"}}
        )
        assert rejected.status_code == 409
        accepted = client.post(
            f"/api/jobs/{job.id}/review", json={"accept_uncertain": True}
        )
        assert accepted.status_code == 202


def test_spa_routes_and_same_origin_still_enforced(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        for path in (
            "/imports",
            "/imports/single",
            "/imports/creators",
            "/imports/favorites",
            "/jobs",
            "/settings/auth",
            "/settings/system",
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert "shared.js" in page.text
            assert "cdn." not in page.text
        system = client.get("/settings/system")
        assert "清理过期媒体" in system.text
        assert "system-overview" in system.text
        assert "运行检查" in system.text
        blocked = client.post(
            "/api/auth/sessions",
            json={"channel": "video"},
            headers={"origin": "http://evil.example"},
        )
        assert blocked.status_code == 403


def test_worker_heartbeat_and_reload_request(service) -> None:
    from douyin_wiki.worker import Worker

    worker = Worker(service)
    worker._heartbeat()
    heartbeat = service.database.get_worker_heartbeat()
    assert heartbeat["worker_id"] == worker.worker_id
    service.database.request_worker_reload()
    assert worker.reload_requested()
