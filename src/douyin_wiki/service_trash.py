from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from .errors import (
    DouyinWikiError,
    EntryNotFoundError,
)
from .models import (
    EntryRecord,
)
from .time_utils import utc_now


class TrashMixin:
    @property
    def _entry_trash_root(self) -> Path:
        return self.config.vault_path / ".douyin-wiki" / "trash" / "entries"

    def _trash_item_dir(self, trash_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", trash_id):
            raise ValueError("废纸篓条目编号无效")
        root = self._entry_trash_root.resolve()
        target = (root / trash_id).resolve()
        target.relative_to(root)
        return target

    def _entry_managed_paths(self, entry: EntryRecord) -> list[Path]:
        vault = self.config.vault_path.resolve()
        source_relative = Path(entry.source_path)
        raw_relative = Path(entry.raw_path)
        for label, relative in (("资料页", source_relative), ("原始记录", raw_relative)):
            if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
                raise ValueError(f"{label}路径无效，已停止删除")
        if source_relative.parts[:2] == ("wiki", "sources"):
            creator_folder = ""
            if not raw_relative.parts or raw_relative.parts[0] != "raw":
                raise ValueError("原始记录与资料页不属于同一资料目录，已停止删除")
        elif (
            len(source_relative.parts) >= 4
            and source_relative.parts[0] == "creators"
            and source_relative.parts[2] == "sources"
        ):
            creator_folder = Path(*source_relative.parts[:2]).as_posix()
            if raw_relative.parts[:3] != (
                source_relative.parts[0],
                source_relative.parts[1],
                "raw",
            ):
                raise ValueError("原始记录与博主资料页不属于同一目录，已停止删除")
        else:
            raise ValueError("资料页不在受管目录中，已停止删除")
        machine_relative = (
            Path(creator_folder) / ".data" / "sources" / f"{entry.video_id}.md"
            if creator_folder
            else Path("wiki") / ".data" / "sources" / f"{entry.video_id}.md"
        )
        candidates: list[Path] = [
            self.config.vault_path / source_relative,
            self.config.vault_path / raw_relative,
            self.config.vault_path / machine_relative,
        ]
        raw_parent = raw_relative.parent
        raw_root = raw_parent.parent if raw_parent.name == "records" else raw_parent
        candidates.extend(
            [
                self.config.vault_path / raw_root / "assets" / entry.video_id,
                self.config.vault_path / raw_root / "images" / entry.video_id,
            ]
        )
        candidates.extend(
            (self.config.vault_path / raw_root / "covers").glob(f"{entry.video_id}.*")
        )

        safe: list[Path] = []
        configured_root = self.config.vault_path.absolute()
        for candidate in candidates:
            try:
                lexical_relative = candidate.absolute().relative_to(configured_root)
            except ValueError:
                continue
            cursor = configured_root
            for part in lexical_relative.parts:
                cursor /= part
                if cursor.is_symlink():
                    raise ValueError("资料包含 Vault 内部符号链接，已停止删除")
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(vault)
            except (OSError, ValueError):
                continue
            if not relative.parts or relative.parts[0] == ".douyin-wiki" or not resolved.exists():
                continue
            safe.append(resolved)
        selected: list[Path] = []
        for candidate in sorted(set(safe), key=lambda item: len(item.relative_to(vault).parts)):
            if any(candidate == parent or parent in candidate.parents for parent in selected):
                continue
            selected.append(candidate)
        return selected

    @staticmethod
    def _path_size(path: Path) -> int:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )

    def _read_trash_manifest(self, trash_id: str) -> tuple[Path, dict[str, Any]]:
        item_dir = self._trash_item_dir(trash_id)
        manifest_path = item_dir / "manifest.json"
        if not manifest_path.is_file():
            raise EntryNotFoundError("废纸篓条目不存在")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("废纸篓条目已损坏，无法读取") from exc
        if manifest.get("trash_id") != trash_id or manifest.get("version") != 1:
            raise ValueError("废纸篓条目格式无效")
        return item_dir, manifest

    @staticmethod
    def _trash_phase(manifest: dict[str, Any]) -> str:
        # Manifests produced before the durable journal existed represent a
        # completed delete and remain fully restorable.
        return str(manifest.get("phase") or "completed")

    def _write_trash_manifest(self, item_dir: Path, manifest: dict[str, Any]) -> None:
        """Atomically persist a crash-recovery checkpoint and flush it to disk."""
        item_dir.mkdir(parents=True, exist_ok=True)
        target = item_dir / "manifest.json"
        temporary = item_dir / "manifest.json.tmp"
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        directory_fd = os.open(item_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _entry_exists(self, entry_id: str) -> bool:
        try:
            self.database.get_entry(entry_id)
        except EntryNotFoundError:
            return False
        return True

    def _restore_entry_projection(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> tuple[dict[str, list[str]], list[str]]:
        self.database.upsert_entry(entry, data)
        chunks, relations, reminders = self.prepare_entry_bundle(entry, data)
        self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
        dependency_result = self.database.restore_entry_dependencies(entry.id, dependencies)
        restored_topic_ids = self._restore_trashed_topic_sources(entry, dependencies, persist=False)
        return dependency_result, restored_topic_ids

    def _move_trashed_files_back(self, item_dir: Path, manifest: dict[str, Any]) -> None:
        vault = self.config.vault_path.resolve()
        files_root = (item_dir / "files").resolve()
        for item in reversed(manifest.get("files", [])):
            relative = Path(str(item.get("path") or ""))
            source = (files_root / relative).resolve()
            target = (vault / relative).resolve()
            try:
                source.relative_to(files_root)
                target.relative_to(vault)
            except ValueError as exc:
                raise ValueError("废纸篓条目包含不安全的文件路径") from exc
            if source.exists() and target.exists():
                raise ValueError(f"恢复位置已有文件：{relative.as_posix()}")
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))

    def _finalize_deleted_entry(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        dependencies: dict[str, Any],
        paths: list[Path],
    ) -> list[str]:
        """Refresh rebuildable views without turning a successful delete into failure."""
        warnings: list[str] = []
        try:
            with self.vault.locked():
                changed = self.vault.remove_entry_links(entry, data)
                index = self.vault.rebuild_index(self.database.list_entries())
                log = self.vault.append_log(
                    "trash",
                    entry.title,
                    "整条资料及其视频、图片和封面已移入抖库废纸篓",
                    [*paths, index],
                )
                changed.extend([*paths, index, log])
                self.vault.commit(changed, f"trash: {entry.video_id} {entry.title}")
        except Exception as exc:
            warnings.append(f"资料已删除，但知识库导航更新失败：{exc}")
        for topic_id in dependencies.get("topics", {}):
            try:
                self.refresh_topic(topic_id)
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已删除，但专题 {topic_id} 更新失败：{exc}")
        creator_ids = {
            str(item.get("creator_id"))
            for item in dependencies.get("creator_works", [])
            if item.get("creator_id")
        }
        for creator_id in creator_ids:
            try:
                self.refresh_creator_documents(creator_id, action="删除入库资料")
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已删除，但博主 {creator_id} 更新失败：{exc}")
        return warnings

    def _finalize_restored_entry(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        dependencies: dict[str, Any],
        restored_topic_ids: list[str],
        creator_ids: list[str],
    ) -> list[str]:
        warnings: list[str] = []
        try:
            with self.vault.locked():
                changed = self.vault.restore_entry_links(entry, data)
                index = self.vault.rebuild_index(self.database.list_entries())
                log = self.vault.append_log(
                    "restore",
                    entry.title,
                    "从抖库废纸篓恢复整条资料及其媒体",
                    [self.config.vault_path / entry.source_path, index],
                )
                changed.extend([index, log])
                self.vault.commit(changed, f"restore: {entry.video_id} {entry.title}")
        except Exception as exc:
            warnings.append(f"资料已恢复，但知识库导航更新失败：{exc}")
        for topic_id in restored_topic_ids:
            try:
                self.persist_topic(self.database.get_topic(topic_id))
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已恢复，但专题 {topic_id} 更新失败：{exc}")
        for creator_id in creator_ids:
            try:
                self.refresh_creator_documents(creator_id, action="恢复入库资料")
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已恢复，但博主 {creator_id} 更新失败：{exc}")
        return warnings

    @staticmethod
    def _normalize_trashed_entry_data(entry: EntryRecord, data: dict[str, Any]) -> dict[str, Any]:
        """Fill fields omitted by older database projections before restoring files."""
        normalized = dict(data)
        metadata = dict(normalized.get("metadata") or {})
        metadata.setdefault("video_id", entry.video_id)
        metadata.setdefault("original_url", entry.original_url)
        metadata.setdefault("canonical_url", entry.canonical_url)
        metadata.setdefault("title", entry.title)
        metadata.setdefault("author", "未知作者")
        metadata.setdefault("source_kind", "video")
        normalized["metadata"] = metadata
        normalized.setdefault("share_text", entry.original_url)
        normalized.setdefault("raw_transcript", [])
        normalized.setdefault("corrected_transcript", [])
        normalized.setdefault("ocr", [])
        normalized.setdefault("review_issues", [])
        normalized.setdefault("fact_checks", [])
        return normalized

    def _restore_trashed_topic_sources(
        self,
        entry: EntryRecord,
        dependencies: dict[str, Any],
        *,
        persist: bool,
    ) -> list[str]:
        restored_ids: list[str] = []
        for topic_id, original_sources in dependencies.get("topics", {}).items():
            try:
                topic = self.database.get_topic(topic_id)
            except KeyError:
                continue
            if any(source.entry_id == entry.id for source in topic.sources):
                continue
            original = next(
                (item for item in original_sources if item.get("entry_id") == entry.id),
                None,
            )
            if original is None:
                continue
            ordered = [
                (self.database.get_entry(source.entry_id), source.enabled)
                for source in topic.sources
            ]
            position = max(0, min(int(original.get("position") or 1) - 1, len(ordered)))
            ordered.insert(position, (entry, bool(original.get("enabled", 1))))
            revision, _ = self.topic_revision(ordered)
            restored_topic = self.database.set_topic_sources(
                topic_id,
                [(value.id, enabled, value.updated_at.isoformat()) for value, enabled in ordered],
                source_revision=revision,
            )
            if persist:
                self.persist_topic(restored_topic)
            restored_ids.append(topic_id)
        return restored_ids

    def list_trashed_entries(self) -> list[dict[str, Any]]:
        root = self._entry_trash_root
        if not root.is_dir():
            return []
        values: list[dict[str, Any]] = []
        for manifest_path in root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("version") != 1 or self._trash_phase(manifest) in {
                    "restored",
                    "purging",
                }:
                    continue
                values.append(
                    {
                        "trash_id": manifest["trash_id"],
                        "entry_id": manifest["entry"]["id"],
                        "work_id": manifest["entry"]["video_id"],
                        "title": manifest["entry"]["title"],
                        "author": manifest.get("author") or "未知作者",
                        "source_kind": manifest.get("source_kind") or "video",
                        "deleted_at": manifest["deleted_at"],
                        "file_count": len(manifest.get("files", [])),
                        "size_bytes": int(manifest.get("size_bytes") or 0),
                    }
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(values, key=lambda item: item["deleted_at"], reverse=True)

    def trash_entry(self, entry_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("删除整条资料前需要用户明确确认")
        with self.vault.entry_operations_locked():
            entry = self.database.get_entry(entry_id)
            data = self.database.get_entry_data(entry_id)
            dependencies = self.database.snapshot_entry_dependencies(entry.id)
            paths = self._entry_managed_paths(entry)
            trash_id = uuid.uuid4().hex
            root = self._entry_trash_root
            staging = root / f".{trash_id}.staging"
            item_dir = self._trash_item_dir(trash_id)
            files_root = item_dir / "files"
            vault = self.config.vault_path.resolve()
            files = [
                {
                    "path": source.relative_to(vault).as_posix(),
                    "is_directory": source.is_dir(),
                    "size_bytes": self._path_size(source),
                }
                for source in paths
            ]
            manifest: dict[str, Any] = {
                "version": 1,
                "trash_id": trash_id,
                "phase": "prepared",
                "deleted_at": utc_now().isoformat(),
                "entry": entry.model_dump(mode="json"),
                "data": data,
                "author": data.get("metadata", {}).get("author") or "未知作者",
                "source_kind": data.get("metadata", {}).get("source_kind") or "video",
                "files": files,
                "size_bytes": sum(item["size_bytes"] for item in files),
                "dependencies": dependencies,
                "warnings": [],
            }
            root.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True)
            self._write_trash_manifest(staging, manifest)
            staging.replace(item_dir)
            try:
                for source in paths:
                    relative = source.relative_to(vault)
                    target = files_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                manifest["phase"] = "files_moved"
                self._write_trash_manifest(item_dir, manifest)
                # Re-snapshot inside the delete transaction; the entry-operation
                # lock guarantees this has not drifted from the durable copy.
                dependencies = self.database.delete_entry_projection(entry.id)
                manifest["dependencies"] = dependencies
                manifest["phase"] = "database_deleted"
                self._write_trash_manifest(item_dir, manifest)
            except Exception:
                rollback_error: Exception | None = None
                try:
                    if not self._entry_exists(entry.id):
                        restored_data = self._normalize_trashed_entry_data(entry, data)
                        self._restore_entry_projection(entry, restored_data, dependencies)
                    self._move_trashed_files_back(item_dir, manifest)
                except Exception as restore_exc:  # pragma: no cover - emergency path
                    rollback_error = restore_exc
                if rollback_error is None:
                    shutil.rmtree(item_dir, ignore_errors=True)
                if rollback_error is not None:
                    raise RuntimeError(
                        "资料移入废纸篓失败，且自动回滚未能完成；重启抖库将继续恢复"
                    ) from rollback_error
                raise

            warnings = self._finalize_deleted_entry(entry, data, dependencies, paths)
            manifest["warnings"] = warnings
            manifest["phase"] = "completed"
            try:
                self._write_trash_manifest(item_dir, manifest)
            except Exception as exc:
                warnings.append(f"删除已完成，但恢复日志保存失败；重启后会自动补全：{exc}")
            return {
                "trash_id": trash_id,
                "entry_id": entry.id,
                "work_id": entry.video_id,
                "title": entry.title,
                "author": manifest["author"],
                "source_kind": manifest["source_kind"],
                "deleted_at": manifest["deleted_at"],
                "file_count": len(files),
                "size_bytes": manifest["size_bytes"],
                "warnings": warnings,
            }

    def restore_trashed_entry(self, trash_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("恢复资料前需要用户明确确认")
        with self.vault.entry_operations_locked():
            item_dir, manifest = self._read_trash_manifest(trash_id)
            if self._trash_phase(manifest) not in {"completed", "database_deleted"}:
                raise ValueError("废纸篓条目正在恢复或维护，请稍后重试")
            entry = EntryRecord.model_validate(manifest["entry"])
            data = self._normalize_trashed_entry_data(entry, dict(manifest["data"]))
            if self._entry_exists(entry.id):
                raise ValueError("资料库中已经存在同一作品，无法恢复")
            dependencies = dict(manifest.get("dependencies") or {})
            vault = self.config.vault_path.resolve()
            restore_pairs: list[tuple[Path, Path]] = []
            for item in manifest.get("files", []):
                relative = Path(str(item.get("path") or ""))
                source = (item_dir / "files" / relative).resolve()
                target = (vault / relative).resolve()
                try:
                    source.relative_to((item_dir / "files").resolve())
                    target.relative_to(vault)
                except ValueError as exc:
                    raise ValueError("废纸篓条目包含不安全的文件路径") from exc
                if not source.exists():
                    raise ValueError(f"废纸篓文件缺失：{relative.as_posix()}")
                if target.exists():
                    raise ValueError(f"恢复位置已有文件：{relative.as_posix()}")
                restore_pairs.append((source, target))

            manifest["phase"] = "restoring"
            self._write_trash_manifest(item_dir, manifest)
            moved: list[tuple[Path, Path]] = []
            try:
                for source, target in restore_pairs:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                    moved.append((source, target))
                dependency_result, restored_topic_ids = self._restore_entry_projection(
                    entry, data, dependencies
                )
                manifest["phase"] = "restored"
                self._write_trash_manifest(item_dir, manifest)
            except Exception:
                if self._entry_exists(entry.id):
                    with suppress(Exception):
                        self.database.delete_entry_projection(entry.id)
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(source))
                manifest["phase"] = "completed"
                with suppress(Exception):
                    self._write_trash_manifest(item_dir, manifest)
                raise

            warnings: list[str] = []
            try:
                shutil.rmtree(item_dir)
            except Exception as exc:
                warnings.append(f"资料已恢复，但废纸篓残留清理失败；重启后会重试：{exc}")
            warnings.extend(
                self._finalize_restored_entry(
                    entry,
                    data,
                    dependencies,
                    restored_topic_ids,
                    dependency_result.get("creator_ids", []),
                )
            )
            return {
                "status": "已恢复",
                "entry_id": entry.id,
                "title": entry.title,
                "source_path": entry.source_path,
                "warnings": warnings,
            }

    def permanently_delete_trashed_entry(
        self, trash_id: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("彻底删除前需要用户明确确认")
        with self.vault.entry_operations_locked():
            item_dir, manifest = self._read_trash_manifest(trash_id)
            if self._trash_phase(manifest) != "completed":
                raise ValueError("废纸篓条目正在恢复或维护，暂时不能彻底删除")
            title = str(manifest.get("entry", {}).get("title") or "已删除资料")
            size_bytes = int(manifest.get("size_bytes") or 0)
            manifest["phase"] = "purging"
            self._write_trash_manifest(item_dir, manifest)
            shutil.rmtree(item_dir)
            return {
                "status": "已彻底删除",
                "trash_id": trash_id,
                "title": title,
                "size_bytes": size_bytes,
                "warnings": [],
            }

    @staticmethod
    def _unique_trash_ids(trash_ids: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for trash_id in trash_ids:
            if not trash_id or trash_id in seen:
                continue
            seen.add(trash_id)
            unique.append(trash_id)
        if not unique:
            raise ValueError("请选择要处理的资料")
        if len(unique) > 500:
            raise ValueError("一次最多处理 500 条资料")
        return unique

    def restore_trashed_entries(
        self, trash_ids: list[str], *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("恢复资料前需要用户明确确认")
        restored: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        warnings: list[str] = []
        for trash_id in self._unique_trash_ids(trash_ids):
            try:
                result = self.restore_trashed_entry(trash_id, confirmed=True)
                restored.append(result)
                warnings.extend(str(item) for item in result.get("warnings") or [])
            except (EntryNotFoundError, ValueError, DouyinWikiError) as exc:
                failed.append({"trash_id": trash_id, "error": str(exc)})
        if not restored:
            raise ValueError("恢复失败：" + "；".join(item["error"] for item in failed[:5]))
        if failed:
            warnings.append(
                f"{len(failed)} 条未能恢复："
                + "；".join(f"{item['trash_id']}（{item['error']}）" for item in failed[:5])
            )
        return {
            "status": "已恢复",
            "restored": restored,
            "failed": failed,
            "warnings": warnings,
        }

    def permanently_delete_trashed_entries(
        self, trash_ids: list[str], *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("彻底删除前需要用户明确确认")
        deleted: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        warnings: list[str] = []
        for trash_id in self._unique_trash_ids(trash_ids):
            try:
                result = self.permanently_delete_trashed_entry(trash_id, confirmed=True)
                deleted.append(result)
                warnings.extend(str(item) for item in result.get("warnings") or [])
            except (EntryNotFoundError, ValueError, DouyinWikiError) as exc:
                failed.append({"trash_id": trash_id, "error": str(exc)})
        if not deleted:
            raise ValueError("彻底删除失败：" + "；".join(item["error"] for item in failed[:5]))
        if failed:
            warnings.append(
                f"{len(failed)} 条未能删除："
                + "；".join(f"{item['trash_id']}（{item['error']}）" for item in failed[:5])
            )
        return {
            "status": "已彻底删除",
            "deleted": deleted,
            "failed": failed,
            "warnings": warnings,
        }

    def recover_entry_trash_operations(self) -> dict[str, Any]:
        """Recover interrupted delete/restore operations after an unclean exit."""
        with self.vault.entry_operations_locked():
            return self.recover_entry_trash_operations_locked()

    def recover_entry_trash_operations_locked(self) -> dict[str, Any]:
        root = self._entry_trash_root
        report: dict[str, Any] = {"recovered": [], "completed": [], "warnings": []}
        if not root.is_dir():
            return report
        for staging in root.glob(".*.staging"):
            # A staging directory is renamed before any managed file is moved.
            # It is therefore always safe to discard after a crash.
            shutil.rmtree(staging, ignore_errors=True)
        for manifest_path in sorted(root.glob("*/manifest.json")):
            item_dir = manifest_path.parent
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("version") != 1:
                    continue
                phase = self._trash_phase(manifest)
                entry = EntryRecord.model_validate(manifest["entry"])
                data = self._normalize_trashed_entry_data(entry, dict(manifest["data"]))
                dependencies = dict(manifest.get("dependencies") or {})
                if phase == "purging":
                    shutil.rmtree(item_dir)
                    report["completed"].append(manifest["trash_id"])
                    continue
                if phase == "restored":
                    shutil.rmtree(item_dir)
                    report["completed"].append(manifest["trash_id"])
                    continue
                if phase in {"prepared", "files_moved"}:
                    if self._entry_exists(entry.id):
                        self._move_trashed_files_back(item_dir, manifest)
                        shutil.rmtree(item_dir)
                        report["recovered"].append(manifest["trash_id"])
                        continue
                    manifest["phase"] = "database_deleted"
                    self._write_trash_manifest(item_dir, manifest)
                    phase = "database_deleted"
                if phase == "database_deleted":
                    if self._entry_exists(entry.id):
                        self._move_trashed_files_back(item_dir, manifest)
                        shutil.rmtree(item_dir)
                        report["recovered"].append(manifest["trash_id"])
                        continue
                    paths = [
                        self.config.vault_path / str(item.get("path") or "")
                        for item in manifest.get("files", [])
                    ]
                    warnings = self._finalize_deleted_entry(entry, data, dependencies, paths)
                    manifest["warnings"] = [
                        *manifest.get("warnings", []),
                        *warnings,
                    ]
                    manifest["phase"] = "completed"
                    self._write_trash_manifest(item_dir, manifest)
                    report["completed"].append(manifest["trash_id"])
                    report["warnings"].extend(warnings)
                    continue
                if phase == "restoring":
                    if not self._entry_exists(entry.id):
                        vault = self.config.vault_path.resolve()
                        files_root = (item_dir / "files").resolve()
                        for item in reversed(manifest.get("files", [])):
                            relative = Path(str(item.get("path") or ""))
                            source = (vault / relative).resolve()
                            target = (files_root / relative).resolve()
                            source.relative_to(vault)
                            target.relative_to(files_root)
                            if source.exists() and target.exists():
                                raise ValueError(f"恢复中断后出现文件冲突：{relative.as_posix()}")
                            if source.exists():
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(source), str(target))
                        manifest["phase"] = "completed"
                        self._write_trash_manifest(item_dir, manifest)
                        report["recovered"].append(manifest["trash_id"])
                        continue
                    dependency_result, restored_topic_ids = self._restore_entry_projection(
                        entry, data, dependencies
                    )
                    manifest["phase"] = "restored"
                    self._write_trash_manifest(item_dir, manifest)
                    shutil.rmtree(item_dir)
                    warnings = self._finalize_restored_entry(
                        entry,
                        data,
                        dependencies,
                        restored_topic_ids,
                        dependency_result.get("creator_ids", []),
                    )
                    report["completed"].append(manifest["trash_id"])
                    report["warnings"].extend(warnings)
            except Exception as exc:
                report["warnings"].append(f"废纸篓事务 {item_dir.name} 自动恢复失败：{exc}")
        return report
