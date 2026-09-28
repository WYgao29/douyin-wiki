from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from .adapters.llm import (
    PROMPT_VERSION,
    OpenAICompatibleProvider,
)
from .errors import (
    ExternalToolError,
    JobStateError,
)
from .models import (
    AnalysisMode,
    AnalysisResult,
    EntryRecord,
    JobRecord,
    JobStatus,
    OCRObservation,
    ReminderCandidate,
    RetentionPolicy,
    SourceKind,
    TranscriptSegment,
    VideoMetadata,
)
from .review import (
    deduplicate_review_issues,
    apply_review_resolutions,
    detect_review_issues,
    transcript_confidence_info,
)
from .time_utils import beijing_date, utc_now
from .vault import safe_filename


class CaptureMixin:
    async def _process_media_restore(self, job: JobRecord) -> JobRecord:
        entry_id = str(job.artifacts.get("entry_id") or "")
        entry = self.database.get_entry(entry_id)
        if not entry.favorite:
            return self.database.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                progress=1,
                result={"entry_id": entry.id, "media_restored": False, "skipped": True},
                unlock=True,
            )
        data = self.database.get_entry_data(entry.id)
        raw_parent = Path(entry.raw_path).parent
        raw_root = raw_parent.parent if raw_parent.name == "records" else raw_parent
        assets_dir = self.config.vault_path / raw_root / "assets" / entry.video_id
        self.database.update_job(job.id, status=JobStatus.DOWNLOADING, progress=0.2)
        async with self.download_semaphore:
            metadata = await self.downloader.download(
                entry.canonical_url,
                entry.video_id,
                assets_dir,
            )
        data["metadata"] = metadata.model_dump(mode="json")
        with self.vault.entry_operations_locked():
            current = self.database.get_entry(entry.id)
            now = utc_now()
            keep = current.favorite
            updated = current.model_copy(
                update={
                    "media_status": "present",
                    "retention": (RetentionPolicy.KEEP if keep else RetentionPolicy.TEMPORARY),
                    "media_expires_at": (
                        None if keep else now + timedelta(days=self.config.media.retention_days)
                    ),
                    "updated_at": now,
                }
            )
            self._write_entry_documents(
                updated,
                data,
                action="media_restore",
                log_summary="恢复收藏视频媒体",
                commit_message=f"media: restore {entry.video_id} {entry.title}",
            )
            self.database.upsert_entry(updated, data)
        return self.database.update_job(
            job.id,
            status=JobStatus.COMPLETED,
            progress=1,
            result={"entry_id": entry.id, "media_restored": True},
            unlock=True,
        )

    async def _process_capture(self, job: JobRecord) -> JobRecord:
        request = job.request
        artifacts = dict(job.artifacts)

        if "resolved" not in artifacts:
            self.database.update_job(job.id, status=JobStatus.RESOLVING, progress=0.03)
            resolved = await self.resolver.resolve(request.share_text)
            artifacts["resolved"] = {
                "original_url": resolved.original_url,
                "canonical_url": resolved.canonical_url,
                "video_id": resolved.video_id,
                "redirect_chain": list(resolved.redirect_chain),
                "source_kind": resolved.source_kind.value,
            }
            self.database.update_job(
                job.id, artifacts={"resolved": artifacts["resolved"]}, progress=0.08
            )
        resolved_data = artifacts["resolved"]
        video_id = resolved_data["video_id"]

        async with self._work_capture_locked(video_id):
            return await self._process_resolved_capture(job, artifacts, resolved_data)

    @asynccontextmanager
    async def _work_capture_locked(self, work_id: str):
        lock = self.vault.work_capture_locked(work_id)
        acquisition = asyncio.create_task(asyncio.to_thread(lock.__enter__))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            # Cancellation cannot stop the executor thread.  Wait for a
            # blocked flock to finish, then release it before propagating the
            # cancellation so a later capture cannot inherit a leaked lock.
            while not acquisition.done():
                try:
                    await asyncio.shield(acquisition)
                except asyncio.CancelledError:
                    continue
            if not acquisition.cancelled():
                try:
                    acquisition.result()
                except BaseException:
                    pass
                else:
                    await asyncio.to_thread(lock.__exit__, None, None, None)
            raise
        try:
            yield
        except BaseException as exc:
            await asyncio.to_thread(lock.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await asyncio.to_thread(lock.__exit__, None, None, None)

    async def _process_resolved_capture(
        self,
        job: JobRecord,
        artifacts: dict[str, Any],
        resolved_data: dict[str, Any],
    ) -> JobRecord:
        request = job.request
        work_dir = self.config.work_dir / job.id
        work_dir.mkdir(parents=True, exist_ok=True)
        video_id = resolved_data["video_id"]

        if not artifacts.get("creator_context"):
            known_creator = self.database.find_creator_for_work(video_id)
            if known_creator:
                artifacts["creator_context"] = self._creator_context(
                    known_creator.id, known_creator.folder_path, video_id
                )
                self.database.update_job(
                    job.id, artifacts={"creator_context": artifacts["creator_context"]}
                )

        if resolved_data.get("source_kind", SourceKind.VIDEO.value) == SourceKind.IMAGE_NOTE.value:
            return await self._process_image_note_capture(job, artifacts, resolved_data)

        with self.vault.entry_operations_locked():
            # Re-read after the work lock so a preceding capture can win the
            # first-write race and its user data is never lost.
            existing = self.database.find_entry_by_video_id(video_id)
            if existing:
                for inspiration in request.inspirations:
                    existing = self._add_inspiration_locked(existing.id, inspiration)
                should_reacquire = (
                    existing.media_status == "removed"
                    and request.options.retention != RetentionPolicy.DISCARD
                )
                if not should_reacquire:
                    existing = self._repair_entry_if_needed_locked(existing)
                    return self.database.update_job(
                        job.id,
                        status=JobStatus.COMPLETED,
                        progress=1,
                        result={
                            "entry_id": existing.id,
                            "duplicate": True,
                            "source_path": existing.source_path,
                        },
                        unlock=True,
                    )
        effective_inspirations = existing.inspirations if existing else request.inspirations

        creator_context = artifacts.get("creator_context") or {}
        creator_folder = str(creator_context.get("folder_path") or "")
        assets_dir = (
            self.config.vault_path / creator_folder / "raw" / "assets" / video_id
            if creator_folder
            else self.config.vault_path / "raw" / "assets" / video_id
        )
        checkpoint_video = Path(str(artifacts.get("video_path") or ""))
        video_checkpoint_valid = (
            bool(artifacts.get("video_path"))
            and checkpoint_video.is_file()
            and checkpoint_video.stat().st_size > 0
        )
        if "metadata" not in artifacts or not video_checkpoint_valid:
            self.database.update_job(job.id, status=JobStatus.DOWNLOADING, progress=0.12)
            async with self.download_semaphore:
                metadata = await self.downloader.download(
                    resolved_data["canonical_url"], video_id, assets_dir
                )
            artifacts.update(
                {"metadata": metadata.model_dump(mode="json"), "video_path": metadata.media_path}
            )
            self.database.update_job(
                job.id,
                artifacts={
                    "metadata": artifacts["metadata"],
                    "video_path": artifacts["video_path"],
                },
                progress=0.28,
            )
        metadata = VideoMetadata.model_validate(artifacts["metadata"])
        if not creator_folder:
            creator_context, metadata, assets_dir = self._adopt_creator_capture(
                metadata,
                work_id=video_id,
                original_url=str(resolved_data["original_url"]),
                canonical_url=str(resolved_data["canonical_url"]),
                current_dir=assets_dir,
                media_folder="assets",
            )
            if creator_context:
                creator_folder = str(creator_context["folder_path"])
                artifacts["creator_context"] = creator_context
                artifacts["metadata"] = metadata.model_dump(mode="json")
                artifacts["video_path"] = metadata.media_path
                self.database.update_job(
                    job.id,
                    artifacts={
                        "creator_context": creator_context,
                        "metadata": artifacts["metadata"],
                        "video_path": artifacts["video_path"],
                    },
                )
        video_path = Path(artifacts["video_path"])
        if metadata.duration_seconds is None:
            metadata.duration_seconds = await self.media.probe_duration(video_path)
            artifacts["metadata"] = metadata.model_dump(mode="json")
            self.database.update_job(job.id, artifacts={"metadata": artifacts["metadata"]})

        duration_minutes = (metadata.duration_seconds or 0) / 60
        if (
            duration_minutes > self.config.media.max_duration_minutes
            and not request.options.allow_long
            and not artifacts.get("long_video_approved")
        ):
            return self.database.update_job(
                job.id,
                status=JobStatus.WAITING_CONFIRMATION,
                progress=0.3,
                result={
                    "reason": "video_too_long",
                    "duration_minutes": round(duration_minutes, 1),
                    "message": "视频超过默认时长上限；确认后可继续处理已下载媒体",
                    "next_tool": "approve_job",
                },
                unlock=True,
            )
        approved = (
            request.options.approve_cloud_analysis
            or artifacts.get("ai_analysis_approved")
            or artifacts.get("cloud_analysis_approved")
        )
        uses_token_analysis = self.config.analysis_mode == AnalysisMode.GATEWAY or (
            self.config.analysis_mode == AnalysisMode.PROVIDER and self.analysis.configured
        )
        if (
            duration_minutes > self.config.media.cloud_confirmation_minutes
            and uses_token_analysis
            and not approved
        ):
            return self.database.update_job(
                job.id,
                status=JobStatus.WAITING_CONFIRMATION,
                progress=0.3,
                artifacts={"metadata": artifacts["metadata"]},
                result={
                    "reason": "ai_analysis_confirmation_required",
                    "duration_minutes": round(duration_minutes, 2),
                },
                unlock=True,
            )

        if "transcript_raw" not in artifacts:
            self.database.update_job(job.id, status=JobStatus.TRANSCRIBING, progress=0.34)
            audio_path = assets_dir / "audio.wav"
            frames_dir = assets_dir / "frames"
            async with self.media_semaphore:
                # Prefer audio.mp4; also accept other retained audio.* from CDN merge.
                retained_audio = [
                    path
                    for path in sorted(
                        assets_dir.glob("audio.*"),
                        key=lambda item: (
                            0 if item.suffix.lower() == ".mp4" else 1,
                            item.name,
                        ),
                    )
                    if path.is_file()
                    and path.suffix.lower()
                    not in {".wav", ".json", ".part", ".jpg", ".jpeg", ".png", ".webp"}
                ]
                audio_sources = [*retained_audio, video_path]
                audio_ready = False
                for source in audio_sources:
                    if not source.exists():
                        continue
                    try:
                        await self.media.extract_audio(source, audio_path)
                    except ExternalToolError as exc:
                        audio_path.unlink(missing_ok=True)
                        if source == video_path:
                            detail = str(exc.details.get("stderr", "")).lower()
                            no_audio = "does not contain any stream" in detail or "没有音轨" in str(
                                exc
                            )
                            if not no_audio:
                                raise
                        continue
                    if audio_path.is_file() and audio_path.stat().st_size > 0:
                        audio_ready = True
                        break
                    audio_path.unlink(missing_ok=True)
                if not audio_ready:
                    artifacts["transcript_skipped"] = "no_audio_available"
                try:
                    transcript = (
                        await self.transcriber.transcribe(audio_path, work_dir / "asr")
                        if audio_ready
                        else []
                    )
                    frames = await self.media.extract_frames(
                        video_path,
                        frames_dir,
                        duration_seconds=metadata.duration_seconds or 0,
                        interval_seconds=self.config.media.frame_interval_seconds,
                        scene_threshold=self.config.media.scene_threshold,
                        max_frames=self.config.media.max_frames,
                    )
                    try:
                        ocr = await self.ocr.recognize(frames)
                    except ExternalToolError as exc:
                        ocr = []
                        artifacts["ocr_warning"] = str(exc)
                finally:
                    if audio_path.exists():
                        audio_path.unlink()
            artifacts["transcript_raw"] = [item.model_dump(mode="json") for item in transcript]
            artifacts["ocr"] = [item.model_dump(mode="json") for item in ocr]
            artifacts["media_provenance"] = {
                "asr": getattr(self.transcriber, "provenance", {}) if audio_ready else {},
                "ocr": getattr(self.ocr, "provenance", {}),
            }
            self.database.update_job(
                job.id,
                artifacts={
                    "transcript_raw": artifacts["transcript_raw"],
                    "ocr": artifacts["ocr"],
                    "media_provenance": artifacts["media_provenance"],
                    **(
                        {"ocr_warning": artifacts["ocr_warning"]}
                        if artifacts.get("ocr_warning")
                        else {}
                    ),
                    **(
                        {"transcript_skipped": artifacts["transcript_skipped"]}
                        if artifacts.get("transcript_skipped")
                        else {}
                    ),
                },
                progress=0.58,
            )
        transcript_raw = [
            TranscriptSegment.model_validate(item) for item in artifacts["transcript_raw"]
        ]
        # Also enrich resumed jobs from their stored transcript, without changing
        # any existing review issue or acknowledging it on the user's behalf.
        confidence_info = transcript_confidence_info(transcript_raw)
        provenance = dict(artifacts.get("media_provenance") or {})
        asr_provenance = {**(provenance.get("asr") or {}), **confidence_info}
        if confidence_info and provenance.get("asr") != asr_provenance:
            provenance["asr"] = asr_provenance
            artifacts["media_provenance"] = provenance
            self.database.update_job(job.id, artifacts={"media_provenance": provenance})
        ocr_items = artifacts.get("ocr", [])

        checkpoints = artifacts.setdefault("llm_checkpoints", {})
        model_stats = artifacts.setdefault(
            "llm_stats",
            {
                "successful_calls": 0,
                "http_requests": 0,
                "retry_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        last_model_response_at = (artifacts.get("analysis_progress") or {}).get("last_response_at")
        last_model_usage = (artifacts.get("analysis_progress") or {}).get("usage", {})

        def save_checkpoint(_fingerprint: str, _value: dict[str, Any]) -> None:
            nonlocal last_model_response_at, last_model_usage
            last_model_response_at = utc_now().isoformat()
            last_model_usage = getattr(self.analysis, "last_usage", {}).copy()
            increments = {
                "successful_calls": 1,
                "http_requests": last_model_usage.get("request_count", 1),
                "retry_count": last_model_usage.get("retry_count", 0),
                "input_tokens": last_model_usage.get(
                    "input_tokens",
                    last_model_usage.get("prompt_tokens", 0),
                ),
                "output_tokens": last_model_usage.get(
                    "output_tokens",
                    last_model_usage.get("completion_tokens", 0),
                ),
            }
            for key, amount in increments.items():
                model_stats[key] = int(model_stats.get(key, 0)) + amount
            detail = {
                **(artifacts.get("analysis_progress") or {}),
                "last_response_at": last_model_response_at,
                "usage": last_model_usage,
            }
            artifacts["analysis_progress"] = detail
            self.database.update_job(
                job.id,
                artifacts={
                    "llm_checkpoints": checkpoints,
                    "analysis_progress": detail,
                    "llm_stats": model_stats,
                },
            )

        def model_progress(phase: str, completed: int, total: int) -> None:
            current_progress = self.database.get_job(job.id).progress
            target = (
                0.62 + 0.04 * completed / max(total, 1)
                if phase == "correction"
                else 0.70 + 0.08 * completed / max(total, 1)
                if phase == "analysis"
                else 0.79
            )
            detail = {
                "phase": phase,
                "completed_chunks": completed,
                "total_chunks": total,
                "waiting_for_resource": False,
                "last_response_at": last_model_response_at,
                "usage": last_model_usage,
            }
            artifacts["analysis_progress"] = detail
            self.database.update_job(
                job.id,
                progress=max(current_progress, target),
                artifacts={"analysis_progress": detail},
            )

        if (
            self.config.analysis_mode == AnalysisMode.GATEWAY
            and "transcript_corrected" not in artifacts
        ):
            return self.database.update_job(
                job.id,
                status=JobStatus.AWAITING_AGENT_ANALYSIS,
                progress=0.62,
                result={
                    "phase": "transcript_correction",
                    "next_tool": "get_analysis_context",
                },
                unlock=True,
            )

        if "transcript_corrected" not in artifacts:
            if isinstance(self.analysis, OpenAICompatibleProvider):
                # The semaphore may queue this job for a while. Mark the model
                # phase before waiting so every read surface reflects the work.
                waiting_detail = {
                    "phase": "correction",
                    "completed_chunks": 0,
                    "total_chunks": 0,
                    "waiting_for_resource": True,
                    "last_response_at": last_model_response_at,
                    "usage": last_model_usage,
                }
                artifacts["analysis_progress"] = waiting_detail
                self.database.update_job(
                    job.id,
                    status=JobStatus.ANALYZING,
                    progress=max(self.database.get_job(job.id).progress, 0.62),
                    artifacts={"analysis_progress": waiting_detail},
                )
            async with self.analysis_semaphore:
                if isinstance(self.analysis, OpenAICompatibleProvider):
                    artifacts["analysis_progress"]["waiting_for_resource"] = False
                    self.database.update_job(
                        job.id,
                        artifacts={"analysis_progress": artifacts["analysis_progress"]},
                    )
                correction_kwargs = (
                    {
                        "checkpoints": checkpoints,
                        "on_checkpoint": save_checkpoint,
                        "on_progress": model_progress,
                    }
                    if isinstance(self.analysis, OpenAICompatibleProvider)
                    else {}
                )
                corrected, llm_issues = await self.analysis.correct_transcript(
                    transcript_raw,
                    [self._ocr_model(item) for item in ocr_items],
                    **correction_kwargs,
                )
            issues = deduplicate_review_issues([*detect_review_issues(transcript_raw), *llm_issues])
            artifacts["transcript_corrected"] = [item.model_dump(mode="json") for item in corrected]
            artifacts["review_issues"] = [item.model_dump(mode="json") for item in issues]
            self.database.update_job(
                job.id,
                artifacts={
                    "transcript_corrected": artifacts["transcript_corrected"],
                    "review_issues": artifacts["review_issues"],
                },
                progress=0.66,
            )
            if issues and not artifacts.get("review_resolved"):
                self.database.replace_review_issues(job.id, issues)
                return self.database.update_job(
                    job.id,
                    status=JobStatus.NEEDS_REVIEW,
                    result={"review_issue_count": len(issues)},
                    unlock=True,
                )

        corrected = [
            TranscriptSegment.model_validate(item) for item in artifacts["transcript_corrected"]
        ]
        review_issues = self.database.get_review_issues(job.id)
        if artifacts.get("review_resolved") and review_issues:
            corrected = apply_review_resolutions(corrected, review_issues)
            artifacts["transcript_corrected"] = [item.model_dump(mode="json") for item in corrected]
            self.database.update_job(
                job.id,
                artifacts={"transcript_corrected": artifacts["transcript_corrected"]},
                progress=0.68,
            )

        if self.config.analysis_mode == AnalysisMode.GATEWAY and "analysis" not in artifacts:
            return self.database.update_job(
                job.id,
                status=JobStatus.AWAITING_AGENT_ANALYSIS,
                progress=0.7,
                result={"phase": "analysis", "next_tool": "get_analysis_context"},
                unlock=True,
            )

        self.database.update_job(
            job.id,
            status=JobStatus.ANALYZING,
            progress=max(self.database.get_job(job.id).progress, 0.7),
            artifacts={
                "analysis_progress": {
                    **(artifacts.get("analysis_progress") or {}),
                    "phase": "analysis",
                    "waiting_for_resource": True,
                }
            }
            if isinstance(self.analysis, OpenAICompatibleProvider)
            else None,
        )
        if "analysis" not in artifacts:
            if "analysis_candidate" in artifacts:
                analysis = AnalysisResult.model_validate(artifacts["analysis_candidate"])
            else:
                analysis_metadata = metadata.model_dump(mode="json")
                if metadata.image_paths:
                    analysis_metadata["images"] = [
                        {"image_index": index} for index in range(1, len(metadata.image_paths) + 1)
                    ]
                for private_path_key in ("image_paths", "thumbnail_path", "media_path"):
                    analysis_metadata.pop(private_path_key, None)
                context_text = "\n".join(
                    [
                        *[inspiration.text for inspiration in effective_inspirations],
                        *[segment.text for segment in corrected],
                    ]
                )
                analysis_metadata["existing_knowledge"] = self.indexer.find_related_claims(
                    context_text
                )
                async with self.analysis_semaphore:
                    analysis_kwargs = (
                        {
                            "checkpoints": checkpoints,
                            "on_checkpoint": save_checkpoint,
                            "on_progress": model_progress,
                        }
                        if isinstance(self.analysis, OpenAICompatibleProvider)
                        else {}
                    )
                    analysis = await self.analysis.analyze(
                        corrected,
                        [self._ocr_model(item) for item in ocr_items],
                        effective_inspirations,
                        analysis_metadata,
                        **analysis_kwargs,
                    )
                artifacts["analysis_candidate"] = analysis.model_dump(mode="json")
                self.database.update_job(
                    job.id,
                    artifacts={"analysis_candidate": artifacts["analysis_candidate"]},
                    progress=0.8,
                )
            evidence_context = {**artifacts, "metadata": metadata.model_dump(mode="json")}
            if self.config.analysis_mode == AnalysisMode.PROVIDER:
                audit: list[dict[str, Any]] = []
                analysis, removed = self._prune_unverified_analysis_evidence(
                    analysis,
                    evidence_context,
                    audit=audit,
                )
                artifacts["analysis_evidence_audit"] = audit
                if removed:
                    artifacts["analysis_evidence_warning"] = (
                        f"模型生成的 {removed} 条无法核实的证据或内容已移除"
                    )
                model_progress("evidence", 1, 1)
            self._validate_analysis_evidence(analysis, evidence_context)
            artifacts["analysis"] = analysis.model_dump(mode="json")
            artifacts["llm_checkpoints"] = {}
            self.database.update_job(
                job.id,
                artifacts={
                    "analysis": artifacts["analysis"],
                    "llm_checkpoints": {},
                    "analysis_evidence_audit": artifacts.get("analysis_evidence_audit", []),
                    **(
                        {"analysis_evidence_warning": artifacts["analysis_evidence_warning"]}
                        if artifacts.get("analysis_evidence_warning")
                        else {}
                    ),
                },
                progress=0.83,
            )

        now = utc_now()
        validated_analysis = AnalysisResult.model_validate(artifacts["analysis"])
        self._validate_analysis_evidence(
            validated_analysis,
            {**artifacts, "metadata": metadata.model_dump(mode="json")},
        )
        analysis_data = validated_analysis.model_dump(mode="json")
        analysis_data["contradictions"] = self._normalize_contradictions(
            analysis_data.get("contradictions", []), f"dy-{video_id}"
        )
        title = safe_filename(analysis_data.get("title") or metadata.title)
        entry_id = f"dy-{video_id}"
        date_prefix = beijing_date(metadata.published_at or now)
        filename = f"{date_prefix}_{title}_{video_id}.md"
        raw_relative = (
            existing.raw_path
            if existing
            else str(
                Path(creator_folder) / "raw" / "records" / filename
                if creator_folder
                else Path("raw") / filename
            )
        )
        source_relative = (
            existing.source_path
            if existing
            else str(
                Path(creator_folder) / "sources" / filename
                if creator_folder
                else Path("wiki") / "sources" / filename
            )
        )
        cover_path = self._persist_video_cover(
            video_id,
            assets_dir,
            thumbnail_path=metadata.thumbnail_path,
            creator_folder=creator_folder or None,
        )
        expiry = (
            now + timedelta(days=self.config.media.retention_days)
            if request.options.retention == RetentionPolicy.TEMPORARY
            else None
        )
        entry = EntryRecord(
            id=entry_id,
            video_id=video_id,
            title=title,
            original_url=resolved_data["original_url"],
            canonical_url=resolved_data["canonical_url"],
            raw_path=raw_relative,
            source_path=source_relative,
            status="active",
            media_status="present",
            retention=request.options.retention,
            media_expires_at=expiry,
            summary=analysis_data.get("one_liner") or analysis_data.get("summary", ""),
            inspirations=effective_inspirations,
            tags=analysis_data.get("tags", []),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        data: dict[str, Any] = {
            "share_text": request.share_text,
            "inspirations": [item.model_dump(mode="json") for item in effective_inspirations],
            "metadata": metadata.model_dump(mode="json"),
            "transcript_raw": artifacts["transcript_raw"],
            "transcript_corrected": artifacts["transcript_corrected"],
            "ocr": ocr_items,
            "review_issues": [item.model_dump(mode="json") for item in review_issues]
            or artifacts.get("review_issues", []),
            "analysis": analysis_data,
            "relations": [],
            "provider": (
                f"agent:{artifacts.get('analysis_producer', 'gateway')}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.name
            ),
            "model": (
                artifacts.get("analysis_model", "agent")
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.model
            ),
            "prompt_version": (
                f"external:{PROMPT_VERSION}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else PROMPT_VERSION
            ),
            "cover_path": cover_path,
            "cover_kind": metadata.thumbnail_kind or ("fallback" if cover_path else None),
            "creator": (
                {
                    "id": creator_context.get("id"),
                    "folder_path": creator_folder,
                    "parent_job_id": creator_context.get("parent_job_id"),
                }
                if creator_folder
                else {}
            ),
        }
        reminders = [
            ReminderCandidate.model_validate(item) for item in analysis_data.get("reminders", [])
        ]
        if request.options.retention == RetentionPolicy.DISCARD:
            self._trash_assets(entry, mark_database=False)
            entry = entry.model_copy(update={"media_status": "removed"})
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data, reminders=reminders)
        self._persist_entry_documents_and_bundle(
            entry,
            data,
            chunks,
            relations,
            reminders,
            action="ingest",
            log_summary=entry.summary,
            commit_message=f"ingest: {video_id} {entry.title}",
        )

        warnings = []
        if artifacts.get("ocr_warning"):
            warnings.append(f"OCR 未完成：{artifacts['ocr_warning']}")
        if artifacts.get("transcript_skipped") == "no_audio_available":
            warnings.append("视频无音轨，未生成逐字稿；分析仅依据画面文字和作品信息")
        if self.config.analysis_mode != AnalysisMode.GATEWAY and not self.analysis.configured:
            warnings.append("模型未配置，使用本地降级分析")
        if artifacts.get("analysis_evidence_warning"):
            warnings.append(artifacts["analysis_evidence_warning"])
        status = JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED
        return self.database.update_job(
            job.id,
            status=status,
            progress=1,
            result={
                "entry_id": entry.id,
                "source_path": entry.source_path,
                "raw_path": entry.raw_path,
                "summary": entry.summary,
                "ai_judgment": analysis_data.get("ai_judgment", ""),
                "reminder_candidates": analysis_data.get("reminders", []),
                "warnings": warnings,
                "media_provenance": artifacts.get("media_provenance", {}),
                "reacquired": bool(existing),
            },
            unlock=True,
        )

    async def _process_image_note_capture(
        self,
        job: JobRecord,
        artifacts: dict[str, Any],
        resolved_data: dict[str, Any],
    ) -> JobRecord:
        """Process a static image work without invoking any video-only adapter."""
        request = job.request
        work_id = str(resolved_data["video_id"])
        with self.vault.entry_operations_locked():
            # The work lock makes this re-read authoritative for a waiter.
            existing = self.database.find_entry_by_video_id(work_id)
            if existing:
                for inspiration in request.inspirations:
                    existing = self._add_inspiration_locked(existing.id, inspiration)
                existing_data = self.database.get_entry_data(existing.id)
                if self._image_note_files_intact(existing_data):
                    existing = self._repair_entry_if_needed_locked(existing)
                    return self.database.update_job(
                        job.id,
                        status=JobStatus.COMPLETED,
                        progress=1,
                        result={
                            "entry_id": existing.id,
                            "duplicate": True,
                            "source_kind": SourceKind.IMAGE_NOTE.value,
                            "source_path": existing.source_path,
                        },
                        unlock=True,
                    )
        effective_inspirations = existing.inspirations if existing else request.inspirations

        creator_context = artifacts.get("creator_context") or {}
        creator_folder = str(creator_context.get("folder_path") or "")
        images_dir = (
            self.config.vault_path / creator_folder / "raw" / "images" / work_id
            if creator_folder
            else self.config.vault_path / "raw" / "images" / work_id
        )
        metadata: VideoMetadata | None = None
        if "metadata" in artifacts:
            candidate = VideoMetadata.model_validate(artifacts["metadata"])
            if self._metadata_image_paths_exist(candidate):
                metadata = candidate
        if metadata is None:
            self.database.update_job(job.id, status=JobStatus.DOWNLOADING, progress=0.15)
            async with self.download_semaphore:
                metadata = await self.image_note_downloader.download(
                    resolved_data["canonical_url"], work_id, images_dir
                )
            metadata.original_url = resolved_data["original_url"]
            metadata.canonical_url = resolved_data["canonical_url"]
            metadata.source_kind = SourceKind.IMAGE_NOTE
            artifacts["metadata"] = metadata.model_dump(mode="json")
            self.database.update_job(
                job.id, artifacts={"metadata": artifacts["metadata"]}, progress=0.42
            )

        if not creator_folder:
            creator_context, metadata, images_dir = self._adopt_creator_capture(
                metadata,
                work_id=work_id,
                original_url=str(resolved_data["original_url"]),
                canonical_url=str(resolved_data["canonical_url"]),
                current_dir=images_dir,
                media_folder="images",
            )
            if creator_context:
                creator_folder = str(creator_context["folder_path"])
                artifacts["creator_context"] = creator_context
                artifacts["metadata"] = metadata.model_dump(mode="json")
                self.database.update_job(
                    job.id,
                    artifacts={
                        "creator_context": creator_context,
                        "metadata": artifacts["metadata"],
                    },
                )

        absolute_images = [self._vault_path(value) for value in metadata.image_paths]
        images_complete = absolute_images and all(
            path.is_file() and path.stat().st_size for path in absolute_images
        )
        if not images_complete:
            raise JobStateError("图文采集完成，但原图文件不完整")

        if "ocr" not in artifacts:
            self.database.update_job(job.id, status=JobStatus.EXTRACTING, progress=0.48)
            async with self.media_semaphore:
                try:
                    raw_ocr = await self.ocr.recognize(
                        [(index, path) for index, path in enumerate(absolute_images, start=1)]
                    )
                except ExternalToolError as exc:
                    raw_ocr = []
                    artifacts["ocr_warning"] = str(exc)
            image_index_by_path = {
                str(path): index for index, path in enumerate(absolute_images, start=1)
            }
            ocr_items: list[OCRObservation] = []
            for observation in raw_ocr:
                image_index = (
                    observation.image_index
                    or observation.source_index
                    or image_index_by_path.get(
                        str(Path(observation.image_path).resolve())
                        if observation.image_path
                        else ""
                    )
                )
                if image_index is None and observation.timestamp_ms:
                    image_index = int(observation.timestamp_ms)
                ocr_items.append(
                    observation.model_copy(
                        update={
                            "timestamp_ms": None,
                            "image_index": image_index,
                            "image_path": self._vault_relative(
                                absolute_images[(image_index or 1) - 1]
                            ),
                        }
                    )
                )
            artifacts["ocr"] = [item.model_dump(mode="json") for item in ocr_items]
            artifacts["media_provenance"] = {"ocr": getattr(self.ocr, "provenance", {})}
            self.database.update_job(
                job.id,
                artifacts={
                    "ocr": artifacts["ocr"],
                    "media_provenance": artifacts["media_provenance"],
                    **(
                        {"ocr_warning": artifacts["ocr_warning"]}
                        if artifacts.get("ocr_warning")
                        else {}
                    ),
                },
                progress=0.58,
            )
        ocr_models = [self._ocr_model(item) for item in artifacts.get("ocr", [])]

        issues = self._detect_image_review_issues(ocr_models, metadata.post_text or "")
        if issues and not artifacts.get("review_resolved"):
            artifacts["review_issues"] = [item.model_dump(mode="json") for item in issues]
            self.database.replace_review_issues(job.id, issues)
            return self.database.update_job(
                job.id,
                status=JobStatus.NEEDS_REVIEW,
                progress=0.62,
                artifacts={"review_issues": artifacts["review_issues"]},
                result={
                    "phase": "image_ocr_review",
                    "review_issue_count": len(issues),
                },
                unlock=True,
            )
        if artifacts.get("review_resolved"):
            resolved_issues = self.database.get_review_issues(job.id)
            ocr_models = self._apply_image_review_resolutions(ocr_models, resolved_issues)
            artifacts["ocr"] = [item.model_dump(mode="json") for item in ocr_models]
            self.database.update_job(job.id, artifacts={"ocr": artifacts["ocr"]})

        if self.config.analysis_mode == AnalysisMode.GATEWAY and "analysis" not in artifacts:
            return self.database.update_job(
                job.id,
                status=JobStatus.AWAITING_AGENT_ANALYSIS,
                progress=0.68,
                result={"phase": "analysis", "next_tool": "get_analysis_context"},
                unlock=True,
            )

        self.database.update_job(job.id, status=JobStatus.ANALYZING, progress=0.7)
        if "analysis" not in artifacts:
            analysis_metadata = metadata.model_dump(mode="json")
            analysis_metadata["images"] = [
                {"image_index": index} for index in range(1, len(metadata.image_paths) + 1)
            ]
            for private_path_key in ("image_paths", "thumbnail_path", "media_path"):
                analysis_metadata.pop(private_path_key, None)
            context_text = "\n".join(
                [
                    *[item.text for item in effective_inspirations],
                    metadata.post_text or "",
                    *[item.text for item in ocr_models],
                ]
            )
            analysis_metadata["existing_knowledge"] = self.indexer.find_related_claims(context_text)
            async with self.analysis_semaphore:
                analysis = await self.analysis.analyze(
                    [], ocr_models, effective_inspirations, analysis_metadata
                )
            self._validate_analysis_evidence(
                analysis,
                {**artifacts, "metadata": metadata.model_dump(mode="json")},
            )
            artifacts["analysis"] = analysis.model_dump(mode="json")
            self.database.update_job(
                job.id, artifacts={"analysis": artifacts["analysis"]}, progress=0.83
            )

        now = utc_now()
        validated_analysis = AnalysisResult.model_validate(artifacts["analysis"])
        self._validate_analysis_evidence(
            validated_analysis,
            {**artifacts, "metadata": metadata.model_dump(mode="json")},
        )
        analysis_data = validated_analysis.model_dump(mode="json")
        entry_id = f"dy-{work_id}"
        analysis_data["contradictions"] = self._normalize_contradictions(
            analysis_data.get("contradictions", []), entry_id
        )
        title = safe_filename(analysis_data.get("title") or metadata.title)
        date_prefix = beijing_date(metadata.published_at or now)
        filename = f"{date_prefix}_{title}_{work_id}.md"
        raw_relative = (
            existing.raw_path
            if existing
            else str(
                Path(creator_folder) / "raw" / "records" / filename
                if creator_folder
                else Path("raw") / filename
            )
        )
        source_relative = (
            existing.source_path
            if existing
            else str(
                Path(creator_folder) / "sources" / filename
                if creator_folder
                else Path("wiki") / "sources" / filename
            )
        )

        relative_images = [self._vault_relative(path) for path in absolute_images]
        stored_metadata = metadata.model_copy(
            update={
                "image_paths": relative_images,
                "thumbnail_path": relative_images[0],
                "thumbnail_kind": "image_note_first_image",
                "source_kind": SourceKind.IMAGE_NOTE,
            }
        )
        entry = EntryRecord(
            id=entry_id,
            video_id=work_id,
            title=title,
            original_url=resolved_data["original_url"],
            canonical_url=resolved_data["canonical_url"],
            raw_path=raw_relative,
            source_path=source_relative,
            status="active",
            media_status="present",
            retention=RetentionPolicy.KEEP,
            media_expires_at=None,
            summary=analysis_data.get("one_liner") or analysis_data.get("summary", ""),
            inspirations=effective_inspirations,
            tags=analysis_data.get("tags", []),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        review_issues = self.database.get_review_issues(job.id)
        data: dict[str, Any] = {
            "share_text": request.share_text,
            "inspirations": [item.model_dump(mode="json") for item in effective_inspirations],
            "metadata": stored_metadata.model_dump(mode="json"),
            "ocr": [item.model_dump(mode="json") for item in ocr_models],
            "review_issues": [item.model_dump(mode="json") for item in review_issues]
            or artifacts.get("review_issues", []),
            "analysis": analysis_data,
            "relations": [],
            "provider": (
                f"agent:{artifacts.get('analysis_producer', 'gateway')}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.name
            ),
            "model": (
                artifacts.get("analysis_model", "agent")
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.model
            ),
            "prompt_version": (
                f"external:{PROMPT_VERSION}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else PROMPT_VERSION
            ),
            "cover_path": relative_images[0],
            "cover_kind": "image_note_first_image",
            "creator": (
                {
                    "id": creator_context.get("id"),
                    "folder_path": creator_folder,
                    "parent_job_id": creator_context.get("parent_job_id"),
                }
                if creator_folder
                else {}
            ),
        }
        reminders = [
            ReminderCandidate.model_validate(item) for item in analysis_data.get("reminders", [])
        ]
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data, reminders=reminders)
        self._persist_entry_documents_and_bundle(
            entry,
            data,
            chunks,
            relations,
            reminders,
            action="ingest",
            log_summary=entry.summary,
            commit_message=f"ingest-image-note: {work_id} {entry.title}",
        )
        warnings: list[str] = []
        if artifacts.get("ocr_warning"):
            warnings.append(f"OCR 未完成：{artifacts['ocr_warning']}")
        if self.config.analysis_mode != AnalysisMode.GATEWAY and not self.analysis.configured:
            warnings.append("模型未配置，使用本地降级分析")
        status = JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED
        return self.database.update_job(
            job.id,
            status=status,
            progress=1,
            result={
                "entry_id": entry.id,
                "source_kind": SourceKind.IMAGE_NOTE.value,
                "source_path": entry.source_path,
                "raw_path": entry.raw_path,
                "image_count": len(relative_images),
                "summary": entry.summary,
                "reminder_candidates": analysis_data.get("reminders", []),
                "warnings": warnings,
                "media_provenance": artifacts.get("media_provenance", {}),
                "reacquired": bool(existing),
            },
            unlock=True,
        )

    def _persist_video_cover(
        self,
        video_id: str,
        assets_dir: Path,
        *,
        thumbnail_path: str | None = None,
        replace_existing: bool = False,
        creator_folder: str | None = None,
    ) -> str | None:
        covers_dir = (
            self.config.vault_path / creator_folder / "raw" / "covers"
            if creator_folder
            else self.config.vault_path / "raw" / "covers"
        )
        covers_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(covers_dir.glob(f"{video_id}.*"))
        if existing and not replace_existing:
            return str(existing[0].relative_to(self.config.vault_path))

        image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
        candidates: list[Path] = []
        if thumbnail_path:
            candidates.append(Path(thumbnail_path))
        candidates.extend(
            path for path in assets_dir.glob("original.*") if path.suffix.lower() in image_suffixes
        )
        candidates.extend(path for path in (assets_dir / "frames").glob("*.jpg") if path.is_file())
        source = next(
            (
                path
                for path in candidates
                if path.is_file() and path.suffix.lower() in image_suffixes
            ),
            None,
        )
        if source is None:
            return str(existing[0].relative_to(self.config.vault_path)) if existing else None
        suffix = ".jpg" if source.suffix.lower() == ".jpeg" else source.suffix.lower()
        target = covers_dir / f"{video_id}{suffix}"
        shutil.copy2(source, target)
        return str(target.relative_to(self.config.vault_path))
