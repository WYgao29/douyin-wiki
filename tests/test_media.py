from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

from douyin_wiki.adapters.media import (
    VisionOCR,
    YtDlpDownloader,
    _chromium_cookie_databases,
    _inspect_chromium_auth_cookies,
    _preferred_cover_urls,
    _require_success,
    download_preferred_cover,
)
from douyin_wiki.config import MediaSettings
from douyin_wiki.errors import (
    CookieRequiredError,
    ExternalToolError,
    RegionRestrictedError,
    VideoUnavailableError,
)


def test_external_tool_success_is_noop() -> None:
    result = subprocess.CompletedProcess(["yt-dlp"], 0, stdout="ok", stderr="")
    _require_success(result, "yt-dlp")


@pytest.mark.asyncio
async def test_vision_ocr_missing_runtime_is_an_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "vision.swift"
    script.write_text("// test", encoding="utf-8")
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    monkeypatch.setattr("douyin_wiki.adapters.media.shutil.which", lambda _: None)
    with pytest.raises(ExternalToolError, match="Swift"):
        await VisionOCR(script).recognize([(0, frame)])


@pytest.mark.asyncio
async def test_vision_ocr_invalid_output_is_an_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "vision.swift"
    script.write_text("// test", encoding="utf-8")
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    monkeypatch.setattr("douyin_wiki.adapters.media.shutil.which", lambda _: "/usr/bin/swift")
    monkeypatch.setattr(
        "douyin_wiki.adapters.media._run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )
    with pytest.raises(ExternalToolError, match="无效 JSON"):
        await VisionOCR(script).recognize([(0, frame)])


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        ("Fresh cookies are needed", CookieRequiredError),
        ("ERROR: cookies have expired; please log in", CookieRequiredError),
        ("ERROR: private video", VideoUnavailableError),
        (
            "This video is not available in your region: restricted",
            RegionRestrictedError,
        ),
        ("unclassified downloader failure", ExternalToolError),
    ],
)
def test_external_tool_errors_have_stable_categories(
    message: str, error_type: type[ExternalToolError]
) -> None:
    result = subprocess.CompletedProcess(["yt-dlp"], 1, stdout="", stderr=message)
    with pytest.raises(error_type):
        _require_success(result, "yt-dlp")


def test_prefers_douyin_static_cover_over_origin_and_dynamic() -> None:
    info = {
        "thumbnails": [
            {"id": "dynamic_cover", "url": "https://example.test/dynamic"},
            {"id": "cover", "url": "https://example.test/static"},
            {"id": "origin_cover", "url": "https://example.test/origin"},
        ]
    }
    assert _preferred_cover_urls(info) == ["https://example.test/static"]


def test_downloads_preferred_cover(monkeypatch, tmp_path: Path) -> None:
    class FakeResponse:
        content = b"\xff\xd8\xfffake-jpeg"
        headers = {"content-type": "image/jpeg"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        @staticmethod
        def get(url: str):
            assert url == "https://example.test/static"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    path = download_preferred_cover(
        {"thumbnails": [{"id": "cover", "url": "https://example.test/static"}]},
        tmp_path,
    )
    assert path == tmp_path / "cover.jpg"
    assert path.read_bytes().startswith(b"\xff\xd8\xff")


def test_inspects_chromium_cookie_expiry_without_reading_values(tmp_path: Path) -> None:
    profile = tmp_path / "Profile 1"
    database = profile / "Network" / "Cookies"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)")
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "irrelevant", 99_999_999_999_999_999),
        )

    settings = MediaSettings(browser="chrome", browser_profile=str(profile))
    assert _chromium_cookie_databases(settings) == [database]
    state, count, source = _inspect_chromium_auth_cookies([database])
    assert (state, count, source) == ("available", 1, database)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE cookies SET expires_utc=1 WHERE name='sessionid'")
    state, count, _ = _inspect_chromium_auth_cookies([database])
    assert (state, count) == ("expired", 1)


@pytest.mark.asyncio
async def test_video_auth_probe_never_returns_cookie_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "Default"
    database = profile / "Network" / "Cookies"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)")
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )
    monkeypatch.setattr(
        "douyin_wiki.adapters.media._run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["yt-dlp"], 0, stdout="7672717300746907078\n", stderr=""
        ),
    )
    downloader = YtDlpDownloader(
        MediaSettings(browser="chrome", browser_profile=str(profile)), tmp_path / "browser"
    )
    async def no_cdn(_url: str):
        return None

    monkeypatch.setattr(downloader, "_capture_cdn_url", no_cdn)
    result = await downloader.check_auth(
        video_url="https://www.douyin.com/video/7672717300746907078"
    )
    payload = result.model_dump(mode="json")
    assert result.state == "ready" and result.server_verified is True
    assert "sessionid" not in str(payload).lower()


@pytest.mark.asyncio
async def test_video_auth_accepts_media_captured_by_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return "https://cdn.douyinvod.com/media-video.mp4", None, "测试作品"

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)
    result = await downloader.check_auth(video_url="https://www.douyin.com/video/123")
    assert result.state == "ready"
    assert result.server_verified is True
    assert result.cookie_source == str(tmp_path / "browser")


@pytest.mark.asyncio
async def test_cdn_download_merges_audio_after_both_streams_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return (
            "https://cdn.douyinvod.com/media-video.mp4",
            "https://cdn.douyinvod.com/media-audio.mp4",
            "测试作品",
        )

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)
    calls: list[str] = []

    def fake_run(command: list[str], *, timeout: float = 3600):
        if command[0] == "ffmpeg":
            assert (tmp_path / "original.mp4").exists()
            assert (tmp_path / "audio.mp4").exists()
            Path(command[-1]).write_bytes(b"merged video and audio")
            calls.append("merge")
        elif "media-video.mp4" in command[-1]:
            assert "--write-info-json" not in command
            (tmp_path / "original.mp4").write_bytes(b"video")
            calls.append("video")
        elif "media-audio.mp4" in command[-1]:
            (tmp_path / "audio.mp4").write_bytes(b"audio")
            calls.append("audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    metadata = await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert calls == ["video", "audio", "merge"]
    assert Path(metadata.media_path).read_bytes() == b"merged video and audio"
    assert metadata.video_id == "123" and metadata.title == "测试作品"
    assert not (tmp_path / "audio.mp4").exists()


@pytest.mark.asyncio
async def test_cdn_audio_failure_does_not_return_silent_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return (
            "https://cdn.douyinvod.com/media-video.mp4",
            "https://cdn.douyinvod.com/media-audio.mp4",
            "测试作品",
        )

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)

    def fake_run(command: list[str], *, timeout: float = 3600):
        if "media-video.mp4" in command[-1]:
            (tmp_path / "original.mp4").write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "audio download failed")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    with pytest.raises(ExternalToolError):
        await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)


@pytest.mark.asyncio
async def test_fresh_cookies_error_with_local_login_is_not_reported_as_login_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "Default"
    database = profile / "Network" / "Cookies"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)")
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )
    downloader = YtDlpDownloader(
        MediaSettings(browser="chrome", browser_profile=str(profile)), tmp_path / "browser"
    )

    async def no_cdn(_url: str):
        return None

    monkeypatch.setattr(downloader, "_capture_cdn_url", no_cdn)
    monkeypatch.setattr(
        "douyin_wiki.adapters.media._run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "ERROR: Fresh cookies (not necessarily logged in) are needed"
        ),
    )

    result = await downloader.check_auth(video_url="https://www.douyin.com/video/123")
    assert result.state == "unverified" and result.ok is False
    with pytest.raises(ExternalToolError, match="不能判定为需要重新登录") as exc_info:
        await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert not isinstance(exc_info.value, CookieRequiredError)
