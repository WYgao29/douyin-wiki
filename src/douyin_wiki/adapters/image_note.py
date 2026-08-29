from __future__ import annotations

import asyncio
import fcntl
import hashlib
import mimetypes
import re
import shutil
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..config import MediaSettings
from ..errors import (
    BrowserAuthRequiredError,
    ExternalToolError,
    LivePhotoUnsupportedError,
    RegionRestrictedError,
    VideoUnavailableError,
)
from ..models import AuthCheckResult, SourceKind, VideoMetadata

IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
AUTH_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_guard"}
SEO_AUTHOR_PATTERN = re.compile(r"\s+-\s+(?P<author>[^\n]{1,80}?)于(?P<date>\d{8})发布在抖音")


def _acquire_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_file_lock(handle: Any) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _usable_auth_cookies(
    cookies: list[dict[str, Any]], *, now: float | None = None
) -> list[dict[str, Any]]:
    """Filter login cookies by name, Douyin scope and explicit expiry."""
    current = datetime.now(UTC).timestamp() if now is None else now
    return [
        cookie
        for cookie in cookies
        if cookie.get("name") in AUTH_COOKIE_NAMES
        and str(cookie.get("domain") or "").lstrip(".").endswith("douyin.com")
        and (
            float(cookie.get("expires") or -1) <= 0 or float(cookie.get("expires") or -1) > current
        )
    ]


def _page_auth_blocked(text: str, status: int | None, url: str | None = None) -> bool:
    if status in {401, 403}:
        return True
    lowered_url = (url or "").lower()
    if any(
        token in lowered_url
        for token in ("passport.douyin.com", "/verify", "captcha", "security-check")
    ):
        return True
    sample = text[:20_000].lower()
    if any(
        token in sample
        for token in (
            "captcha_verify",
            "verifycenter",
            "login-panel",
            '"is_login":false',
            '"islogin":false',
        )
    ):
        return True
    # Plain words such as “请登录” may be legitimate post text. Treat them as an
    # auth wall only when the visible page itself is a short blocking message.
    visible = re.sub(r"\s+", " ", sample).strip()
    return len(visible) <= 500 and any(
        token in visible for token in ("登录后查看", "请登录", "验证码", "安全验证")
    )


def _find_aweme_detail(value: Any, *, expected_work_id: str | None = None) -> dict[str, Any] | None:
    """Find a work-detail object in the different envelopes used by Douyin."""
    def matches(candidate: dict[str, Any]) -> bool:
        candidate_id = str(candidate.get("aweme_id") or candidate.get("item_id") or "")
        return bool(candidate_id and (expected_work_id is None or candidate_id == expected_work_id))

    if isinstance(value, dict):
        for key in ("aweme_detail", "aweme", "item", "note_detail"):
            candidate = value.get(key)
            if isinstance(candidate, dict) and (
                matches(candidate)
                and (candidate.get("images") or candidate.get("image_post_info"))
            ):
                return candidate
        if matches(value) and (value.get("images") or value.get("image_post_info")):
            return value
        for child in value.values():
            found = _find_aweme_detail(child, expected_work_id=expected_work_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_aweme_detail(child, expected_work_id=expected_work_id)
            if found:
                return found
    return None


def _url_candidates(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith("http"):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_url_candidates(item))
        return result
    if not isinstance(value, dict):
        return []

    result: list[str] = []
    preferred_keys = (
        "download_url_list",
        "url_list",
        "download_url",
        "origin_url",
        "display_url",
        "url",
    )
    for key in preferred_keys:
        if key in value:
            result.extend(_url_candidates(value[key]))
    return result


def _dedupe_image_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        # Signed CDN URLs for the same asset frequently differ only by query params.
        identity = f"{parsed.hostname or ''}{parsed.path}"
        if identity in seen:
            continue
        seen.add(identity)
        result.append(url)
    return result


def _best_image_url(value: Any) -> str | None:
    variants: list[tuple[int, int, str]] = []
    sequence = 0

    def visit(node: Any, inherited_score: int = 0) -> None:
        nonlocal sequence
        if isinstance(node, list):
            for child in node:
                visit(child, inherited_score)
            return
        if not isinstance(node, dict):
            return
        try:
            own_score = int(node.get("width") or 0) * int(node.get("height") or 0)
        except (TypeError, ValueError):
            own_score = 0
        score = own_score or inherited_score
        direct: list[str] = []
        for key in (
            "download_url_list",
            "url_list",
            "download_url",
            "origin_url",
            "display_url",
            "url",
        ):
            if key in node:
                direct.extend(_url_candidates(node[key]))
        if direct:
            variants.append((score, -sequence, direct[0]))
            sequence += 1
        for key in (
            "origin_image",
            "display_image",
            "owner_watermark_image",
            "thumbnail",
            "image",
        ):
            if key in node:
                visit(node[key], score)

    visit(value)
    return max(variants)[2] if variants else None


def _contains_live_photo(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"live_photo", "livephoto", "is_live_photo"} and child:
                return True
            if lowered in {"video", "video_url", "video_play_addr"} and child:
                return True
            if _contains_live_photo(child):
                return True
    elif isinstance(value, list):
        return any(_contains_live_photo(child) for child in value)
    return False


def _metadata_from_aweme(
    detail: dict[str, Any], *, url: str, work_id: str
) -> tuple[VideoMetadata, list[str]]:
    image_container = detail.get("image_post_info") or {}
    images = detail.get("images") or image_container.get("images") or []
    if not isinstance(images, list):
        images = []
    if any(_contains_live_photo(item) for item in images):
        raise LivePhotoUnsupportedError("检测到 Live Photo，当前版本暂不支持动态内容")

    image_urls: list[str] = []
    for image in images:
        if best_url := _best_image_url(image):
            image_urls.append(best_url)
    image_urls = _dedupe_image_urls(image_urls)

    author = detail.get("author") or {}
    music = detail.get("music") or {}
    timestamp = detail.get("create_time")
    published_at = None
    if timestamp:
        with suppress(TypeError, ValueError, OSError):
            published_at = datetime.fromtimestamp(float(timestamp), UTC)
    post_text = str(detail.get("desc") or detail.get("description") or "").strip()
    title = post_text.splitlines()[0][:120] if post_text else "抖音图文作品"
    music_metadata = None
    if isinstance(music, dict) and music:
        music_metadata = {
            key: music.get(key)
            for key in ("title", "author", "mid")
            if music.get(key) not in (None, "")
        } or None

    metadata = VideoMetadata(
        video_id=str(detail.get("aweme_id") or work_id),
        original_url=url,
        canonical_url=f"https://www.douyin.com/note/{work_id}",
        title=title,
        author=(
            author.get("nickname") or author.get("unique_id") if isinstance(author, dict) else None
        ),
        published_at=published_at,
        description=post_text or None,
        post_text=post_text or None,
        music_metadata=music_metadata,
        source_kind=SourceKind.IMAGE_NOTE,
        creator_sec_uid=(str(author.get("sec_uid") or "") if isinstance(author, dict) else None)
        or None,
        creator_uid=(str(author.get("uid") or "") if isinstance(author, dict) else None) or None,
        creator_unique_id=(str(author.get("unique_id") or "") if isinstance(author, dict) else None)
        or None,
        creator_url=(
            f"https://www.douyin.com/user/{author.get('sec_uid')}"
            if isinstance(author, dict) and author.get("sec_uid")
            else None
        ),
    )
    return metadata, image_urls


def _suffix_for_image(content_type: str, content: bytes, url: str) -> str:
    mime = content_type.partition(";")[0].strip().lower()
    if mime in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[mime]
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if content.startswith(b"\x89PNG"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    guessed = mimetypes.guess_type(urlsplit(url).path)[0]
    return IMAGE_EXTENSIONS.get(guessed or "", ".webp")


def _dom_text_metadata(
    description: str, text_blocks: list[str], fallback_author: str
) -> tuple[str, str, str | None, datetime | None]:
    seo_match = SEO_AUTHOR_PATTERN.search(description)
    description_body = (
        description[: seo_match.start()].strip() if seo_match else description.strip()
    )
    headline_match = re.match(r"^(.{1,120}?[？！!?。])", description_body)
    headline = headline_match.group(1).strip() if headline_match else ""
    anchor = re.sub(r"\s+", "", headline or description_body)[:24]
    candidates = [
        value.strip()
        for value in text_blocks
        if value.strip()
        and (not anchor or anchor in re.sub(r"\s+", "", value))
        and len(value.strip()) <= 20_000
    ]
    post_text = max(candidates, key=len, default=description_body)
    title = (headline or min(candidates, key=len, default=post_text or "抖音图文作品"))[:120]

    author = fallback_author.strip() or None
    published_at = None
    if seo_match:
        author = seo_match.group("author").strip() or author
        with suppress(ValueError):
            local_date = datetime.strptime(seo_match.group("date"), "%Y%m%d").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            )
            published_at = local_date
    return title, post_text, author, published_at


class PlaywrightImageNoteDownloader:
    """Collect static Douyin image works using an isolated persistent browser."""

    def __init__(self, settings: MediaSettings, profile_dir: Path) -> None:
        self.settings = settings
        self.profile_dir = profile_dir
        # Chromium permits only one persistent context per profile directory.
        self._profile_lock = asyncio.Lock()

    @asynccontextmanager
    async def _locked_profile(self):
        """Serialize persistent Chromium access across threads and processes."""
        async with self._profile_lock:
            lock_path = self.profile_dir.parent / f"{self.profile_dir.name}.lock"
            lock_file = await asyncio.to_thread(_acquire_file_lock, lock_path)
            try:
                yield
            finally:
                await asyncio.to_thread(_release_file_lock, lock_file)

    def _launch_options(self, *, headless: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "locale": "zh-CN",
            "viewport": {"width": 1440, "height": 1000},
        }
        browser = self.settings.browser.lower()
        if browser == "edge":
            browser = "msedge"
        channels = {
            "chrome",
            "chrome-beta",
            "chrome-dev",
            "chrome-canary",
            "msedge",
            "msedge-beta",
            "msedge-dev",
            "msedge-canary",
        }
        if browser in channels:
            options["channel"] = browser
        return options

    async def authenticate(self, *, timeout_seconds: int = 600) -> None:
        async with self._locked_profile():
            await self._authenticate_locked(timeout_seconds=timeout_seconds)

    async def check_auth(self) -> AuthCheckResult:
        async with self._locked_profile():
            return await self._check_auth_locked()

    async def _check_auth_locked(self) -> AuthCheckResult:
        source = str(self.profile_dir)
        if not self.profile_dir.exists():
            return AuthCheckResult(
                scope="image_note",
                state="missing",
                ok=False,
                cookie_source=source,
                message="图文专用浏览器会话尚未初始化",
                action="douyin-wiki auth douyin",
            )
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return AuthCheckResult(
                scope="image_note",
                state="unavailable",
                ok=False,
                cookie_source=source,
                message="未安装 Playwright，无法验证图文会话",
                action="uv sync",
            )

        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    **self._launch_options(headless=True)
                )
                page = context.pages[0] if context.pages else await context.new_page()
                response = await page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await page.wait_for_timeout(1000)
                cookies = await context.cookies("https://www.douyin.com/")
                usable = _usable_auth_cookies(cookies)
                body_text = (await page.locator("body").inner_text(timeout=10_000))[:20_000]
                status = response.status if response else None
                final_url = page.url
                await context.close()
                if _page_auth_blocked(body_text, status, final_url):
                    return AuthCheckResult(
                        scope="image_note",
                        state="needs_login",
                        ok=False,
                        server_verified=True,
                        cookie_source=source,
                        message="抖音页面要求登录或安全验证",
                        action="douyin-wiki auth douyin",
                    )
                if not usable:
                    return AuthCheckResult(
                        scope="image_note",
                        state="missing",
                        ok=False,
                        server_verified=True,
                        cookie_source=source,
                        message="专用浏览器中没有未过期的抖音登录 Cookie",
                        action="douyin-wiki auth douyin",
                    )
                return AuthCheckResult(
                    scope="image_note",
                    state="ready",
                    ok=True,
                    server_verified=True,
                    cookie_source=source,
                    message="专用浏览器会话已通过抖音页面验证",
                )
        except Exception as exc:
            return AuthCheckResult(
                scope="image_note",
                state="error",
                ok=False,
                server_verified=False,
                cookie_source=source,
                message=f"图文会话验证失败：{type(exc).__name__}",
                action="douyin-wiki auth douyin",
            )

    async def _authenticate_locked(self, *, timeout_seconds: int) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ExternalToolError("未安装 Playwright；请运行 uv sync") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    **self._launch_options(headless=False)
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while asyncio.get_running_loop().time() < deadline:
                    cookies = await context.cookies("https://www.douyin.com/")
                    body_text = (await page.locator("body").inner_text(timeout=10_000))[:20_000]
                    if _usable_auth_cookies(cookies) and not _page_auth_blocked(
                        body_text, None, page.url
                    ):
                        await context.close()
                        return
                    await asyncio.sleep(2)
                await context.close()
        except BrowserAuthRequiredError:
            raise
        except Exception as exc:
            raise BrowserAuthRequiredError(
                "未在限定时间内完成抖音登录", details={"cause": str(exc)}
            ) from exc
        raise BrowserAuthRequiredError("未在限定时间内完成抖音登录")

    async def download(self, url: str, work_id: str, target_dir: Path) -> VideoMetadata:
        async with self._locked_profile():
            return await self._download_locked(url, work_id, target_dir)

    async def _download_locked(self, url: str, work_id: str, target_dir: Path) -> VideoMetadata:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ExternalToolError("未安装 Playwright；请运行 uv sync") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        payloads: list[Any] = []
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    **self._launch_options(headless=True)
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()

                    async def capture_response(response: Any) -> None:
                        lowered = response.url.lower()
                        if not any(token in lowered for token in ("aweme", "detail", "note")):
                            return
                        if "json" not in response.headers.get("content-type", "").lower():
                            return
                        try:
                            payloads.append(await response.json())
                        except Exception:
                            return

                    page.on("response", capture_response)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    for _ in range(20):
                        if any(
                            _find_aweme_detail(payload, expected_work_id=work_id)
                            for payload in payloads
                        ):
                            break
                        await page.wait_for_timeout(500)
                    body_text = (await page.locator("body").inner_text(timeout=10_000))[:20_000]

                    metadata: VideoMetadata | None = None
                    image_urls: list[str] = []
                    for payload in payloads:
                        if detail := _find_aweme_detail(payload, expected_work_id=work_id):
                            metadata, image_urls = _metadata_from_aweme(
                                detail, url=url, work_id=work_id
                            )
                            if image_urls:
                                break

                    if not image_urls:
                        metadata, image_urls = await self._from_dom(page, url=url, work_id=work_id)

                    if not image_urls:
                        self._raise_page_error(
                            body_text, response.status if response else None, page.url
                        )

                    image_paths = await self._download_images(context, image_urls, target_dir)
                    if metadata is None:
                        metadata = VideoMetadata(
                            video_id=work_id,
                            original_url=url,
                            canonical_url=f"https://www.douyin.com/note/{work_id}",
                            title="抖音图文作品",
                            source_kind=SourceKind.IMAGE_NOTE,
                        )
                    metadata.image_paths = [str(path) for path in image_paths]
                    metadata.thumbnail_path = str(image_paths[0])
                    metadata.thumbnail_kind = "image_note_first_image"
                    return metadata
                finally:
                    await context.close()
        except (BrowserAuthRequiredError, LivePhotoUnsupportedError, RegionRestrictedError):
            raise
        except VideoUnavailableError:
            raise
        except Exception as exc:
            lowered = str(exc).lower()
            if "executable doesn't exist" in lowered or "browser executable" in lowered:
                raise ExternalToolError(
                    "找不到可用浏览器；请安装 Chrome，或运行 playwright install chromium"
                ) from exc
            raise ExternalToolError("抖音图文采集失败", details={"cause": str(exc)}) from exc

    async def _from_dom(
        self, page: Any, *, url: str, work_id: str
    ) -> tuple[VideoMetadata, list[str]]:
        data = await page.evaluate(
            """
            () => {
              const root = document.querySelector('main') || document.body;
              const nodes = Array.from(root.querySelectorAll('img'));
              const candidates = nodes.map((img, order) => ({
                order,
                src: img.currentSrc || img.src || '',
                srcset: img.srcset || '',
                alt: img.alt || '',
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0
              })).filter(x => x.src && x.width >= 300 && x.height >= 300);
              const maxArea = Math.max(0, ...candidates.map(x => x.width * x.height));
              const images = candidates.filter(x =>
                x.width * x.height >= maxArea * 0.35 &&
                !x.src.includes('PackSourceEnum_WEBPC_RELATED_AWEME')
              );
              const authorLink = root.querySelector('a[href*="/user/"]');
              const musicLink = root.querySelector('a[href*="/music/"]');
              const text = (root.innerText || '').trim();
              const description = document.querySelector('meta[name="description"]')?.content || '';
              const textBlocks = Array.from(root.querySelectorAll('h1, p, span'))
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(text => text.length >= 4 && text.length <= 20000);
              return {
                images,
                author: authorLink?.innerText?.trim() || '',
                musicTitle: musicLink?.innerText?.trim() || '',
                livePhoto: Array.from(root.querySelectorAll('video')).some(video =>
                  (video.videoWidth || video.clientWidth || 0) >= 300 &&
                  (video.videoHeight || video.clientHeight || 0) >= 300
                ),
                text,
                description,
                textBlocks
              };
            }
            """
        )
        if data.get("livePhoto"):
            raise LivePhotoUnsupportedError("检测到 Live Photo，当前版本暂不支持动态内容")
        image_items = data.get("images", [])
        aweme_images = [
            item for item in image_items if "biz_tag=aweme_images" in str(item.get("src") or "")
        ]
        if aweme_images:
            image_items = aweme_images
        urls: list[str] = []
        for item in image_items:
            srcset = str(item.get("srcset") or "")
            if srcset:
                candidates = [part.strip().split(" ")[0] for part in srcset.split(",")]
                if candidates:
                    urls.append(candidates[-1])
                    continue
            urls.append(str(item.get("src") or ""))
        urls = _dedupe_image_urls(urls)
        description = str(data.get("description") or "").strip()
        text_blocks = [str(value) for value in data.get("textBlocks", [])]
        if not text_blocks and data.get("text"):
            text_blocks = [str(data["text"])]
        title, post_text, author, published_at = _dom_text_metadata(
            description, text_blocks, str(data.get("author") or "")
        )
        music_title = str(data.get("musicTitle") or "").strip()
        return (
            VideoMetadata(
                video_id=work_id,
                original_url=url,
                canonical_url=f"https://www.douyin.com/note/{work_id}",
                title=title,
                author=author,
                published_at=published_at,
                description=post_text or None,
                post_text=post_text or None,
                music_metadata={"title": music_title} if music_title else None,
                source_kind=SourceKind.IMAGE_NOTE,
            ),
            urls,
        )

    @staticmethod
    def _raise_page_error(text: str, status: int | None, url: str | None = None) -> None:
        if _page_auth_blocked(text, status, url):
            raise BrowserAuthRequiredError("抖音会话需要登录或验证；请运行 douyin-wiki auth douyin")
        if "地区" in text and any(token in text for token in ("限制", "不可用", "无法查看")):
            raise RegionRestrictedError("该图文作品存在地区限制")
        if status == 404 or any(
            token in text for token in ("作品不存在", "作品已删除", "私密作品", "无法查看")
        ):
            raise VideoUnavailableError("图文作品已删除、私密或不可访问")
        raise BrowserAuthRequiredError(
            "无法取得图文图片，可能需要登录；请运行 douyin-wiki auth douyin"
        )

    async def _download_images(self, context: Any, urls: list[str], target_dir: Path) -> list[Path]:
        staging = target_dir.parent / f".{target_dir.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        staged: list[Path] = []
        seen_content: set[str] = set()
        try:
            for request_index, url in enumerate(urls, start=1):
                response = await context.request.get(
                    url,
                    headers={"Referer": "https://www.douyin.com/"},
                    timeout=60_000,
                )
                if not response.ok:
                    raise ExternalToolError(
                        f"第 {request_index} 张图片下载失败",
                        details={"status": response.status},
                    )
                content = await response.body()
                content_type = response.headers.get("content-type", "")
                if not content or (content_type and not content_type.startswith("image/")):
                    raise ExternalToolError(f"第 {request_index} 张图片返回了无效内容")
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen_content:
                    continue
                seen_content.add(digest)
                suffix = _suffix_for_image(content_type, content, url)
                path = staging / f"{len(staged) + 1:03d}{suffix}"
                path.write_bytes(content)
                staged.append(path)

            target_dir.mkdir(parents=True, exist_ok=True)
            final_paths: list[Path] = []
            for staged_path in staged:
                final = target_dir / staged_path.name
                staged_path.replace(final)
                final_paths.append(final)
            return final_paths
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def profile_fingerprint(profile_dir: Path) -> str:
    """Stable, non-sensitive identifier useful in doctor output and tests."""
    return hashlib.sha256(str(profile_dir).encode()).hexdigest()[:12]
