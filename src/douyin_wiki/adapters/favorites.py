from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..config import MediaSettings
from ..errors import BrowserAuthRequiredError, ExternalToolError
from ..favorites_models import FavoriteFolder, FavoriteInventory, FavoriteWork
from ..models import AuthCheckResult
from .image_note import (
    PlaywrightImageNoteDownloader,
    _page_auth_blocked,
    _usable_auth_cookies,
)

Checkpoint = Callable[[FavoriteInventory], Awaitable[None]]
_STABLE_FOLDER_ID = re.compile(r"^[0-9]{1,32}$")
_FOLDER_PATHS = (re.compile(r"/(?:collection|favorite/folder)/(\d+)(?:/|$)"),)

_ACCOUNT_SNAPSHOT_JS = r"""
() => { // douyin-wiki:account
  const roots = [
    document.querySelector('[data-e2e="user-info"]'),
    document.querySelector('[data-e2e="profile-user-info"]'),
    document.querySelector('header'),
  ].filter(Boolean);
  for (const root of roots) {
    const identified = root.matches('[data-sec-uid]') ? root : root.querySelector('[data-sec-uid]');
    const hrefNode = root.querySelector('a[href*="/user/"]');
    const hrefMatch = hrefNode?.getAttribute('href')?.match(/\/user\/([^/?#]+)/);
    const accountId = identified?.getAttribute('data-sec-uid') || hrefMatch?.[1] || '';
    if (!accountId || accountId === 'self') continue;
    const nickname = root.querySelector('[data-e2e="user-title"], h1, h2')
        ?.textContent?.trim() || '';
    return {account_id: accountId, nickname};
  }
  const heading = document.querySelector('h1, [data-e2e="user-title"]');
  let root = heading?.parentElement;
  for (let i = 0; root && i < 5; i++, root = root.parentElement) {
    const match = root.textContent.match(/抖音号[：:]\s*([a-zA-Z0-9_.-]+)/);
    if (match) return {account_id: 'douyin:' + match[1], nickname: heading.textContent.trim()};
  }
  const ownProfile = location.pathname.match(/^\/user\/([^/?#]+)/);
  return {account_id: ownProfile?.[1] === 'self' ? '' : ownProfile?.[1] || '', nickname: ''};
}
"""

_DIRECTORY_SNAPSHOT_JS = r"""
() => { // douyin-wiki:directory
  const candidates = [...document.querySelectorAll(
    '[data-e2e="favorite-folder-list"], #semiTabPanelfavorite_folder, [data-e2e="favorite_folder"]'
  )].filter(node => node.getClientRects().length && node.getAttribute('role') !== 'tab'
    && node.tagName !== 'BUTTON');
  const root = candidates.find(node =>
    node.querySelector('a, [data-folder-id], [data-collection-id]'))
    || candidates[0];
  if (!root) return {records: [], terminal: false, reported_count: null, boundary_found: false};
  const nodes = root.querySelectorAll('a, [data-collection-id], [data-folder-id]');
  const records = [...nodes].map(node => ({
    name: node.querySelector('[data-e2e="folder-name"]')
        ?.textContent?.trim()
      || node.getAttribute('aria-label')?.trim()
      || node.textContent?.trim()
      || '',
    href: node.getAttribute('href') || '',
    platform_id: node.getAttribute('data-collection-id')
      || node.getAttribute('data-folder-id') || '',
    reported_count: node.getAttribute('data-count'),
  }));
  const countText = root.getAttribute('data-total-count');
  const terminal = Boolean(root.querySelector(
    '[data-e2e="favorite-folder-end"], [data-e2e="list-end"]'));
  return {
    records,
    terminal,
    reported_count: /^\d+$/.test(countText || '') ? Number(countText) : null,
    boundary_found: true,
  };
}
"""

_LISTING_SNAPSHOT_JS = r"""
() => { // douyin-wiki:listing
  const roots = [...document.querySelectorAll(
    'ul.cPDrcaOY.QhXy7t32, [data-e2e="favorite-list"]'
  )].filter(node => node.getClientRects().length);
  const root = roots.length === 1 ? roots[0] : null;
  if (!root) return {records: [], terminal: false, reported_count: null, boundary_found: false};
  const records = [...root.querySelectorAll('a[href]')].map(anchor => {
    const card = anchor.closest('[data-e2e="favorite-item"], li, article') || anchor;
    const alt = anchor.querySelector('img[alt]')?.getAttribute('alt') || '';
    const separator = alt.indexOf('：');
    return {
      scope: 'favorites-list',
      href: anchor.getAttribute('href') || '',
      source_kind: [...card.querySelectorAll('span, div')].some(
        node => !node.childElementCount && node.textContent.trim() === '文章') ? 'article' : '',
      title: card.querySelector('[data-e2e="video-desc"], [data-e2e="work-title"]')
        ?.textContent?.trim()
        || anchor.getAttribute('title')?.trim()
        || (separator >= 0 ? alt.slice(separator + 1) : alt)
        || anchor.innerText?.trim() || '',
      author: card.querySelector('[data-e2e="video-author"], [data-e2e="work-author"]')
        ?.textContent?.trim()
        || (separator >= 0 ? alt.slice(0, separator) : ''),
      unavailable: card.matches('[aria-disabled="true"], [data-unavailable="true"]'),
      folder_ids: (card.getAttribute('data-folder-ids') || '').split(',').filter(Boolean),
    };
  });
  const countText = root.getAttribute('data-total-count');
  const end = root.nextElementSibling;
  const terminal = Boolean(root.querySelector(
    '[data-e2e="favorite-list-end"], [data-e2e="list-end"]'))
    || Boolean(end && /^(暂时没有更多了|没有更多了)$/.test(end.textContent.trim()));
  return {
    records,
    terminal,
    reported_count: /^\d+$/.test(countText || '') ? Number(countText) : null,
    boundary_found: true,
  };
}
"""

_OPEN_FOLDER_JS = r"""
(folderId) => { // douyin-wiki:open-folder
  const candidates = [...document.querySelectorAll(
    '[data-e2e="favorite-folder-list"], #semiTabPanelfavorite_folder, [data-e2e="favorite_folder"]'
  )].filter(node => node.getClientRects().length && node.getAttribute('role') !== 'tab'
    && node.tagName !== 'BUTTON');
  const root = candidates.find(node =>
    node.querySelector('a, [data-folder-id], [data-collection-id]'))
    || candidates[0];
  if (!root) return false;
  for (const node of root.querySelectorAll('a, [data-collection-id], [data-folder-id]')) {
    const explicitId = node.getAttribute('data-collection-id')
      || node.getAttribute('data-folder-id');
    let hrefId = '';
    try {
      const url = new URL(node.getAttribute('href') || '', location.origin);
      if (!['www.douyin.com', 'douyin.com'].includes(url.hostname)
          || !['http:', 'https:'].includes(url.protocol) || url.port
          || url.username || url.password) continue;
      hrefId = url.searchParams.get('collection_id') || url.searchParams.get('folder_id') || '';
      if (!hrefId) hrefId = url.pathname
        .match(/\/(?:collection|favorite\/folder)\/(\d+)(?:\/|$)/)?.[1] || '';
    } catch (_) { continue; }
    if ((explicitId || hrefId) === folderId) {
      node.click();
      return true;
    }
  }
  return false;
}
"""

_SCROLL_LISTING_JS = r"""
() => { // douyin-wiki:scroll
  const roots = [...document.querySelectorAll(
    'ul.cPDrcaOY.QhXy7t32, [data-e2e="favorite-list"]'
  )].filter(node => node.getClientRects().length);
  const root = roots.length === 1 ? roots[0] : null;
  if (!root) return false;
  const scrollParent = root.closest('.route-scroll-container, [data-e2e="scroll-container"]');
  if (scrollParent) scrollParent.scrollTop = scrollParent.scrollHeight;
  else root.lastElementChild?.scrollIntoView({block: 'end'});
  return true;
}
"""


def _stable_folder_id(record: dict[str, Any]) -> str | None:
    href = str(record.get("href") or "").strip()
    if href:
        try:
            target = urlsplit(urljoin("https://www.douyin.com/", href))
            if (
                target.scheme not in {"http", "https"}
                or target.hostname not in {"www.douyin.com", "douyin.com"}
                or target.username
                or target.password
                or target.port
            ):
                return None
        except ValueError:
            return None
    explicit = str(record.get("platform_id") or "").strip()
    if _STABLE_FOLDER_ID.fullmatch(explicit):
        return explicit
    href = str(record.get("href") or "").strip()
    parsed = urlsplit(href if not href.startswith("//") else f"https:{href}")
    for key in ("collection_id", "folder_id"):
        candidate = (parse_qs(parsed.query).get(key) or [""])[0]
        if _STABLE_FOLDER_ID.fullmatch(candidate):
            return candidate
    for pattern in _FOLDER_PATHS:
        if match := pattern.search(parsed.path):
            return match.group(1)
    return None


def _optional_count(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    text = str(value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _folders_from_dom(
    records: Iterable[dict[str, Any]],
    *,
    terminal: bool,
    reported_count: int | None,
) -> tuple[list[FavoriteFolder], bool, list[str]]:
    folders: dict[str, FavoriteFolder] = {}
    unidentified = 0
    for record in records:
        folder_id = _stable_folder_id(record)
        if folder_id is None:
            unidentified += 1
            continue
        name = str(record.get("name") or "").strip() or "未命名收藏夹"
        folder = FavoriteFolder(
            id=folder_id,
            name=name,
            reported_count=_optional_count(record.get("reported_count")),
        )
        folders.setdefault(folder.id, folder)

    warnings: list[str] = []
    complete = terminal
    if unidentified:
        complete = False
        warnings.append(f"有 {unidentified} 个收藏夹未暴露稳定 ID，未加入目录")
    if not terminal:
        warnings.append("无法确认收藏夹目录分页结束")
    if reported_count is not None and len(folders) != reported_count:
        complete = False
        warnings.append(f"收藏夹目录显示 {reported_count} 个，实际识别 {len(folders)} 个")
    return list(folders.values()), complete, warnings


def _folder_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if _STABLE_FOLDER_ID.fullmatch(str(value).strip())
        )
    )


def _works_from_dom(
    records: Iterable[dict[str, Any]], *, folder_id: str | None = None
) -> tuple[list[FavoriteWork], list[str]]:
    works: list[FavoriteWork] = []
    warnings: list[str] = []
    for record in records:
        if record.get("scope") != "favorites-list":
            continue
        href = str(record.get("href") or "").strip()
        href = urljoin("https://www.douyin.com/", href)
        parsed = urlsplit(href)
        match = re.fullmatch(r"/(video|note|article)/(\d+)/?", parsed.path)
        if match is None:
            continue
        route, work_id = match.groups()
        source_kind = {"video": "video", "note": "image_note", "article": "article"}[route]
        if route == "note" and record.get("source_kind") == "article":
            source_kind = "article"
        memberships = _folder_ids(record.get("folder_ids"))
        if folder_id and folder_id not in memberships:
            memberships.append(folder_id)
        try:
            works.append(
                FavoriteWork(
                    work_id=work_id,
                    canonical_url=href,
                    title=str(record.get("title") or "抖音作品"),
                    author=str(record.get("author") or ""),
                    source_kind=source_kind,
                    folder_ids=memberships,
                    available=not bool(record.get("unavailable")),
                )
            )
        except ValueError:
            warnings.append(f"作品 {work_id} 的链接不是规范抖音来源，已忽略")
    return _merge_works(works), warnings


def _merge_works(works: Iterable[FavoriteWork]) -> list[FavoriteWork]:
    merged: dict[str, FavoriteWork] = {}
    for work in works:
        existing = merged.get(work.work_id)
        if existing is None:
            merged[work.work_id] = work.model_copy(deep=True)
            continue
        if existing.canonical_url != work.canonical_url:
            continue
        if existing.source_kind != work.source_kind:
            if "article" in {existing.source_kind, work.source_kind}:
                existing = existing.model_copy(update={"source_kind": "article"})
            else:
                continue
        folder_ids = list(dict.fromkeys([*existing.folder_ids, *work.folder_ids]))
        merged[work.work_id] = existing.model_copy(
            update={
                "folder_ids": folder_ids,
                "available": existing.available or work.available,
                "title": work.title if existing.title == "抖音作品" else existing.title,
                "author": existing.author or work.author,
            }
        )
    return list(merged.values())


class DouyinFavoritesAdapter(PlaywrightImageNoteDownloader):
    """Read favorites through the isolated persistent browser profile."""

    def __init__(self, settings: MediaSettings, profile_dir: Path) -> None:
        super().__init__(settings, profile_dir)

    async def check_auth(self) -> AuthCheckResult:
        async with self._locked_profile():
            result = await self._check_auth_locked()
        messages = {
            "missing": "收藏清点使用的专用浏览器会话尚未初始化",
            "unavailable": "未安装 Playwright，无法验证收藏清点会话",
            "needs_login": "收藏清点使用的专用浏览器需要重新登录抖音",
            "ready": "专用浏览器已登录；收藏页面访问将在清点时验证",
        }
        return result.model_copy(
            update={"scope": "favorites", "message": messages.get(result.state, result.message)}
        )

    async def inventory(
        self,
        *,
        folder_ids: list[str] | None = None,
        directory_only: bool = False,
        expected_account_id: str | None = None,
        on_checkpoint: Checkpoint | None = None,
    ) -> FavoriteInventory:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ExternalToolError("未安装 Playwright；请运行 uv sync") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        async with self._locked_profile(), async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                **self._launch_options(headless=True)
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                cookies = await context.cookies("https://www.douyin.com/")
                return await self._inventory_page(
                    page,
                    cookies=cookies,
                    folder_ids=folder_ids,
                    directory_only=directory_only,
                    expected_account_id=expected_account_id,
                    on_checkpoint=on_checkpoint,
                )
            finally:
                await context.close()

    async def _inventory_page(
        self,
        page: Any,
        *,
        cookies: list[dict[str, Any]],
        folder_ids: list[str] | None,
        directory_only: bool,
        expected_account_id: str | None,
        on_checkpoint: Checkpoint | None,
    ) -> FavoriteInventory:
        response = await page.goto(
            "https://www.douyin.com/user/self?showTab=favorite_collection",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        await page.wait_for_timeout(750)
        body = (await page.locator("body").inner_text(timeout=10_000))[:20_000]
        if not _usable_auth_cookies(cookies) or _page_auth_blocked(
            body, response.status if response else None, page.url
        ):
            raise BrowserAuthRequiredError(
                "收藏清点需要专用浏览器登录",
                details={"action": "douyin-wiki auth douyin"},
            )

        account = await page.evaluate(_ACCOUNT_SNAPSHOT_JS)
        account_id = str((account or {}).get("account_id") or "").strip()
        nickname = str((account or {}).get("nickname") or "").strip()
        if expected_account_id and account_id and account_id != expected_account_id:
            raise BrowserAuthRequiredError(
                "专用浏览器登录账号已变化；请恢复原账号或新建收藏导入任务",
                details={
                    "action": "douyin-wiki auth douyin",
                    "expected_account_id": expected_account_id,
                    "actual_account_id": account_id,
                },
            )

        warnings: list[str] = []
        if not account_id or account_id == "self":
            raise BrowserAuthRequiredError("无法确认收藏所属账号，请登录专用浏览器后重试")

        if not await self._click_tab(page, '[data-e2e="favorite_collection"]'):
            warnings.append("未找到 favorite_collection 收藏区域，未扫描作品")
            return FavoriteInventory(
                account_id=account_id,
                nickname=nickname,
                warnings=warnings,
            )

        folders: list[FavoriteFolder] = []
        folders_complete = False
        if await self._click_tab(page, '[data-e2e="favorite_folder"]'):
            directory = await page.evaluate(_DIRECTORY_SNAPSHOT_JS)
            if not directory.get("boundary_found", True):
                warnings.append("未找到 favorite_folder 目录列表边界")
            else:
                folders, folders_complete, folder_warnings = _folders_from_dom(
                    directory.get("records") or [],
                    terminal=directory.get("terminal") is True,
                    reported_count=_optional_count(directory.get("reported_count")),
                )
                warnings.extend(folder_warnings)
        else:
            warnings.append("未找到 favorite_folder 收藏夹子页")

        base = FavoriteInventory(
            account_id=account_id,
            nickname=nickname,
            folders=folders,
            complete=folders_complete if directory_only else False,
            folders_complete=folders_complete,
            warnings=warnings,
        )
        if directory_only:
            if on_checkpoint:
                await on_checkpoint(base.model_copy(deep=True))
            return base

        requested = None if folder_ids is None else list(dict.fromkeys(folder_ids))
        known_ids = {folder.id for folder in folders}
        if requested is not None:
            invalid_ids = [folder_id for folder_id in requested if folder_id not in known_ids]
            if invalid_ids:
                raise ValueError(f"未知或无稳定标识的收藏夹 ID：{', '.join(invalid_ids)}")
            scopes: list[str | None] = requested
        else:
            scopes = [None]
            if folders:
                folders_complete = False
                warnings.append("全部收藏清点未遍历各收藏夹，作品的收藏夹归属可能不完整")

        all_works: list[FavoriteWork] = []
        all_complete = True
        for folder_id in scopes:
            if folder_id is None:
                if not await self._click_tab(page, '[data-e2e="video"]'):
                    warnings.append("未找到 video 收藏作品子页")
                    all_complete = False
                    continue
            elif not (
                await self._click_tab(page, '[data-e2e="favorite_folder"]')
                and await page.evaluate(_OPEN_FOLDER_JS, folder_id)
            ):
                warnings.append(f"无法按稳定 ID 打开收藏夹 {folder_id}")
                all_complete = False
                continue
            await page.wait_for_timeout(500)
            all_works, scope_complete, scan_warnings = await self._scan_listing(
                page,
                account_id=account_id,
                nickname=nickname,
                folders=folders,
                folders_complete=folders_complete,
                existing=all_works,
                folder_id=folder_id,
                inherited_warnings=warnings,
                on_checkpoint=on_checkpoint,
            )
            warnings.extend(scan_warnings)
            all_complete = all_complete and scope_complete

            if folder_id is not None:
                reported = next(
                    (folder.reported_count for folder in folders if folder.id == folder_id), None
                )
                observed = sum(folder_id in work.folder_ids for work in all_works)
                if reported is not None and observed != reported:
                    all_complete = False
                    warnings.append(
                        f"收藏夹 {folder_id} 显示 {reported} 条，实际识别 {observed} 条"
                    )

        complete = all_complete and bool(account_id)
        return FavoriteInventory(
            account_id=account_id,
            nickname=nickname,
            folders=folders,
            works=_merge_works(all_works),
            complete=complete,
            folders_complete=folders_complete,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def _scan_listing(
        self,
        page: Any,
        *,
        account_id: str,
        nickname: str,
        folders: list[FavoriteFolder],
        folders_complete: bool,
        existing: list[FavoriteWork],
        folder_id: str | None,
        inherited_warnings: list[str],
        on_checkpoint: Checkpoint | None,
    ) -> tuple[list[FavoriteWork], bool, list[str]]:
        works = _merge_works(existing)
        warnings: list[str] = []
        previous_count = len(works)
        idle_rounds = 0
        terminal = False
        for _ in range(500):
            body = (await page.locator("body").inner_text(timeout=10_000))[:20_000]
            current = await page.evaluate(_ACCOUNT_SNAPSHOT_JS)
            if (
                _page_auth_blocked(body, None, page.url)
                or (current or {}).get("account_id") != account_id
            ):
                raise BrowserAuthRequiredError("清点期间登录失效或账号已变化，请恢复原账号后重试")
            snapshot = await page.evaluate(_LISTING_SNAPSHOT_JS)
            if not snapshot.get("boundary_found", True):
                warnings.append("未找到收藏作品列表边界；未扫描全页链接")
                break
            batch, batch_warnings = _works_from_dom(
                snapshot.get("records") or [], folder_id=folder_id
            )
            warnings.extend(batch_warnings)
            works = _merge_works([*works, *batch])
            terminal = snapshot.get("terminal") is True

            if on_checkpoint:
                checkpoint_warnings = list(dict.fromkeys([*inherited_warnings, *warnings]))
                await on_checkpoint(
                    FavoriteInventory(
                        account_id=account_id,
                        nickname=nickname,
                        folders=folders,
                        works=works,
                        complete=False,
                        folders_complete=folders_complete,
                        warnings=checkpoint_warnings,
                    )
                )
            if terminal:
                break

            if len(works) == previous_count:
                idle_rounds += 1
            else:
                idle_rounds = 0
                previous_count = len(works)
            if idle_rounds >= 5:
                break
            scrolled = await page.evaluate(_SCROLL_LISTING_JS)
            if not scrolled:
                break
            await page.wait_for_timeout(750)
        else:
            warnings.append("收藏清点达到安全扫描上限，结果不完整")

        reported = _optional_count(snapshot.get("reported_count"))
        observed = sum(folder_id is None or folder_id in work.folder_ids for work in works)
        if reported is not None and reported != observed:
            terminal = False
            warnings.append(f"作品列表显示 {reported} 条，实际识别 {observed} 条")
        if warnings:
            terminal = False
        if not terminal:
            warnings.append("未取得可信的收藏作品分页结束信号")
        return works, terminal, list(dict.fromkeys(warnings))

    async def _click_tab(self, page: Any, selector: str) -> bool:
        tab_ids = {
            '[data-e2e="favorite_collection"]': "#semiTabfavorite_collection",
            '[data-e2e="favorite_folder"]': "#semiTabfavorite_folder",
            '[data-e2e="video"]': "#semiTabvideo",
        }
        for candidate in (tab_ids.get(selector, selector), selector):
            locator = page.locator(candidate).first
            if await locator.count() == 0:
                continue
            with suppress(Exception):
                await locator.click(timeout=10_000)
                await page.wait_for_timeout(500)
                return True
        return False
