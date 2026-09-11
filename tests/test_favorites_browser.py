"""Offline browser tests: synthetic HTML only, all network access intercepted."""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from douyin_wiki.adapters.favorites import (
    _ACCOUNT_SNAPSHOT_JS,
    _LISTING_SNAPSHOT_JS,
    _works_from_dom,
)


@pytest.fixture
async def offline_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("**/*", lambda route: route.abort())
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def test_observed_dom_boundary_extracts_relative_links_and_ignores_footer(offline_page):
    page = offline_page
    await page.set_content("""<div><h1>模拟账号</h1><span>抖音号：fixture_123</span></div>
        <div id="semiTabPanelfavorite_collection"></div><div id="semiTabPanelvideo"></div>
        <ul class="cPDrcaOY QhXy7t32"><li><div><a href="/video/123456">
          <img alt="模拟作者：标题含 &lt;script&gt; 内容"></a></div></li>
        <li><div><a href="//www.douyin.com/note/234567"><img alt="作者乙：图文标题"></a></div></li>
        </ul><div>暂时没有更多了</div><footer><a href="/video/999999">无关推荐</a></footer>""")
    account = await page.evaluate(_ACCOUNT_SNAPSHOT_JS)
    snapshot = await page.evaluate(_LISTING_SNAPSHOT_JS)
    works, warnings = _works_from_dom(snapshot["records"])
    assert account == {"account_id": "douyin:fixture_123", "nickname": "模拟账号"}
    assert snapshot["terminal"] is True
    assert [work.work_id for work in works] == ["123456", "234567"]
    assert works[0].title == "标题含 <script> 内容"
    assert works[0].author == "模拟作者"
    assert works[1].source_kind == "image_note"
    assert not warnings
    await page.set_content(
        '<div id="semiTabPanelvideo"></div>'
        '<footer>暂时没有更多了<a href="/video/9">推荐</a></footer>'
    )
    assert (await page.evaluate(_LISTING_SNAPSHOT_JS))["boundary_found"] is False


async def test_ui_inventory_requires_separate_confirm_and_partial_acceptance(
    offline_page, tmp_path
):
    page = offline_page
    root = Path(__file__).parents[1] / "src/douyin_wiki/webapp"
    template = (root / "templates/app.html").read_text()
    begin = template.index('      <section id="imports-favorites-view"')
    end = template.index('      <section id="jobs-view"')
    html = '<meta charset="utf-8">' + template[begin:end]
    data = {
        "job_id": "fixture-parent",
        "status": "needs_selection",
        "status_label": "待选择作品",
        "nickname": "模拟账号",
        "directory_only": False,
        "confirmed": False,
        "include_images": False,
        "complete": False,
        "warnings": ["模拟分页未完成"],
        "folders": [],
        "analysis_mode": "gateway",
        "total": 1,
        "has_more": False,
        "summary": {"discovered": 1, "selected": 1, "failed": 0},
        "items": [
            {
                "work_id": "123456",
                "canonical_url": "https://www.douyin.com/video/123456",
                "title": "<img src=x onerror=alert(1)>",
                "author": "测试作者",
                "source_kind": "video",
                "selected": True,
                "disposition": "pending",
            }
        ],
    }
    calls = []

    async def route_handler(route):
        path = route.request.url.split("http://fixture.test")[1].split("?")[0]
        body = route.request.post_data_json if route.request.method == "POST" else None
        calls.append((path, body))
        if path.endswith("/confirm"):
            assert body == {"accept_partial": True}
            data.update(status="monitoring", confirmed=True)
            result = {"job_id": "fixture-parent", "status": "monitoring"}
        elif path == "/api/favorites/imports":
            result = (
                {"job_id": "fixture-parent", "status": "queued"} if body is not None else [data]
            )
        else:
            result = data
        await route.fulfill(json=result)

    await page.route("http://fixture.test/**", route_handler)
    # A synthetic origin allows relative fetch without connecting to any service.
    await page.route(
        "http://fixture.test/", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    await page.goto("http://fixture.test/")
    await page.add_style_tag(content=(root / "static/app.css").read_text())
    await page.add_script_tag(content=(root / "static/shared.js").read_text())
    await page.evaluate(
        "document.getElementById('imports-favorites-view').classList.remove('hidden')"
    )
    await page.add_script_tag(content=(root / "static/imports-favorites.js").read_text())
    await page.evaluate(
        "window.dispatchEvent(new CustomEvent('douku:route',"
        " {detail:{path:'/imports/favorites'}}))"
    )
    await page.locator("#favorites-scan").click()
    await page.locator("#favorites-items a").wait_for()
    assert await page.locator("#favorites-items img").count() == 0
    assert await page.locator("#favorites-confirm").is_disabled()
    await page.screenshot(path=str(tmp_path / "favorites-preview.png"), full_page=True)
    assert not any(path.endswith("/confirm") for path, _ in calls)
    await page.locator("#favorites-partial").check()
    await page.locator("#favorites-confirm").click()
    await page.wait_for_timeout(500)
    assert sum(path.endswith("/confirm") for path, _ in calls) == 1
