from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..config import MediaSettings
from ..errors import (
    CookieRequiredError,
    ExternalToolError,
    RegionRestrictedError,
    VideoUnavailableError,
)
from ..models import AuthCheckResult, OCRObservation, TranscriptSegment, VideoMetadata
from .image_note import _acquire_file_lock, _release_file_lock
from .share import extract_creator_sec_uid

logger = logging.getLogger(__name__)

DOUYIN_AUTH_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_guard"}
CHROMIUM_DATA_DIRS = {
    "chrome": Path.home() / "Library/Application Support/Google/Chrome",
    "chrome-beta": Path.home() / "Library/Application Support/Google/Chrome Beta",
    "chrome-dev": Path.home() / "Library/Application Support/Google/Chrome Dev",
    "chrome-canary": Path.home() / "Library/Application Support/Google/Chrome Canary",
    "edge": Path.home() / "Library/Application Support/Microsoft Edge",
    "msedge": Path.home() / "Library/Application Support/Microsoft Edge",
    "msedge-beta": Path.home() / "Library/Application Support/Microsoft Edge Beta",
    "msedge-dev": Path.home() / "Library/Application Support/Microsoft Edge Dev",
    "msedge-canary": Path.home() / "Library/Application Support/Microsoft Edge Canary",
}
BROWSER_APPS = {
    "chrome": "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "chrome-dev": "Google Chrome Dev",
    "chrome-canary": "Google Chrome Canary",
    "edge": "Microsoft Edge",
    "msedge": "Microsoft Edge",
    "msedge-beta": "Microsoft Edge Beta",
    "msedge-dev": "Microsoft Edge Dev",
    "msedge-canary": "Microsoft Edge Canary",
    "safari": "Safari",
    "firefox": "Firefox",
}
AUTH_FAILURE_TERMS = (
    "fresh cookies",
    "cookies are needed",
    "cookies have expired",
    "cookie has expired",
    "login required",
    "not logged in",
    "please log in",
    "sign in",
    "authentication required",
)



@dataclass
class CapturedCdnMedia:
    """CDN media URLs plus optional page metadata for VideoMetadata."""

    video_url: str
    audio_url: str | None = None
    page_title: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


def _cdn_url_identity(url: str) -> str:
    parsed = urlsplit(url)
    return f"{(parsed.hostname or '').lower()}{parsed.path}"


def _dedupe_cdn_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        identity = _cdn_url_identity(url)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(url)
    return result


def _unique_candidate_rows(
    candidates: list[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    """Dedupe CDN candidates by host+path, preferring 200+MIME over 206 empty MIME."""
    best: dict[str, tuple[str, str, int]] = {}
    for url, content_type, status in candidates:
        identity = _cdn_url_identity(url)
        if not identity:
            continue
        current = best.get(identity)
        if current is None:
            best[identity] = (url, content_type, status)
            continue
        _, cur_type, cur_status = current
        # Complete responses beat partial ones; at equal status prefer richer MIME.
        if status == 200 and cur_status != 200:
            best[identity] = (url, content_type, status)
        elif status == cur_status and len(content_type) > len(cur_type):
            best[identity] = (url, content_type, status)
    return list(best.values())


def _score_cdn_video_url(url: str, content_type: str = "", status: int = 200) -> tuple[int, int]:
    lowered = url.lower()
    mime = content_type.partition(";")[0].strip().lower()
    score = 0
    if mime.startswith("video/"):
        score += 50
    if "media-video" in lowered:
        score += 40
    elif "video" in lowered:
        score += 10
    if ".mp4" in lowered or "mime_type=video_mp4" in lowered:
        score += 20
    if "media-audio" in lowered or "audiomp4" in lowered:
        score -= 100
    if status == 200:
        score += 5
    elif status == 206:
        score += 1
    return score, len(url)


def _score_cdn_audio_url(url: str, content_type: str = "", status: int = 200) -> tuple[int, int]:
    lowered = url.lower()
    mime = content_type.partition(";")[0].strip().lower()
    score = 0
    if mime.startswith("audio/"):
        score += 50
    if "media-audio" in lowered:
        score += 40
    elif "audiomp4" in lowered:
        score += 30
    elif "audio" in lowered:
        score += 10
    if ".mp4" in lowered or "mp4" in lowered:
        score += 10
    if "media-video" in lowered:
        score -= 100
    if status == 200:
        score += 5
    elif status == 206:
        score += 1
    return score, len(url)


def _pick_best_cdn_url(
    candidates: list[tuple[str, str, int]], *, audio: bool = False
) -> str | None:
    if not candidates:
        return None
    scorer = _score_cdn_audio_url if audio else _score_cdn_video_url
    best = max(candidates, key=lambda item: scorer(item[0], item[1], item[2]))
    if scorer(best[0], best[1], best[2])[0] < 0:
        return None
    return best[0]


def _find_video_aweme_detail(
    value: Any, *, expected_work_id: str | None = None
) -> dict[str, Any] | None:
    """Find a video aweme detail object in Douyin API envelopes."""

    def matches(candidate: dict[str, Any]) -> bool:
        candidate_id = str(candidate.get("aweme_id") or candidate.get("item_id") or "")
        if not candidate_id:
            return False
        if expected_work_id is not None and candidate_id != expected_work_id:
            return False
        return bool(candidate.get("video") or candidate.get("author") or candidate.get("desc"))

    if isinstance(value, dict):
        for key in ("aweme_detail", "aweme", "item", "aweme_info"):
            candidate = value.get(key)
            if isinstance(candidate, dict) and matches(candidate):
                return candidate
        if matches(value):
            return value
        for child in value.values():
            found = _find_video_aweme_detail(child, expected_work_id=expected_work_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_video_aweme_detail(child, expected_work_id=expected_work_id)
            if found:
                return found
    return None


def _cover_urls_from_aweme(detail: dict[str, Any]) -> list[str]:
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    urls: list[str] = []
    for key in ("origin_cover", "cover", "dynamic_cover"):
        node = video.get(key) if video else None
        if isinstance(node, dict):
            for item in node.get("url_list") or []:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
    return _dedupe_cdn_urls(urls)


def _info_from_aweme_detail(detail: dict[str, Any]) -> dict[str, Any]:
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    sec_uid = str(author.get("sec_uid") or "") or None
    description = str(detail.get("desc") or detail.get("description") or "").strip()
    duration_ms = video.get("duration")
    duration = None
    if duration_ms is not None:
        try:
            # Douyin aweme `video.duration` is milliseconds. Do not use a
            # `> 1000` seconds/ms guess — a 40-minute video reported in seconds
            # would be wrongly divided into ~2.4s.
            duration = float(duration_ms) / 1000.0
        except (TypeError, ValueError):
            duration = None
    cover_urls = _cover_urls_from_aweme(detail)
    return {
        "id": str(detail.get("aweme_id") or "") or None,
        "title": (description.splitlines()[0][:120] if description else None),
        "description": description or None,
        "channel": author.get("nickname") or author.get("unique_id"),
        "uploader": author.get("unique_id") or author.get("uid"),
        "uploader_id": author.get("uid"),
        "channel_url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else None,
        "uploader_url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else None,
        "timestamp": detail.get("create_time"),
        "duration": duration,
        "thumbnails": [{"id": "cover", "url": url} for url in cover_urls],
        "creator_sec_uid": sec_uid,
    }


def _info_from_dom_meta(
    *,
    page_title: str | None,
    description: str,
    author: str | None,
    author_href: str | None,
) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if page_title:
        info["title"] = page_title
    if description:
        info["description"] = description
        info.setdefault("title", description.splitlines()[0][:120])
    if author:
        info["channel"] = author
    if author_href:
        sec_uid = extract_creator_sec_uid(author_href)
        if sec_uid:
            info["channel_url"] = f"https://www.douyin.com/user/{sec_uid}"
            info["uploader_url"] = info["channel_url"]
            info["creator_sec_uid"] = sec_uid
    return info


def _merge_info(*parts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if value in (None, "", [], {}):
                continue
            if key == "thumbnails" and merged.get("thumbnails"):
                continue
            merged.setdefault(key, value)
    return merged


def _run(command: list[str], *, timeout: float = 3600) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(f"缺少外部工具：{command[0]}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ExternalToolError(f"外部工具超时：{command[0]}") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _require_success(result: subprocess.CompletedProcess[str], tool: str) -> None:
    if result.returncode == 0:
        return
    message = (result.stderr or result.stdout or "unknown error").strip()
    lowered = message.lower()
    if _looks_like_auth_failure(lowered):
        raise CookieRequiredError("抖音下载需要更新的浏览器 cookie", details={"stderr": message})
    if "region" in lowered and ("restrict" in lowered or "available" in lowered):
        raise RegionRestrictedError("该视频存在地区限制", details={"stderr": message})
    if any(term in lowered for term in ("not available", "deleted", "private video", "404")):
        raise VideoUnavailableError("视频已删除、私密或不可访问", details={"stderr": message})
    raise ExternalToolError(f"{tool} 执行失败", details={"stderr": message})


def _looks_like_auth_failure(message: str) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in AUTH_FAILURE_TERMS)


def _chromium_cookie_databases(settings: MediaSettings) -> list[Path]:
    """Locate candidate cookie databases without reading or copying cookie values."""
    browser = settings.browser.lower()
    root = CHROMIUM_DATA_DIRS.get(browser)
    if root is None:
        return []
    configured = settings.browser_profile
    if configured:
        profile = Path(configured).expanduser()
        profiles = [profile if profile.is_absolute() else root / configured]
    else:
        profiles = [root / "Default"]
        if root.exists():
            profiles.extend(path for path in sorted(root.glob("Profile *")) if path.is_dir())
    candidates: list[Path] = []
    for profile in profiles:
        for relative in (Path("Network/Cookies"), Path("Cookies")):
            path = profile / relative
            if path.is_file() and path not in candidates:
                candidates.append(path)
    return candidates


def _playwright_profile_cookie_databases(profile_dir: Path) -> list[Path]:
    """Locate Cookie DBs under the dedicated Playwright user-data-dir."""
    candidates: list[Path] = []
    for relative in (
        Path("Default/Network/Cookies"),
        Path("Default/Cookies"),
        Path("Network/Cookies"),
        Path("Cookies"),
    ):
        path = profile_dir / relative
        if path.is_file() and path not in candidates:
            candidates.append(path)
    return candidates


def _inspect_chromium_auth_cookies(databases: list[Path]) -> tuple[str, int, Path | None]:
    """Return local cookie state while never decrypting or returning cookie values."""
    if not databases:
        return "missing", 0, None
    chrome_epoch = datetime(1601, 1, 1, tzinfo=UTC)
    now_chrome_us = int((datetime.now(UTC) - chrome_epoch).total_seconds() * 1_000_000)
    found = 0
    expired = 0
    readable: Path | None = None
    for database in databases:
        try:
            uri = f"file:{quote(str(database))}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
                rows = connection.execute(
                    """SELECT name, expires_utc FROM cookies
                       WHERE host_key LIKE '%douyin.com'
                         AND name IN (?, ?, ?)""",
                    tuple(sorted(DOUYIN_AUTH_COOKIE_NAMES)),
                ).fetchall()
            readable = database
        except sqlite3.Error:
            continue
        for _, expires_utc in rows:
            found += 1
            expiry = int(expires_utc or 0)
            if expiry and expiry <= now_chrome_us:
                expired += 1
    if found and expired < found:
        return "available", found - expired, readable
    if found:
        return "expired", found, readable
    if readable is not None:
        return "missing", 0, readable
    return "error", 0, databases[0]


def _validate_douyin_probe_url(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "douyin.com"
        or hostname.endswith(".douyin.com")
        or hostname == "iesdouyin.com"
        or hostname.endswith(".iesdouyin.com")
    ):
        raise ValueError("video_url 必须是抖音 URL")
    return url


def _preferred_cover_urls(info: dict[str, Any]) -> list[str]:
    thumbnails = info.get("thumbnails", [])
    return [
        item["url"]
        for item in thumbnails
        if isinstance(item, dict) and item.get("id") == "cover" and item.get("url")
    ]


def download_preferred_cover(info: dict[str, Any], target_dir: Path) -> Path | None:
    """Download Douyin's separately configured static cover when available."""
    urls = _preferred_cover_urls(info)
    if not urls:
        return None
    headers = {
        "Referer": "https://www.douyin.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
        ),
    }
    with httpx.Client(follow_redirects=True, timeout=60, headers=headers) as client:
        for url in urls:
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            content = response.content
            content_type = response.headers.get("content-type", "").lower()
            if not content or (content_type and not content_type.startswith("image/")):
                continue
            suffix = ".png" if content.startswith(b"\x89PNG") else ".jpg"
            target = target_dir / f"cover{suffix}"
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(content)
            temporary.replace(target)
            return target
    return None


class YtDlpDownloader:
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

    def _browser_spec(self) -> str:
        """System Chrome/Edge spec — last-resort yt-dlp cookie source only."""
        browser = self.settings.browser
        if self.settings.browser_profile:
            browser = f"{browser}:{self.settings.browser_profile}"
        return browser

    def _preferred_yt_dlp_cookie_spec(self) -> tuple[str, str, str]:
        """Prefer dedicated Playwright profile cookies over system Chrome.

        Returns ``(cookies_from_browser_value, cookie_source, source_kind)`` where
        ``source_kind`` is ``playwright_profile`` or ``system_browser``.

        yt-dlp accepts an absolute Chromium profile path as
        ``chromium:/path/to/Default``; pointing at ``Default`` makes Local State
        resolve to the Playwright user-data-dir for decryption.
        """
        profile_dbs = _playwright_profile_cookie_databases(self.profile_dir)
        if profile_dbs:
            default = self.profile_dir / "Default"
            spec = f"chromium:{default if default.is_dir() else self.profile_dir}"
            return spec, str(self.profile_dir), "playwright_profile"
        chrome = self._browser_spec()
        return chrome, f"{chrome} browser cookie store", "system_browser"

    async def check_auth(self, *, video_url: str | None = None) -> AuthCheckResult:
        if video_url:
            probe_url = _validate_douyin_probe_url(video_url)
            if await self._capture_cdn_url(probe_url):
                return AuthCheckResult(
                    scope="video",
                    state="ready",
                    ok=True,
                    server_verified=True,
                    cookie_source=str(self.profile_dir),
                    message="专用浏览器已访问作品并获取视频媒体地址",
                )
        return await asyncio.to_thread(self._check_auth_sync, video_url)

    def _check_auth_sync(self, video_url: str | None) -> AuthCheckResult:
        cookie_spec, preferred_source, source_kind = self._preferred_yt_dlp_cookie_spec()
        profile_state, profile_count, _ = _inspect_chromium_auth_cookies(
            _playwright_profile_cookie_databases(self.profile_dir)
        )
        chrome_state, _chrome_count, chrome_db = _inspect_chromium_auth_cookies(
            _chromium_cookie_databases(self.settings)
        )
        if source_kind == "playwright_profile":
            state, cookie_count = profile_state, profile_count
            source = preferred_source
        else:
            state, cookie_count = chrome_state, _chrome_count
            source = preferred_source
            if chrome_db is not None:
                source = str(chrome_db.parent)

        dual_note = ""
        if source_kind == "playwright_profile" and chrome_state == "available":
            dual_note = (
                "（主路径：专用 Playwright Profile；系统 Chrome 另有登录 Cookie，"
                "仅作 yt-dlp 最后回退）"
            )
        elif source_kind == "system_browser" and profile_state in {"missing", "expired"}:
            dual_note = (
                "（专用 Playwright Profile 尚无可用会话；"
                "当前回退系统浏览器 Cookie。"
                "主下载走 CDN 时请运行 douyin-wiki auth douyin "
                "写入专用 Profile）"
            )

        if video_url:
            probe_url = _validate_douyin_probe_url(video_url)
            result = _run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--cookies-from-browser",
                    cookie_spec,
                    "--simulate",
                    "--no-warnings",
                    "--print",
                    "%(id)s",
                    probe_url,
                ],
                timeout=120,
            )
            if result.returncode == 0:
                where = (
                    "专用 Playwright Profile"
                    if source_kind == "playwright_profile"
                    else "本机浏览器会话"
                )
                return AuthCheckResult(
                    scope="video",
                    state="ready",
                    ok=True,
                    server_verified=True,
                    cookie_source=source,
                    message=f"yt-dlp 已使用{where}成功访问该抖音作品{dual_note}",
                )
            message = (result.stderr or result.stdout or "").lower()
            if _looks_like_auth_failure(message) and state != "available":
                return AuthCheckResult(
                    scope="video",
                    state="needs_login",
                    ok=False,
                    server_verified=True,
                    cookie_source=source,
                    message=f"视频探测失败，且未检测到有效登录 Cookie{dual_note}",
                    action=(
                        "douyin-wiki auth douyin"
                        if source_kind == "playwright_profile"
                        else "douyin-wiki auth video"
                    ),
                )
            local_message = {
                "available": "检测到未过期的抖音登录 Cookie",
                "expired": "检测到的抖音登录 Cookie 已过期",
                "missing": "没有检测到抖音登录 Cookie",
                "error": "无法读取浏览器 Cookie 数据库",
            }[state]
            return AuthCheckResult(
                scope="video",
                state="unverified" if state == "available" else state,
                ok=False,
                server_verified=False,
                cookie_source=source,
                message=(
                    f"{local_message}；yt-dlp 未能提取该作品，"
                    f"无法判定是否需要重新登录{dual_note}"
                ),
                action=None
                if state == "available"
                else (
                    "douyin-wiki auth douyin"
                    if source_kind == "playwright_profile"
                    else "douyin-wiki auth video"
                ),
            )

        browser_key = self.settings.browser.lower()
        if source_kind == "system_browser" and browser_key not in CHROMIUM_DATA_DIRS:
            return AuthCheckResult(
                scope="video",
                state="unavailable",
                ok=False,
                server_verified=False,
                cookie_source=source,
                message="当前浏览器不支持本地无泄露 Cookie 健康检查",
                action="请使用 auth status --video-url 指定抖音作品进行 yt-dlp 探测",
            )
        messages = {
            "available": f"检测到 {cookie_count} 个未过期的抖音登录 Cookie；尚未联网验证",
            "expired": "检测到的抖音登录 Cookie 已过期",
            "missing": "没有检测到抖音登录 Cookie",
            "error": "浏览器 Cookie 数据库存在，但当前进程无法读取",
        }
        action = None
        if state != "available":
            # Prefer seeding the dedicated Playwright profile (CDN primary path).
            action = (
                "douyin-wiki auth douyin"
                if profile_state != "available"
                else "douyin-wiki auth video"
            )
        return AuthCheckResult(
            scope="video",
            state=state,
            ok=state == "available",
            server_verified=False,
            cookie_source=source,
            message=f"{messages[state]}{dual_note}",
            action=action,
        )

    async def authenticate(self) -> AuthCheckResult:
        """Open system browser to seed last-resort yt-dlp cookies.

        Primary video download uses the dedicated Playwright profile (CDN path).
        Prefer ``douyin-wiki auth douyin`` to seed that profile; this command only
        refreshes the configured system browser Cookie DB used as yt-dlp fallback.
        """
        browser = self.settings.browser.lower()
        app_name = BROWSER_APPS.get(browser)
        if app_name is None:
            raise ExternalToolError(f"不支持自动打开浏览器：{self.settings.browser}")
        result = await asyncio.to_thread(
            _run,
            ["open", "-a", app_name, "https://www.douyin.com/"],
            timeout=30,
        )
        _require_success(result, "open")
        return AuthCheckResult(
            scope="video",
            state="unverified",
            ok=False,
            server_verified=False,
            cookie_source=f"{self._browser_spec()} browser cookie store",
            message=(
                "已打开系统浏览器（仅作 yt-dlp 最后回退的 Cookie 来源）。"
                "主下载/CDN 路径请改用 douyin-wiki auth douyin "
                "登录专用 Playwright Profile；完成后运行 auth status"
            ),
            action="douyin-wiki auth douyin",
        )

    async def _capture_cdn_url(self, url: str) -> CapturedCdnMedia | None:
        """Capture video/audio CDN URLs and page metadata from the work page."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright 未安装，跳过 CDN 拦截下载")
            return None

        profile_dir = self.profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        expected_work_id = None
        for part in urlsplit(url).path.strip("/").split("/"):
            if part.isdigit() and len(part) >= 10:
                expected_work_id = part
                break

        video_candidates: list[tuple[str, str, int]] = []
        audio_candidates: list[tuple[str, str, int]] = []
        payloads: list[Any] = []
        payload_tasks: list[asyncio.Task[Any]] = []

        try:
            async with self._locked_profile(), async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                )
                try:
                    page = (
                        context.pages[0] if context.pages else await context.new_page()
                    )

                    def on_response(response: Any) -> None:
                        resp_url = response.url
                        host = (urlsplit(resp_url).hostname or "").lower()
                        content_type = response.headers.get("content-type", "")
                        status = int(response.status or 0)
                        lowered_type = content_type.lower()
                        if host == "douyinvod.com" or host.endswith(".douyinvod.com"):
                            if status not in {200, 206}:
                                return
                            lowered = resp_url.lower()
                            is_audio = lowered_type.startswith("audio/") or any(
                                tag in lowered
                                for tag in ("media-audio", "audiomp4", "/audio")
                            )
                            is_video = (
                                lowered_type.startswith("video/")
                                or "media-video" in lowered
                                or ("video" in lowered and not is_audio)
                            )
                            if is_audio:
                                audio_candidates.append((resp_url, content_type, status))
                            elif is_video:
                                video_candidates.append((resp_url, content_type, status))
                            return
                        if status != 200 or "json" not in lowered_type:
                            return
                        if not any(
                            token in resp_url.lower()
                            for token in ("aweme", "detail", "video", "item")
                        ):
                            return

                        async def _store() -> None:
                            try:
                                payloads.append(
                                    await asyncio.wait_for(response.json(), timeout=5.0)
                                )
                            except Exception as exc:
                                logger.debug(
                                    "忽略无法解析的作品接口响应：%s",
                                    type(exc).__name__,
                                )

                        try:
                            payload_tasks.append(
                                asyncio.get_running_loop().create_task(_store())
                            )
                        except RuntimeError:
                            return

                    page.on("response", on_response)
                    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

                    deadline = asyncio.get_running_loop().time() + 15.0
                    video_url: str | None = None
                    audio_url: str | None = None
                    while asyncio.get_running_loop().time() < deadline:
                        video_url = _pick_best_cdn_url(
                            _unique_candidate_rows(video_candidates)
                        )
                        audio_url = _pick_best_cdn_url(
                            _unique_candidate_rows(audio_candidates), audio=True
                        )
                        # Once video is present, wait a short extra window for audio.
                        if video_url and (
                            audio_url
                            or asyncio.get_running_loop().time() + 3.0 >= deadline
                        ):
                            break
                        try:
                            await page.wait_for_event("response", timeout=500)
                        except Exception as exc:
                            # Timeouts are expected while polling CDN; do not treat
                            # bare Playwright Error as silent noise.
                            if type(exc).__name__ != "TimeoutError":
                                logger.debug(
                                    "等待 CDN 响应时出现异常：%s",
                                    type(exc).__name__,
                                )
                            await page.wait_for_timeout(250)

                    # Short grace so late aweme JSON after CDN ready can still enqueue.
                    if payload_tasks or video_url:
                        try:
                            await page.wait_for_timeout(400)
                        except Exception:
                            await asyncio.sleep(0.4)

                    if payload_tasks:
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*payload_tasks, return_exceptions=True),
                                timeout=5.0,
                            )
                        except asyncio.TimeoutError:
                            for task in payload_tasks:
                                if not task.done():
                                    task.cancel()
                            logger.debug(
                                "作品接口 JSON 解析超时，继续使用已捕获的元数据"
                            )

                    page_title = (
                        (await page.title()).strip().removesuffix(" - 抖音").strip()
                    )
                    dom_meta = await page.evaluate(
                        """
                            () => {
                              const description =
                                document.querySelector('meta[name="description"]')
                                  ?.content || '';
                              const authorLink =
                                document.querySelector('a[href*="/user/"]');
                              return {
                                description,
                                author: authorLink?.innerText?.trim() || '',
                                authorHref: authorLink?.href || ''
                              };
                            }
                            """
                    )

                    info_parts: list[dict[str, Any]] = []
                    for payload in payloads:
                        detail = _find_video_aweme_detail(
                            payload, expected_work_id=expected_work_id
                        )
                        if detail:
                            info_parts.append(_info_from_aweme_detail(detail))
                            break
                    info_parts.append(
                        _info_from_dom_meta(
                            page_title=page_title or None,
                            description=str((dom_meta or {}).get("description") or ""),
                            author=str((dom_meta or {}).get("author") or "") or None,
                            author_href=str((dom_meta or {}).get("authorHref") or "")
                            or None,
                        )
                    )
                    info = _merge_info(*info_parts)

                    video_url = video_url or _pick_best_cdn_url(
                        _unique_candidate_rows(video_candidates)
                    )
                    audio_url = audio_url or _pick_best_cdn_url(
                        _unique_candidate_rows(audio_candidates), audio=True
                    )
                    if not video_url:
                        return None
                    return CapturedCdnMedia(
                        video_url=video_url,
                        audio_url=audio_url,
                        page_title=page_title or info.get("title"),
                        info=info,
                    )
                finally:
                    await context.close()
        except Exception:
            logger.exception("Playwright CDN 拦截失败：%s", url)
            return None

    async def _fetch_info_json(self, url: str, target_dir: Path) -> dict[str, Any]:
        """Secondary metadata-only yt-dlp pass for CDN downloads that lack info.json."""
        target_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(target_dir / "original.%(ext)s")
        cookie_spec, _, _ = self._preferred_yt_dlp_cookie_spec()
        command = [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--cookies-from-browser",
            cookie_spec,
            "--no-warnings",
            "--output",
            output_template,
            url,
        ]
        result = await asyncio.to_thread(_run, command, timeout=180)
        if result.returncode != 0:
            logger.warning(
                "CDN 路径元数据补充失败：%s",
                (result.stderr or result.stdout or "").strip()[:500],
            )
            return {}
        info_files = list(target_dir.glob("original.info.json"))
        if not info_files:
            return {}
        try:
            return json.loads(info_files[0].read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("CDN 路径元数据 JSON 无法解析")
            return {}

    async def download(self, url: str, video_id: str, target_dir: Path) -> VideoMetadata:
        target_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Use Playwright to capture CDN video + audio URLs from the video page.
        cdn_result = await self._capture_cdn_url(url)

        video_url: str
        audio_url: str | None
        page_title: str | None = None
        page_info: dict[str, Any] = {}
        if cdn_result is None:
            video_url = url
            audio_url = None
        else:
            video_url = cdn_result.video_url
            audio_url = cdn_result.audio_url
            page_title = cdn_result.page_title
            page_info = dict(cdn_result.info)

        # Download the video before merging a separately served audio stream.
        output_template = str(target_dir / "original.%(ext)s")
        command = [
            "yt-dlp",
            "--no-playlist",
            "--output",
            output_template,
        ]
        if cdn_result is not None:
            command += ["--referer", "https://www.douyin.com/"]
            command.append(video_url)
        else:
            # Fallback: prefer dedicated Playwright profile cookies; system Chrome last.
            cookie_spec, _, _ = self._preferred_yt_dlp_cookie_spec()
            command[2:2] = [
                "--cookies-from-browser",
                cookie_spec,
                "--write-info-json",
                "--write-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--merge-output-format",
                "mp4",
            ]
            command.append(url)
        result = await asyncio.to_thread(_run, command, timeout=7200)
        try:
            _require_success(result, "yt-dlp")
        except CookieRequiredError as exc:
            _, preferred_source, source_kind = self._preferred_yt_dlp_cookie_spec()
            if source_kind == "playwright_profile":
                local_state, _, _ = _inspect_chromium_auth_cookies(
                    _playwright_profile_cookie_databases(self.profile_dir)
                )
            else:
                local_state, _, _ = _inspect_chromium_auth_cookies(
                    _chromium_cookie_databases(self.settings)
                )
            if cdn_result is None and local_state == "available" and "fresh cookies" in (
                result.stderr or ""
            ).lower():
                raise ExternalToolError(
                    "yt-dlp 未能提取抖音作品；"
                    f"{preferred_source} 已有未过期登录 Cookie，"
                    "不能判定为需要重新登录",
                    details=exc.details,
                ) from exc
            raise

        def media_files(prefix: str) -> list[Path]:
            return [
                path
                for path in target_dir.glob(f"{prefix}.*")
                if path.is_file()
                and path.suffix.lower()
                not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".part"}
            ]

        video_files = media_files("original")
        if not video_files:
            raise ExternalToolError("yt-dlp 未生成视频文件")
        if audio_url:
            audio_command = [
                "yt-dlp",
                "--no-playlist",
                "--output",
                str(target_dir / "audio.%(ext)s"),
                "--referer",
                "https://www.douyin.com/",
                audio_url,
            ]
            audio_result = await asyncio.to_thread(_run, audio_command, timeout=7200)
            _require_success(audio_result, "yt-dlp 音频下载")
            audio_files = media_files("audio")
            if not audio_files:
                raise ExternalToolError("yt-dlp 未生成音频文件")
            # Same selection rule as final media_path: largest candidate wins.
            video_path = max(video_files, key=lambda path: path.stat().st_size)
            audio_path = max(audio_files, key=lambda path: path.stat().st_size)
            merged = target_dir / "merged.mp4"
            merge_result = await asyncio.to_thread(
                _run,
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-i",
                    str(audio_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c",
                    "copy",
                    "-shortest",
                    str(merged),
                ],
                timeout=600,
            )
            _require_success(merge_result, "ffmpeg 合并音视频")
            if not merged.is_file() or merged.stat().st_size == 0:
                raise ExternalToolError("ffmpeg 未生成合并后的视频")
            final_video = target_dir / "original.mp4"
            for path in video_files:
                path.unlink()
            merged.replace(final_video)
            # Keep independent audio.mp4 for ASR (service looks for that name).
            preferred_audio = target_dir / "audio.mp4"
            if audio_path.resolve() != preferred_audio.resolve():
                preferred_audio.unlink(missing_ok=True)
                audio_path.replace(preferred_audio)

        info_files = list(target_dir.glob("original.info.json"))
        info: dict[str, Any] = {}
        if info_files:
            info = json.loads(info_files[0].read_text(encoding="utf-8"))
        elif cdn_result is not None:
            info = dict(page_info)
            # Fill gaps so creator adoption and thumbnails still work.
            has_creator = bool(
                info.get("creator_sec_uid")
                or extract_creator_sec_uid(
                    str(info.get("channel_url") or info.get("uploader_url") or "")
                )
            )
            has_identity = bool(info.get("title") or info.get("description"))
            has_author = bool(info.get("channel") or info.get("uploader"))
            if (
                not has_creator
                or not info.get("thumbnails")
                or not has_identity
                or not has_author
            ):
                secondary = await self._fetch_info_json(url, target_dir)
                info = _merge_info(info, secondary)
        preferred_cover = download_preferred_cover(info, target_dir)
        candidates = media_files("original")
        if not candidates:
            raise ExternalToolError("yt-dlp 未生成视频文件")
        media_path = max(candidates, key=lambda path: path.stat().st_size)
        thumbnails = [
            path
            for path in target_dir.glob("original.*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        # Also accept cover.* written by download_preferred_cover.
        thumbnails.extend(
            path
            for path in target_dir.glob("cover.*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        fallback_thumbnail = max(thumbnails, key=lambda path: path.stat().st_size, default=None)
        thumbnail_path = preferred_cover or fallback_thumbnail
        timestamp = info.get("timestamp") or info.get("release_timestamp")
        published_at = datetime.fromtimestamp(timestamp, UTC) if timestamp else None
        duration = info.get("duration")
        thumbnail_kind = None
        if preferred_cover:
            thumbnail_kind = "douyin_cover"
        elif fallback_thumbnail:
            thumbnail_kind = "origin_cover"
        channel_url = str(info.get("channel_url") or info.get("uploader_url") or "")
        creator_sec_uid = info.get("creator_sec_uid") or extract_creator_sec_uid(channel_url)
        return VideoMetadata(
            video_id=video_id if cdn_result is not None else str(info.get("id") or video_id),
            original_url=url,
            canonical_url=f"https://www.douyin.com/video/{video_id}",
            title=page_title or info.get("title") or info.get("description") or "抖音视频",
            # Douyin exposes the human-readable display name as ``channel`` while
            # ``uploader`` can be the numeric account id.
            author=info.get("channel") or info.get("uploader"),
            published_at=published_at,
            duration_seconds=float(duration) if duration is not None else None,
            description=info.get("description"),
            media_path=str(media_path),
            thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
            thumbnail_kind=thumbnail_kind,
            creator_sec_uid=str(creator_sec_uid) if creator_sec_uid else None,
            creator_uid=str(info.get("uploader_id") or "") or None,
            creator_unique_id=(
                str(info.get("uploader") or "")
                if str(info.get("uploader") or "").isdigit()
                else None
            ),
            creator_url=info.get("channel_url") or info.get("uploader_url"),
        )



class FFmpegMediaProcessor:
    async def probe_duration(self, video_path: Path) -> float:
        result = await asyncio.to_thread(
            _run,
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
        )
        _require_success(result, "ffprobe")
        try:
            return float(result.stdout.strip())
        except ValueError as exc:
            raise ExternalToolError("ffprobe 无法读取视频时长") from exc

    async def extract_audio(self, video_path: Path, audio_path: Path) -> Path:
        result = await asyncio.to_thread(
            _run,
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ],
        )
        _require_success(result, "ffmpeg")
        return audio_path

    async def extract_frames(
        self,
        video_path: Path,
        frames_dir: Path,
        *,
        duration_seconds: float,
        interval_seconds: int,
        scene_threshold: float,
        max_frames: int,
    ) -> list[tuple[int, Path]]:
        return await asyncio.to_thread(
            self._extract_frames_sync,
            video_path,
            frames_dir,
            duration_seconds,
            interval_seconds,
            scene_threshold,
            max_frames,
        )

    def _extract_frames_sync(
        self,
        video_path: Path,
        frames_dir: Path,
        duration_seconds: float,
        interval_seconds: int,
        scene_threshold: float,
        max_frames: int,
    ) -> list[tuple[int, Path]]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        scene_result = _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(video_path),
                "-vf",
                f"select='gt(scene,{scene_threshold})',showinfo",
                "-f",
                "null",
                "-",
            ],
            timeout=7200,
        )
        scene_times = [
            float(value) for value in re.findall(r"pts_time:([0-9.]+)", scene_result.stderr)
        ]
        periodic = [
            float(value) for value in range(0, max(1, int(duration_seconds) + 1), interval_seconds)
        ]
        times = sorted(
            {round(value, 2) for value in [*periodic, *scene_times] if value <= duration_seconds}
        )
        if len(times) > max_frames:
            step = len(times) / max_frames
            times = [times[int(index * step)] for index in range(max_frames)]

        frames: list[tuple[int, Path]] = []
        for timestamp in times:
            timestamp_ms = int(timestamp * 1000)
            output = frames_dir / f"frame-{timestamp_ms:010d}.jpg"
            result = _run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1280,iw)':-2",
                    "-q:v",
                    "3",
                    str(output),
                ]
            )
            if result.returncode == 0 and output.exists():
                frames.append((timestamp_ms, output))
        return frames


class WhisperTranscriber:
    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings

    async def transcribe(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        return await asyncio.to_thread(self._transcribe_sync, audio_path, output_dir)

    def _transcribe_sync(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        provider = self.settings.whisper_provider
        if provider in {"auto", "mlx"}:
            try:
                return self._transcribe_mlx(audio_path)
            except (ImportError, ModuleNotFoundError) as exc:
                if provider == "mlx":
                    raise ExternalToolError("未安装 mlx-whisper；运行 uv sync --extra mlx") from exc
        return self._transcribe_cli(audio_path, output_dir)

    def _transcribe_mlx(self, audio_path: Path) -> list[TranscriptSegment]:
        import mlx_whisper  # type: ignore[import-not-found]

        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.settings.whisper_model,
            language="zh",
            word_timestamps=True,
        )
        return _segments_from_whisper(result.get("segments", []))

    def _transcribe_cli(self, audio_path: Path, output_dir: Path) -> list[TranscriptSegment]:
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "whisper",
            str(audio_path),
            "--model",
            self.settings.whisper_cli_model,
            "--language",
            "zh",
            "--output_dir",
            str(output_dir),
            "--output_format",
            "json",
            "--word_timestamps",
            "True",
        ]
        result = _run(command, timeout=14400)
        _require_success(result, "whisper")
        output = output_dir / f"{audio_path.stem}.json"
        if not output.exists():
            raise ExternalToolError("Whisper 未生成 JSON 逐字稿")
        payload = json.loads(output.read_text(encoding="utf-8"))
        return _segments_from_whisper(payload.get("segments", []))


def _segments_from_whisper(segments: list[dict[str, Any]]) -> list[TranscriptSegment]:
    converted: list[TranscriptSegment] = []
    for index, segment in enumerate(segments):
        avg_logprob = segment.get("avg_logprob")
        confidence = None
        if avg_logprob is not None:
            confidence = max(0.0, min(1.0, 1.0 + float(avg_logprob) / 3.0))
        converted.append(
            TranscriptSegment(
                id=int(segment.get("id", index)),
                start_ms=max(0, int(float(segment.get("start", 0)) * 1000)),
                end_ms=max(0, int(float(segment.get("end", 0)) * 1000)),
                text=str(segment.get("text", "")).strip(),
                avg_logprob=avg_logprob,
                no_speech_prob=segment.get("no_speech_prob"),
                confidence=confidence,
            )
        )
    return converted


class VisionOCR:
    def __init__(self, script_path: Path) -> None:
        self.script_path = script_path

    async def recognize(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        if not frames:
            return []
        if not shutil.which("swift"):
            raise ExternalToolError("未找到 Swift，无法运行 macOS Vision OCR")
        if not self.script_path.exists():
            raise ExternalToolError(
                "Vision OCR 脚本不存在", details={"path": str(self.script_path)}
            )
        return await asyncio.to_thread(self._recognize_sync, frames)

    def _recognize_sync(self, frames: list[tuple[int, Path]]) -> list[OCRObservation]:
        arguments = [
            value
            for source_index, path in frames
            for value in (str(source_index), str(path))
        ]
        result = _run(["swift", str(self.script_path), *arguments], timeout=3600)
        if result.returncode != 0:
            raise ExternalToolError(
                "macOS Vision OCR 执行失败",
                details={"stderr": result.stderr[-2000:]},
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalToolError(
                "macOS Vision OCR 返回了无效 JSON",
                details={"cause": str(exc)},
            ) from exc
        if not isinstance(payload, list):
            raise ExternalToolError("macOS Vision OCR 返回格式错误")
        failures = [item for item in payload if item.get("error")]
        if failures:
            raise ExternalToolError(
                "macOS Vision OCR 无法解码部分图片",
                details={"failures": failures[:20]},
            )
        observations: list[OCRObservation] = []
        for item in payload:
            text = str(item.get("text", "")).strip()
            path = str(item.get("path", ""))
            source_index = int(item.get("sourceIndex", 0))
            if text:
                observations.append(
                    OCRObservation(
                        timestamp_ms=source_index,
                        source_index=source_index,
                        text=text,
                        confidence=item.get("confidence"),
                        image_path=path,
                    )
                )
        return observations
