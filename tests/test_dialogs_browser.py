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


async def test_capture_dialog_cancel_buttons_close_without_required_input(offline_page):
    page = offline_page
    root = Path(__file__).parents[1] / "src/douyin_wiki/webapp"
    template = (root / "templates/app.html").read_text()
    begin = template.index('  <dialog id="capture-dialog"')
    end = template.index('  <dialog id="inspiration-dialog"')
    await page.set_content(template[begin:end])

    dialog = page.locator("#capture-dialog")
    await dialog.evaluate("(node) => node.showModal()")
    await page.get_by_role("button", name="取消", exact=True).click()
    assert not await dialog.is_visible()

    await dialog.evaluate("(node) => node.showModal()")
    await page.get_by_role("button", name="关闭写入窗口").click()
    assert not await dialog.is_visible()
