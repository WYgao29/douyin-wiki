from pathlib import Path

import pytest
from playwright.async_api import async_playwright


@pytest.fixture
async def offline_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def test_single_import_requires_a_link_before_submitting(offline_page):
    page = offline_page
    root = Path(__file__).parents[1] / "src/douyin_wiki/webapp"
    template = (root / "templates/app.html").read_text()
    begin = template.index('<section id="imports-single-view"')
    end = template.index('<section id="imports-creators-view"', begin)
    await page.set_content(template[begin:end])
    await page.evaluate("""() => {
      window.submitCount = 0;
      document.getElementById('single-form').addEventListener('submit', event => {
        event.preventDefault();
        window.submitCount += 1;
      });
    }""")
    await page.get_by_role("button", name="确认提交").click()
    assert await page.evaluate("window.submitCount") == 0
    await page.locator("#single-share").fill("https://www.douyin.com/video/123")
    await page.get_by_role("button", name="确认提交").click()
    assert await page.evaluate("window.submitCount") == 1
