from __future__ import annotations

import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

from .adapters.creator import creator_id_for
from .errors import (
    JobStateError,
)
from .models import (
    CaptureOptions,
    CaptureRequest,
    CreatorInventoryResult,
    CreatorInventoryWork,
    CreatorWorkDecision,
    GatewayContext,
    InspirationInput,
    JobRecord,
    JobStatus,
    SourceKind,
    VideoMetadata,
)
from .vault import safe_filename


class ImportMixin:
    def capture_douyin_creator(
        self,
        source_text: str,
        inspirations: list[InspirationInput] | None = None,
        options: CaptureOptions | None = None,
        gateway_context: GatewayContext | None = None,
    ) -> JobRecord:
        request = CaptureRequest(
            share_text=source_text,
            inspirations=inspirations or [],
            options=options or CaptureOptions(),
            gateway_context=gateway_context,
        )
        return self.database.create_job(
            request,
            kind="creator_import",
            artifacts={"creator_action": "initial"},
        )

    def sync_creator(
        self,
        creator_id: str,
        *,
        gateway_context: GatewayContext | None = None,
    ) -> JobRecord:
        creator = self.database.get_creator(creator_id)
        request = CaptureRequest(
            share_text=creator.canonical_url,
            inspirations=creator.inspirations,
            gateway_context=gateway_context,
        )
        return self.database.create_job(
            request,
            kind="creator_import",
            artifacts={"creator_action": "sync", "creator_id": creator.id},
        )

    def get_creator_inventory(
        self,
        job_id: str,
        *,
        page: int | None = None,
        limit: int | None = None,
        decision: CreatorWorkDecision | None = None,
        source_kind: SourceKind | None = None,
        query: str | None = None,
        not_imported: bool = False,
    ) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if job.kind != "creator_import":
            raise JobStateError("该任务不是博主清点任务")
        paginated = page is not None or limit is not None
        effective_page = max(1, page or 1)
        effective_limit = max(1, min(1000, limit or 10)) if paginated else 100_000
        items, total = self.database.list_creator_inventory(
            job_id,
            offset=(effective_page - 1) * effective_limit if paginated else 0,
            limit=effective_limit,
            decision=decision,
            source_kind=source_kind,
            query=query,
            not_imported=not_imported,
        )
        return {
            "job_id": job_id,
            "creator_id": job.artifacts.get("creator_id"),
            "status": job.status.value,
            "display_mode": "paginated" if paginated else "all",
            "page": effective_page,
            "limit": effective_limit if paginated else total,
            "total": total,
            "has_more": effective_page * effective_limit < total if paginated else False,
            "partial": bool(job.artifacts.get("inventory_partial")),
            "summary": self.database.creator_inventory_summary(job_id),
            "items": [item.model_dump(mode="json") for item in items],
        }

    def set_creator_work_selection(
        self,
        job_id: str,
        decision: CreatorWorkDecision,
        *,
        ordinals: list[int] | None = None,
        work_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if job.kind != "creator_import" or job.status != JobStatus.NEEDS_SELECTION:
            raise JobStateError("只有处于“待选择作品”状态的博主任务可以修改选择")
        changed = self.database.set_creator_run_selection(
            job_id, decision, ordinals=ordinals, work_ids=work_ids
        )
        summary = self.database.creator_inventory_summary(job_id)
        self.database.update_job(job_id, result={**job.result, "selection": summary})
        self.refresh_creator_documents(str(job.artifacts["creator_id"]), action="selection")
        return {"job_id": job_id, "changed": changed, "selection": summary}

    def confirm_creator_import(self, job_id: str, *, accept_partial: bool = False) -> JobRecord:
        job = self.database.get_job(job_id)
        if job.kind != "creator_import" or job.status != JobStatus.NEEDS_SELECTION:
            raise JobStateError("只有处于“待选择作品”状态的博主任务可以确认")
        if job.artifacts.get("inventory_partial") and not accept_partial:
            raise JobStateError("清单不完整；如仍要处理已发现作品，请设置 accept_partial=true")
        summary = self.database.creator_inventory_summary(job_id)
        if summary.get(CreatorWorkDecision.PENDING.value, 0):
            raise JobStateError("仍有未决定作品；请先选择或跳过全部作品")
        creator_id = str(job.artifacts["creator_id"])
        creator = self.database.get_creator(creator_id)
        selected, _ = self.database.list_creator_inventory(
            job_id, limit=5000, decision=CreatorWorkDecision.SELECTED
        )
        self.database.update_job(job_id, status=JobStatus.DISPATCHING, progress=0.55)
        child_ids: list[str] = []
        for item in selected:
            work = item.work
            if work.entry_id:
                self.database.mark_creator_work_imported(creator_id, work.work_id, work.entry_id)
                continue
            if work.last_job_id:
                with suppress(JobStateError):
                    existing_job = self.database.get_job(work.last_job_id)
                    if existing_job.status not in {
                        JobStatus.FAILED,
                        JobStatus.COMPLETED,
                        JobStatus.COMPLETED_WITH_WARNINGS,
                    }:
                        child_ids.append(existing_job.id)
                        continue
            child_request = CaptureRequest(
                share_text=work.canonical_url,
                inspirations=[],
                options=job.request.options,
                gateway_context=job.request.gateway_context,
            )
            child = self.database.create_job(
                child_request,
                artifacts={
                    "creator_context": {
                        "id": creator.id,
                        "folder_path": creator.folder_path,
                        "parent_job_id": job.id,
                        "work_id": work.work_id,
                        "batch_silent": True,
                    }
                },
            )
            self.database.attach_creator_child_job(creator_id, work.work_id, child.id)
            child_ids.append(child.id)
        result = {
            **job.result,
            "selection": self.database.creator_inventory_summary(job_id),
            "child_job_ids": child_ids,
            "queued_count": len(child_ids),
            "accept_partial": accept_partial,
        }
        status = JobStatus.MONITORING if child_ids else JobStatus.COMPLETED
        updated = self.database.update_job(
            job_id,
            status=status,
            progress=0.65 if child_ids else 1,
            result=result,
            unlock=True,
        )
        self.refresh_creator_documents(creator_id, action="confirm-import")
        return updated

    def import_creator_works(
        self,
        creator_id: str,
        work_ids: list[str],
        *,
        gateway_context: GatewayContext | None = None,
    ) -> JobRecord:
        creator = self.database.get_creator(creator_id)
        request = CaptureRequest(
            share_text=creator.canonical_url,
            inspirations=creator.inspirations,
            gateway_context=gateway_context,
        )
        job = self.database.create_job(
            request,
            kind="creator_import",
            artifacts={
                "creator_action": "manual-import",
                "creator_id": creator_id,
                "inventory_partial": False,
            },
            status=JobStatus.NEEDS_SELECTION,
            progress=0.5,
        )
        self.database.create_creator_run_items(job.id, creator_id, work_ids)
        self.database.set_creator_run_selection(
            job.id, CreatorWorkDecision.SELECTED, work_ids=work_ids
        )
        self.database.update_job(
            job.id,
            status=JobStatus.NEEDS_SELECTION,
            progress=0.5,
            result={"selection": self.database.creator_inventory_summary(job.id)},
            unlock=True,
        )
        return self.confirm_creator_import(job.id)

    def get_creator(self, creator_id: str) -> dict[str, Any]:
        creator = self.database.get_creator(creator_id)
        works = self.database.list_creator_works(creator_id)
        counts = {item.value: 0 for item in CreatorWorkDecision}
        for work in works:
            counts[work.decision.value] += 1
        return {
            **creator.model_dump(mode="json"),
            "counts": counts,
            "work_count": len(works),
        }

    def list_creators(self) -> list[dict[str, Any]]:
        return [self.get_creator(creator.id) for creator in self.database.list_creators()]

    def list_creator_works(self, creator_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json") for item in self.database.list_creator_works(creator_id)
        ]

    async def process_creator_import(self, job: JobRecord) -> JobRecord:
        artifacts = dict(job.artifacts)
        action = str(artifacts.get("creator_action") or "initial")
        self.database.update_job(job.id, status=JobStatus.RESOLVING, progress=0.05)
        target_dir = self.config.work_dir / job.id / "creator-inventory"
        async with self.download_semaphore:
            inventory = await self.creator_adapter.inventory(job.request.share_text, target_dir)
        self.database.update_job(job.id, status=JobStatus.INVENTORYING, progress=0.25)
        creator_id = creator_id_for(inventory.profile.sec_uid)
        existing = self.database.find_creator_by_sec_uid(inventory.profile.sec_uid)
        folder = (
            existing.folder_path
            if existing
            else str(
                Path("creators")
                / f"{safe_filename(inventory.profile.nickname, max_length=48)}_{creator_id[-8:]}"
            )
        )
        profile = self._persist_creator_avatar(inventory, folder)
        inventory = inventory.model_copy(
            update={
                "profile": profile,
                "works": self._persist_creator_previews(inventory, folder),
            }
        )
        creator = self.database.upsert_creator(
            profile,
            creator_id=creator_id,
            folder_path=folder,
            inspirations=job.request.inspirations,
        )
        inventory_result = self.database.record_creator_inventory(
            job.id,
            creator.id,
            inventory.works,
            complete=inventory.complete,
            sync=action == "sync",
        )
        artifacts_update = {
            "creator_id": creator.id,
            "creator_folder": creator.folder_path,
            "inventory_partial": not inventory.complete,
            "reported_count": inventory.reported_count,
        }
        summary = self.database.creator_inventory_summary(job.id)
        result = {
            "creator_id": creator.id,
            "creator_name": creator.nickname,
            "creator_path": creator.folder_path,
            "inventory": inventory_result,
            "selection": summary,
            "partial": not inventory.complete,
            "warnings": inventory.warnings,
            "next_tool": "get_creator_inventory" if summary["total"] else None,
        }
        self.database.update_job(job.id, artifacts=artifacts_update)
        self.refresh_creator_documents(creator.id, action=f"inventory-{action}")
        if not summary["total"] and inventory.complete:
            return self.database.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                progress=1,
                result={**result, "message": "同步完成：没有发现新作品"},
                unlock=True,
            )
        return self.database.update_job(
            job.id,
            status=JobStatus.NEEDS_SELECTION,
            progress=0.5,
            result={
                **result,
                **(
                    {"message": "清单不完整，需明确接受 partial 后才能结束本次清点"}
                    if not summary["total"]
                    else {}
                ),
            },
            unlock=True,
        )

    def _persist_creator_avatar(self, inventory: CreatorInventoryResult, folder_path: str):
        source_value = inventory.profile.avatar_path
        if not source_value:
            return inventory.profile
        source = Path(source_value)
        if not source.is_file():
            return inventory.profile.model_copy(update={"avatar_path": None})
        suffix = (
            source.suffix.lower()
            if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"}
            else ".jpg"
        )
        target = self.config.vault_path / folder_path / "raw" / f"avatar{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return inventory.profile.model_copy(
            update={"avatar_path": str(target.relative_to(self.config.vault_path))}
        )

    def _persist_creator_previews(
        self, inventory: CreatorInventoryResult, folder_path: str
    ) -> list[CreatorInventoryWork]:
        covers = self.config.vault_path / folder_path / "raw" / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        persisted: list[CreatorInventoryWork] = []
        for work in inventory.works:
            source = Path(work.thumbnail_path) if work.thumbnail_path else None
            if source is None or not source.is_file():
                persisted.append(work.model_copy(update={"thumbnail_path": None}))
                continue
            suffix = (
                source.suffix.lower()
                if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"}
                else ".jpg"
            )
            target = covers / f"{work.work_id}{suffix}"
            shutil.copy2(source, target)
            persisted.append(
                work.model_copy(
                    update={"thumbnail_path": str(target.relative_to(self.config.vault_path))}
                )
            )
        return persisted

    @staticmethod
    def creator_context(creator_id: str, folder_path: str, work_id: str) -> dict[str, Any]:
        return {
            "id": creator_id,
            "folder_path": folder_path,
            "parent_job_id": "",
            "work_id": work_id,
            "batch_silent": False,
        }

    def adopt_creator_capture(
        self,
        metadata: VideoMetadata,
        *,
        work_id: str,
        original_url: str,
        canonical_url: str,
        current_dir: Path,
        media_folder: str,
    ) -> tuple[dict[str, Any] | None, VideoMetadata, Path]:
        if not metadata.creator_sec_uid:
            return None, metadata, current_dir
        creator = self.database.find_creator_by_sec_uid(metadata.creator_sec_uid)
        if creator is None:
            return None, metadata, current_dir
        target_dir = self.config.vault_path / creator.folder_path / "raw" / media_folder / work_id
        metadata = self._relocate_capture_metadata(metadata, current_dir, target_dir)
        self.database.register_creator_work(
            creator.id,
            CreatorInventoryWork(
                work_id=work_id,
                source_kind=metadata.source_kind,
                canonical_url=canonical_url,
                original_url=original_url,
                title=metadata.title,
                published_at=metadata.published_at,
                duration_seconds=metadata.duration_seconds,
                thumbnail_path=metadata.thumbnail_path,
            ),
        )
        return self.creator_context(creator.id, creator.folder_path, work_id), metadata, target_dir

    @staticmethod
    def _relocate_capture_metadata(
        metadata: VideoMetadata, current_dir: Path, target_dir: Path
    ) -> VideoMetadata:
        current_dir = current_dir.resolve()
        target_dir = target_dir.resolve()
        if current_dir != target_dir and current_dir.exists():
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            if not target_dir.exists():
                shutil.move(str(current_dir), str(target_dir))
            else:
                for source in current_dir.iterdir():
                    target = target_dir / source.name
                    if not target.exists():
                        shutil.move(str(source), str(target))

        def relocated(value: str | None) -> str | None:
            if not value:
                return value
            path = Path(value).resolve()
            try:
                relative = path.relative_to(current_dir)
            except ValueError:
                return value
            return str(target_dir / relative)

        return metadata.model_copy(
            update={
                "media_path": relocated(metadata.media_path),
                "thumbnail_path": relocated(metadata.thumbnail_path),
                "image_paths": [
                    value
                    for value in (relocated(path) for path in metadata.image_paths)
                    if value is not None
                ],
            }
        )

    def refresh_creator_documents(self, creator_id: str, *, action: str) -> None:
        if not creator_id:
            return
        creator = self.database.get_creator(creator_id)
        works = self.database.list_creator_works(creator_id)
        entries = self.database.list_entries()
        with self.vault.locked():
            changed = self.vault.write_creator(creator, works, entries, action=action)
            root_index = self.vault.rebuild_index(entries)
            changed.append(root_index)
            if (self.config.vault_path / ".git").exists():
                self.commit_vault(changed, f"creator: {creator.nickname} {action}")

    def refresh_creator_parent(self, parent_job_id: str) -> None:
        if not parent_job_id:
            return
        with suppress(JobStateError):
            parent = self.database.get_job(parent_job_id)
            if parent.kind != "creator_import" or parent.status not in {
                JobStatus.MONITORING,
                JobStatus.COMPLETED,
                JobStatus.COMPLETED_WITH_WARNINGS,
            }:
                return
            child_ids = list(parent.result.get("child_job_ids", []))
            if not child_ids:
                return
            children = [self.database.get_job(job_id) for job_id in child_ids]
            child_updates = {child.id: child.updated_at.isoformat() for child in children}
            terminal = {
                JobStatus.COMPLETED,
                JobStatus.COMPLETED_WITH_WARNINGS,
                JobStatus.FAILED,
            }
            counts: dict[str, int] = {}
            for child in children:
                counts[child.status.value] = counts.get(child.status.value, 0) + 1
            finished = sum(counts.get(status.value, 0) for status in terminal)
            result = {
                **parent.result,
                "selection": self.database.creator_inventory_summary(parent.id),
                "child_status_counts": counts,
                "finished_count": finished,
            }
            if children and finished < len(children):
                self.database.update_job(
                    parent.id,
                    status=JobStatus.MONITORING,
                    progress=0.65 + 0.35 * finished / len(children),
                    result={**result, "completed_count": finished, "warning_count": 0},
                    expected_updated_at=parent.updated_at,
                    expected_child_updates=child_updates,
                )
                return
            warnings = counts.get(JobStatus.FAILED.value, 0) + counts.get(
                JobStatus.COMPLETED_WITH_WARNINGS.value, 0
            )
            self.database.update_job(
                parent.id,
                status=(JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED),
                progress=1,
                result={**result, "completed_count": len(children), "warning_count": warnings},
                unlock=True,
                expected_updated_at=parent.updated_at,
                expected_child_updates=child_updates,
            )
