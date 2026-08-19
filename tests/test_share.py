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
