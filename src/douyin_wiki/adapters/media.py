from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
from contextlib import closing
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
from .share import extract_creator_sec_uid

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
    def __init__(self, settings: MediaSettings) -> None:
        self.settings = settings

    def _browser_spec(self) -> str:
        browser = self.settings.browser
        if self.settings.browser_profile:
            browser = f"{browser}:{self.settings.browser_profile}"
        return browser

    async def check_auth(self, *, video_url: str | None = None) -> AuthCheckResult:
        return await asyncio.to_thread(self._check_auth_sync, video_url)

    def _check_auth_sync(self, video_url: str | None) -> AuthCheckResult:
        browser_spec = self._browser_spec()
        state, cookie_count, database = _inspect_chromium_auth_cookies(
            _chromium_cookie_databases(self.settings)
        )
        source = f"{browser_spec} browser cookie store"
        if database is not None:
            source = str(database.parent)

        if video_url:
            probe_url = _validate_douyin_probe_url(video_url)
            result = _run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--cookies-from-browser",
                    browser_spec,
                    "--simulate",
                    "--no-warnings",
                    "--print",
                    "%(id)s",
                    probe_url,
                ],
                timeout=120,
            )
            if result.returncode == 0:
                return AuthCheckResult(
                    scope="video",
                    state="ready",
                    ok=True,
                    server_verified=True,
                    cookie_source=source,
                    message="yt-dlp 已使用本机浏览器会话成功访问该抖音作品",
                )
            message = (result.stderr or result.stdout or "").lower()
            if _looks_like_auth_failure(message):
                return AuthCheckResult(
                    scope="video",
                    state="needs_login",
                    ok=False,
                    server_verified=True,
                    cookie_source=source,
                    message="抖音拒绝了当前浏览器会话，需要重新登录",
                    action="douyin-wiki auth video",
                )
            local_message = {
                "available": "检测到未过期的抖音登录 Cookie",
                "expired": "检测到的抖音登录 Cookie 已过期",
                "missing": "没有检测到抖音登录 Cookie",
                "error": "无法读取浏览器 Cookie 数据库",
            }[state]
            return AuthCheckResult(
                scope="video",
                state=state,
                ok=state == "available",
                server_verified=False,
                cookie_source=source,
                message=f"{local_message}；指定作品探测失败，无法确认服务器状态",
                action=None if state == "available" else "douyin-wiki auth video",
            )

        if self.settings.browser.lower() not in CHROMIUM_DATA_DIRS:
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
        return AuthCheckResult(
            scope="video",
            state=state,
            ok=state == "available",
            server_verified=False,
            cookie_source=source,
            message=messages[state],
            action=None if state == "available" else "douyin-wiki auth video",
        )

    async def authenticate(self) -> AuthCheckResult:
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
            message="已打开视频下载使用的浏览器；请完成抖音登录后运行 auth status",
            action="douyin-wiki auth status --video-url <抖音视频URL>",
        )

    async def download(self, url: str, video_id: str, target_dir: Path) -> VideoMetadata:
        return await asyncio.to_thread(self._download_sync, url, video_id, target_dir)

    def _download_sync(self, url: str, video_id: str, target_dir: Path) -> VideoMetadata:
        target_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(target_dir / "original.%(ext)s")
        command = [
            "yt-dlp",
            "--no-playlist",
            "--cookies-from-browser",
            self._browser_spec(),
            "--write-info-json",
            "--write-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--merge-output-format",
            "mp4",
            "--output",
            output_template,
            url,
        ]
        result = _run(command, timeout=7200)
        _require_success(result, "yt-dlp")

        info_files = list(target_dir.glob("original.info.json"))
        info: dict[str, Any] = {}
        if info_files:
            info = json.loads(info_files[0].read_text(encoding="utf-8"))
        preferred_cover = download_preferred_cover(info, target_dir)
        candidates = [
            path
            for path in target_dir.glob("original.*")
            if path.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".part"}
        ]
        if not candidates:
            raise ExternalToolError("yt-dlp 未生成视频文件")
        media_path = max(candidates, key=lambda path: path.stat().st_size)
        thumbnails = [
            path
            for path in target_dir.glob("original.*")
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
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
        return VideoMetadata(
            video_id=str(info.get("id") or video_id),
            original_url=url,
            canonical_url=f"https://www.douyin.com/video/{video_id}",
            title=info.get("title") or info.get("description") or "抖音视频",
            # Douyin exposes the human-readable display name as ``channel`` while
            # ``uploader`` can be the numeric account id.
            author=info.get("channel") or info.get("uploader"),
            published_at=published_at,
            duration_seconds=float(duration) if duration is not None else None,
            description=info.get("description"),
            media_path=str(media_path),
            thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
            thumbnail_kind=thumbnail_kind,
            creator_sec_uid=extract_creator_sec_uid(str(info.get("channel_url") or "")),
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
