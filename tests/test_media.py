from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

from douyin_wiki.adapters.media import (
    CapturedCdnMedia,
    VisionOCR,
    YtDlpDownloader,
    _chromium_cookie_databases,
    _collect_aweme_media_anchors,
    _info_from_aweme_detail,
    _inspect_chromium_auth_cookies,
    _pick_best_cdn_url,
    _playwright_profile_cookie_databases,
    _preferred_cover_urls,
    _require_success,
    _unique_candidate_rows,
    download_preferred_cover,
    preferred_yt_dlp_cookie_spec,
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
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            page_title="测试作品",
        )

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
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            audio_url="https://cdn.douyinvod.com/media-audio.mp4",
            page_title="测试作品",
            info={
                "channel": "测试作者",
                "description": "作品简介",
                "timestamp": 1_700_000_000,
                "creator_sec_uid": "MS4wLjABAAAAtest",
                "channel_url": "https://www.douyin.com/user/MS4wLjABAAAAtest",
                "thumbnails": [{"id": "cover", "url": "https://example.invalid/cover.jpg"}],
            },
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
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    metadata = await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert calls == ["video", "audio", "merge"]
    assert Path(metadata.media_path).read_bytes() == b"merged video and audio"
    assert metadata.video_id == "123" and metadata.title == "测试作品"
    assert metadata.author == "测试作者"
    assert metadata.creator_sec_uid == "MS4wLjABAAAAtest"
    assert metadata.description == "作品简介"
    # Keep independent audio.mp4 for ASR after merge.
    assert (tmp_path / "audio.mp4").exists()


@pytest.mark.asyncio
async def test_cdn_audio_failure_does_not_return_silent_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            audio_url="https://cdn.douyinvod.com/media-audio.mp4",
            page_title="测试作品",
            info={
                "creator_sec_uid": "MS4wLjABAAAAtest",
                "channel_url": "https://www.douyin.com/user/MS4wLjABAAAAtest",
                "thumbnails": [{"id": "cover", "url": "https://example.invalid/cover.jpg"}],
            },
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


def test_cdn_url_selection_prefers_full_media_mime() -> None:
    rows = _unique_candidate_rows(
        [
            ("https://v3.douyinvod.com/path/media-video/seg", "", 206),
            ("https://v3.douyinvod.com/path/media-video/seg", "video/mp4", 200),
            ("https://v3.douyinvod.com/path/media-audio/a", "audio/mp4", 200),
            ("https://v3.douyinvod.com/path/media-video/seg?x=1", "video/mp4", 206),
        ]
    )
    video_rows = [row for row in rows if "media-video" in row[0]]
    assert len(video_rows) == 1
    assert video_rows[0][1] == "video/mp4"
    assert video_rows[0][2] == 200
    # Reverse observation order: 200+MIME must still beat a later 206 empty MIME.
    rows_reversed = _unique_candidate_rows(
        [
            ("https://v3.douyinvod.com/path/media-video/seg", "video/mp4", 200),
            ("https://v3.douyinvod.com/path/media-video/seg", "", 206),
        ]
    )
    assert rows_reversed == [
        ("https://v3.douyinvod.com/path/media-video/seg", "video/mp4", 200)
    ]
    video = _pick_best_cdn_url(rows)
    audio = _pick_best_cdn_url(rows, audio=True)
    assert video is not None and "media-video" in video and "audio" not in video
    assert audio is not None and "media-audio" in audio


def test_cdn_url_selection_prefers_work_bound_over_higher_mime_score() -> None:
    """Ad/recommend CDN with richer MIME cues must lose to play_addr-bound URL."""
    work_id = "7672717300746907078"
    target = "https://v3.douyinvod.com/target/media-video/seg"
    unrelated = "https://v9.douyinvod.com/ad/media-video/full.mp4"
    rows = _unique_candidate_rows(
        [
            (target, "video/mp4", 200),
            (unrelated, "video/mp4", 200),
        ]
    )
    # Without binding, the ad URL wins on .mp4 path bonus.
    assert _pick_best_cdn_url(rows) == unrelated

    play_addr = f"{target}?video_id=v0200targeturi0001"
    bound = _pick_best_cdn_url(
        rows,
        expected_work_id=work_id,
        anchor_urls=[play_addr],
        uri_markers={"v0200targeturi0001"},
    )
    assert bound == target


def test_cdn_url_selection_rejects_unrelated_when_play_addr_known() -> None:
    work_id = "7672717300746907078"
    unrelated = "https://v9.douyinvod.com/ad/media-video/full.mp4"
    rows = _unique_candidate_rows([(unrelated, "video/mp4", 200)])
    assert (
        _pick_best_cdn_url(
            rows,
            expected_work_id=work_id,
            anchor_urls=[
                "https://v3.douyinvod.com/target/media-video/seg?video_id=v0200targeturi0001"
            ],
            uri_markers={"v0200targeturi0001"},
        )
        is None
    )


def test_cdn_url_selection_rejects_work_id_only_when_play_addr_known() -> None:
    """Ad CDN that only embeds work_id must not count as matched once play_addr known."""
    work_id = "7672717300746907078"
    ad_with_work_id = f"https://v9.douyinvod.com/ad/{work_id}/media-video/full.mp4"
    rows = _unique_candidate_rows([(ad_with_work_id, "video/mp4", 200)])
    assert (
        _pick_best_cdn_url(
            rows,
            expected_work_id=work_id,
            anchor_urls=[
                "https://v3.douyinvod.com/target/media-video/seg?video_id=v0200targeturi0001"
            ],
            uri_markers={"v0200targeturi0001"},
        )
        is None
    )


def test_cdn_url_selection_boosts_work_id_marker_in_url() -> None:
    work_id = "7672717300746907078"
    bound = f"https://v3.douyinvod.com/path/{work_id}/media-video/seg"
    unrelated = "https://v9.douyinvod.com/ad/media-video/full.mp4"
    rows = _unique_candidate_rows(
        [
            (bound, "video/mp4", 200),
            (unrelated, "video/mp4", 200),
        ]
    )
    assert _pick_best_cdn_url(rows) == unrelated
    assert _pick_best_cdn_url(rows, expected_work_id=work_id) == bound


def test_collect_aweme_media_anchors_reads_play_addr_and_uri() -> None:
    detail = {
        "aweme_id": "7672717300746907078",
        "video": {
            "play_addr": {
                "uri": "v0200targeturi0001",
                "url_list": [
                    "https://v3.douyinvod.com/target/media-video/seg?video_id=v0200targeturi0001",
                ],
            },
            "bit_rate": [
                {
                    "play_addr": {
                        "uri": "v0200targeturi0001",
                        "url_list": [
                            "https://v5.douyinvod.com/target/media-video/hi.mp4",
                        ],
                    }
                }
            ],
        },
        "music": {
            "play_url": {
                "uri": "audio-uri-1",
                "url_list": ["https://v3.douyinvod.com/target/media-audio/a"],
            }
        },
    }
    video_urls, video_markers = _collect_aweme_media_anchors(detail, audio=False)
    audio_urls, audio_markers = _collect_aweme_media_anchors(detail, audio=True)
    assert any("target/media-video/seg" in url for url in video_urls)
    assert "v0200targeturi0001" in video_markers
    assert audio_urls and "media-audio" in audio_urls[0]
    assert "audio-uri-1" in audio_markers


@pytest.mark.asyncio
async def test_capture_rebinds_cdn_to_aweme_play_addr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Higher-scoring ad CDN must lose once aweme play_addr anchors are known."""
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")
    work_id = "7672717300746907078"
    target = "https://v3.douyinvod.com/target/media-video/seg"
    ad = "https://v9.douyinvod.com/ad/media-video/full.mp4"

    class FakeResponse:
        def __init__(self, url: str, payload: dict, *, content_type: str, status: int = 200):
            self.url = url
            self.status = status
            self.headers = {"content-type": content_type}
            self._payload = payload

        async def json(self):
            return self._payload

    class FakePage:
        def __init__(self):
            self._handler = None

        def on(self, event: str, handler):
            assert event == "response"
            self._handler = handler

        async def goto(self, url: str, **kwargs):
            assert self._handler is not None
            # Target first (weaker MIME cues), then a richer ad CDN.
            self._handler(FakeResponse(target, {}, content_type="video/mp4"))
            self._handler(
                FakeResponse(
                    "https://v3.douyinvod.com/target/media-audio/a",
                    {},
                    content_type="audio/mp4",
                )
            )
            self._handler(FakeResponse(ad, {}, content_type="video/mp4"))
            self._handler(
                FakeResponse(
                    f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={work_id}",
                    {
                        "aweme_detail": {
                            "aweme_id": work_id,
                            "desc": "目标作品",
                            "author": {
                                "nickname": "作者",
                                "sec_uid": "MS4wLjABAAAAbound",
                                "uid": "1",
                            },
                            "video": {
                                "duration": 5000,
                                "play_addr": {
                                    "uri": "v0200targeturi0001",
                                    "url_list": [
                                        f"{target}?video_id=v0200targeturi0001",
                                    ],
                                },
                                "cover": {"url_list": ["https://example.invalid/c.jpg"]},
                            },
                        }
                    },
                    content_type="application/json",
                )
            )

        async def title(self):
            return "目标作品 - 抖音"

        async def evaluate(self, _script: str):
            return {"description": "", "author": "", "authorHref": ""}

        async def wait_for_event(self, _event: str, timeout: float = 0):
            raise TimeoutError("timeout")

        async def wait_for_timeout(self, ms: int):
            await asyncio.sleep(max(ms, 0) / 1000)

    class FakeContext:
        pages: list = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright()
    )
    captured = await downloader._capture_cdn_url(
        f"https://www.douyin.com/video/{work_id}"
    )
    assert captured is not None
    assert captured.video_url == target
    assert captured.info.get("id") == work_id


@pytest.mark.asyncio
async def test_capture_keeps_collecting_for_late_target_after_mime_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MIME video+audio must not stop observation before a late target CDN arrives."""
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")
    work_id = "7672717300746907078"
    target = "https://v3.douyinvod.com/target/media-video/seg"
    ad = "https://v9.douyinvod.com/ad/media-video/full.mp4"

    class FakeResponse:
        def __init__(self, url: str, payload: dict, *, content_type: str, status: int = 200):
            self.url = url
            self.status = status
            self.headers = {"content-type": content_type}
            self._payload = payload

        async def json(self):
            return self._payload

    class FakePage:
        def __init__(self):
            self._handler = None
            self._late_fired = False

        def on(self, event: str, handler):
            assert event == "response"
            self._handler = handler

        async def goto(self, url: str, **kwargs):
            assert self._handler is not None
            # Only ad MIME pair up front — target arrives later during wait loop.
            self._handler(FakeResponse(ad, {}, content_type="video/mp4"))
            self._handler(
                FakeResponse(
                    "https://v9.douyinvod.com/ad/media-audio/a",
                    {},
                    content_type="audio/mp4",
                )
            )

        async def title(self):
            return "目标作品 - 抖音"

        async def evaluate(self, _script: str):
            return {"description": "", "author": "", "authorHref": ""}

        async def wait_for_event(self, _event: str, timeout: float = 0):
            if not self._late_fired and self._handler is not None:
                self._late_fired = True
                self._handler(FakeResponse(target, {}, content_type="video/mp4"))
                self._handler(
                    FakeResponse(
                        "https://v3.douyinvod.com/target/media-audio/a",
                        {},
                        content_type="audio/mp4",
                    )
                )
                self._handler(
                    FakeResponse(
                        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={work_id}",
                        {
                            "aweme_detail": {
                                "aweme_id": work_id,
                                "desc": "目标作品",
                                "author": {
                                    "nickname": "作者",
                                    "sec_uid": "MS4wLjABAAAAbound",
                                    "uid": "1",
                                },
                                "video": {
                                    "duration": 5000,
                                    "play_addr": {
                                        "uri": "v0200targeturi0001",
                                        "url_list": [
                                            f"{target}?video_id=v0200targeturi0001",
                                        ],
                                    },
                                    "cover": {
                                        "url_list": ["https://example.invalid/c.jpg"]
                                    },
                                },
                            }
                        },
                        content_type="application/json",
                    )
                )
                return FakeResponse(target, {}, content_type="video/mp4")
            raise TimeoutError("timeout")

        async def wait_for_timeout(self, ms: int):
            await asyncio.sleep(max(ms, 0) / 1000)

    class FakeContext:
        pages: list = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright()
    )
    captured = await downloader._capture_cdn_url(
        f"https://www.douyin.com/video/{work_id}"
    )
    assert captured is not None
    assert captured.video_url == target
    assert captured.info.get("id") == work_id


@pytest.mark.asyncio
async def test_cdn_download_renames_retained_audio_to_mp4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            audio_url="https://cdn.douyinvod.com/media-audio.m4a",
            page_title="测试作品",
            info={
                "channel": "测试作者",
                "description": "作品简介",
                "creator_sec_uid": "MS4wLjABAAAAtest",
                "channel_url": "https://www.douyin.com/user/MS4wLjABAAAAtest",
                "thumbnails": [{"id": "cover", "url": "https://example.invalid/cover.jpg"}],
            },
        )

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)

    def fake_run(command: list[str], *, timeout: float = 3600):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"merged")
        elif "media-video.mp4" in command[-1]:
            (tmp_path / "original.mp4").write_bytes(b"video")
        elif "media-audio.m4a" in command[-1]:
            (tmp_path / "audio.m4a").write_bytes(b"audio")
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert (tmp_path / "audio.mp4").exists()
    assert not (tmp_path / "audio.m4a").exists()


@pytest.mark.asyncio
async def test_cdn_download_fetches_secondary_info_when_creator_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            page_title="仅有标题",
            info={"title": "仅有标题"},
        )

    async def secondary(_url: str, _target_dir: Path):
        return {
            "channel": "补全作者",
            "creator_sec_uid": "MS4wLjABAAAAfilled",
            "channel_url": "https://www.douyin.com/user/MS4wLjABAAAAfilled",
            "thumbnails": [{"id": "cover", "url": "https://example.invalid/cover.jpg"}],
            "description": "补全简介",
        }

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)
    monkeypatch.setattr(downloader, "_fetch_info_json", secondary)

    def fake_run(command: list[str], *, timeout: float = 3600):
        if "media-video.mp4" in command[-1]:
            (tmp_path / "original.mp4").write_bytes(b"video")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    metadata = await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert metadata.author == "补全作者"
    assert metadata.creator_sec_uid == "MS4wLjABAAAAfilled"
    assert metadata.description == "补全简介"


@pytest.mark.asyncio
async def test_capture_awaits_aweme_payload_tasks_before_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ensure delayed response.json() tasks finish before payloads are read."""
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")
    work_id = "7672717300746907078"
    released = asyncio.Event()
    order: list[str] = []

    class FakeResponse:
        def __init__(self, url: str, payload: dict, *, content_type: str, status: int = 200):
            self.url = url
            self.status = status
            self.headers = {"content-type": content_type}
            self._payload = payload

        async def json(self):
            await released.wait()
            order.append("json")
            return self._payload

    class FakePage:
        def __init__(self):
            self._handler = None

        def on(self, event: str, handler):
            assert event == "response"
            self._handler = handler

        async def goto(self, url: str, **kwargs):
            assert self._handler is not None
            # Video+audio CDN so the wait loop can exit; aweme JSON is delayed.
            self._handler(
                FakeResponse(
                    "https://v3.douyinvod.com/path/media-video/seg",
                    {},
                    content_type="video/mp4",
                )
            )
            self._handler(
                FakeResponse(
                    "https://v3.douyinvod.com/path/media-audio/a",
                    {},
                    content_type="audio/mp4",
                )
            )
            self._handler(
                FakeResponse(
                    f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={work_id}",
                    {
                        "aweme_detail": {
                            "aweme_id": work_id,
                            "desc": "接口标题",
                            "author": {
                                "nickname": "接口作者",
                                "sec_uid": "MS4wLjABAAAAapi",
                                "uid": "1",
                            },
                            "video": {
                                "duration": 5000,
                                "cover": {"url_list": ["https://example.invalid/c.jpg"]},
                            },
                        }
                    },
                    content_type="application/json",
                )
            )

        async def title(self):
            order.append("title")
            return "页面标题 - 抖音"

        async def evaluate(self, _script: str):
            return {"description": "", "author": "", "authorHref": ""}

        async def wait_for_event(self, _event: str, timeout: float = 0):
            raise TimeoutError("timeout")

        async def wait_for_timeout(self, ms: int):
            await asyncio.sleep(max(ms, 0) / 1000)

    class FakeContext:
        pages: list = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright()
    )

    async def delayed_release():
        await asyncio.sleep(0.05)
        order.append("release")
        released.set()

    releaser = asyncio.create_task(delayed_release())
    captured = await downloader._capture_cdn_url(
        f"https://www.douyin.com/video/{work_id}"
    )
    await releaser
    assert captured is not None
    assert captured.video_url is not None
    assert captured.info.get("channel") == "接口作者"
    assert captured.info.get("creator_sec_uid") == "MS4wLjABAAAAapi"
    # json must complete before page title read (await gather precedes title()).
    assert order.index("json") < order.index("title")



@pytest.mark.asyncio
async def test_capture_does_not_stall_on_hung_aweme_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hung response.json() must not block CDN capture indefinitely."""
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")
    work_id = "7672717300746907078"

    class FakeResponse:
        def __init__(self, url: str, payload: dict, *, content_type: str, status: int = 200):
            self.url = url
            self.status = status
            self.headers = {"content-type": content_type}
            self._payload = payload

        async def json(self):
            if "aweme" in self.url:
                await asyncio.Event().wait()  # never completes
            return self._payload

    class FakePage:
        def __init__(self):
            self._handler = None

        def on(self, event: str, handler):
            self._handler = handler

        async def goto(self, url: str, **kwargs):
            assert self._handler is not None
            self._handler(
                FakeResponse(
                    "https://v3.douyinvod.com/path/media-video/seg",
                    {},
                    content_type="video/mp4",
                )
            )
            self._handler(
                FakeResponse(
                    "https://v3.douyinvod.com/path/media-audio/a",
                    {},
                    content_type="audio/mp4",
                )
            )
            self._handler(
                FakeResponse(
                    f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={work_id}",
                    {"aweme_detail": {"aweme_id": work_id, "desc": "迟到"}},
                    content_type="application/json",
                )
            )

        async def title(self):
            return "页面标题 - 抖音"

        async def evaluate(self, _script: str):
            return {"description": "", "author": "", "authorHref": ""}

        async def wait_for_event(self, _event: str, timeout: float = 0):
            raise TimeoutError("timeout")

        async def wait_for_timeout(self, ms: int):
            await asyncio.sleep(max(ms, 0) / 1000)

    class FakeContext:
        pages: list = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: FakePlaywright()
    )

    captured = await asyncio.wait_for(
        downloader._capture_cdn_url(f"https://www.douyin.com/video/{work_id}"),
        timeout=12.0,
    )
    assert captured is not None
    assert captured.video_url is not None
    assert captured.audio_url is not None

def test_aweme_duration_is_always_milliseconds() -> None:
    """Aweme video.duration is ms; long second-like values must not be kept raw."""
    short = _info_from_aweme_detail(
        {"aweme_id": "1", "author": {}, "video": {"duration": 5000}, "desc": "x"}
    )
    assert short["duration"] == 5.0
    long_minutes = _info_from_aweme_detail(
        {
            "aweme_id": "2",
            "author": {},
            "video": {"duration": 40 * 60 * 1000},
            "desc": "y",
        }
    )
    assert long_minutes["duration"] == 2400.0


def test_playwright_profile_cookie_databases(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    database = profile / "Default" / "Cookies"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    assert _playwright_profile_cookie_databases(profile) == [database]


def test_cookie_spec_falls_back_when_profile_has_no_usable_session(tmp_path: Path) -> None:
    """Cookie DB file existence alone must not beat system Chrome with a live session."""
    profile_dir = tmp_path / "browser-profile"
    empty_db = profile_dir / "Default" / "Cookies"
    empty_db.parent.mkdir(parents=True)
    with sqlite3.connect(empty_db) as connection:
        connection.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)"
        )

    chrome_profile = tmp_path / "ChromeDefault"
    chrome_db = chrome_profile / "Network" / "Cookies"
    chrome_db.parent.mkdir(parents=True)
    with sqlite3.connect(chrome_db) as connection:
        connection.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)"
        )
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )

    settings = MediaSettings(browser="chrome", browser_profile=str(chrome_profile))
    spec, source, kind = preferred_yt_dlp_cookie_spec(profile_dir, settings)
    assert kind == "system_browser"
    assert spec == f"chrome:{chrome_profile}"
    assert "browser cookie store" in source

    # Usable Playwright cookies still win over system Chrome.
    with sqlite3.connect(empty_db) as connection:
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )
    spec, source, kind = preferred_yt_dlp_cookie_spec(profile_dir, settings)
    assert kind == "playwright_profile"
    assert source == str(profile_dir)
    assert spec == f"chromium:{profile_dir / 'Default'}"


@pytest.mark.asyncio
async def test_side_paths_prefer_playwright_profile_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_dir = tmp_path / "browser-profile"
    cookie_db = profile_dir / "Default" / "Cookies"
    cookie_db.parent.mkdir(parents=True)
    with sqlite3.connect(cookie_db) as connection:
        connection.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)"
        )
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )

    # System Chrome also has cookies — must not be preferred.
    chrome_profile = tmp_path / "ChromeDefault"
    chrome_db = chrome_profile / "Network" / "Cookies"
    chrome_db.parent.mkdir(parents=True)
    with sqlite3.connect(chrome_db) as connection:
        connection.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, expires_utc INTEGER)"
        )
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            (".douyin.com", "sessionid", 99_999_999_999_999_999),
        )

    downloader = YtDlpDownloader(
        MediaSettings(browser="chrome", browser_profile=str(chrome_profile)),
        profile_dir,
    )
    spec, source, kind = downloader._preferred_yt_dlp_cookie_spec()
    assert kind == "playwright_profile"
    assert source == str(profile_dir)
    assert spec == f"chromium:{profile_dir / 'Default'}"

    seen: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: float = 3600):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="123\n", stderr="")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)

    async def no_cdn(_url: str):
        return None

    monkeypatch.setattr(downloader, "_capture_cdn_url", no_cdn)
    result = await downloader.check_auth(
        video_url="https://www.douyin.com/video/123"
    )
    assert result.ok is True
    assert result.cookie_source == str(profile_dir)
    assert "专用 Playwright Profile" in result.message
    assert "--cookies-from-browser" in seen[0]
    assert seen[0][seen[0].index("--cookies-from-browser") + 1] == spec

    # _fetch_info_json also uses the preferred spec.
    seen.clear()
    await downloader._fetch_info_json("https://www.douyin.com/video/123", tmp_path / "out")
    assert seen and seen[0][seen[0].index("--cookies-from-browser") + 1] == spec


@pytest.mark.asyncio
async def test_cdn_merge_picks_largest_video_and_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloader = YtDlpDownloader(MediaSettings(), tmp_path / "browser")

    async def captured(_url: str):
        return CapturedCdnMedia(
            video_url="https://cdn.douyinvod.com/media-video.mp4",
            audio_url="https://cdn.douyinvod.com/media-audio.mp4",
            page_title="测试作品",
            info={
                "title": "测试作品",
                "description": "简介",
                "channel": "作者",
                "creator_sec_uid": "MS4wLjABAAAAtest",
                "channel_url": "https://www.douyin.com/user/MS4wLjABAAAAtest",
                "thumbnails": [{"id": "cover", "url": "https://example.invalid/cover.jpg"}],
            },
        )

    monkeypatch.setattr(downloader, "_capture_cdn_url", captured)

    async def no_secondary(_url: str, _target_dir: Path):
        return {}

    monkeypatch.setattr(downloader, "_fetch_info_json", no_secondary)
    merge_inputs: list[str] = []

    def fake_run(command: list[str], *, timeout: float = 3600):
        if command[0] == "ffmpeg":
            # Locate both -i operands.
            indexes = [i for i, part in enumerate(command) if part == "-i"]
            merge_inputs.extend([command[i + 1] for i in indexes[:2]])
            Path(command[-1]).write_bytes(b"merged-large")
        elif "media-video.mp4" in command[-1]:
            (tmp_path / "original.webm").write_bytes(b"tiny")
            (tmp_path / "original.mp4").write_bytes(b"video-bytes-larger")
        elif "media-audio.mp4" in command[-1]:
            (tmp_path / "audio.m4a").write_bytes(b"a")
            (tmp_path / "audio.mp4").write_bytes(b"audio-bytes-larger!!")
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("douyin_wiki.adapters.media._run", fake_run)
    await downloader.download("https://www.douyin.com/video/123", "123", tmp_path)
    assert merge_inputs[0].endswith("original.mp4")
    assert merge_inputs[1].endswith("audio.mp4")

