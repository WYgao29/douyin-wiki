from __future__ import annotations

import httpx
import pytest

from douyin_wiki.adapters.share import (
    DouyinShareResolver,
    extract_douyin_url,
    extract_video_id,
)
from douyin_wiki.errors import InvalidShareTextError
from douyin_wiki.models import SourceKind


def test_extract_url_from_full_share_text() -> None:
    text = "6.43 复制打开抖音，看看作品 https://v.douyin.com/uvHsRpXIn8s/ :5pm 10/23 g@o.Dh"
    assert extract_douyin_url(text) == "https://v.douyin.com/uvHsRpXIn8s/"


def test_extract_video_id_from_supported_urls() -> None:
    assert extract_video_id("https://www.douyin.com/video/7672717300746907078") == (
        "7672717300746907078"
    )
    assert extract_video_id("https://www.iesdouyin.com/share/video/7672717300746907078/") == (
        "7672717300746907078"
    )
    assert extract_video_id("https://www.douyin.com/note/7674987897195870714") == (
        "7674987897195870714"
    )


def test_reject_non_douyin_url() -> None:
    with pytest.raises(InvalidShareTextError):
        extract_douyin_url("https://example.com/video/123")


@pytest.mark.asyncio
async def test_resolve_short_link_chain_to_sample_video_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={"location": "https://www.iesdouyin.com/share/video/7672717300746907078/"},
            )
        return httpx.Response(404)

    resolver = DouyinShareResolver(transport=httpx.MockTransport(handler))
    result = await resolver.resolve("复制打开 https://v.douyin.com/uvHsRpXIn8s/")
    assert result.video_id == "7672717300746907078"
    assert result.canonical_url == "https://www.douyin.com/video/7672717300746907078"


@pytest.mark.asyncio
async def test_resolve_image_note_sample() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={"location": "https://www.douyin.com/note/7674987897195870714"},
            )
        return httpx.Response(404)

    resolver = DouyinShareResolver(transport=httpx.MockTransport(handler))
    result = await resolver.resolve("3.05 复制打开抖音 https://v.douyin.com/oH4K0gee_Ok/ :6pm")
    assert result.video_id == "7674987897195870714"
    assert result.source_kind == SourceKind.IMAGE_NOTE
    assert result.canonical_url == "https://www.douyin.com/note/7674987897195870714"


@pytest.mark.asyncio
async def test_final_note_redirect_overrides_intermediate_video_route() -> None:
    work_id = "7659645255277039717"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://www.iesdouyin.com/share/video/{work_id}/"
                },
            )
        if request.url.host == "www.iesdouyin.com":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        f"https://www.douyin.com/note/{work_id}"
                        "?previous_page=web_code_link"
                    )
                },
            )
        return httpx.Response(200)

    result = await DouyinShareResolver(transport=httpx.MockTransport(handler)).resolve(
        "https://v.douyin.com/example/"
    )

    assert result.video_id == work_id
    assert result.source_kind == SourceKind.IMAGE_NOTE
    assert result.canonical_url == f"https://www.douyin.com/note/{work_id}"
    assert result.redirect_chain[-1].startswith(f"https://www.douyin.com/note/{work_id}")


@pytest.mark.asyncio
async def test_final_video_redirect_overrides_intermediate_note_route() -> None:
    work_id = "7659645255277039717"
    locations = iter(
        (
            f"https://www.douyin.com/note/{work_id}",
            f"https://www.douyin.com/video/{work_id}",
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        location = next(locations, None)
        return (
            httpx.Response(302, headers={"location": location})
            if location
            else httpx.Response(200)
        )

    result = await DouyinShareResolver(transport=httpx.MockTransport(handler)).resolve(
        "https://v.douyin.com/example/"
    )

    assert result.source_kind == SourceKind.VIDEO
    assert result.canonical_url == f"https://www.douyin.com/video/{work_id}"


@pytest.mark.asyncio
async def test_redirect_chain_rejects_conflicting_work_ids() -> None:
    locations = iter(
        (
            "https://www.douyin.com/video/7659645255277039717",
            "https://www.douyin.com/note/7678561149449331835",
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        location = next(locations, None)
        return (
            httpx.Response(302, headers={"location": location})
            if location
            else httpx.Response(200)
        )

    with pytest.raises(InvalidShareTextError, match="作品 ID 不一致"):
        await DouyinShareResolver(transport=httpx.MockTransport(handler)).resolve(
            "https://v.douyin.com/example/"
        )


@pytest.mark.asyncio
async def test_short_link_redirect_limit_is_rejected() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            302,
            headers={"location": f"https://v.douyin.com/hop-{request_count}/"},
        )

    with pytest.raises(InvalidShareTextError, match="重定向次数过多"):
        await DouyinShareResolver(transport=httpx.MockTransport(handler)).resolve(
            "https://v.douyin.com/example/"
        )
    assert request_count == 8


@pytest.mark.asyncio
async def test_direct_canonical_url_never_calls_transport() -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("canonical URL must not perform an HTTP request")

    work_id = "7659645255277039717"
    resolver = DouyinShareResolver(transport=httpx.MockTransport(unexpected_request))

    result = await resolver.resolve(f"https://www.douyin.com/note/{work_id}")

    assert result.source_kind == SourceKind.IMAGE_NOTE
    assert result.canonical_url == f"https://www.douyin.com/note/{work_id}"
