from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

_WORK_PATH = re.compile(r"^/(video|note|article)/(\d+)/?$")


class FavoriteFolder(BaseModel):
    id: str
    name: str
    reported_count: int | None = Field(default=None, ge=0)


class FavoriteWork(BaseModel):
    work_id: str
    canonical_url: str
    title: str = "抖音作品"
    author: str = ""
    source_kind: Literal["video", "image_note", "article"] = "video"
    folder_ids: list[str] = Field(default_factory=list)
    available: bool = True

    @field_validator("work_id")
    @classmethod
    def validate_work_id(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized.isascii() or not normalized.isdigit():
            raise ValueError("work_id must be a numeric Douyin work ID")
        return normalized

    @model_validator(mode="after")
    def normalize_and_validate_url(self) -> FavoriteWork:
        raw_url = self.canonical_url.strip()
        if raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("canonical_url must use HTTP or HTTPS")
        if parsed.username or parsed.password or parsed.port:
            raise ValueError("canonical_url must use the canonical Douyin origin")
        if (parsed.hostname or "").lower() not in {"douyin.com", "www.douyin.com"}:
            raise ValueError("canonical_url must use the canonical Douyin origin")
        match = _WORK_PATH.fullmatch(parsed.path)
        if match is None:
            raise ValueError("canonical_url must identify a Douyin work")
        route, url_work_id = match.groups()
        if url_work_id != self.work_id:
            raise ValueError("canonical_url work ID does not match work_id")
        expected_kind = {"video": "video", "note": "image_note", "article": "article"}[route]
        if self.source_kind != expected_kind and not (
            route == "note" and self.source_kind == "article"
        ):
            raise ValueError("canonical_url route does not match source_kind")
        self.canonical_url = f"https://www.douyin.com/{route}/{self.work_id}"
        self.title = self.title.strip() or "抖音作品"
        self.author = self.author.strip()
        self.folder_ids = list(
            dict.fromkeys(folder_id.strip() for folder_id in self.folder_ids if folder_id.strip())
        )
        return self


class FavoriteInventory(BaseModel):
    account_id: str
    nickname: str = ""
    folders: list[FavoriteFolder] = Field(default_factory=list)
    works: list[FavoriteWork] = Field(default_factory=list)
    complete: bool = False
    folders_complete: bool = False
    warnings: list[str] = Field(default_factory=list)
