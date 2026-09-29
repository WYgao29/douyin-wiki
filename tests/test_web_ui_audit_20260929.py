"""Web UI audit 2026-09-29: Major 1–4 and Nit/UX fixes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from douyin_wiki.models import JobStatus
from douyin_wiki.webapp.app import WEB_VERSION, create_app
from tests.test_web import _web_fixture
from tests.test_web_operation import FakeAuthDownloader, FakeDouyinAuth, local_client


def test_job_detail_exposes_result_warnings_and_review_aligns(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    service.database.update_job(
        job.id,
        status=JobStatus.COMPLETED_WITH_WARNINGS,
        progress=1,
        result={
            "entry_id": "entry-audit-1",
            "warnings": ["模型生成的 6 条无法核实的证据或内容已移除"],
        },
        unlock=True,
    )
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        detail = client.get(f"/api/jobs/{job.id}").json()
        review = client.get(f"/api/jobs/{job.id}/review").json()
    assert detail["status"] == "completed_with_warnings"
    assert detail["result"]["warnings"] == ["模型生成的 6 条无法核实的证据或内容已移除"]
    assert detail["next_action"]["code"] == "open_entry"
    assert review["warnings"] == detail["result"]["warnings"]
    assert review["issues"] == []


def test_jobs_js_renders_warnings_dedupes_open_entry_hides_empty_children(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        script = client.get(f"/static/jobs.js?v={WEB_VERSION}")
    assert script.status_code == 200
    body = script.text
    assert "job.result?.warnings" in body or "job.result.warnings" in body or "result?.warnings" in body
    assert "openEntryViaAction" in body
    assert "hasChildWork" in body
    assert "job-warnings" in body


def test_system_nav_has_data_route_and_sidebar_body_keeps_footer_visible(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        page = client.get("/")
        css = client.get(f"/static/app.css?v={WEB_VERSION}")
    assert 'id="settings-nav"' in page.text
    assert 'id="system-nav"' not in page.text
    assert 'data-route="/settings/system"' in page.text
    assert 'class="sidebar-body"' in page.text
    assert "从资料库选择来源" in page.text
    assert ".sidebar-body" in css.text
    assert "overflow: hidden;" in css.text
    assert ".sidebar-footer" in css.text


def test_unknown_browser_path_returns_spa_shell_not_bare_json(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        response = client.get("/no-such-page-audit", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers.get("content-type", "")
    assert "not-found-view" in response.text
    assert "页面不存在" in response.text
    assert '{"detail":"Not Found"}' not in response.text


def test_unknown_api_path_still_json_404(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        response = client.get("/api/no-such-endpoint-audit")
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


def test_library_honors_limit_query(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    sources = config.vault_path / "wiki" / "sources"
    for index in range(2):
        video_id = f"45{index}"
        (sources / f"额外文章_{video_id}.md").write_text(
            f"""---
type: source
video_id: '{video_id}'
author: 额外作者
source_kind: video
content_type: explanation
---
# 额外文章{index}

## 一句话

额外正文{index}
""",
            encoding="utf-8",
        )
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        all_items = client.get("/api/library").json()
        limited = client.get("/api/library", params={"limit": 1}).json()
    assert all_items["total"] >= 2
    assert limited["limit"] == 1
    assert len(limited["items"]) == 1
    assert limited["total"] == 1


def test_overview_alerts_unverified_video_channel(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    service.downloader = FakeAuthDownloader(state="available", ok=True, verified=False)
    service.image_note_downloader = FakeDouyinAuth(ok=True)
    service.creator_adapter = service.image_note_downloader
    service.favorites.adapter = service.image_note_downloader
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        overview = client.get("/api/overview").json()
        auth = client.get("/api/auth/status").json()
    video = auth["channels"]["video"]
    assert video["user_state"] == "unverified"
    assert "Cookie" in video["user_state_label"] or "联网" in video["user_state_label"]
    assert any(item["code"] == "video_auth_unverified" for item in overview["alerts"])


def test_auth_and_app_scripts_clarify_channels_and_404(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        auth_js = client.get(f"/static/auth.js?v={WEB_VERSION}").text
        app_js = client.get(f"/static/app.js?v={WEB_VERSION}").text
        shared = client.get(f"/static/shared.js?v={WEB_VERSION}").text
        dead = client.get("/static/favorites.js")
    assert "联网检查状态" in auth_js
    assert "不代表这里已验证通过" in auth_js
    assert "refreshAuthChannelBanners" in app_js
    assert "isKnownAppPath" in shared
    assert "not-found-view" in shared
    assert dead.status_code == 404


def test_doctor_points_web_users_to_auth_page(tmp_path) -> None:
    config, service = _web_fixture(tmp_path)
    config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(config, service=service, start_watcher=False)
    with local_client(app) as client:
        doctor = client.post("/api/system/doctor", json={}).json()
    message = doctor["douyin_browser_profile"]["message"]
    assert "设置 › 授权状态" in message
    assert "网页「授权状态」" not in message
    web_llm = doctor["web_llm"]["message"] or ""
    if web_llm:
        assert "设置 › 共用模型" in web_llm
        assert "模型设置" not in web_llm
