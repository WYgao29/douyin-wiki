from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from ..errors import InvalidShareTextError
from ..models import SourceKind

URL_PATTERN = re.compile(r"https?://[^\s<>\]\[\)\(]+", re.IGNORECASE)
WORK_ID_PATTERNS = (
    re.compile(r"/(?:share/)?(?P<kind>video|note|gallery)/(?P<id>\d{10,})"),
    re.compile(r"[?&](?:aweme_id|item_id)=(?P<id>\d{10,})"),
)
CREATOR_PATH_PATTERN = re.compile(r"/user/(?P<sec_uid>[A-Za-z0-9_-]{8,256})")
ALLOWED_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")


@dataclass(frozen=True)
class ResolvedShare:
    original_url: str
    canonical_url: str
    video_id: str
    redirect_chain: tuple[str, ...]
    source_kind: SourceKind = SourceKind.VIDEO


def extract_douyin_url(share_text: str) -> str:
    for match in URL_PATTERN.finditer(share_text):
        candidate = match.group(0).rstrip(".,;:!?'\"，。；：！？）")
        host = (urlparse(candidate).hostname or "").lower()
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
            return candidate
    raise InvalidShareTextError("分享文本中没有找到有效的抖音链接")


def extract_work_identity(value: str) -> tuple[str, SourceKind] | None:
    for pattern in WORK_ID_PATTERNS:
        if match := pattern.search(value):
            kind = match.groupdict().get("kind")
            source_kind = SourceKind.IMAGE_NOTE if kind in {"note", "gallery"} else SourceKind.VIDEO
            return match.group("id"), source_kind
    return None


def extract_video_id(value: str) -> str | None:
    """Compatibility helper returning the Douyin work ID for any supported work."""
    identity = extract_work_identity(value)
    return identity[0] if identity else None


def extract_creator_sec_uid(value: str) -> str | None:
    """Return the stable creator sec_uid from a canonical Douyin user URL."""
    if match := CREATOR_PATH_PATTERN.search(value):
        return match.group("sec_uid")
    return None


def _resolved(
    original_url: str, identity: tuple[str, SourceKind], chain: list[str]
) -> ResolvedShare:
    work_id, source_kind = identity
    route = "note" if source_kind == SourceKind.IMAGE_NOTE else "video"
    return ResolvedShare(
        original_url=original_url,
        canonical_url=f"https://www.douyin.com/{route}/{work_id}",
        video_id=work_id,
        redirect_chain=tuple(chain),
        source_kind=source_kind,
    )


def _merge_identity(
    current: tuple[str, SourceKind] | None,
    candidate: tuple[str, SourceKind],
    chain: list[str],
) -> tuple[str, SourceKind]:
    if current is not None and current[0] != candidate[0]:
        raise InvalidShareTextError(
            "短链重定向中的作品 ID 不一致",
            details={"redirect_chain": chain},
        )
    return candidate


class DouyinShareResolver:
    def __init__(
        self, *, timeout_seconds: float = 15, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def resolve(self, share_text: str) -> ResolvedShare:
        original_url = extract_douyin_url(share_text)
        if identity := extract_work_identity(original_url):
            return _resolved(original_url, identity, [original_url])

        chain = [original_url]
        current = original_url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, headers=headers, transport=self.transport
        ) as client:
            identity: tuple[str, SourceKind] | None = None
            for _ in range(8):
                try:
                    response = await client.get(current, follow_redirects=False)
                except httpx.HTTPError as exc:
                    raise InvalidShareTextError(
                        "无法解析抖音短链", details={"url": current, "cause": str(exc)}
                    ) from exc
                if response_identity := extract_work_identity(str(response.url)):
                    identity = _merge_identity(identity, response_identity, chain)
                location = response.headers.get("location")
                if location:
                    current = urljoin(current, location)
                    chain.append(current)
                    if location_identity := extract_work_identity(current):
                        identity = _merge_identity(identity, location_identity, chain)
                    continue
                if identity:
                    return _resolved(original_url, identity, chain)
                break
            else:
                raise InvalidShareTextError(
                    "抖音短链重定向次数过多",
                    details={"redirect_chain": chain},
                )
        raise InvalidShareTextError(
            "短链已解析，但没有得到抖音作品 ID", details={"redirect_chain": chain}
        )
