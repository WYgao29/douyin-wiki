from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from ..database import Database
from ..models import InspirationInput, LibraryItem, SourceKind
from ..time_utils import parse_datetime

CONTENT_TYPE_LABELS = {
    "tutorial": "教程",
    "explanation": "知识解释",
    "opinion": "观点",
    "recommendation": "推荐",
    "news_event": "新闻事件",
    "story_case": "故事案例",
    "collection": "清单",
    "other": "其他",
}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    loaded = yaml.safe_load(text[4:marker]) or {}
    return (loaded if isinstance(loaded, dict) else {}), text[marker + 5 :]


def _section(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)", body)
    return match.group(1).strip() if match else ""


def _plain(text: str) -> str:
    text = re.sub(r"!\[[^]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _inspirations(value: Any, body: str) -> list[InspirationInput]:
    result: list[InspirationInput] = []
    has_frontmatter_value = isinstance(value, list)
    if isinstance(value, list):
        for item in value:
            try:
                result.append(
                    InspirationInput(text=item)
                    if isinstance(item, str)
                    else InspirationInput.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
    if not result and not has_frontmatter_value:
        section = _section(body, "灵感")
        for line in section.splitlines():
            text = re.sub(r"^\s*[-*]\s+", "", line).strip()
            if text and text not in {"暂无", "无"}:
                result.append(InspirationInput(text=text))
    return result


class LibraryCatalog:
    """Read-only projection of visible source notes in a Vault."""

    def __init__(self, vault_path: Path, database: Database) -> None:
        self.vault_path = vault_path.expanduser().resolve()
        self.database = database
        self._items: dict[str, LibraryItem] = {}
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._version = 0
        self._lock = threading.RLock()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        with self._lock:
            return self._fingerprint

    @property
    def watch_roots(self) -> list[Path]:
        roots = [self.vault_path / "wiki" / "sources"]
        creators = self.vault_path / "creators"
        if creators.is_dir():
            roots.extend(path for path in creators.glob("*/sources") if path.is_dir())
        return [path for path in roots if path.is_dir()]

    def refresh(self) -> list[LibraryItem]:
        db_entries = {entry.video_id: entry for entry in self.database.list_entries()}
        candidates: list[Path] = []
        legacy = self.vault_path / "wiki" / "sources"
        if legacy.is_dir():
            candidates.extend(legacy.glob("*.md"))
        creators = self.vault_path / "creators"
        if creators.is_dir():
            candidates.extend(creators.glob("*/sources/*.md"))

        items: dict[str, LibraryItem] = {}
        for path in sorted(candidates):
            item = self._read_item(path, db_entries)
            if item is None:
                continue
            previous = items.get(item.entry_id)
            preferred = db_entries.get(item.work_id)
            if previous is None or (
                preferred and Path(preferred.source_path) == path.relative_to(self.vault_path)
            ):
                items[item.entry_id] = item
        with self._lock:
            self._items = items
            self._fingerprint = self.compute_fingerprint()
            self._version += 1
            return list(items.values())

    def compute_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        paths: list[Path] = []
        legacy = self.vault_path / "wiki" / "sources"
        if legacy.is_dir():
            paths.extend(legacy.glob("*.md"))
        creators = self.vault_path / "creators"
        if creators.is_dir():
            paths.extend(creators.glob("*/sources/*.md"))
        values: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            values.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(values))

    def list_items(self) -> list[LibraryItem]:
        with self._lock:
            return sorted(
                self._items.values(),
                key=lambda item: (
                    item.captured_at or item.published_at or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )

    def get(self, entry_id: str) -> LibraryItem | None:
        with self._lock:
            return self._items.get(entry_id)

    def filter(
        self,
        *,
        query: str = "",
        author: str = "",
        content_type: str = "",
        tag: str = "",
        inspiration_only: bool = False,
    ) -> list[LibraryItem]:
        needle = query.casefold().strip()
        result: list[LibraryItem] = []
        for item in self.list_items():
            haystack = "\n".join(
                [
                    item.title,
                    item.author,
                    item.summary,
                    " ".join(item.tags),
                    " ".join(value.text for value in item.inspirations),
                    item.body_markdown,
                ]
            ).casefold()
            if needle and needle not in haystack:
                continue
            if author and item.author != author:
                continue
            if content_type and item.content_type != content_type:
                continue
            if tag and tag not in item.tags:
                continue
            if inspiration_only and not item.inspirations:
                continue
            result.append(item)
        return result

    def _read_item(self, path: Path, db_entries: dict[str, Any]) -> LibraryItem | None:
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter, body = split_frontmatter(text)
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if frontmatter.get("type") not in {None, "source"}:
            return None
        work_id = str(frontmatter.get("video_id") or "").strip()
        if not work_id:
            match = re.search(r"(?<!\d)(\d{12,22})(?!\d)", path.stem)
            work_id = match.group(1) if match else ""
        if not work_id:
            return None
        entry = db_entries.get(work_id)
        entry_id = entry.id if entry else f"dy-{work_id}"
        title = str(frontmatter.get("title") or "").strip()
        if not title:
            match = re.search(r"(?m)^#\s+(.+)$", body)
            title = match.group(1).strip() if match else path.stem
        author = str(frontmatter.get("author") or "未知作者").strip()
        content_type = str(frontmatter.get("content_type") or "other")
        tags_value = frontmatter.get("tags") or []
        tags = [str(value) for value in tags_value] if isinstance(tags_value, list) else []
        cover = str(frontmatter.get("cover_image") or "").strip() or None
        cover_url = self.media_url(cover) if cover and self.safe_media_path(cover) else None
        source_kind_value = str(frontmatter.get("source_kind") or "video")
        try:
            source_kind = SourceKind(source_kind_value)
        except ValueError:
            source_kind = SourceKind.VIDEO
        captured = frontmatter.get("captured") or frontmatter.get("captured_at")
        published = frontmatter.get("published") or frontmatter.get("published_at")
        original_url = str(
            frontmatter.get("original_url")
            or (entry.original_url if entry else "")
            or frontmatter.get("source_url")
            or frontmatter.get("canonical_url")
        )
        return LibraryItem(
            entry_id=entry_id,
            work_id=work_id,
            title=title,
            author=author,
            cover_path=cover,
            cover_url=cover_url,
            summary=_plain(_section(body, "一句话").split("\n\n", 1)[0])
            or (entry.summary if entry else ""),
            content_type=content_type,
            tags=tags,
            inspirations=_inspirations(frontmatter.get("inspirations"), body),
            published_at=parse_datetime(published),
            captured_at=parse_datetime(captured),
            status=str(frontmatter.get("status") or "已入库"),
            favorite=entry.favorite if entry else bool(frontmatter.get("favorite", False)),
            media_status=entry.media_status if entry else "present",
            retention=(
                entry.retention
                if entry
                else "temporary"
            ),
            source_kind=source_kind,
            source_path=str(path.relative_to(self.vault_path)),
            original_url=original_url,
            creator_id=str(frontmatter.get("creator_id") or "") or None,
            body_markdown=body,
        )

    def safe_media_path(self, relative: str) -> Path | None:
        if not relative or relative.startswith(("http://", "https://", "/")):
            return None
        try:
            target = (self.vault_path / relative).resolve()
            target.relative_to(self.vault_path)
        except (OSError, ValueError):
            return None
        if target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}:
            return None
        return target if target.is_file() else None

    @staticmethod
    def media_url(relative: str) -> str:
        return "/media/" + quote(relative.replace("\\", "/"), safe="/")
