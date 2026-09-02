from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from .localization import (
    label_entry_status,
    label_media_status,
    label_retention,
    parse_entry_status,
    parse_media_status,
    parse_retention,
)
from .models import (
    AnalysisResult,
    CreatorRecord,
    CreatorWorkDecision,
    CreatorWorkRecord,
    EntryRecord,
    InspirationInput,
    ResearchTopic,
    RetentionPolicy,
    SourceKind,
    TopicArtifact,
)
from .time_utils import (
    beijing_date,
    beijing_iso,
    format_beijing,
    parse_datetime,
    user_times_to_beijing,
    utc_now,
)

VAULT_AGENTS = """# 抖库维护规则

这个 Vault 由抖库与 AI 维护，供 AI 检索个人收藏的抖音知识。

- `raw/` 是不可变来源；日常维护不得覆盖或单独删除原始分享文本、ASR、校正版逐字稿和 OCR。
  只有用户在 Web 中明确确认删除整条资料时，才可将整组资料移入抖库废纸篓。
- `wiki/sources/` 是每条作品的主资料页。
- `creators/` 中每个博主拥有独立、自包含的资料目录。
- `topics/` 保存用户选定来源的研究专题、成果和用户专题笔记。
- `wiki/concepts/`、`wiki/entities/`、`wiki/syntheses/` 保存跨资料知识。
- 用户填写的“灵感”必须逐字保留，AI 不得代写或改写。
- 知识页区分作品原话、作品正文、OCR、AI 推断和用户灵感。
- 回答问题时返回原作品链接；视频引用提供时间戳，图文引用提供图片编号。
- 过期内容标记为“已过期”，不静默删除 Markdown。
- `log.md` 只追加，不改写历史记录。
"""


@dataclass
class WrittenEntry:
    raw_path: Path
    source_path: Path
    machine_path: Path
    changed_paths: list[Path]


def safe_filename(value: str, *, max_length: int = 80) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "抖音作品")[:max_length].rstrip()


def format_timestamp(milliseconds: int | None) -> str:
    if milliseconds is None:
        return ""
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def encode_markdown_path(value: str) -> str:
    """Encode a Vault-relative Markdown destination without escaping path separators."""
    return quote(Path(value).as_posix(), safe="/.-_~")


class VaultWriter:
    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path
        self.last_entry_load_errors: list[dict[str, str]] = []

    @contextmanager
    def locked(self) -> Iterator[None]:
        lock_path = self.vault_path / ".douyin-wiki" / "vault.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def entry_operations_locked(self) -> Iterator[None]:
        """Serialize entry mutations across Web, CLI, MCP, and worker processes.

        This lock must always be acquired before ``locked()`` when both are
        needed.  Keeping it separate from the short-lived Vault file lock lets
        one logical entry mutation cover its SQLite and filesystem changes.
        """
        lock_path = self.vault_path / ".douyin-wiki" / "entry-operations.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, *, initialize_git: bool = True) -> list[Path]:
        directories = [
            ".obsidian",
            "raw/assets",
            "raw/covers",
            "raw/images",
            "wiki/sources",
            "wiki/.data/sources",
            "wiki/concepts",
            "wiki/entities",
            "wiki/syntheses",
            "wiki/questions",
            "creators",
            "topics",
            ".douyin-wiki/work",
        ]
        for relative in directories:
            (self.vault_path / relative).mkdir(parents=True, exist_ok=True)

        defaults = {
            "AGENTS.md": VAULT_AGENTS,
            "index.md": "---\ntype: index\nupdated: null\n---\n\n# 抖库\n\n暂无内容。\n",
            "log.md": "---\ntype: log\n---\n\n# 维护日志\n",
            ".gitignore": (
                ".DS_Store\n.obsidian/\n.douyin-wiki/\nraw/assets/**/original.*\n"
                "raw/assets/**/cover.*\nraw/assets/**/audio.wav\n"
                "raw/assets/**/original.info.json\n"
                "raw/assets/**/frames/\nraw/covers/\n"
                "raw/images/\n"
                "creators/**/raw/assets/**/original.*\n"
                "creators/**/raw/assets/**/original.info.json\n"
                "creators/**/raw/assets/**/cover.*\n"
                "creators/**/raw/assets/**/audio.wav\n"
                "creators/**/raw/assets/**/frames/\n"
                "creators/**/raw/covers/\n"
                "creators/**/raw/images/\n"
                "creators/**/raw/avatar.*\n"
            ),
        }
        changed: list[Path] = []
        for relative, content in defaults.items():
            path = self.vault_path / relative
            if not path.exists():
                self._atomic_write(path, content)
                changed.append(path)
        gitignore = self.vault_path / ".gitignore"
        gitignore_content = gitignore.read_text(encoding="utf-8")
        required_ignores = [
            ".obsidian/",
            "raw/assets/**/original.info.json",
            "raw/assets/**/cover.*",
            "raw/covers/",
            "raw/images/",
            "creators/**/raw/assets/**/original.*",
            "creators/**/raw/assets/**/original.info.json",
            "creators/**/raw/assets/**/cover.*",
            "creators/**/raw/assets/**/audio.wav",
            "creators/**/raw/assets/**/frames/",
            "creators/**/raw/covers/",
            "creators/**/raw/images/",
            "creators/**/raw/avatar.*",
        ]
        missing_ignores = [
            rule for rule in required_ignores if rule not in gitignore_content.splitlines()
        ]
        if missing_ignores:
            updated = gitignore_content.rstrip() + "\n" + "\n".join(missing_ignores) + "\n"
            self._atomic_write(gitignore, updated)
            if gitignore not in changed:
                changed.append(gitignore)
        if initialize_git:
            self._ensure_git()
            self.commit(changed, "chore: initialize 抖库 vault")
        return changed

    def migrate_branding(self) -> list[Path]:
        """Update only exact, system-generated legacy brand strings in existing Vaults."""
        replacements = {
            "# Douyin Wiki 维护规则": "# 抖库维护规则",
            "这个 Vault 是由 AI 维护、供 AI 检索的个人抖音知识库。": (
                "这个 Vault 由抖库与 AI 维护，供 AI 检索个人收藏的抖音知识。"
            ),
            "# 抖音知识库": "# 抖库",
            "此文件由 Douyin Wiki 管理": "此文件由抖库管理",
        }
        candidates = [self.vault_path / "index.md", self.vault_path / "AGENTS.md"]
        candidates.extend((self.vault_path / "wiki" / ".data" / "sources").glob("*.md"))
        candidates.extend((self.vault_path / "creators").glob("*/.data/sources/*.md"))
        changed: list[Path] = []
        for path in candidates:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            updated = content
            for old, new in replacements.items():
                updated = updated.replace(old, new)
            if updated == content:
                continue
            self._atomic_write(path, updated)
            changed.append(path)
        return changed

    def migrate_visible_status_labels(self) -> list[Path]:
        """Translate system-owned frontmatter statuses without touching hidden machine data."""
        replacements = {
            "status": {
                "active": label_entry_status("active"),
                "stale": label_entry_status("stale"),
                "raw": label_entry_status("raw"),
            },
            "media_status": {
                "present": label_media_status("present"),
                "removed": label_media_status("removed"),
            },
            "media_retention": {item.value: label_retention(item) for item in RetentionPolicy},
        }
        candidates: list[Path] = []
        for pattern in (
            "raw/*.md",
            "wiki/sources/*.md",
            "wiki/concepts/*.md",
            "wiki/entities/*.md",
            "wiki/syntheses/*.md",
            "wiki/questions/*.md",
            "creators/*/sources/*.md",
            "creators/*/raw/records/*.md",
            "creators/*/concepts/*.md",
            "creators/*/entities/*.md",
            "creators/*/syntheses/*.md",
        ):
            candidates.extend(self.vault_path.glob(pattern))
        changed: list[Path] = []
        for path in candidates:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---\n"):
                continue
            boundary = content.find("\n---\n", 4)
            if boundary < 0:
                continue
            header, body = content[:boundary], content[boundary:]
            updated = header
            for field, values in replacements.items():
                for code, label in values.items():
                    updated = re.sub(
                        rf"(?m)^({re.escape(field)}:\s*)['\"]?{re.escape(code)}['\"]?\s*$",
                        rf"\g<1>{label}",
                        updated,
                    )
            if updated == header:
                continue
            self._atomic_write(path, updated + body)
            changed.append(path)
        return changed

    def migrate_visible_times_to_beijing(self) -> list[Path]:
        """Convert system-owned visible timestamps without touching user inspiration text."""
        candidates = [self.vault_path / "index.md", self.vault_path / "log.md"]
        for pattern in (
            "raw/*.md",
            "wiki/sources/*.md",
            "wiki/concepts/*.md",
            "wiki/entities/*.md",
            "wiki/syntheses/*.md",
            "wiki/questions/*.md",
            "creators/*/index.md",
            "creators/*/log.md",
            "creators/*/sources/*.md",
            "creators/*/raw/records/*.md",
            "creators/*/concepts/*.md",
            "creators/*/entities/*.md",
            "creators/*/syntheses/*.md",
        ):
            candidates.extend(self.vault_path.glob(pattern))
        timestamp = (
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
        )
        changed: list[Path] = []
        for path in dict.fromkeys(candidates):
            if not path.is_file():
                continue
            original = path.read_text(encoding="utf-8")
            match = re.match(
                r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)$", original, re.S
            )
            if not match:
                continue
            frontmatter = yaml.safe_load(match.group("frontmatter")) or {}
            localized_frontmatter = user_times_to_beijing(frontmatter)
            body = match.group("body")
            body = re.sub(
                rf"(?m)^(- 最近同步：)({timestamp})$",
                lambda item: item.group(1) + format_beijing(item.group(2)),
                body,
            )
            body = re.sub(
                rf"(?m)^(## )({timestamp})( · .+)$",
                lambda item: item.group(1) + format_beijing(item.group(2)) + item.group(3),
                body,
            )
            body = re.sub(
                rf"(?m)^(- 提醒候选：.*? — )({timestamp})$",
                lambda item: item.group(1) + format_beijing(item.group(2)),
                body,
            )
            updated = self._frontmatter(localized_frontmatter) + body
            if updated == original:
                continue
            self._atomic_write(path, updated)
            changed.append(path)
        return changed

    def creator_index_needs_refresh(self, folder_path: str) -> bool:
        index = self.vault_path / folder_path / "index.md"
        if not index.is_file():
            return True
        frontmatter, _ = self._parse_document(index)
        return frontmatter.get("creator_view_version") != 2

    def write_entry(self, entry: EntryRecord, data: dict[str, Any]) -> WrittenEntry:
        raw_path = self.vault_path / entry.raw_path
        source_path = self.vault_path / entry.source_path
        creator_folder = str(data.get("creator", {}).get("folder_path") or "")
        machine_relative = (
            Path(creator_folder) / ".data" / "sources" / f"{entry.video_id}.md"
            if creator_folder
            else Path("wiki") / ".data" / "sources" / f"{entry.video_id}.md"
        )
        machine_path = self.vault_path / machine_relative
        changed: list[Path] = []
        # Render every document before replacing any path. Keep the previous bytes
        # so a later filesystem failure can restore the complete visible pair.
        raw_content = None if raw_path.exists() else self._render_raw(entry, data)
        source_content = self._render_source(entry, data)
        machine_content = self._render_machine(entry, data)
        previous = {
            path: path.read_text(encoding="utf-8") if path.exists() else None
            for path in (raw_path, source_path, machine_path)
        }
        try:
            if raw_content is not None:
                self._atomic_write(raw_path, raw_content)
                changed.append(raw_path)
            if previous[source_path] != source_content:
                self._atomic_write(source_path, source_content)
                changed.append(source_path)
            if previous[machine_path] != machine_content:
                self._atomic_write(machine_path, machine_content)
                changed.append(machine_path)
        except Exception:
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    self._atomic_write(path, content)
            raise

        changed.extend(self.restore_entry_links(entry, data))
        return WrittenEntry(
            raw_path=raw_path,
            source_path=source_path,
            machine_path=machine_path,
            changed_paths=changed,
        )

    def restore_entry_links(self, entry: EntryRecord, data: dict[str, Any]) -> list[Path]:
        """Restore concept and entity backlinks without rewriting restored evidence files."""
        analysis = AnalysisResult.model_validate(data["analysis"])
        creator_folder = str(data.get("creator", {}).get("folder_path") or "")
        knowledge_root = (
            self.vault_path / creator_folder if creator_folder else self.vault_path / "wiki"
        )
        changed: list[Path] = []
        for concept in analysis.concepts:
            path = self._collision_safe_page_path(knowledge_root / "concepts", concept)
            if self._ensure_link_page(path, "concept", concept, entry):
                changed.append(path)
        for entity in analysis.entities:
            path = self._collision_safe_page_path(knowledge_root / "entities", entity.name)
            if self._ensure_link_page(path, "entity", entity.name, entry, entity.description):
                changed.append(path)
        return changed

    @staticmethod
    def _collision_safe_page_path(folder: Path, title: str) -> Path:
        path = folder / f"{safe_filename(title)}.md"
        if not path.exists():
            return path
        content = path.read_text(encoding="utf-8", errors="ignore")
        heading = re.search(r"^#\s+(.+)$", content, re.M)
        if heading and heading.group(1).strip() == title:
            return path
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        return folder / f"{safe_filename(title, max_length=70)}-{digest}.md"

    def migrate_inspiration_vocabulary(self) -> list[Path]:
        """Update system labels while preserving all captured user content."""
        candidates = [self.vault_path / "AGENTS.md"]
        candidates.extend((self.vault_path / "raw").glob("*.md"))
        candidates.extend((self.vault_path / "wiki" / "sources").glob("*.md"))
        replacements = (
            ("用户填写的“用途”", "用户填写的“灵感”"),
            ("用户用途和外部核验", "用户灵感和外部核验"),
            ("## 采集时用途（逐字保留）", "## 采集时灵感（逐字保留）"),
            ("## 用户用途（逐字保留）", "## 用户灵感（逐字保留）"),
            ("## 用户灵感（逐字保留）", "## 灵感"),
            ("\npurposes:\n", "\ninspirations:\n"),
            ("AI 不推测用户用途", "AI 不推测用户灵感"),
        )
        changed: list[Path] = []
        for path in candidates:
            if not path.exists():
                continue
            original = path.read_text(encoding="utf-8")
            updated = original
            for old, new in replacements:
                updated = updated.replace(old, new)
            if updated != original:
                self._atomic_write(path, updated)
                changed.append(path)
        return changed

    def write_creator(
        self,
        creator: CreatorRecord,
        works: list[CreatorWorkRecord],
        entries: list[EntryRecord],
        *,
        action: str,
        append_log: bool = True,
    ) -> list[Path]:
        root = self.vault_path / creator.folder_path
        for relative in (
            "sources",
            "raw/records",
            "raw/assets",
            "raw/images",
            "raw/covers",
            "concepts",
            "entities",
            "syntheses",
            ".data/sources",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        entries_by_id = {entry.id: entry for entry in entries}
        counts = {item.value: 0 for item in CreatorWorkDecision}
        for work in works:
            counts[work.decision.value] += 1
        pending_import_count = (
            counts[CreatorWorkDecision.PENDING.value] + counts[CreatorWorkDecision.SELECTED.value]
        )
        frontmatter = {
            "type": "douyin-creator",
            "creator_view_version": 2,
            "creator_id": creator.id,
            "sec_uid": creator.sec_uid,
            "nickname": creator.nickname,
            "douyin_id": creator.unique_id,
            "canonical_url": creator.canonical_url,
            "last_synced_at": creator.last_synced_at,
            "reported_work_count": creator.reported_work_count,
            "work_count": len(works),
            "pending_import_count": pending_import_count,
            "imported_count": counts[CreatorWorkDecision.IMPORTED.value],
            "not_imported_count": counts[CreatorWorkDecision.SKIPPED.value],
            "updated": creator.updated_at,
        }
        lines = [self._frontmatter(frontmatter), f"# {creator.nickname}"]
        avatar = creator.avatar_path
        if avatar:
            relative_avatar = os.path.relpath(avatar, start=creator.folder_path)
            lines.extend(["", f"![博主头像]({encode_markdown_path(relative_avatar)})"])
        lines.extend(
            [
                "",
                "## 博主信息",
                "",
                f"- [打开抖音主页]({creator.canonical_url})",
                f"- 抖音号：{creator.unique_id or '未提供'}",
                f"- 简介：{creator.signature or '未提供'}",
                "- 最近同步："
                + (
                    format_beijing(creator.last_synced_at) if creator.last_synced_at else "尚未同步"
                ),
                "",
                "## 灵感",
                "",
                *self._inspiration_lines(creator.inspirations),
            ]
        )

        def work_line(work: CreatorWorkRecord) -> str:
            date = beijing_date(work.published_at) if work.published_at else "日期未知"
            kind = "图文" if work.source_kind == SourceKind.IMAGE_NOTE else "视频"
            state = {
                CreatorWorkDecision.PENDING: "等待决定",
                CreatorWorkDecision.SELECTED: "入库处理中",
                CreatorWorkDecision.SKIPPED: "未入库",
                CreatorWorkDecision.IMPORTED: "已入库",
            }[work.decision]
            entry = entries_by_id.get(work.entry_id or "")
            if entry:
                target = Path(entry.source_path).with_suffix("")
                link = f"[[{target}|{work.title}]]"
            else:
                link = f"[{work.title}]({work.original_url})"
            return f"- {date} · {kind} · {state} · {link}"

        groups = (
            (
                "待入库",
                [
                    work
                    for work in works
                    if work.decision in {CreatorWorkDecision.PENDING, CreatorWorkDecision.SELECTED}
                ],
                "暂无待入库作品。",
            ),
            (
                "已入库",
                [work for work in works if work.decision == CreatorWorkDecision.IMPORTED],
                "暂无已入库作品。",
            ),
            (
                "未入库",
                [work for work in works if work.decision == CreatorWorkDecision.SKIPPED],
                "暂无未入库作品。",
            ),
        )
        for heading, group, empty_text in groups:
            lines.extend(["", f"## {heading}（{len(group)}）", ""])
            lines.extend(work_line(work) for work in group)
            if not group:
                lines.append(empty_text)

        index_path = root / "index.md"
        self._atomic_write(index_path, "\n".join(lines).rstrip() + "\n")
        payload = {
            "creator": creator.model_dump(mode="json"),
            "works": [work.model_dump(mode="json") for work in works],
        }
        machine = (
            self._frontmatter(
                {
                    "type": "creator-machine-data",
                    "creator_id": creator.id,
                    "updated": creator.updated_at,
                }
            )
            + f"# Machine Data · {creator.id}\n\n"
            + "```yaml\n"
            + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
            + "\n```\n"
        )
        machine_path = root / ".data" / "creator.md"
        self._atomic_write(machine_path, machine)
        log_path = root / "log.md"
        changed = [index_path, machine_path]
        if not log_path.exists():
            self._atomic_write(log_path, "---\ntype: creator-log\n---\n\n# 操作日志\n")
            changed.append(log_path)
        if append_log:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n## {format_beijing(utc_now())} · {action}\n\n"
                    f"- 作品：{len(works)}\n"
                    f"- 已入库：{counts[CreatorWorkDecision.IMPORTED.value]}\n"
                    f"- 未入库：{counts[CreatorWorkDecision.SKIPPED.value]}\n"
                    f"- 待入库：{pending_import_count}\n"
                )
            if log_path not in changed:
                changed.append(log_path)
        return changed

    def remove_external_validation_labels(self) -> list[Path]:
        path = self.vault_path / "AGENTS.md"
        if not path.exists():
            return []
        original = path.read_text(encoding="utf-8")
        updated = original.replace(
            "- 区分视频原话、AI 推断、用户灵感和外部核验；未核验主张必须标为 `unverified`。",
            "- 知识页只区分视频原话、AI 推断和用户灵感。",
        )
        if updated == original:
            return []
        self._atomic_write(path, updated)
        return [path]

    def refresh_source(self, entry: EntryRecord, data: dict[str, Any]) -> Path:
        path = self.vault_path / entry.source_path
        self._atomic_write(path, self._render_source(entry, data))
        return path

    def remove_entry_links(self, entry: EntryRecord, data: dict[str, Any]) -> list[Path]:
        """Remove system-managed concept/entity backlinks to a trashed source page."""
        creator_folder = str(data.get("creator", {}).get("folder_path") or "")
        root = self.vault_path / creator_folder if creator_folder else self.vault_path / "wiki"
        source_target = Path(entry.source_path).with_suffix("").as_posix()
        marker = f"[[{source_target}"
        changed: list[Path] = []
        for folder in ("concepts", "entities"):
            for path in (root / folder).glob("*.md"):
                original = path.read_text(encoding="utf-8")
                lines = [line for line in original.splitlines() if marker not in line]
                updated = "\n".join(lines).rstrip() + "\n"
                if updated != original:
                    self._atomic_write(path, updated)
                    changed.append(path)
        return changed

    def rebuild_index(self, entries: list[EntryRecord]) -> Path:
        now = beijing_date()
        lines = ["---", "type: index", f"updated: {now}", "---", "", "# 抖库", ""]
        creator_indexes = sorted((self.vault_path / "creators").glob("*/index.md"))
        legacy_entries = [
            entry for entry in entries if Path(entry.source_path).parts[:1] != ("creators",)
        ]
        if not legacy_entries and not creator_indexes:
            lines.append("暂无内容。")
        if creator_indexes:
            lines.extend(["## 博主资料", ""])
            for creator_index in creator_indexes:
                relative = creator_index.relative_to(self.vault_path).with_suffix("")
                frontmatter, _ = self._parse_document(creator_index)
                title = str(frontmatter.get("nickname") or creator_index.parent.name)
                lines.append(f"- [[{relative}|{title}]]")
            lines.append("")
        if legacy_entries:
            lines.extend(["## 作品资料", ""])
            for entry in sorted(legacy_entries, key=lambda item: item.created_at, reverse=True):
                source_no_suffix = str(Path(entry.source_path).with_suffix(""))
                status = "（已过期）" if entry.status == "stale" else ""
                lines.append(
                    f"- [[{source_no_suffix}|{entry.title}]]{status} — {entry.summary[:120]}"
                )
        path = self.vault_path / "index.md"
        self._atomic_write(path, "\n".join(lines).rstrip() + "\n")
        return path

    def append_log(self, action: str, title: str, summary: str, paths: list[Path]) -> Path:
        path = self.vault_path / "log.md"
        relative_paths = [str(item.relative_to(self.vault_path)) for item in paths]
        block = (
            f"\n## [{beijing_date()}] {action} | {title}\n\n"
            f"- 摘要：{summary}\n"
            f"- 修改文件：{', '.join(relative_paths)}\n"
            "- 开放问题：无\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)
        return path

    def write_maintenance_report(self, report: dict[str, Any]) -> Path:
        date = beijing_date()
        path = self.vault_path / "wiki" / "syntheses" / f"维护报告 {date}.md"
        content = [
            "---",
            "type: synthesis",
            f"status: {label_entry_status('active')}",
            f"created: {date}",
            f"updated: {date}",
            "tags: [维护报告]",
            "---",
            "",
            f"# 维护报告 {date}",
            "",
            "## 结果",
            "",
            f"- 标记过期片段：{report.get('stale_chunks', 0)}",
            f"- 清理媒体：{len(report.get('media_removed', []))}",
            f"- 孤立页面：{len(report.get('orphan_pages', []))}",
            "",
            "## 孤立页面",
            "",
        ]
        content.extend(f"- `{item}`" for item in report.get("orphan_pages", []))
        self._atomic_write(path, "\n".join(content).rstrip() + "\n")
        return path

    def write_topic(
        self,
        topic: ResearchTopic,
        artifacts: list[TopicArtifact],
        entries: dict[str, EntryRecord],
    ) -> list[Path]:
        topic_dir = self.vault_path / "topics" / topic.id
        index_path = topic_dir / "index.md"
        machine_path = topic_dir / ".data" / "topic.md"
        changed: list[Path] = []
        source_lines: list[str] = []
        for source in topic.sources:
            entry = entries.get(source.entry_id)
            if entry is None:
                continue
            target = Path(entry.source_path).with_suffix("").as_posix()
            state = "启用" if source.enabled else "停用"
            source_lines.append(
                f"{source.position}. [[{target}|{entry.title}]] · {state}"
            )
        artifact_lines = [
            f"- [[topics/{topic.id}/artifacts/{artifact.id}|{artifact.title}]]"
            f" · {'需要更新' if artifact.status == 'needs_update' else '当前版本'}"
            for artifact in artifacts
        ]
        index = self._frontmatter(
            {
                "type": "research_topic",
                "topic_id": topic.id,
                "title": topic.title,
                "source_revision": topic.source_revision,
                "created_at": topic.created_at,
                "updated_at": topic.updated_at,
            }
        )
        index += (
            f"\n# {topic.title}\n\n"
            f"## 研究目标\n\n{topic.goal or '未填写。'}\n\n"
            f"## 自定义指令\n\n{topic.instructions or '无。'}\n\n"
            "## 来源\n\n"
            + ("\n".join(source_lines) if source_lines else "暂无来源。")
            + "\n\n## 研究成果\n\n"
            + ("\n".join(artifact_lines) if artifact_lines else "尚未生成成果。")
            + "\n"
        )
        self._atomic_write(index_path, index)
        changed.append(index_path)

        machine = self._frontmatter(
            {
                "type": "research_topic_data",
                **topic.model_dump(mode="json"),
                "artifacts": [
                    artifact.model_dump(mode="json", exclude={"content_markdown"})
                    for artifact in artifacts
                ],
            }
        )
        machine += "\n# 专题机器数据\n\n此文件由抖库管理。\n"
        self._atomic_write(machine_path, machine)
        changed.append(machine_path)

        for artifact in artifacts:
            artifact_path = topic_dir / "artifacts" / f"{artifact.id}.md"
            artifact_document = self._frontmatter(
                {
                    "type": "topic_note" if artifact.user_authored else "topic_artifact",
                    "topic_id": topic.id,
                    "artifact_id": artifact.id,
                    "artifact_kind": artifact.kind,
                    "title": artifact.title,
                    "status": (
                        "需要更新" if artifact.status == "needs_update" else "当前版本"
                    ),
                    "source_revision": artifact.source_revision,
                    "source_revisions": [
                        item.model_dump(mode="json") for item in artifact.source_revisions
                    ],
                    "model": artifact.model,
                    "prompt_version": artifact.prompt_version,
                    "token_usage": {
                        "prompt": artifact.prompt_tokens,
                        "completion": artifact.completion_tokens,
                        "total": artifact.total_tokens,
                    },
                    "user_authored": artifact.user_authored,
                    "created_at": artifact.created_at,
                    "updated_at": artifact.updated_at,
                }
            )
            artifact_document += f"\n# {artifact.title}\n\n{artifact.content_markdown.rstrip()}\n"
            self._atomic_write(artifact_path, artifact_document)
            changed.append(artifact_path)
        return changed

    def write_topics_index(self, topics: list[ResearchTopic]) -> Path:
        path = self.vault_path / "topics" / "index.md"
        lines = [
            "---",
            "type: research_topics_index",
            f"updated_at: {beijing_iso(utc_now())}",
            "---",
            "",
            "# 专题",
            "",
        ]
        if topics:
            lines.extend(
                f"- [[topics/{topic.id}/index|{topic.title}]] · {len(topic.sources)} 个来源"
                for topic in topics
            )
        else:
            lines.append("暂无专题。")
        self._atomic_write(path, "\n".join(lines).rstrip() + "\n")
        return path

    def commit(self, paths: list[Path], message: str) -> bool:
        if not paths or not (self.vault_path / ".git").exists():
            return False
        relative = []
        for path in paths:
            try:
                relative.append(str(path.relative_to(self.vault_path)))
            except ValueError:
                continue
        if not relative:
            return False
        add = subprocess.run(
            ["git", "add", "--", *relative],
            cwd=self.vault_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if add.returncode != 0:
            return False
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.vault_path, check=False
        )
        if diff.returncode == 0:
            return False
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self.vault_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return commit.returncode == 0

    def load_entries(self) -> list[tuple[EntryRecord, dict[str, Any]]]:
        """Rehydrate the rebuildable knowledge cache from tracked Markdown files."""
        results: list[tuple[EntryRecord, dict[str, Any]]] = []
        self.last_entry_load_errors = []
        machine_paths = list((self.vault_path / "wiki" / ".data" / "sources").glob("*.md"))
        machine_paths.extend((self.vault_path / "creators").glob("*/.data/sources/*.md"))
        for machine_path in sorted(machine_paths):
            try:
                loaded = self._load_entry(machine_path)
            except Exception as exc:  # A user-edited sidecar must not block all rebuilds.
                self.last_entry_load_errors.append(
                    {
                        "path": str(machine_path.relative_to(self.vault_path)),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if loaded is not None:
                results.append(loaded)
        return results

    def _load_entry(self, machine_path: Path) -> tuple[EntryRecord, dict[str, Any]]:
        machine_frontmatter, machine_body = self._parse_document(machine_path)
        payload_match = re.search(r"```yaml\s*\n(?P<payload>.*?)\n```", machine_body, re.S)
        if not payload_match:
            raise ValueError("机器侧车缺少 YAML 数据块")
        payload = yaml.safe_load(payload_match.group("payload")) or {}
        if not isinstance(payload, dict):
            raise ValueError("机器侧车 YAML 顶层必须是对象")
        source_page = machine_frontmatter.get("source_page")
        if not source_page:
            raise ValueError("机器侧车缺少 source_page")
        source_path = self.vault_path / str(source_page)
        if not source_path.is_file():
            raise FileNotFoundError(f"资料页不存在：{source_page}")
        source_frontmatter, source_body = self._parse_document(source_path)
        analysis = AnalysisResult.model_validate(payload.get("analysis", {}))
        title_match = re.search(r"^#\s+(.+)$", source_body, re.M)
        title = title_match.group(1).strip() if title_match else analysis.title
        captured_at = parse_datetime(source_frontmatter.get("captured_at")) or utc_now()
        updated_at = parse_datetime(machine_frontmatter.get("updated")) or captured_at
        inspirations = [
            InspirationInput.model_validate(item)
            for item in source_frontmatter.get("inspirations", [])
        ]
        favorite = bool(source_frontmatter.get("favorite", False))
        retention = parse_retention(
            str(source_frontmatter.get("media_retention", RetentionPolicy.TEMPORARY.value))
        )
        if favorite:
            retention = RetentionPolicy.KEEP
        entry = EntryRecord(
            id=f"dy-{source_frontmatter['video_id']}",
            video_id=str(source_frontmatter["video_id"]),
            title=title,
            original_url=str(source_frontmatter.get("source_url") or ""),
            canonical_url=str(source_frontmatter.get("canonical_url") or ""),
            raw_path=str(source_frontmatter.get("source_path") or ""),
            source_path=str(source_page),
            status=parse_entry_status(str(source_frontmatter.get("status") or "active")),
            media_status=parse_media_status(
                str(source_frontmatter.get("media_status") or "present")
            ),
            retention=retention,
            favorite=favorite,
            media_expires_at=(
                None
                if favorite
                else parse_datetime(source_frontmatter.get("media_expires_at"))
            ),
            summary=analysis.one_liner,
            inspirations=inspirations,
            tags=[str(item) for item in source_frontmatter.get("tags", [])],
            created_at=captured_at,
            updated_at=updated_at,
        )
        provenance = payload.get("model_provenance", {})
        data = {
            "share_text": payload.get("share_text", ""),
            "inspirations": [item.model_dump(mode="json") for item in inspirations],
            "metadata": payload.get("metadata", {}),
            "ocr": payload.get("ocr", []),
            "review_issues": payload.get("review_issues", []),
            "relations": payload.get("relations", []),
            "creator": payload.get("creator", {}),
            "analysis": analysis.model_dump(mode="json"),
            "provider": provenance.get("provider"),
            "model": provenance.get("model"),
            "prompt_version": provenance.get("prompt_version"),
            "cover_path": source_frontmatter.get("cover_image"),
            "cover_kind": source_frontmatter.get("cover_kind"),
            "reminder_states": payload.get("reminder_states", []),
        }
        if machine_frontmatter.get("source_kind") == SourceKind.VIDEO.value:
            data["transcript_raw"] = payload.get("transcript_raw", [])
            data["transcript_corrected"] = payload.get("transcript_corrected", [])
        return entry, data

    def load_creators(self) -> list[tuple[CreatorRecord, list[CreatorWorkRecord]]]:
        """Load creator decisions from the tracked hidden creator sidecars."""
        results: list[tuple[CreatorRecord, list[CreatorWorkRecord]]] = []
        machine_paths = sorted((self.vault_path / "creators").glob("*/.data/creator.md"))
        for machine_path in machine_paths:
            _, machine_body = self._parse_document(machine_path)
            payload_match = re.search(r"```yaml\s*\n(?P<payload>.*?)\n```", machine_body, re.S)
            if not payload_match:
                continue
            payload = yaml.safe_load(payload_match.group("payload")) or {}
            if not isinstance(payload, dict) or not isinstance(payload.get("creator"), dict):
                continue
            try:
                creator = CreatorRecord.model_validate(payload["creator"])
                works = [
                    CreatorWorkRecord.model_validate(item)
                    for item in payload.get("works", [])
                    if isinstance(item, dict)
                ]
            except (TypeError, ValueError):
                continue
            results.append((creator, works))
        return results

    def load_topics(self) -> list[tuple[ResearchTopic, list[TopicArtifact]]]:
        """Load research topics and versioned artifacts from tracked Markdown."""
        results: list[tuple[ResearchTopic, list[TopicArtifact]]] = []
        machine_paths = sorted((self.vault_path / "topics").glob("*/.data/topic.md"))
        for machine_path in machine_paths:
            frontmatter, _ = self._parse_document(machine_path)
            try:
                topic = ResearchTopic.model_validate(frontmatter)
            except (TypeError, ValueError):
                continue
            artifacts: list[TopicArtifact] = []
            for metadata in frontmatter.get("artifacts", []):
                if not isinstance(metadata, dict) or not metadata.get("id"):
                    continue
                artifact_path = (
                    machine_path.parent.parent
                    / "artifacts"
                    / f"{metadata['id']}.md"
                )
                if not artifact_path.is_file():
                    continue
                _, body = self._parse_document(artifact_path)
                content = re.sub(r"^\s*#\s+.*?\n+", "", body, count=1).rstrip()
                try:
                    artifacts.append(
                        TopicArtifact.model_validate(
                            {**metadata, "content_markdown": content}
                        )
                    )
                except (TypeError, ValueError):
                    continue
            results.append((topic, artifacts))
        return results

    def _ensure_git(self) -> None:
        if not (self.vault_path / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=self.vault_path,
                capture_output=True,
                check=False,
            )
        subprocess.run(["git", "config", "user.name", "抖库"], cwd=self.vault_path, check=False)
        subprocess.run(
            ["git", "config", "user.email", "douyin-wiki@local"],
            cwd=self.vault_path,
            check=False,
        )

    def _render_raw(self, entry: EntryRecord, data: dict[str, Any]) -> str:
        metadata = data["metadata"]
        if metadata.get("source_kind") == SourceKind.IMAGE_NOTE.value:
            return self._render_raw_image_note(entry, data)
        frontmatter = {
            "type": "raw-video",
            "status": label_entry_status("raw"),
            "video_id": entry.video_id,
            "title": entry.title,
            "author": metadata.get("author"),
            "published_at": metadata.get("published_at"),
            "captured_at": beijing_iso(entry.created_at),
            "original_url": entry.original_url,
            "canonical_url": entry.canonical_url,
            "media_retention": label_retention(entry.retention),
            "media_expires_at": beijing_iso(entry.media_expires_at)
            if entry.media_expires_at
            else None,
            "model_provider": data.get("provider"),
            "model": data.get("model"),
            "prompt_version": data.get("prompt_version"),
        }
        lines = [self._frontmatter(frontmatter), f"# {entry.title}", "", "## 原始分享文本", ""]
        lines.extend(["> " + line for line in data.get("share_text", "").splitlines()])
        lines.extend(["", "## 采集时灵感（逐字保留）", ""])
        lines.extend(self._inspiration_lines(entry.inspirations))
        lines.extend(["", "## 原始逐字稿", ""])
        lines.extend(self._transcript_lines(data.get("transcript_raw", [])))
        lines.extend(["", "## 校正逐字稿", ""])
        lines.extend(self._transcript_lines(data.get("transcript_corrected", [])))
        lines.extend(["", "## 画面 OCR", ""])
        for item in data.get("ocr", []):
            lines.append(f"- [{format_timestamp(item.get('timestamp_ms'))}] {item.get('text', '')}")
        lines.extend(["", "## 校对记录", ""])
        for item in data.get("review_issues", []):
            resolution = item.get("resolution") or "未人工修正"
            lines.append(
                f"- [{format_timestamp(item.get('start_ms'))}] `{item.get('raw_text', '')}` → "
                f"{resolution}（{item.get('reason', '')}）"
            )
        lines.extend(["", "## 来源", "", f"- [打开原视频]({entry.original_url})"])
        return "\n".join(lines).rstrip() + "\n"

    def _render_raw_image_note(self, entry: EntryRecord, data: dict[str, Any]) -> str:
        metadata = data["metadata"]
        frontmatter = {
            "type": "raw-image-note",
            "source_kind": SourceKind.IMAGE_NOTE.value,
            "status": label_entry_status("raw"),
            "video_id": entry.video_id,
            "title": entry.title,
            "author": metadata.get("author"),
            "published_at": metadata.get("published_at"),
            "captured_at": beijing_iso(entry.created_at),
            "original_url": entry.original_url,
            "canonical_url": entry.canonical_url,
            "image_paths": metadata.get("image_paths", []),
            "music_metadata": metadata.get("music_metadata"),
            "model_provider": data.get("provider"),
            "model": data.get("model"),
            "prompt_version": data.get("prompt_version"),
        }
        lines = [self._frontmatter(frontmatter), f"# {entry.title}", "", "## 原始分享文本", ""]
        lines.extend("> " + line for line in data.get("share_text", "").splitlines())
        lines.extend(["", "## 采集时灵感（逐字保留）", ""])
        lines.extend(self._inspiration_lines(entry.inspirations))
        lines.extend(["", "## 作品正文", "", metadata.get("post_text") or "无正文。"])
        lines.extend(["", "## 原图清单", ""])
        for index, image_path in enumerate(metadata.get("image_paths", []), start=1):
            relative = os.path.relpath(image_path, start=Path(entry.raw_path).parent)
            lines.append(
                f"- [第 {index} 张图片]({encode_markdown_path(Path(relative).as_posix())})"
            )
        lines.extend(["", "## 逐图 OCR", ""])
        for item in data.get("ocr", []):
            image_index = item.get("image_index") or "?"
            lines.append(f"- 第 {image_index} 张：{item.get('text', '')}")
        if data.get("review_issues"):
            lines.extend(["", "## 校对记录", ""])
            for item in data["review_issues"]:
                resolution = item.get("resolution") or "未人工修正"
                lines.append(
                    f"- 第 {item.get('image_index') or '?'} 张："
                    f"`{item.get('raw_text', '')}` → {resolution}（{item.get('reason', '')}）"
                )
        lines.extend(["", "## 来源", "", f"- [打开原作品]({entry.original_url})"])
        return "\n".join(lines).rstrip() + "\n"

    def _render_source(self, entry: EntryRecord, data: dict[str, Any]) -> str:
        analysis = AnalysisResult.model_validate(data["analysis"])
        metadata = data.get("metadata", {})
        source_kind = metadata.get("source_kind", SourceKind.VIDEO.value)
        is_image_note = source_kind == SourceKind.IMAGE_NOTE.value
        cover_path = data.get("cover_path")
        creator_folder = str(data.get("creator", {}).get("folder_path") or "")
        machine_data_path = (
            str(Path(creator_folder) / ".data" / "sources" / f"{entry.video_id}.md")
            if creator_folder
            else f"wiki/.data/sources/{entry.video_id}.md"
        )
        frontmatter = {
            "type": "source",
            "status": label_entry_status(entry.status),
            "video_id": entry.video_id,
            "source_kind": source_kind,
            "author": metadata.get("author"),
            "published_at": metadata.get("published_at"),
            "captured_at": beijing_iso(entry.created_at),
            "created": beijing_date(entry.created_at),
            "updated": beijing_date(entry.updated_at),
            "source_path": entry.raw_path,
            "source_url": entry.original_url,
            "canonical_url": entry.canonical_url,
            "media_status": label_media_status(entry.media_status),
            "media_retention": label_retention(entry.retention),
            "favorite": entry.favorite,
            "media_expires_at": beijing_iso(entry.media_expires_at)
            if entry.media_expires_at
            else None,
            "inspirations": [
                inspiration.model_dump(mode="json") for inspiration in entry.inspirations
            ],
            "tags": entry.tags,
            "model_provider": data.get("provider"),
            "model": data.get("model"),
            "prompt_version": data.get("prompt_version"),
            "cover_image": cover_path,
            "cover_kind": data.get("cover_kind"),
            "image_paths": metadata.get("image_paths", []),
            "analysis_version": 2,
            "content_type": analysis.content_type,
            "facets": analysis.facets,
            "machine_data_path": machine_data_path,
            "creator_id": data.get("creator", {}).get("id"),
        }
        lines = [self._frontmatter(frontmatter), f"# {entry.title}"]
        if cover_path:
            relative_cover = os.path.relpath(
                cover_path,
                start=Path(entry.source_path).parent,
            )
            alt = "抖音图文第 1 张" if is_image_note else "抖音视频封面"
            lines.extend(
                [
                    "",
                    f"![{alt}]({encode_markdown_path(Path(relative_cover).as_posix())})",
                ]
            )
        raw_relative = os.path.relpath(entry.raw_path, start=Path(entry.source_path).parent)
        lines.extend(
            [
                "",
                "## 灵感",
                "",
                *self._inspiration_lines(entry.inspirations),
                "",
                "## 一句话",
                "",
                analysis.one_liner,
            ]
        )
        if analysis.relevance_to_inspiration:
            lines.extend(
                ["", "> [!note] AI 推断：与灵感的关系", f"> {analysis.relevance_to_inspiration}"]
            )
        if analysis.takeaways:
            lines.extend(["", "## 核心收获", ""])
            lines.extend(f"- {value}" for value in analysis.takeaways[:5])

        card_lines = self._content_card_lines(
            user_times_to_beijing(analysis.content_card.model_dump(mode="json"))
        )
        if is_image_note and card_lines:
            card_heading = f"## 内容卡片 · {self._content_type_label(analysis.content_type)}"
            lines.extend(["", card_heading, ""])
            lines.extend(card_lines)

        remaining_images = metadata.get("image_paths", [])[1:] if is_image_note else []
        if remaining_images:
            lines.extend(["", "## 原图", ""])
            for index, image_path in enumerate(remaining_images, start=2):
                relative = os.path.relpath(image_path, start=Path(entry.source_path).parent)
                lines.append(
                    f"![抖音图文第 {index} 张]({encode_markdown_path(Path(relative).as_posix())})"
                )

        if analysis.chapters:
            lines.extend(["", "## 时间轴图解", ""])
            chapter_numbers = [
                "一",
                "二",
                "三",
                "四",
                "五",
                "六",
                "七",
                "八",
                "九",
                "十",
                "十一",
                "十二",
            ]
            for index, chapter in enumerate(analysis.chapters[:12]):
                timestamp = format_timestamp(chapter.start_ms)
                number = chapter_numbers[index]
                lines.extend(
                    [
                        f"### ▶ {timestamp}　{number}、{chapter.title}",
                        "",
                        chapter.summary,
                    ]
                )
                if chapter.key_points:
                    lines.append("")
                    lines.extend(f"- {point}" for point in chapter.key_points)
                if chapter.comparison_table is not None:
                    lines.extend(
                        [
                            "",
                            *self._chapter_table_lines(
                                chapter.comparison_table.headers,
                                chapter.comparison_table.rows,
                            ),
                        ]
                    )
                lines.append("")

        if not is_image_note and card_lines:
            card_heading = f"## 内容卡片 · {self._content_type_label(analysis.content_type)}"
            lines.extend(["", card_heading, ""])
            lines.extend(card_lines)

        if analysis.actions or analysis.reminders:
            lines.extend(["", "## 下一步", ""])
            lines.extend(f"- {value}" for value in analysis.actions[:3])
            for reminder in analysis.reminders[:3]:
                due = format_beijing(reminder.due_at) if reminder.due_at else "时间待澄清"
                lines.append(f"- 提醒候选：{reminder.title} — {due}")
        lines.extend(
            [
                "",
                "## 来源",
                "",
                f"- [原始采集记录]({encode_markdown_path(raw_relative)})",
                f"- [打开原作品]({entry.original_url})",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _render_machine(self, entry: EntryRecord, data: dict[str, Any]) -> str:
        analysis = AnalysisResult.model_validate(data["analysis"])
        payload = {
            "analysis": analysis.model_dump(mode="json"),
            "share_text": data.get("share_text", ""),
            "inspirations_verbatim": [item.model_dump(mode="json") for item in entry.inspirations],
            "metadata": data.get("metadata", {}),
            "ocr": data.get("ocr", []),
            "review_issues": data.get("review_issues", []),
            "relations": data.get("relations", []),
            "creator": data.get("creator", {}),
            "reminder_states": data.get("reminder_states", []),
            "model_provenance": {
                "provider": data.get("provider"),
                "model": data.get("model"),
                "prompt_version": data.get("prompt_version"),
            },
        }
        source_kind = data.get("metadata", {}).get("source_kind", SourceKind.VIDEO.value)
        if source_kind == SourceKind.VIDEO.value:
            payload["transcript_raw"] = data.get("transcript_raw", [])
            payload["transcript_corrected"] = data.get("transcript_corrected", [])
        frontmatter = {
            "type": "source-machine-data",
            "status": entry.status,
            "video_id": entry.video_id,
            "source_kind": source_kind,
            "analysis_version": 2,
            "content_type": analysis.content_type,
            "facets": analysis.facets,
            "source_page": entry.source_path,
            "updated": beijing_iso(entry.updated_at),
        }
        serialized = yaml.safe_dump(
            payload, allow_unicode=True, sort_keys=False, default_flow_style=False
        ).rstrip()
        return (
            f"{self._frontmatter(frontmatter)}# Machine Data · {entry.video_id}\n\n"
            "此文件由抖库管理，供 Agent 和索引器读取。\n\n"
            f"```yaml\n{serialized}\n```\n"
        )

    @staticmethod
    def _content_type_label(value: str) -> str:
        return {
            "tutorial": "教程",
            "explanation": "知识解释",
            "opinion": "观点",
            "recommendation": "推荐",
            "news_event": "新闻/事件",
            "story_case": "故事/案例",
            "collection": "清单",
            "other": "其他",
        }.get(value, "其他")

    @staticmethod
    def _content_card_lines(card: dict[str, Any]) -> list[str]:
        kind = card.get("kind", "other")
        labels = {
            "tutorial": {
                "goal": "目标",
                "prerequisites": "前置",
                "parameters": "参数",
                "steps": "步骤",
                "pitfalls": "易错点",
            },
            "explanation": {
                "question": "问题",
                "concepts": "概念",
                "mechanism": "机制",
                "examples": "例子",
            },
            "opinion": {
                "thesis": "结论",
                "reasons": "理由",
                "assumptions": "前提",
                "counterpoints": "反方观点",
            },
            "recommendation": {
                "subjects": "对象",
                "criteria": "标准",
                "pros": "优点",
                "cons": "缺点",
                "best_for": "适合",
            },
            "news_event": {
                "event": "事件",
                "absolute_time": "时间",
                "impact": "影响",
                "actions": "行动",
                "valid_until": "有效期",
            },
            "story_case": {
                "context": "背景",
                "turning_points": "转折",
                "outcome": "结果",
                "lessons": "经验",
            },
            "collection": {"items": "项目"},
            "other": {"notes": "要点"},
        }.get(kind, {})
        lines: list[str] = []
        for key, label in labels.items():
            value = card.get(key)
            if not value:
                continue
            if key == "items":
                rendered = "; ".join(
                    f"{item.get('name', '')}（"
                    f"{'、'.join(item.get('traits', []) + item.get('scenarios', []))}）"
                    for item in value
                )
            elif isinstance(value, list):
                rendered = "；".join(str(item) for item in value)
            else:
                rendered = str(value)
            lines.append(f"- **{label}**：{rendered}")
        return lines

    @staticmethod
    def _chapter_table_lines(headers: list[str], rows: list[list[str]]) -> list[str]:
        def cell(value: str) -> str:
            return str(value).replace("|", "\\|").replace("\n", "<br>")

        rendered_headers = [cell(value) for value in headers]
        lines = [
            "| " + " | ".join(rendered_headers) + " |",
            "| " + " | ".join("---" for _ in rendered_headers) + " |",
        ]
        lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
        return lines

    def _ensure_link_page(
        self,
        path: Path,
        page_type: str,
        title: str,
        entry: EntryRecord,
        description: str = "",
    ) -> bool:
        source_link = f"[[{Path(entry.source_path).with_suffix('')}|{entry.title}]]"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if source_link in content:
                return False
            self._atomic_write(path, content.rstrip() + f"\n- {source_link}\n")
            return True
        today = beijing_date()
        content = (
            f"---\ntype: {page_type}\nstatus: {label_entry_status('active')}\ncreated: {today}\n"
            f"updated: {today}\ntags: []\n---\n\n"
            f"# {title}\n\n{description}\n\n## 来源\n\n- {source_link}\n"
        )
        self._atomic_write(path, content)
        return True

    @staticmethod
    def _inspiration_lines(inspirations: list[InspirationInput]) -> list[str]:
        if not inspirations:
            return ["- 未填写；AI 不推测用户灵感。"]
        lines = []
        for inspiration in inspirations:
            suffix = ""
            if inspiration.start_ms is not None:
                suffix = f"（{format_timestamp(inspiration.start_ms)}"
                if inspiration.end_ms is not None:
                    suffix += f"–{format_timestamp(inspiration.end_ms)}"
                suffix += "）"
            lines.append(f"- {inspiration.text}{suffix}")
            if inspiration.quote:
                lines.append(f"  - 指定原句：{inspiration.quote}")
        return lines

    @staticmethod
    def _transcript_lines(segments: list[dict[str, Any]]) -> list[str]:
        if not segments:
            return ["无可用逐字稿。"]
        return [
            f"[{format_timestamp(segment.get('start_ms'))}] {segment.get('text', '')}"
            for segment in segments
        ]

    @staticmethod
    def _frontmatter(data: dict[str, Any]) -> str:
        dumped = yaml.safe_dump(
            user_times_to_beijing(data),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        return f"---\n{dumped}\n---\n"

    @staticmethod
    def _parse_document(path: Path) -> tuple[dict[str, Any], str]:
        content = path.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)$", content, re.S)
        if not match:
            return {}, content
        frontmatter = yaml.safe_load(match.group("frontmatter")) or {}
        return frontmatter, match.group("body")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            temporary = Path(temporary_name)
            if temporary.exists():
                temporary.unlink()
