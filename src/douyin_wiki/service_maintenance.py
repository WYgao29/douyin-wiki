from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from send2trash import send2trash

from .adapters.media import (
    download_preferred_cover,
)
from .errors import (
    JobStateError,
)
from .models import (
    EntryRecord,
    ReminderCandidate,
    ResearchTopic,
    SourceKind,
    VideoMetadata,
)
from .time_utils import beijing_date


class MaintenanceMixin:
    def backfill_video_covers(self) -> dict[str, Any]:
        with self.vault.entry_operations_locked():
            return self._backfill_video_covers_locked()

    def _backfill_video_covers_locked(self) -> dict[str, Any]:
        changed: list[Path] = []
        updated_entries: list[str] = []
        with self.vault.locked():
            for entry in self.database.list_entries():
                data = self.database.get_entry_data(entry.id)
                metadata = dict(data.get("metadata", {}))
                creator_folder = str(data.get("creator", {}).get("folder_path") or "")
                assets_dir = (
                    self.config.vault_path / creator_folder / "raw" / "assets" / entry.video_id
                    if creator_folder
                    else self.config.vault_path / "raw" / "assets" / entry.video_id
                )
                preferred_cover: Path | None = None
                if data.get("cover_kind") != "douyin_cover":
                    info_path = assets_dir / "original.info.json"
                    try:
                        info = json.loads(info_path.read_text(encoding="utf-8"))
                        preferred_cover = download_preferred_cover(info, info_path.parent)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        preferred_cover = None
                    if preferred_cover:
                        metadata["thumbnail_path"] = str(preferred_cover)
                        metadata["thumbnail_kind"] = "douyin_cover"
                cover_path = self.persist_video_cover(
                    entry.video_id,
                    assets_dir,
                    thumbnail_path=str(preferred_cover)
                    if preferred_cover
                    else metadata.get("thumbnail_path"),
                    replace_existing=preferred_cover is not None,
                    creator_folder=creator_folder or None,
                )
                if not cover_path:
                    continue
                source = self.config.vault_path / entry.source_path
                source_has_cover = source.exists() and "![抖音视频封面]" in source.read_text(
                    encoding="utf-8"
                )
                cover_kind = (
                    "douyin_cover" if preferred_cover else data.get("cover_kind") or "fallback"
                )
                if (
                    data.get("cover_path") == cover_path
                    and data.get("cover_kind") == cover_kind
                    and source_has_cover
                ):
                    continue
                data["cover_path"] = cover_path
                data["cover_kind"] = cover_kind
                data["metadata"] = metadata
                self.database.upsert_entry(entry, data)
                changed.extend(self.vault.write_entry(entry, data).changed_paths)
                updated_entries.append(entry.id)
            if changed:
                log = self.vault.append_log(
                    "schema",
                    "回填视频首页截图",
                    f"为 {len(updated_entries)} 条资料保存并嵌入视频封面",
                    changed,
                )
                changed.append(log)
                self.vault.commit(changed, "schema: add video cover images")
        return {
            "status": "completed",
            "updated_entries": updated_entries,
            "changed_files": [str(path.relative_to(self.config.vault_path)) for path in changed],
        }

    def _entry_documents_intact(self, entry: EntryRecord) -> bool:
        data = self.database.get_entry_data(entry.id)
        creator_folder = str(data.get("creator", {}).get("folder_path") or "")
        machine_path = (
            self.config.vault_path / creator_folder / ".data" / "sources" / f"{entry.video_id}.md"
            if creator_folder
            else self.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
        )
        return all(
            path.is_file() and path.stat().st_size > 0
            for path in (
                self.config.vault_path / entry.raw_path,
                self.config.vault_path / entry.source_path,
                machine_path,
            )
        )

    def _repair_entry_if_needed(self, entry: EntryRecord) -> EntryRecord:
        with self.vault.entry_operations_locked():
            return self.repair_entry_if_needed_locked(entry)

    def repair_entry_if_needed_locked(self, entry: EntryRecord) -> EntryRecord:
        # Re-read under the operation lock so a concurrent delete cannot be
        # undone by a stale worker repair.
        entry = self.database.get_entry(entry.id)
        if self._entry_documents_intact(entry) and self.database.entry_chunk_count(entry.id) > 0:
            return entry
        data = self.database.get_entry_data(entry.id)
        chunks, relations, reminders = self.prepare_entry_bundle(entry, data)
        self.write_entry_documents(
            entry,
            data,
            action="repair",
            log_summary="修复不完整的 Markdown 或 SQLite 投影",
            commit_message=f"repair: {entry.video_id} {entry.title}",
        )
        return self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)

    def rebuild_database_from_vault(self, *, apply: bool = False) -> dict[str, Any]:
        if apply:
            with self.vault.entry_operations_locked():
                return self._rebuild_database_from_vault_locked(apply=True)
        return self._rebuild_database_from_vault_locked(apply=False)

    def _rebuild_database_from_vault_locked(self, *, apply: bool) -> dict[str, Any]:
        loaded = self.vault.load_entries()
        loaded_creators = self.vault.load_creators()
        loaded_topics = self.vault.load_topics()
        entry_ids = [entry.id for entry, _ in loaded]
        entry_id_set = set(entry_ids)

        def entry_sidecar_path(entry: EntryRecord, data: dict[str, Any]) -> Path:
            creator_folder = str(data.get("creator", {}).get("folder_path") or "")
            return self.config.vault_path / (
                Path(creator_folder) / ".data" / "sources" / f"{entry.video_id}.md"
                if creator_folder
                else Path("wiki") / ".data" / "sources" / f"{entry.video_id}.md"
            )

        def creator_sidecar_path(creator) -> Path:
            return self.config.vault_path / creator.folder_path / ".data" / "creator.md"

        def topic_sidecar_path(topic: ResearchTopic) -> Path:
            return self.config.vault_path / "topics" / topic.id / ".data" / "topic.md"

        def append_error(collection: list[dict[str, str]], path: Path, message: str) -> None:
            self.vault._append_load_error(collection, path, ValueError(message))

        for index, entry_id in enumerate(entry_ids):
            if entry_id in entry_ids[:index]:
                entry, data = loaded[index]
                append_error(
                    self.vault.last_entry_load_errors,
                    entry_sidecar_path(entry, data),
                    f"重复 entry.id：{entry_id}",
                )

        prepared: list[
            tuple[
                EntryRecord,
                dict[str, Any],
                list[dict[str, Any]],
                list[dict[str, Any]],
                list[ReminderCandidate],
            ]
        ] = []
        for entry, data in loaded:
            sidecar_path = entry_sidecar_path(entry, data)
            try:
                chunks = self.indexer.index_entry(entry, data, persist=False)
            except Exception as exc:
                self.vault._append_load_error(self.vault.last_entry_load_errors, sidecar_path, exc)
                chunks = []

            raw_relations = data.get("relations", [])
            relations: list[dict[str, Any]] = []
            if not isinstance(raw_relations, list):
                append_error(
                    self.vault.last_entry_load_errors,
                    sidecar_path,
                    "relations 必须是数组",
                )
            else:
                for relation in raw_relations:
                    if not isinstance(relation, dict):
                        append_error(
                            self.vault.last_entry_load_errors,
                            sidecar_path,
                            "relations 中每项必须是对象",
                        )
                        continue
                    target_id = relation.get("target_entry_id")
                    if target_id not in entry_id_set or target_id == entry.id:
                        append_error(
                            self.vault.last_entry_load_errors,
                            sidecar_path,
                            f"关系目标不存在：{target_id}",
                        )
                        continue
                    try:
                        confidence = float(relation.get("confidence", 0))
                    except (TypeError, ValueError) as exc:
                        self.vault._append_load_error(
                            self.vault.last_entry_load_errors, sidecar_path, exc
                        )
                        continue
                    if not 0 <= confidence <= 1:
                        append_error(
                            self.vault.last_entry_load_errors,
                            sidecar_path,
                            f"关系 confidence 超出范围：{confidence}",
                        )
                        continue
                    relations.append(relation)

            reminders: list[ReminderCandidate] = []
            raw_reminders = data.get("analysis", {}).get("reminders", [])
            if not isinstance(raw_reminders, list):
                append_error(
                    self.vault.last_entry_load_errors,
                    sidecar_path,
                    "analysis.reminders 必须是数组",
                )
            else:
                for reminder in raw_reminders:
                    try:
                        candidate = ReminderCandidate.model_validate(reminder)
                        self.vault._safe_id(candidate.id, field="reminder.id")
                        reminders.append(candidate)
                    except Exception as exc:
                        self.vault._append_load_error(
                            self.vault.last_entry_load_errors, sidecar_path, exc
                        )
            raw_reminder_states = data.get("reminder_states", [])
            if not isinstance(raw_reminder_states, list):
                append_error(
                    self.vault.last_entry_load_errors,
                    sidecar_path,
                    "reminder_states 必须是数组",
                )
            else:
                for state in raw_reminder_states:
                    if not isinstance(state, dict) or not isinstance(state.get("id"), str):
                        append_error(
                            self.vault.last_entry_load_errors,
                            sidecar_path,
                            "reminder_states 中每项必须包含字符串 id",
                        )
                    elif state.get("status") not in {"candidate", "creating", "created"}:
                        append_error(
                            self.vault.last_entry_load_errors,
                            sidecar_path,
                            f"reminder 状态无效：{state.get('status')}",
                        )
                    else:
                        try:
                            self.vault._safe_id(state["id"], field="reminder_state.id")
                        except Exception as exc:
                            self.vault._append_load_error(
                                self.vault.last_entry_load_errors, sidecar_path, exc
                            )
            prepared.append((entry, data, chunks, relations, reminders))

        for creator, works in loaded_creators:
            sidecar_path = creator_sidecar_path(creator)
            for work in works:
                if work.entry_id and work.entry_id not in entry_id_set:
                    append_error(
                        self.vault.last_creator_load_errors,
                        sidecar_path,
                        f"作品关联资料不存在：{work.entry_id}",
                    )

        for topic, artifacts in loaded_topics:
            sidecar_path = topic_sidecar_path(topic)
            for source in topic.sources:
                if source.entry_id not in entry_id_set:
                    append_error(
                        self.vault.last_topic_load_errors,
                        sidecar_path,
                        f"专题来源资料不存在：{source.entry_id}",
                    )
            for artifact in artifacts:
                artifact_path = (
                    self.config.vault_path / "topics" / topic.id / "artifacts" / f"{artifact.id}.md"
                )
                if artifact.topic_id != topic.id:
                    append_error(
                        self.vault.last_artifact_load_errors,
                        artifact_path,
                        "artifact.topic_id 与 topic.id 不一致",
                    )
                for revision in artifact.source_revisions:
                    if revision.entry_id not in entry_id_set:
                        append_error(
                            self.vault.last_artifact_load_errors,
                            artifact_path,
                            f"成果关联资料不存在：{revision.entry_id}",
                        )

        report = {
            "dry_run": not apply,
            "entry_count": len(loaded),
            "entry_ids": entry_ids,
            "creator_count": len(loaded_creators),
            "creator_ids": [creator.id for creator, _ in loaded_creators],
            "topic_count": len(loaded_topics),
            "topic_ids": [topic.id for topic, _ in loaded_topics],
            "entry_errors": list(self.vault.last_entry_load_errors),
            "creator_errors": list(self.vault.last_creator_load_errors),
            "topic_errors": list(self.vault.last_topic_load_errors),
            "artifact_errors": list(self.vault.last_artifact_load_errors),
        }
        embedding_signature = self.indexer.embeddings.signature()
        if (
            self.vault.last_entry_load_errors
            or self.vault.last_creator_load_errors
            or self.vault.last_topic_load_errors
            or self.vault.last_artifact_load_errors
        ):
            if not apply:
                return report
            raise JobStateError(
                "Vault 中存在无法解析的资料；请根据 dry-run 的 entry_errors、"
                "creator_errors、topic_errors、artifact_errors 修复后再重建"
            )
        if not apply:
            return report
        if self.database.has_unfinished_creator_jobs():
            raise JobStateError("存在未完成的博主清点或批量任务，完成后再重建 SQLite")
        self.database.replace_knowledge_cache(
            entries=prepared,
            creators=loaded_creators,
            topics=loaded_topics,
            embedding_signature=embedding_signature,
        )
        return {**report, "dry_run": False, "status": "rebuilt"}

    def run_maintenance(self, *, apply: bool = False) -> dict[str, Any]:
        if apply:
            with self.vault.entry_operations_locked():
                return self._run_maintenance_locked(apply=True)
        return self._run_maintenance_locked(apply=False)

    def _run_maintenance_locked(self, *, apply: bool) -> dict[str, Any]:
        expired = self.database.entries_with_expired_media()
        report: dict[str, Any] = {
            "dry_run": not apply,
            "stale_chunks": 0,
            "media_candidates": [entry.id for entry in expired],
            "media_removed": [],
            "orphan_pages": self._find_orphan_pages(),
            "relation_updates": [],
        }
        changed: list[Path] = []
        if apply:
            report["stale_chunks"], stale_entries = self.database.mark_stale_claims()
            with self.vault.locked():
                for entry_id in stale_entries:
                    stale_entry = self.database.get_entry(entry_id)
                    stale_data = self.database.get_entry_data(entry_id)
                    changed.extend(self.vault.write_entry(stale_entry, stale_data).changed_paths)
                for entry in self.database.list_entries():
                    data = self.database.get_entry_data(entry.id)
                    previous_relations = json.dumps(
                        data.get("relations", []), ensure_ascii=False, sort_keys=True
                    )
                    chunks, relations, reminders = self.prepare_entry_bundle(entry, data)
                    current_relations = json.dumps(relations, ensure_ascii=False, sort_keys=True)
                    if current_relations == previous_relations:
                        continue
                    written = self.vault.write_entry(entry, data)
                    changed.extend(written.changed_paths)
                    self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
                    report["relation_updates"].append(entry.id)
                for entry in expired:
                    self.trash_assets(entry)
                    report["media_removed"].append(entry.id)
                    refreshed = self.database.get_entry(entry.id)
                    data = self.database.get_entry_data(entry.id)
                    changed.extend(self.vault.write_entry(refreshed, data).changed_paths)
                report_path = self.vault.write_maintenance_report(report)
                index = self.vault.rebuild_index(self.database.list_entries())
                log = self.vault.append_log(
                    "maintenance",
                    "每周健康检查",
                    "检查关联、过期与媒体保留",
                    [report_path, index],
                )
                changed.extend([report_path, index, log])
                changed = list(dict.fromkeys(changed))
                self.vault.commit(changed, f"maintenance: {beijing_date()}")
            self.database.record_maintenance("weekly", report)
        return report

    def trash_assets(self, entry: EntryRecord, *, mark_database: bool = True) -> None:
        raw_parent = Path(entry.raw_path).parent
        raw_root = raw_parent.parent if raw_parent.name == "records" else raw_parent
        assets = self.config.vault_path / raw_root / "assets" / entry.video_id
        if assets.exists():
            send2trash(str(assets))
        if mark_database:
            self.database.mark_media_removed(entry.id)

    def metadata_image_paths_exist(self, metadata: VideoMetadata) -> bool:
        return bool(metadata.image_paths) and all(
            (path := self.vault_path(value)).is_file() and path.stat().st_size > 0
            for value in metadata.image_paths
        )

    def image_note_files_intact(self, data: dict[str, Any]) -> bool:
        metadata_data = data.get("metadata", {})
        if metadata_data.get("source_kind") != SourceKind.IMAGE_NOTE.value:
            return False
        try:
            return self.metadata_image_paths_exist(VideoMetadata.model_validate(metadata_data))
        except (TypeError, ValueError):
            return False

    def _find_orphan_pages(self) -> list[str]:
        wiki = self.config.vault_path / "wiki"
        creators_root = self.config.vault_path / "creators"
        knowledge_roots = [wiki]
        knowledge_roots.extend(path for path in creators_root.glob("*") if path.is_dir())
        if not any(root.exists() for root in knowledge_roots):
            return []
        all_content = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.config.vault_path.rglob("*.md")
        )
        orphans = []
        for root in knowledge_roots:
            for folder in ("concepts", "entities"):
                for path in (root / folder).glob("*.md"):
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    if re.search(r"\[\[[^\]\n]*/sources/", content):
                        continue
                    relative = str(path.relative_to(self.config.vault_path).with_suffix(""))
                    if f"[[{relative}" not in all_content:
                        orphans.append(str(path.relative_to(self.config.vault_path)))
        return sorted(orphans)
