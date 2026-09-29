from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from douyin_wiki.config import LLMSettings
from douyin_wiki.models import (
    AnalysisMode,
    AuthCheckResult,
    CaptureRequest,
    InspirationInput,
    JobStatus,
    ReviewIssue,
)
from douyin_wiki.webapp.app import WEB_VERSION, create_app
from tests.test_web import _web_fixture


def local_client(app, **kwargs):
    headers = {"Origin": "http://testserver", **(kwargs.pop("headers", {}) or {})}
    return TestClient(app, headers=headers, **kwargs)


def test_model_health_reports_actual_provider_reachability(tmp_path, monkeypatch) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        assert client.get("/api/system/model-health").json()["status"] == "inactive"

    config.analysis_mode = AnalysisMode.PROVIDER
    config.llm = LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model")
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    monkeypatch.setattr(
        "douyin_wiki.web_operation.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        assert client.get("/api/system/model-health").json()["status"] == "ready"


def test_evidence_audit_is_available_in_job_detail_only(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    audit = [{"kind": "knowledge_atom", "id": "a1", "reason": "引文无法定位"}]
    service.database.update_job(job.id, artifacts={"analysis_evidence_audit": audit})
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        detail = client.get(f"/api/jobs/{job.id}").json()
        listing = client.get("/api/jobs").json()
    assert detail["analysis_evidence_audit"] == audit
    assert all("analysis_evidence_audit" not in item for item in listing["items"])


def test_media_provenance_is_available_in_job_detail(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    provenance = {"asr": {"provider": "sensevoice", "model": "iic/SenseVoiceSmall"}}
    service.database.update_job(job.id, artifacts={"media_provenance": provenance})
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        detail = client.get(f"/api/jobs/{job.id}").json()
    assert detail["media_provenance"] == provenance


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
    with local_client(app) as client:
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
    with local_client(app) as client:
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


def test_job_center_lists_parents_with_names_and_can_dismiss_failures(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    parent = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/user/abc"),
        kind="creator_import",
        status=JobStatus.NEEDS_SELECTION,
        progress=0.5,
    )
    service.database.update_job(parent.id, result={"creator_name": "叫我舒老师"}, unlock=True)
    child = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/video/1"),
        artifacts={"creator_context": {"parent_job_id": parent.id, "id": "c1", "work_id": "w1"}},
    )
    warned = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(
        warned.id,
        status=JobStatus.COMPLETED_WITH_WARNINGS,
        progress=1,
        result={"warnings": ["有一条提示"], "summary": "完成摘要"},
        unlock=True,
    )
    failed = service.database.create_job(
        CaptureRequest(share_text="not-a-creator"),
        kind="creator_import",
        status=JobStatus.FAILED,
    )
    service.database.update_job(failed.id, error_message="无法解析", unlock=True)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        listing = client.get("/api/jobs").json()
        ids = {item["id"] for item in listing["items"]}
        assert parent.id in ids
        assert child.id not in ids
        titled = next(item for item in listing["items"] if item["id"] == parent.id)
        assert titled["display_title"] == "博主批量 · 叫我舒老师"
        unnamed = next(item for item in listing["items"] if item["id"] == failed.id)
        assert unnamed["display_title"] == "博主批量 · 链接未能识别博主"
        completed = client.get("/api/jobs", params={"status": "completed"}).json()
        assert warned.id in {item["id"] for item in completed["items"]}
        exact = client.get("/api/jobs", params={"status": "completed_with_warnings"}).json()
        assert {item["id"] for item in exact["items"]} == {warned.id}
        visible_children = client.get("/api/jobs", params={"parents_only": False}).json()
        assert child.id in {item["id"] for item in visible_children["items"]}
        dismissed = client.post(f"/api/jobs/{failed.id}/dismiss")
        assert dismissed.status_code == 200
        assert dismissed.json()["dismissed"] is True
        assert dismissed.json()["requires_user_action"] is False
        waiting = client.get("/api/jobs", params={"requires_user_action": True}).json()
        waiting_ids = {item["id"] for item in waiting["items"]}
        assert parent.id in waiting_ids
        assert failed.id not in waiting_ids
        assert client.post(f"/api/jobs/{parent.id}/dismiss").status_code == 409
        retried = client.post(f"/api/jobs/{failed.id}/retry")
        assert retried.status_code == 202
        assert retried.json()["status"] == "queued"
        assert retried.json()["dismissed"] is False
        scripts = {
            name: client.get(f"/static/{name}?v={WEB_VERSION}").text
            for name in ("jobs.js", "imports-creators.js", "imports-favorites.js", "shared.js")
        }
    assert 'split("?")' in scripts["imports-creators.js"]
    assert 'split("?")' in scripts["imports-favorites.js"]
    assert 'split("?")' in scripts["shared.js"]
    assert "等待你" in scripts["jobs.js"]
    assert "不再提醒" in scripts["jobs.js"]
    assert "display_title" in scripts["jobs.js"]
    assert 'rest: "其他任务"' in scripts["jobs.js"]
    assert "已忽略" in scripts["jobs.js"]


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
    with local_client(app) as client:
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
    with local_client(app) as client:
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
            (parent.id, '{"folder_ids": null, "include_images": false}'),
        )
    entry_id = service.database.list_entries()[0].id
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
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
    with local_client(app) as client:
        review = client.get(f"/api/jobs/{job.id}/review").json()
        assert review["issues"][0]["id"] == "iss-1"
        rejected = client.post(f"/api/jobs/{job.id}/review", json={"resolutions": {"other": "x"}})
        assert rejected.status_code == 409
        accepted = client.post(f"/api/jobs/{job.id}/review", json={"accept_uncertain": True})
        assert accepted.status_code == 202


def test_spa_routes_and_same_origin_still_enforced(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
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


def test_in_progress_filter_child_warnings_and_settings_aliases(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    running = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(running.id, status=JobStatus.ANALYZING, unlock=True)
    queued = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    selecting = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/user/abc"),
        kind="creator_import",
        status=JobStatus.NEEDS_SELECTION,
    )
    gateway = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(gateway.id, status=JobStatus.AWAITING_AGENT_ANALYSIS, unlock=True)
    failed = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(failed.id, status=JobStatus.FAILED, error_message="模拟失败", unlock=True)
    child = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/video/1"),
        status=JobStatus.COMPLETED_WITH_WARNINGS,
        progress=1,
    )
    service.database.update_job(
        child.id,
        result={"warnings": ["字幕有缺口"], "entry_id": "entry-child-1"},
        unlock=True,
    )
    parent = service.database.create_job(
        CaptureRequest(share_text="https://www.douyin.com/user/batch"),
        kind="creator_import",
        status=JobStatus.COMPLETED_WITH_WARNINGS,
        progress=1,
    )
    service.database.update_job(
        parent.id,
        result={"warnings": [], "child_job_ids": [child.id], "creator_name": "批次博主"},
        unlock=True,
    )
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        active = client.get("/api/jobs", params={"status": "in_progress"}).json()
        active_ids = {item["id"] for item in active["items"]}
        assert running.id in active_ids
        assert queued.id in active_ids
        assert selecting.id not in active_ids
        assert gateway.id not in active_ids
        assert failed.id not in active_ids
        assert parent.id not in active_ids
        chinese = client.get("/api/jobs", params={"status": "进行中"}).json()
        assert {item["id"] for item in chinese["items"]} == active_ids
        listed = next(item for item in client.get("/api/jobs").json()["items"] if item["id"] == parent.id)
        assert listed["next_action"]["code"] == "view_children"
        assert listed["requires_user_action"] is False
        detail = client.get(f"/api/jobs/{parent.id}").json()
        assert "字幕有缺口" in detail["result"]["warnings"]
        assert detail["result"]["warnings_from_children"] is True
        assert detail["imported_entry_ids"] == ["entry-child-1"]
        overview = client.get("/api/overview").json()
        assert overview["jobs"]["requires_user_action"] >= 2
        health = client.get("/api/system/health").json()
        assert health["web"]["version"] == WEB_VERSION
        assert health["web"]["version"] != "0.2.0"
        assert "SenseVoice" in health["media"]["asr_label"]
        assert "RapidOCR" in health["media"]["ocr_label"]
        auth = client.get("/auth", follow_redirects=False)
        assert auth.status_code == 307
        assert auth.headers["location"].endswith("/settings/auth")
        settings = client.get("/settings")
        assert settings.status_code == 200
        assert 'id="settings-view"' in settings.text
        assert "共用模型" in settings.text
        creators = client.get("/static/imports-creators.js?v=" + WEB_VERSION).text
        assert "2500" in creators
        assert "source_kind_label" in creators
        assert "北京时间" in creators
        app_js = client.get("/static/app.js?v=" + WEB_VERSION).text
        assert "重新分析" in app_js
        assert "待办" in app_js
        assert "库内收藏" in app_js


def test_web_028_settings_health_and_favorite_copy(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        page = client.get("/")
        health = client.get("/health", follow_redirects=False)
        jobs_js = client.get(f"/static/jobs.js?v={WEB_VERSION}").text
        creators = client.get(f"/static/imports-creators.js?v={WEB_VERSION}").text
        favorites = client.get(f"/static/imports-favorites.js?v={WEB_VERSION}").text
        app_js = client.get(f"/static/app.js?v={WEB_VERSION}").text
    assert 'id="settings-nav"' in page.text
    assert 'data-route="/settings"' in page.text
    assert ">设置</span>" in page.text
    assert 'aria-label="更新抖音收藏"' in page.text
    assert 'aria-label="更新收藏"' not in page.text
    assert health.status_code == 307
    assert health.headers["location"].endswith("/api/system/health")
    assert 'children.id = "job-children"' in jobs_js
    assert "scrollIntoView" in jobs_js
    assert "viewingChildren" in jobs_js
    assert "timestamp_ms}ms" not in jobs_js
    assert "clockFromMs" in jobs_js
    assert 'if (work.entry_id) return "已入库"' in creators
    assert "抖音收藏操作失败" in favorites
    assert 'favoriteFailure("库内收藏", error)' in app_js
    assert '`${kind}操作失败`' in app_js
    assert 'setNav("settings-nav")' in app_js
