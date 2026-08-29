from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import mimetypes
import subprocess
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import MediaSettings
from ..errors import BrowserAuthRequiredError, ExternalToolError, VideoUnavailableError
from ..models import (
    AuthCheckResult,
    CreatorInventoryResult,
    CreatorInventoryWork,
    CreatorProfile,
    SourceKind,
)
from .image_note import _page_auth_blocked, _usable_auth_cookies
from .share import DouyinShareResolver, extract_creator_sec_uid, extract_douyin_url


def creator_id_for(sec_uid: str) -> str:
    return f"dyc-{hashlib.sha256(sec_uid.encode()).hexdigest()[:16]}"


def _profile_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_profile_lock(handle: Any) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _first_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, list):
        for item in value:
            if result := _first_url(item):
                return result
    if isinstance(value, dict):
        for key in ("url_list", "download_url_list", "url", "uri"):
            if key in value and (result := _first_url(value[key])):
                return result
    return None


def _author_from_node(node: Any, *, work_id: str | None = None) -> dict[str, Any] | None:
    if isinstance(node, dict):
        node_work_id = str(node.get("aweme_id") or node.get("item_id") or "")
        author = node.get("author")
        if (
            isinstance(author, dict)
            and author.get("sec_uid")
            and (work_id is None or node_work_id == work_id)
        ):
            return author
        for child in node.values():
            if result := _author_from_node(child, work_id=work_id):
                return result
    elif isinstance(node, list):
        for child in node:
            if result := _author_from_node(child, work_id=work_id):
                return result
    return None


def _work_from_aweme(detail: dict[str, Any]) -> CreatorInventoryWork | None:
    work_id = str(detail.get("aweme_id") or detail.get("item_id") or "")
    if not work_id.isdigit():
        return None
    images = detail.get("images") or (detail.get("image_post_info") or {}).get("images")
    source_kind = SourceKind.IMAGE_NOTE if images else SourceKind.VIDEO
    route = "note" if source_kind == SourceKind.IMAGE_NOTE else "video"
    description = str(detail.get("desc") or detail.get("description") or "").strip()
    title = description.splitlines()[0][:160] if description else "抖音作品"
    published_at = None
    with suppress(TypeError, ValueError, OSError):
        timestamp = float(detail.get("create_time") or 0)
        if timestamp:
            published_at = datetime.fromtimestamp(timestamp, UTC)
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    duration = video.get("duration") or detail.get("duration")
    duration_seconds = None
    with suppress(TypeError, ValueError):
        value = float(duration)
        duration_seconds = value / 1000
    cover = None
    for candidate in (
        detail.get("cover"),
        video.get("cover"),
        video.get("origin_cover"),
        video.get("dynamic_cover"),
        images[0] if isinstance(images, list) and images else None,
    ):
        if cover := _first_url(candidate):
            break
    return CreatorInventoryWork(
        work_id=work_id,
        source_kind=source_kind,
        canonical_url=f"https://www.douyin.com/{route}/{work_id}",
        original_url=f"https://www.douyin.com/{route}/{work_id}",
        title=title,
        published_at=published_at,
        duration_seconds=duration_seconds,
        thumbnail_url=cover,
        is_pinned=bool(detail.get("is_top") or detail.get("is_pinned")),
    )


def _collect_post_payload(payload: Any) -> tuple[list[CreatorInventoryWork], bool | None]:
    works: list[CreatorInventoryWork] = []
    has_more: bool | None = None

    def visit(node: Any) -> None:
        nonlocal has_more
        if isinstance(node, dict):
            if node.get("aweme_id") and (work := _work_from_aweme(node)):
                works.append(work)
            if "has_more" in node and isinstance(node.get("has_more"), (bool, int)):
                has_more = bool(node["has_more"])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(payload)
    unique = {item.work_id: item for item in works}
    return list(unique.values()), has_more


def _profile_from_author(author: dict[str, Any], original_url: str) -> CreatorProfile:
    sec_uid = str(author.get("sec_uid") or "")
    return CreatorProfile(
        sec_uid=sec_uid,
        canonical_url=f"https://www.douyin.com/user/{sec_uid}",
        original_url=original_url,
        nickname=str(author.get("nickname") or author.get("unique_id") or "抖音博主"),
        uid=str(author.get("uid") or "") or None,
        unique_id=str(author.get("unique_id") or "") or None,
        signature=str(author.get("signature") or ""),
        avatar_url=_first_url(author.get("avatar_larger") or author.get("avatar_thumb")),
        reported_work_count=(
            int(author["aweme_count"]) if str(author.get("aweme_count") or "").isdigit() else None
        ),
    )


class DouyinCreatorAdapter:
    """Resolve a creator from a profile or work URL and inventory public posts."""

    def __init__(
        self,
        settings: MediaSettings,
        profile_dir: Path,
        resolver: DouyinShareResolver | None = None,
    ) -> None:
        self.settings = settings
        self.profile_dir = profile_dir
        self.resolver = resolver or DouyinShareResolver()
        self._process_lock = asyncio.Lock()

    def _launch_options(self, *, headless: bool) -> dict[str, Any]:
        browser = self.settings.browser.lower()
        if browser == "edge":
            browser = "msedge"
        options: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "locale": "zh-CN",
            "viewport": {"width": 1440, "height": 1000},
        }
        if browser in {
            "chrome",
            "chrome-beta",
            "chrome-dev",
            "chrome-canary",
            "msedge",
            "msedge-beta",
            "msedge-dev",
            "msedge-canary",
        }:
            options["channel"] = browser
        return options

    @asynccontextmanager
    async def _locked_profile(self):
        async with self._process_lock:
            lock_path = self.profile_dir.parent / f"{self.profile_dir.name}.lock"
            handle = await asyncio.to_thread(_profile_lock, lock_path)
            try:
                yield
            finally:
                await asyncio.to_thread(_release_profile_lock, handle)

    async def check_auth(self) -> AuthCheckResult:
        if not self.profile_dir.exists():
            return AuthCheckResult(
                scope="creator",
                state="missing",
                ok=False,
                cookie_source=str(self.profile_dir),
                message="博主清点使用的专用浏览器会话尚未登录",
                action="douyin-wiki auth douyin",
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return AuthCheckResult(
                scope="creator",
                state="unavailable",
                ok=False,
                cookie_source=str(self.profile_dir),
                message="未安装 Playwright，无法清点博主主页",
                action="uv sync",
            )
        async with self._locked_profile():
            try:
                async with async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(
                        **self._launch_options(headless=True)
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                    response = await page.goto(
                        "https://www.douyin.com/", wait_until="domcontentloaded", timeout=60_000
                    )
                    await page.wait_for_timeout(750)
                    cookies = await context.cookies("https://www.douyin.com/")
                    body = (await page.locator("body").inner_text(timeout=10_000))[:20_000]
                    status = response.status if response else None
                    final_url = page.url
                    await context.close()
                ready = bool(_usable_auth_cookies(cookies)) and not _page_auth_blocked(
                    body, status, final_url
                )
                return AuthCheckResult(
                    scope="creator",
                    state="ready" if ready else "needs_login",
                    ok=ready,
                    server_verified=True,
                    cookie_source=str(self.profile_dir),
                    message=(
                        "专用浏览器可以访问抖音博主主页" if ready else "专用浏览器需要重新登录抖音"
                    ),
                    action=None if ready else "douyin-wiki auth douyin",
                )
            except Exception as exc:
                return AuthCheckResult(
                    scope="creator",
                    state="error",
                    ok=False,
                    cookie_source=str(self.profile_dir),
                    message=f"博主会话验证失败：{type(exc).__name__}",
                    action="douyin-wiki auth douyin",
                )

    async def inventory(self, source_text: str, target_dir: Path) -> CreatorInventoryResult:
        source_url = extract_douyin_url(source_text)
        sec_uid = extract_creator_sec_uid(source_url)
        work_id: str | None = None
        work_url: str | None = None
        if sec_uid is None:
            resolved = await self.resolver.resolve(source_text)
            work_id = resolved.video_id
            work_url = resolved.canonical_url

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ExternalToolError("未安装 Playwright；请运行 uv sync") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        async with self._locked_profile(), async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                **self._launch_options(headless=True)
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                cookies = await context.cookies("https://www.douyin.com/")
                if not _usable_auth_cookies(cookies):
                    raise BrowserAuthRequiredError(
                        "博主主页清点需要专用浏览器登录",
                        details={"action": "douyin-wiki auth douyin"},
                    )

                if sec_uid is None and work_url and work_id:
                    sec_uid = await self._creator_from_work(page, work_url, work_id)
                    if sec_uid is None:
                        sec_uid = await asyncio.to_thread(self._yt_dlp_creator_sec_uid, work_url)
                    if sec_uid is None:
                        raise VideoUnavailableError("无法从该作品识别稳定的博主主页")

                profile_url = f"https://www.douyin.com/user/{sec_uid}"
                return await self._inventory_profile(
                    context, page, profile_url, original_url=source_url, target_dir=target_dir
                )
            finally:
                await context.close()

    async def _creator_from_work(self, page: Any, url: str, work_id: str) -> str | None:
        payloads: list[Any] = []

        async def capture(response: Any) -> None:
            lowered = response.url.lower()
            if not any(token in lowered for token in ("aweme/detail", "aweme/v1/web/aweme")):
                return
            if "json" not in response.headers.get("content-type", "").lower():
                return
            with suppress(Exception):
                payloads.append(await response.json())

        page.on("response", capture)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2000)
        for payload in payloads:
            if (author := _author_from_node(payload, work_id=work_id)) and (
                sec_uid := author.get("sec_uid")
            ):
                return str(sec_uid)
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
        for raw in scripts:
            with suppress(json.JSONDecodeError):
                data = json.loads(raw)
                for item in data.get("itemListElement", []):
                    candidate = str(item.get("item") or "")
                    if sec_uid := extract_creator_sec_uid(candidate):
                        return sec_uid
        return None

    def _yt_dlp_creator_sec_uid(self, url: str) -> str | None:
        browser = self.settings.browser
        if self.settings.browser_profile:
            browser = f"{browser}:{self.settings.browser_profile}"
        try:
            result = subprocess.run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--cookies-from-browser",
                    browser,
                    "--simulate",
                    "--no-warnings",
                    "--print",
                    "%(channel_url)s",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode:
            return None
        return extract_creator_sec_uid(result.stdout.strip())

    async def _inventory_profile(
        self,
        context: Any,
        page: Any,
        profile_url: str,
        *,
        original_url: str,
        target_dir: Path,
    ) -> CreatorInventoryResult:
        post_payloads: list[Any] = []
        other_payloads: list[Any] = []

        async def capture(response: Any) -> None:
            lowered = response.url.lower()
            if "json" not in response.headers.get("content-type", "").lower():
                return
            if not any(token in lowered for token in ("aweme/post", "user/profile", "user/detail")):
                return
            with suppress(Exception):
                payload = await response.json()
                (post_payloads if "aweme/post" in lowered else other_payloads).append(payload)

        page.on("response", capture)
        response = await page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2000)
        body = (await page.locator("body").inner_text(timeout=10_000))[:50_000]
        if _page_auth_blocked(
            body, response.status if response else None, page.url
        ):
            raise BrowserAuthRequiredError(
                "博主主页要求登录或安全验证",
                details={"action": "douyin-wiki auth douyin"},
            )

        discovered: dict[str, CreatorInventoryWork] = {}
        terminal = False
        idle_rounds = 0
        previous_count = 0
        for _ in range(500):
            for payload in post_payloads:
                works, has_more = _collect_post_payload(payload)
                for work in works:
                    discovered.setdefault(work.work_id, work)
                if has_more is False:
                    terminal = True
            if terminal:
                break
            await page.evaluate(
                """() => {
                  const container = document.querySelector('.route-scroll-container');
                  if (container) container.scrollTop = container.scrollHeight;
                  else window.scrollTo(0, document.documentElement.scrollHeight);
                }"""
            )
            await page.wait_for_timeout(1000)
            if len(discovered) == previous_count:
                idle_rounds += 1
            else:
                idle_rounds = 0
                previous_count = len(discovered)
            if idle_rounds >= 5:
                break

        author = None
        expected_sec_uid = extract_creator_sec_uid(profile_url)
        for payload in [*other_payloads, *post_payloads]:
            candidate = _author_from_node(payload)
            candidate_sec_uid = str((candidate or {}).get("sec_uid") or "")
            candidate_uid = str((candidate or {}).get("uid") or "")
            if candidate and (
                candidate_sec_uid == expected_sec_uid
                or (str(expected_sec_uid).isdigit() and candidate_uid == expected_sec_uid)
            ):
                author = candidate
                break
        if author is None:
            if str(expected_sec_uid).isdigit():
                raise VideoUnavailableError("无法把数字 uid 解析为稳定的 sec_uid")
            author = {
                "sec_uid": expected_sec_uid,
                "nickname": (await page.title()).removesuffix("的抖音 - 抖音").strip()
                or "抖音博主",
            }

        profile = _profile_from_author(author, original_url)
        reported = profile.reported_work_count
        complete = terminal or (reported is not None and len(discovered) >= reported)
        warnings: list[str] = []
        if reported is not None and len(discovered) != reported:
            complete = False
            warnings.append(f"主页显示 {reported} 条作品，实际取得 {len(discovered)} 条")
        elif not terminal and reported is None:
            complete = False
            warnings.append("无法确认主页分页是否完整")
        if not post_payloads:
            complete = False
            warnings.append("未取得主页的结构化作品列表；为避免混入推荐内容，未使用全页链接兜底")

        works = list(discovered.values())
        for index, work in enumerate(works, start=1):
            if work.thumbnail_url:
                work.thumbnail_path = await self._download_preview(
                    context, work.thumbnail_url, target_dir / f"{index:03d}-{work.work_id}"
                )
        if profile.avatar_url:
            profile.avatar_path = await self._download_preview(
                context, profile.avatar_url, target_dir / "avatar"
            )
        return CreatorInventoryResult(
            profile=profile,
            works=works,
            complete=complete,
            reported_count=reported,
            warnings=warnings,
        )

    async def _download_preview(self, context: Any, url: str, stem: Path) -> str | None:
        with suppress(Exception):
            response = await context.request.get(
                url,
                headers={"Referer": "https://www.douyin.com/"},
                timeout=30_000,
            )
            if not response.ok:
                return None
            content = await response.body()
            if not content or len(content) > 12 * 1024 * 1024:
                return None
            content_type = response.headers.get("content-type", "").partition(";")[0]
            suffix = mimetypes.guess_extension(content_type) or Path(urlsplit(url).path).suffix
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
                suffix = ".jpg"
            target = stem.with_suffix(suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return str(target)
        return None
