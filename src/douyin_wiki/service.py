from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
import warnings
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

from send2trash import send2trash

from .adapters.creator import DouyinCreatorAdapter, creator_id_for
from .adapters.embeddings import EmbeddingService
from .adapters.image_note import PlaywrightImageNoteDownloader
from .adapters.llm import (
    PROMPT_VERSION,
    AnalysisProvider,
    FallbackAnalysisProvider,
    OpenAICompatibleProvider,
)
from .adapters.media import (
    FFmpegMediaProcessor,
    VisionOCR,
    WhisperTranscriber,
    YtDlpDownloader,
    download_preferred_cover,
)
from .adapters.reminders import MacOSReminderAdapter
from .adapters.share import DouyinShareResolver
from .auth_guidance import AuthGuidanceLauncher, NoopAuthGuidanceLauncher
from .config import AppConfig
from .database import Database
from .errors import (
    BrowserAuthRequiredError,
    CookieRequiredError,
    DouyinWikiError,
    EntryNotFoundError,
    ExternalToolError,
    JobLeaseLostError,
    JobStateError,
)
from .models import (
    AnalysisMode,
    AnalysisResult,
    AuthCheckResult,
    CaptureOptions,
    CaptureRequest,
    CreatorInventoryResult,
    CreatorInventoryWork,
    CreatorWorkDecision,
    EntryRecord,
    GatewayContext,
    InspirationInput,
    JobEvent,
    JobRecord,
    JobStatus,
    OCRObservation,
    ReminderCandidate,
    ResearchTopic,
    RetentionPolicy,
    ReviewIssue,
    SourceKind,
    SourceRevision,
    TopicArtifact,
    TopicArtifactKind,
    TranscriptCorrection,
    TranscriptSegment,
    VideoMetadata,
)
from .review import apply_review_resolutions, detect_review_issues
from .search import KnowledgeIndexer, KnowledgeSearch
from .time_utils import beijing_date, utc_now
from .vault import VaultWriter, safe_filename


class DouyinWikiService:
    def __init__(
        self,
        config: AppConfig,
        *,
        resolver: DouyinShareResolver | None = None,
        downloader: YtDlpDownloader | None = None,
        image_note_downloader: PlaywrightImageNoteDownloader | None = None,
        creator_adapter: DouyinCreatorAdapter | None = None,
        media: FFmpegMediaProcessor | None = None,
        transcriber: WhisperTranscriber | None = None,
        ocr: VisionOCR | None = None,
        analysis: AnalysisProvider | None = None,
        embeddings: EmbeddingService | None = None,
        reminders: MacOSReminderAdapter | None = None,
        auth_guidance_launcher: AuthGuidanceLauncher | None = None,
    ) -> None:
        self.config = config
        self.database = Database(config.database_path)
        self.vault = VaultWriter(config.vault_path)
        self.resolver = resolver or DouyinShareResolver()
        self.downloader = downloader or YtDlpDownloader(config.media)
        self.image_note_downloader = image_note_downloader or PlaywrightImageNoteDownloader(
            config.media, config.browser_profile_dir
        )
        self.creator_adapter = creator_adapter or DouyinCreatorAdapter(
            config.media, config.browser_profile_dir, self.resolver
        )
        self.media = media or FFmpegMediaProcessor()
        self.transcriber = transcriber or WhisperTranscriber(config.media)
        script = Path(str(files("douyin_wiki").joinpath("resources/vision_ocr.swift")))
        self.ocr = ocr or VisionOCR(script)
        if analysis is not None:
            self.analysis = analysis
        elif config.analysis_mode == AnalysisMode.LOCAL:
            self.analysis = FallbackAnalysisProvider()
        elif config.analysis_mode == AnalysisMode.PROVIDER:
            # Provider mode is an explicit promise to use the configured API.
            # Keep the unconfigured provider so the job fails visibly instead
            # of silently replacing an existing analysis with heuristic output.
            self.analysis = OpenAICompatibleProvider(config.llm)
        else:
            self.analysis = FallbackAnalysisProvider()
        self.embeddings = embeddings or EmbeddingService(config.embeddings)
        self.indexer = KnowledgeIndexer(self.database, self.embeddings)
        self.searcher = KnowledgeSearch(self.database, self.embeddings)
        self.reminders = reminders or MacOSReminderAdapter()
        self.auth_guidance_launcher = auth_guidance_launcher or NoopAuthGuidanceLauncher()
        self.download_semaphore = asyncio.Semaphore(config.worker.download_concurrency)
        self.media_semaphore = asyncio.Semaphore(config.worker.media_concurrency)
        self.analysis_semaphore = asyncio.Semaphore(config.worker.analysis_concurrency)

    def initialize(self, *, initialize_git: bool = True) -> None:
        with self.vault.entry_operations_locked():
            self.vault.initialize(initialize_git=initialize_git)
            branding = self.vault.migrate_branding()
            statuses = self.vault.migrate_visible_status_labels()
            times = self.vault.migrate_visible_times_to_beijing()
            self.database.initialize()
            self._recover_entry_trash_operations_locked()
            creator_views = self._migrate_creator_views()
            if initialize_git:
                self.vault.commit(
                    [*branding, *statuses, *times, *creator_views],
                    "chore: localize 抖库 vault",
                )

    def initialize_runtime(self) -> None:
        """Ensure older Vaults receive safe, additive layout migrations on every entrypoint."""
        with self.vault.entry_operations_locked():
            with self.vault.locked():
                changed = self.vault.initialize(initialize_git=False)
                branding = self.vault.migrate_branding()
                statuses = self.vault.migrate_visible_status_labels()
                times = self.vault.migrate_visible_times_to_beijing()
            self.database.initialize()
            self._recover_entry_trash_operations_locked()
            with self.vault.locked():
                creator_views = self._migrate_creator_views()
                self.vault.commit(
                    [*changed, *branding, *statuses, *times, *creator_views],
                    "chore: update 抖库 vault layout",
                )

    def _migrate_creator_views(self) -> list[Path]:
        entries = self.database.list_entries()
        changed: list[Path] = []
        for creator in self.database.list_creators():
            if not self.vault.creator_index_needs_refresh(creator.folder_path):
                continue
            works = self.database.list_creator_works(creator.id)
            changed.extend(
                self.vault.write_creator(
                    creator,
                    works,
                    entries,
                    action="资料页升级",
                    append_log=False,
                )
            )
        return changed

    async def authenticate_douyin(self, *, timeout_seconds: int = 600) -> dict[str, Any]:
        await self.image_note_downloader.authenticate(timeout_seconds=timeout_seconds)
        check = await self.image_note_downloader.check_auth()
        return check.model_dump(mode="json")

    async def authenticate_video(self) -> dict[str, Any]:
        check = await self.downloader.authenticate()
        return check.model_dump(mode="json")

    async def check_auth_scope(
        self,
        scope: str,
        *,
        video_url: str | None = None,
    ) -> AuthCheckResult:
        if scope == "video":
            adapter = self.downloader
            kwargs = {"video_url": video_url}
            source = self.config.media.browser
        elif scope == "image_note":
            adapter = self.image_note_downloader
            kwargs = {}
            source = str(self.config.browser_profile_dir)
        elif scope == "creator":
            adapter = self.creator_adapter
            kwargs = {}
            source = str(self.config.browser_profile_dir)
        else:
            raise ValueError(f"不支持的授权范围：{scope}")

        check_auth = getattr(adapter, "check_auth", None)
        if check_auth is None:
            return AuthCheckResult(
                scope=scope,
                state="unavailable",
                ok=False,
                cookie_source=source,
                message="当前注入的下载适配器不支持认证状态检查",
            )
        return await check_auth(**kwargs)

    async def get_auth_status(self, *, video_url: str | None = None) -> dict[str, Any]:
        video, image_note, creator = await asyncio.gather(
            self.check_auth_scope("video", video_url=video_url),
            self.check_auth_scope("image_note"),
            self.check_auth_scope("creator"),
        )
        return {
            "video": video.model_dump(mode="json"),
            "image_note": image_note.model_dump(mode="json"),
            "creator": creator.model_dump(mode="json"),
            "cookie_values_exposed": False,
        }

    def capture_douyin(
        self,
        share_text: str,
        inspirations: list[InspirationInput] | None = None,
        options: CaptureOptions | None = None,
        gateway_context: GatewayContext | None = None,
    ) -> JobRecord:
        request = CaptureRequest(
            share_text=share_text,
            inspirations=inspirations or [],
            options=options or CaptureOptions(),
            gateway_context=gateway_context,
        )
        return self.database.create_job(request)

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
        self._refresh_creator_documents(str(job.artifacts["creator_id"]), action="selection")
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
        self._refresh_creator_documents(creator_id, action="confirm-import")
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

    def get_job(self, job_id: str) -> JobRecord:
        job = self.database.get_job(job_id)
        self._apply_live_creator_selection(job)
        if job.status == JobStatus.NEEDS_REVIEW:
            job.result["review_issues"] = [
                issue.model_dump(mode="json")
                for issue in self.database.get_review_issues(job_id, open_only=True)
            ]
        return job

    def list_jobs(self, status: JobStatus | None = None, limit: int = 50) -> list[JobRecord]:
        jobs = self.database.list_jobs(status, limit)
        for job in jobs:
            self._apply_live_creator_selection(job)
        return jobs

    def _apply_live_creator_selection(self, job: JobRecord) -> None:
        """Expose current creator decisions instead of the dispatch-time snapshot."""
        if job.kind != "creator_import" or not job.artifacts.get("creator_id"):
            return
        summary = self.database.creator_inventory_summary(job.id)
        if summary.get("total", 0):
            job.result["selection"] = summary

    def list_job_events(
        self,
        *,
        after_event_id: int = 0,
        unacknowledged_only: bool = True,
        limit: int = 50,
    ) -> list[JobEvent]:
        return self.database.list_job_events(
            after_event_id=after_event_id,
            unacknowledged_only=unacknowledged_only,
            limit=limit,
        )

    def acknowledge_job_event(self, event_id: int) -> JobEvent:
        return self.database.acknowledge_job_event(event_id)

    def get_analysis_context(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if job.status not in {
            JobStatus.AWAITING_AGENT_ANALYSIS,
            JobStatus.NEEDS_REVIEW,
        }:
            raise JobStateError("只有处于“待 AI 处理”或“需要人工复核”状态的任务可读取分析上下文")
        artifacts = job.artifacts
        transcript = artifacts.get("transcript_corrected") or artifacts.get("transcript_raw", [])
        metadata = artifacts.get("metadata", {})
        context_text = "\n".join(
            [
                *[item.text for item in job.request.inspirations],
                *[str(item.get("text", "")) for item in transcript],
                str(metadata.get("post_text") or ""),
                *[str(item.get("text", "")) for item in artifacts.get("ocr", [])],
            ]
        )
        is_image_note = metadata.get("source_kind") == SourceKind.IMAGE_NOTE.value
        return {
            "job_id": job.id,
            "status": job.status.value,
            "phase": job.result.get("phase", "transcript_correction"),
            "gateway_context": job.request.gateway_context.model_dump(mode="json")
            if job.request.gateway_context
            else None,
            "metadata": metadata,
            "inspirations_verbatim": [
                item.model_dump(mode="json") for item in job.request.inspirations
            ],
            "transcript_raw": artifacts.get("transcript_raw", []),
            "transcript_corrected": artifacts.get("transcript_corrected"),
            "ocr": artifacts.get("ocr", []),
            "review_issues": [
                issue.model_dump(mode="json")
                for issue in self.database.get_review_issues(job_id, open_only=True)
            ],
            "existing_knowledge": self.indexer.find_related_claims(context_text),
            "analysis_schema": AnalysisResult.model_json_schema(),
            "rules": [
                "灵感必须逐字保留，不得改写或补造。",
                *(
                    []
                    if is_image_note
                    else ["校正只能修复明显识别错误，不得摘要、删句或补充原视频没有的内容。"]
                ),
                "分析必须区分作品原话、作品正文、OCR、AI 推断和用户灵感。",
                "事实、数字、日期、参数和方法应写入 knowledge_atoms，"
                "并尽可能带 quote，以及 timestamp_ms 或 image_index。",
                *(
                    [
                        "视频必须按内容展开顺序生成时间轴图解 chapters；章节起点应定位主题开始，"
                        "每章必须包含可核验的语音或画面证据。"
                    ]
                    if not is_image_note
                    else ["静态图文没有视频时间轴，chapters 必须为空数组。"]
                ),
                *(
                    ["图文不得伪造 00:00 时间戳，必须用 image_index 定位原图。"]
                    if is_image_note
                    else []
                ),
                "content_card.kind 必须与 content_type 相同；提醒时间不明确时必须标记需澄清。",
            ],
        }

    def submit_transcript_correction(
        self,
        job_id: str,
        corrections: list[TranscriptCorrection],
        *,
        producer: str,
        model: str = "agent",
        review_issues: list[ReviewIssue] | None = None,
    ) -> JobRecord:
        job = self.database.get_job(job_id)
        if job.status != JobStatus.AWAITING_AGENT_ANALYSIS:
            raise JobStateError("只有处于“待 AI 处理”状态的任务可提交逐字稿校正")
        raw = [
            TranscriptSegment.model_validate(item)
            for item in job.artifacts.get("transcript_raw", [])
        ]
        known_ids = {segment.id for segment in raw}
        by_id: dict[int, str] = {}
        for correction in corrections:
            if correction.id not in known_ids:
                raise JobStateError(f"未知逐字稿片段 id: {correction.id}")
            if correction.id in by_id:
                raise JobStateError(f"重复逐字稿片段 id: {correction.id}")
            by_id[correction.id] = correction.text.strip()
        corrected = [
            segment.model_copy(update={"text": by_id.get(segment.id, segment.text)})
            for segment in raw
        ]
        issues = _deduplicate_issues([*detect_review_issues(raw), *(review_issues or [])])
        artifact_update = {
            "transcript_corrected": [item.model_dump(mode="json") for item in corrected],
            "review_issues": [item.model_dump(mode="json") for item in issues],
            "correction_producer": producer,
            "correction_model": model,
        }
        self.database.replace_review_issues(job_id, issues)
        if issues:
            return self.database.update_job(
                job_id,
                status=JobStatus.NEEDS_REVIEW,
                progress=0.66,
                artifacts=artifact_update,
                result={"phase": "human_review", "review_issue_count": len(issues)},
                unlock=True,
            )
        updated = self.database.update_job(
            job_id,
            progress=0.68,
            artifacts=artifact_update,
            result={"phase": "analysis"},
            unlock=True,
        )
        self.database.emit_job_event(job_id, JobStatus.AWAITING_AGENT_ANALYSIS, updated.result)
        return updated

    def submit_gateway_analysis(
        self,
        job_id: str,
        analysis: dict[str, Any],
        *,
        producer: str,
        model: str = "agent",
    ) -> JobRecord:
        job = self.database.get_job(job_id)
        if job.status != JobStatus.AWAITING_AGENT_ANALYSIS:
            raise JobStateError("只有处于“待 AI 处理”状态的任务可提交分析")
        source_kind = job.artifacts.get("metadata", {}).get("source_kind", "video")
        if (
            source_kind != SourceKind.IMAGE_NOTE.value
            and "transcript_corrected" not in job.artifacts
        ):
            raise JobStateError("请先提交逐字稿校正")
        if self.database.get_review_issues(job_id, open_only=True):
            raise JobStateError("逐字稿仍有未解决疑点")
        validated = AnalysisResult.model_validate(analysis)
        self._validate_analysis_evidence(validated, job.artifacts)
        reanalyze_entry_id = job.artifacts.get("reanalyze_entry_id")
        video_id = job.artifacts.get("resolved", {}).get("video_id", "")
        source_entry_id = reanalyze_entry_id or f"dy-{video_id}"
        normalized = validated.model_dump(mode="json")
        normalized["contradictions"] = self._normalize_contradictions(
            normalized.get("contradictions", []), source_entry_id
        )
        return self.database.requeue_job(
            job_id,
            artifacts={
                "analysis": normalized,
                "analysis_producer": producer,
                "analysis_model": model,
            },
        )

    def approve_job(self, job_id: str) -> JobRecord:
        job = self.database.get_job(job_id)
        if job.status != JobStatus.WAITING_CONFIRMATION:
            raise JobStateError("只有处于“等待用户确认”状态的任务可以批准")
        return self.database.requeue_job(
            job_id,
            artifacts={
                "ai_analysis_approved": True,
                "cloud_analysis_approved": True,
                "long_video_approved": True,
            },
        )

    def retry_job(self, job_id: str) -> JobRecord:
        """Retry a failed job from its last persisted stage checkpoint."""
        job = self.database.get_job(job_id)
        if job.kind == "media_restore":
            return self.database.requeue_job_deduplicated(
                job_id, match_artifact="entry_id"
            )
        if job.status not in {JobStatus.FAILED, JobStatus.NEEDS_AUTH}:
            raise JobStateError("只有“失败”或“需要登录授权”的任务可以重试")
        return self.database.requeue_job(job_id)

    def resolve_review(
        self,
        job_id: str,
        resolutions: dict[str, str] | None = None,
        *,
        accept_uncertain: bool = False,
    ) -> JobRecord:
        job = self.database.get_job(job_id)
        if job.status != JobStatus.NEEDS_REVIEW:
            raise JobStateError("只有处于“需要人工复核”状态的任务可以提交校对")
        issues = self.database.get_review_issues(job_id, open_only=True)
        if not accept_uncertain and set(resolutions or {}) != {issue.id for issue in issues}:
            raise JobStateError("必须解决全部疑点，或明确 accept_uncertain=true")
        resolved_values = resolutions or {issue.id: issue.raw_text for issue in issues}
        self.database.resolve_review_issues(job_id, resolved_values)
        return self.database.requeue_job(job_id, artifacts={"review_resolved": True})

    def add_inspiration(self, entry_id: str, inspiration: InspirationInput) -> EntryRecord:
        with self.vault.entry_operations_locked():
            return self._add_inspiration_locked(entry_id, inspiration)

    def _add_inspiration_locked(
        self, entry_id: str, inspiration: InspirationInput
    ) -> EntryRecord:
        entry = self.database.get_entry(entry_id)
        if inspiration in entry.inspirations:
            return entry
        entry = entry.model_copy(
            update={
                "inspirations": [*entry.inspirations, inspiration],
                "updated_at": utc_now(),
            }
        )
        data = self.database.get_entry_data(entry_id)
        data.pop("purposes", None)
        data["inspirations"] = [item.model_dump(mode="json") for item in entry.inspirations]
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data)
        self._write_entry_documents(
            entry,
            data,
            action="inspiration",
            log_summary=inspiration.text,
            commit_message=f"inspiration: {entry.video_id} {entry.title}",
        )
        self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
        return self.database.get_entry(entry_id)

    def add_purpose(self, entry_id: str, purpose: InspirationInput) -> EntryRecord:
        """Deprecated compatibility alias; use add_inspiration."""
        return self.add_inspiration(entry_id, purpose)

    def set_entry_favorite(self, entry_id: str, favorite: bool) -> dict[str, Any]:
        with self.vault.entry_operations_locked():
            entry = self.database.get_entry(entry_id)
            data = self.database.get_entry_data(entry_id)
            source_kind = data.get("metadata", {}).get(
                "source_kind", SourceKind.VIDEO.value
            )
            now = utc_now()
            if favorite or source_kind == SourceKind.IMAGE_NOTE.value:
                retention = RetentionPolicy.KEEP
                expires_at = None
            else:
                retention = RetentionPolicy.TEMPORARY
                expires_at = now + timedelta(days=self.config.media.retention_days)
            updated = entry.model_copy(
                update={
                    "favorite": favorite,
                    "retention": retention,
                    "media_expires_at": expires_at,
                    "updated_at": now,
                }
            )
            self._write_entry_documents(
                updated,
                data,
                action="favorite",
                log_summary="收藏资料" if favorite else "取消收藏",
                commit_message=(
                    f"favorite: {entry.video_id} {entry.title}"
                    if favorite
                    else f"unfavorite: {entry.video_id} {entry.title}"
                ),
            )
            persisted = self.database.upsert_entry(updated, data)
            restore_job = None
            if (
                favorite
                and source_kind == SourceKind.VIDEO.value
                and persisted.media_status == "removed"
            ):
                restore_job = self.database.get_or_create_active_job(
                    CaptureRequest(
                        share_text=persisted.original_url,
                        options=CaptureOptions(retention=RetentionPolicy.KEEP),
                    ),
                    kind="media_restore",
                    artifacts={"entry_id": persisted.id},
                    match_artifact="entry_id",
                )
            return {"entry": persisted, "restore_job": restore_job}

    def submit_analysis(
        self,
        entry_id: str,
        analysis: dict[str, Any],
        *,
        producer: str,
        model: str = "agent",
    ) -> EntryRecord:
        with self.vault.entry_operations_locked():
            return self._submit_analysis_locked(
                entry_id, analysis, producer=producer, model=model
            )

    def _submit_analysis_locked(
        self,
        entry_id: str,
        analysis: dict[str, Any],
        *,
        producer: str,
        model: str,
    ) -> EntryRecord:
        entry = self.database.get_entry(entry_id)
        data = self.database.get_entry_data(entry_id)
        current_analysis = data.get("analysis", {})
        incoming = dict(analysis)
        # A v1 integration may edit only `summary` on a payload that also contains
        # v2 compatibility fields. Treat that as an intentional one-liner update.
        if (
            incoming.get("summary")
            and incoming.get("summary") != current_analysis.get("summary")
            and incoming.get("one_liner") == current_analysis.get("one_liner")
        ):
            incoming["one_liner"] = incoming["summary"]
        validated = AnalysisResult.model_validate(incoming)
        self._validate_analysis_evidence(validated, data)
        validated = AnalysisResult.model_validate(
            {
                **validated.model_dump(mode="json"),
                "contradictions": self._normalize_contradictions(
                    validated.model_dump(mode="json").get("contradictions", []), entry.id
                ),
            }
        )
        data["analysis"] = validated.model_dump(mode="json")
        data.pop("fact_checks", None)
        data["provider"] = f"agent:{producer}"
        data["model"] = model
        data["prompt_version"] = f"external:{PROMPT_VERSION}"
        entry = entry.model_copy(
            update={
                "title": safe_filename(validated.title),
                "summary": validated.one_liner,
                "tags": validated.tags,
                "updated_at": utc_now(),
            }
        )
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data)
        self._write_entry_documents(
            entry,
            data,
            action="analysis",
            log_summary=f"由 {producer} 提交重分析",
            commit_message=f"analysis: {entry.video_id} {producer}",
        )
        self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
        return self.database.get_entry(entry_id)

    def reanalyze_entry(
        self,
        entry_id: str,
        *,
        force: bool = False,
        gateway_context: GatewayContext | None = None,
    ) -> JobRecord:
        entry = self.database.get_entry(entry_id)
        data = self.database.get_entry_data(entry_id)
        return self.database.create_reanalysis_job(
            entry,
            force=force,
            gateway_context=gateway_context,
            data=data,
        )

    def reanalyze_all(
        self,
        *,
        force: bool = False,
        gateway_context: GatewayContext | None = None,
    ) -> dict[str, Any]:
        queued: list[str] = []
        skipped: list[str] = []
        for entry in self.database.list_entries():
            data = self.database.get_entry_data(entry.id)
            if data.get("analysis", {}).get("analysis_version") == 2 and not force:
                skipped.append(entry.id)
                continue
            job = self.database.create_reanalysis_job(
                entry,
                force=force,
                gateway_context=gateway_context,
                data=data,
            )
            queued.append(job.id)
        return {"queued_job_ids": queued, "skipped_entry_ids": skipped, "force": force}

    def search_knowledge(
        self,
        query: str,
        *,
        include_stale: bool = False,
        limit: int = 10,
        entry_ids: list[str] | None = None,
    ):
        self.indexer.ensure_embedding_compatibility()
        return self.searcher.search(
            query,
            include_stale=include_stale,
            limit=limit,
            entry_ids=entry_ids,
        )

    @staticmethod
    def _topic_revision(
        entries: list[tuple[EntryRecord, bool]],
    ) -> tuple[str, list[SourceRevision]]:
        revisions = [
            SourceRevision(
                entry_id=entry.id,
                updated_at=entry.updated_at,
                enabled=enabled,
            )
            for entry, enabled in entries
        ]
        payload = [item.model_dump(mode="json") for item in revisions]
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return digest, revisions

    def _refresh_topic(self, topic_id: str) -> ResearchTopic:
        topic = self.database.get_topic(topic_id)
        entries = [
            (self.database.get_entry(source.entry_id), source.enabled)
            for source in topic.sources
        ]
        revision, _ = self._topic_revision(entries)
        if revision != topic.source_revision:
            topic = self.database.update_topic_revision(
                topic_id,
                source_revision=revision,
                source_versions={
                    entry.id: entry.updated_at.isoformat() for entry, _ in entries
                },
            )
            self._persist_topic(topic)
        return topic

    def _persist_topic(self, topic: ResearchTopic) -> list[Path]:
        artifacts = self.database.list_topic_artifacts(topic.id)
        entries = {
            source.entry_id: self.database.get_entry(source.entry_id)
            for source in topic.sources
        }
        with self.vault.locked():
            changed = self.vault.write_topic(topic, artifacts, entries)
            topics_index = self.vault.write_topics_index(self.database.list_topics())
            changed.append(topics_index)
            self._commit_vault(changed, f"docs: update topic {topic.id}")
        return changed

    def create_topic(
        self,
        title: str,
        entry_ids: list[str],
        *,
        goal: str = "",
        instructions: str = "",
    ) -> dict[str, Any]:
        with self.vault.entry_operations_locked():
            return self._create_topic_locked(
                title, entry_ids, goal=goal, instructions=instructions
            )

    def _create_topic_locked(
        self,
        title: str,
        entry_ids: list[str],
        *,
        goal: str,
        instructions: str,
    ) -> dict[str, Any]:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("专题标题不能为空")
        unique_ids = list(dict.fromkeys(value.strip() for value in entry_ids if value.strip()))
        if not unique_ids:
            raise ValueError("请至少选择一篇文章作为专题来源")
        entries = [self.database.get_entry(entry_id) for entry_id in unique_ids]
        revision, _ = self._topic_revision([(entry, True) for entry in entries])
        topic = self.database.create_topic(
            topic_id=f"topic-{uuid.uuid4().hex[:12]}",
            title=normalized_title[:200],
            goal=goal.strip()[:4000],
            instructions=instructions.strip()[:8000],
            source_revision=revision,
        )
        topic = self.database.set_topic_sources(
            topic.id,
            [(entry.id, True, entry.updated_at.isoformat()) for entry in entries],
            source_revision=revision,
        )
        self._persist_topic(topic)
        return self.get_topic(topic.id)

    def get_topic(self, topic_id: str) -> dict[str, Any]:
        topic = self._refresh_topic(topic_id)
        artifacts = self.database.list_topic_artifacts(topic_id)
        return {
            "topic": topic.model_dump(mode="json"),
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        }

    def list_topics(self) -> list[dict[str, Any]]:
        return [self.get_topic(topic.id) for topic in self.database.list_topics()]

    def set_topic_sources(
        self,
        topic_id: str,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.vault.entry_operations_locked():
            return self._set_topic_sources_locked(topic_id, sources)

    def _set_topic_sources_locked(
        self,
        topic_id: str,
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        ordered: list[tuple[EntryRecord, bool]] = []
        seen: set[str] = set()
        for source in sources:
            entry_id = str(source.get("entry_id") or "").strip()
            if not entry_id or entry_id in seen:
                if entry_id in seen:
                    raise ValueError("专题来源不能重复")
                raise ValueError("专题来源缺少文章 ID")
            seen.add(entry_id)
            ordered.append((self.database.get_entry(entry_id), bool(source.get("enabled", True))))
        if not ordered:
            raise ValueError("专题必须保留至少一篇来源")
        revision, _ = self._topic_revision(ordered)
        topic = self.database.set_topic_sources(
            topic_id,
            [
                (entry.id, enabled, entry.updated_at.isoformat())
                for entry, enabled in ordered
            ],
            source_revision=revision,
        )
        self._persist_topic(topic)
        return self.get_topic(topic_id)

    def search_topic(
        self,
        topic_id: str,
        query: str,
        *,
        include_stale: bool = False,
        limit: int = 10,
    ):
        self._refresh_topic(topic_id)
        entry_ids = self.database.enabled_topic_entry_ids(topic_id)
        return self.search_knowledge(
            query,
            include_stale=include_stale,
            limit=limit,
            entry_ids=entry_ids,
        )

    def _topic_context(
        self, topic: ResearchTopic
    ) -> tuple[list[dict[str, Any]], list[SourceRevision]]:
        enabled_sources = [source for source in topic.sources if source.enabled]
        if not enabled_sources:
            raise ValueError("当前专题没有启用的来源")
        enabled_entries = [
            self.database.get_entry(source.entry_id) for source in enabled_sources
        ]
        _, revisions = self._topic_revision(
            [(entry, True) for entry in enabled_entries]
        )
        per_source_budget = max(1200, min(14_000, 52_000 // len(enabled_sources)))
        contexts: list[dict[str, Any]] = []
        for source, entry in zip(enabled_sources, enabled_entries, strict=True):
            data = self.database.get_entry_data(source.entry_id)
            analysis = data.get("analysis", {})
            context = {
                "entry_id": entry.id,
                "title": entry.title,
                "original_url": entry.original_url,
                "inspirations": [item.model_dump(mode="json") for item in entry.inspirations],
                "summary": analysis.get("one_liner") or entry.summary,
                "takeaways": analysis.get("takeaways", []),
                "content_card": analysis.get("content_card", {}),
                "chapters": analysis.get("chapters", []),
                "knowledge_atoms": [
                    atom for atom in analysis.get("knowledge_atoms", []) if not atom.get("stale")
                ],
            }
            serialized = json.dumps(context, ensure_ascii=False, default=str)
            if len(serialized) > per_source_budget:
                context = {
                    "entry_id": entry.id,
                    "title": entry.title,
                    "original_url": entry.original_url,
                    "inspirations": context["inspirations"][:3],
                    "summary": context["summary"],
                    "takeaways": context["takeaways"][:3],
                    "chapters": context["chapters"][:6],
                    "knowledge_atoms": context["knowledge_atoms"][:5],
                }
            contexts.append(context)
        return contexts, revisions

    async def generate_topic_artifact(
        self,
        topic_id: str,
        kind: TopicArtifactKind,
        *,
        provider: Any | None = None,
    ) -> dict[str, Any]:
        labels = {
            "overview": "专题总览",
            "comparison": "跨来源对比表",
            "evidence_map": "证据地图",
            "consensus": "共识与分歧",
            "decision_brief": "决策简报",
            "faq": "专题 FAQ",
        }
        if kind == "note":
            raise ValueError("用户笔记请使用 save_topic_note 保存")
        topic = self._refresh_topic(topic_id)
        contexts, revisions = self._topic_context(topic)
        if provider is None:
            from .webapp.chat import OpenAICompatibleChatProvider

            provider = OpenAICompatibleChatProvider(self.config.llm)
        system = (
            "你是抖库的专题研究助手。只能使用给出的专题来源，不得使用外部知识或全库其他文章。"
            "区分作品原话、作者观点、测试观察和 AI 推断。每项重要结论必须在句末使用"
            "〔entry_id〕标注来源；证据不足时明确写“当前专题没有相关证据”。输出中文 Markdown。"
        )
        request = {
            "artifact": labels[kind],
            "topic_title": topic.title,
            "research_goal": topic.goal,
            "custom_instructions": topic.instructions,
            "required_sections": {
                "overview": ["研究问题", "来源角色", "核心结论"],
                "comparison": ["Markdown 对比表：观点、依据、适用条件、局限"],
                "evidence_map": ["主张", "支持来源", "反对来源", "证据不足"],
                "consensus": ["共识", "分歧", "分歧成立的条件", "未知信息"],
                "decision_brief": ["可选方案", "支持依据", "风险", "未知信息", "下一步行动"],
                "faq": ["只收录来源能够回答的问题与答案"],
            }[kind],
            "sources": contexts,
        }
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(request, ensure_ascii=False, default=str),
            },
        ]
        answer = ""
        usage: dict[str, int | None] = {}
        async for chunk in provider.stream(messages):
            answer += chunk.text
            if chunk.usage:
                usage = chunk.usage
        if not answer.strip():
            raise ExternalToolError("模型没有返回专题成果")
        enabled_ids = {source.entry_id for source in topic.sources if source.enabled}
        if not any(entry_id in answer for entry_id in enabled_ids):
            raise ExternalToolError("专题成果缺少来源标注，未保存；请重试")
        now = utc_now()
        artifact = TopicArtifact(
            id=f"{kind}-{uuid.uuid4().hex[:12]}",
            topic_id=topic.id,
            kind=kind,
            title=labels[kind],
            content_markdown=answer.strip(),
            source_revision=topic.source_revision,
            source_revisions=revisions,
            model=provider.model or None,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            created_at=now,
            updated_at=now,
        )
        artifact = self.database.save_topic_artifact(artifact)
        self._persist_topic(topic)
        return artifact.model_dump(mode="json")

    def save_topic_note(
        self,
        topic_id: str,
        content: str,
        *,
        title: str = "专题笔记",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("保存专题笔记前需要用户明确确认")
        literal = content.strip()
        if not literal:
            raise ValueError("专题笔记不能为空")
        topic = self._refresh_topic(topic_id)
        _, revisions = self._topic_context(topic)
        now = utc_now()
        artifact = TopicArtifact(
            id=f"note-{uuid.uuid4().hex[:12]}",
            topic_id=topic_id,
            kind="note",
            title=title.strip()[:200] or "专题笔记",
            content_markdown=literal,
            source_revision=topic.source_revision,
            source_revisions=revisions,
            prompt_version="user-note-v1",
            user_authored=True,
            created_at=now,
            updated_at=now,
        )
        artifact = self.database.save_topic_artifact(artifact)
        self._persist_topic(topic)
        return artifact.model_dump(mode="json")

    def get_entry(self, entry_id: str, *, include_documents: bool = False) -> dict[str, Any]:
        entry = self.database.get_entry(entry_id)
        entry_data = self.database.get_entry_data(entry_id)
        result: dict[str, Any] = {
            "entry": entry.model_dump(mode="json"),
            "data": entry_data,
            "relations": self.database.get_relations(entry_id),
        }
        if include_documents:
            raw_path = self.config.vault_path / entry.raw_path
            source_path = self.config.vault_path / entry.source_path
            result["raw_markdown"] = (
                raw_path.read_text(encoding="utf-8") if raw_path.exists() else None
            )
            result["source_markdown"] = (
                source_path.read_text(encoding="utf-8") if source_path.exists() else None
            )
            creator_folder = str(entry_data.get("creator", {}).get("folder_path") or "")
            machine_path = self.config.vault_path / (
                Path(creator_folder) / ".data" / "sources" / f"{entry.video_id}.md"
                if creator_folder
                else Path("wiki") / ".data" / "sources" / f"{entry.video_id}.md"
            )
            result["machine_markdown"] = (
                machine_path.read_text(encoding="utf-8") if machine_path.exists() else None
            )
        return result

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
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data)
        self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
        dependency_result = self.database.restore_entry_dependencies(entry.id, dependencies)
        restored_topic_ids = self._restore_trashed_topic_sources(
            entry, dependencies, persist=False
        )
        return dependency_result, restored_topic_ids

    def _move_trashed_files_back(
        self, item_dir: Path, manifest: dict[str, Any]
    ) -> None:
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
                self._refresh_topic(topic_id)
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已删除，但专题 {topic_id} 更新失败：{exc}")
        creator_ids = {
            str(item.get("creator_id"))
            for item in dependencies.get("creator_works", [])
            if item.get("creator_id")
        }
        for creator_id in creator_ids:
            try:
                self._refresh_creator_documents(creator_id, action="删除入库资料")
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
                self._persist_topic(self.database.get_topic(topic_id))
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已恢复，但专题 {topic_id} 更新失败：{exc}")
        for creator_id in creator_ids:
            try:
                self._refresh_creator_documents(creator_id, action="恢复入库资料")
            except (KeyError, Exception) as exc:
                warnings.append(f"资料已恢复，但博主 {creator_id} 更新失败：{exc}")
        return warnings

    @staticmethod
    def _normalize_trashed_entry_data(
        entry: EntryRecord, data: dict[str, Any]
    ) -> dict[str, Any]:
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
            revision, _ = self._topic_revision(ordered)
            restored_topic = self.database.set_topic_sources(
                topic_id,
                [
                    (value.id, enabled, value.updated_at.isoformat())
                    for value, enabled in ordered
                ],
                source_revision=revision,
            )
            if persist:
                self._persist_topic(restored_topic)
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

            warnings = self._finalize_deleted_entry(
                entry, data, dependencies, paths
            )
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

    def restore_trashed_entry(
        self, trash_id: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
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

    def recover_entry_trash_operations(self) -> dict[str, Any]:
        """Recover interrupted delete/restore operations after an unclean exit."""
        with self.vault.entry_operations_locked():
            return self._recover_entry_trash_operations_locked()

    def _recover_entry_trash_operations_locked(self) -> dict[str, Any]:
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
                    warnings = self._finalize_deleted_entry(
                        entry, data, dependencies, paths
                    )
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
                                raise ValueError(
                                    f"恢复中断后出现文件冲突：{relative.as_posix()}"
                                )
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
                report["warnings"].append(
                    f"废纸篓事务 {item_dir.name} 自动恢复失败：{exc}"
                )
        return report

    def migrate_inspiration_vocabulary(self) -> dict[str, Any]:
        migrated_entries = self.database.migrate_inspiration_vocabulary()
        changed_paths: list[Path] = []
        with self.vault.locked():
            changed_paths.extend(self.vault.migrate_inspiration_vocabulary())
            for entry in self.database.list_entries():
                data = self.database.get_entry_data(entry.id)
                self.indexer.index_entry(entry, data)
                changed_paths.append(self.vault.refresh_source(entry, data))
            changed_paths = list(dict.fromkeys(changed_paths))
            if changed_paths:
                log = self.vault.append_log(
                    "vocabulary",
                    "用途更名为灵感",
                    "更新用户可见字段名，保留原始内容和旧接口兼容",
                    changed_paths,
                )
                changed_paths.append(log)
                self.vault.commit(changed_paths, "schema: rename purpose to inspiration")
        return {
            "status": "migrated",
            "entries": migrated_entries,
            "changed_files": [
                str(path.relative_to(self.config.vault_path)) for path in changed_paths
            ],
        }

    def remove_external_validation(self) -> dict[str, Any]:
        report = self.database.remove_external_validation()
        changed_paths: list[Path] = []
        with self.vault.locked():
            changed_paths.extend(self.vault.remove_external_validation_labels())
            for entry in self.database.list_entries():
                data = self.database.get_entry_data(entry.id)
                changed_paths.append(self.vault.refresh_source(entry, data))
            changed_paths = list(dict.fromkeys(changed_paths))
            if changed_paths:
                log = self.vault.append_log(
                    "schema",
                    "移除第三方核验流程",
                    "保留关键主张、视频原话和时间戳；移除核验状态与外部来源",
                    changed_paths,
                )
                changed_paths.append(log)
                self.vault.commit(changed_paths, "schema: remove external validation")
        return {
            "status": "removed",
            **report,
            "changed_files": [
                str(path.relative_to(self.config.vault_path)) for path in changed_paths
            ],
        }

    def confirm_reminder(
        self,
        entry_id: str,
        reminder_id: str,
        *,
        due_at: str | None = None,
        title: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if not confirmed:
            raise JobStateError("创建系统提醒前必须传入 confirmed=true，表示用户已明确确认")
        entry = self.database.get_entry(entry_id)
        candidate, status, system_id = self.database.get_reminder(entry_id, reminder_id)
        if status == "created":
            warning = self._sync_reminder_documents(entry_id)
            return {
                "status": status,
                "system_id": system_id,
                "candidate": candidate.model_dump(mode="json"),
                **({"warning": warning} if warning else {}),
            }
        candidate_data = candidate.model_dump(mode="python")
        if due_at:
            candidate_data["due_at"] = due_at
            candidate_data["needs_clarification"] = False
        if title:
            candidate_data["title"] = title
        candidate = ReminderCandidate.model_validate(candidate_data)
        if status != "creating" and not self.database.claim_reminder_creation(
            entry_id, reminder_id, candidate
        ):
            candidate, status, system_id = self.database.get_reminder(entry_id, reminder_id)
            return {
                "status": status,
                "system_id": system_id,
                "candidate": candidate.model_dump(mode="json"),
            }
        try:
            created_id = self.reminders.create(
                candidate,
                source_url=entry.original_url,
                idempotency_key=f"{entry_id}:{candidate.id}",
            )
        except Exception:
            self.database.release_reminder_creation(entry_id, reminder_id)
            raise
        self.database.mark_reminder_created(entry_id, reminder_id, created_id)
        warning = self._sync_reminder_documents(entry_id)
        return {
            "status": "created",
            "system_id": created_id,
            "candidate": candidate.model_dump(mode="json"),
            **({"warning": warning} if warning else {}),
        }

    def _sync_reminder_documents(self, entry_id: str) -> str | None:
        with self.vault.entry_operations_locked(), self.vault.locked():
            entry = self.database.get_entry(entry_id)
            data = self.database.get_entry_data(entry_id)
            written = self.vault.write_entry(entry, data)
            commit_failed = (
                bool(written.changed_paths)
                and (self.config.vault_path / ".git").exists()
                and not self.vault.commit(
                    written.changed_paths, f"reminder: record state for {entry.video_id}"
                )
            )
            if commit_failed:
                return "提醒已创建并保存，但 Git 提交失败；后续维护会再次提交"
        return None

    async def process_claimed_job(self, job: JobRecord) -> JobRecord:
        try:
            if job.kind == "creator_import":
                outcome = await self._process_creator_import(job)
            elif job.kind == "reanalyze":
                outcome = await self._process_reanalysis(job)
            elif job.kind == "media_restore":
                outcome = await self._process_media_restore(job)
            else:
                outcome = await self._process_capture(job)
        except JobLeaseLostError:
            raise
        except (BrowserAuthRequiredError, CookieRequiredError) as exc:
            is_video = isinstance(exc, CookieRequiredError)
            is_creator = job.kind == "creator_import"
            next_command = "douyin-wiki auth video" if is_video else "douyin-wiki auth douyin"
            auth_scope = "video" if is_video else ("creator" if is_creator else "image_note")
            outcome = self.database.update_job(
                job.id,
                status=JobStatus.NEEDS_AUTH,
                error_code=exc.code,
                error_message=str(exc),
                result={
                    "reason": exc.code,
                    "auth_scope": auth_scope,
                    "next_command": next_command,
                    "retry_command": f"douyin-wiki jobs retry {job.id}",
                    "details": exc.details,
                },
                unlock=True,
            )
            with suppress(Exception):
                self.auth_guidance_launcher.launch(
                    scope=auth_scope,
                    trigger_job_id=job.id,
                )
        except DouyinWikiError as exc:
            outcome = self.database.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_code=exc.code,
                error_message=str(exc),
                result={"details": exc.details},
                unlock=True,
            )
        except Exception as exc:  # noqa: BLE001 - worker must persist unexpected failures
            outcome = self.database.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_code="internal_error",
                error_message=str(exc),
                unlock=True,
            )
        creator_context = outcome.artifacts.get("creator_context")
        if creator_context:
            if outcome.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}:
                entry_id = str(outcome.result.get("entry_id") or "")
                if entry_id:
                    self.database.mark_creator_work_imported(
                        str(creator_context.get("id")),
                        str(creator_context.get("work_id")),
                        entry_id,
                    )
                    self._refresh_creator_documents(
                        str(creator_context.get("id")), action="work-imported"
                    )
            self._refresh_creator_parent(str(creator_context.get("parent_job_id") or ""))
        return outcome

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
                    "retention": (
                        RetentionPolicy.KEEP if keep else RetentionPolicy.TEMPORARY
                    ),
                    "media_expires_at": (
                        None
                        if keep
                        else now + timedelta(days=self.config.media.retention_days)
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

    async def _process_creator_import(self, job: JobRecord) -> JobRecord:
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
        self._refresh_creator_documents(creator.id, action=f"inventory-{action}")
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

    async def _process_reanalysis(self, job: JobRecord) -> JobRecord:
        entry_id = str(job.artifacts.get("reanalyze_entry_id", ""))
        entry = self.database.get_entry(entry_id)
        current_data = self.database.get_entry_data(entry_id)
        if current_data.get("analysis", {}).get("analysis_version") == 2 and not job.artifacts.get(
            "force"
        ):
            return self.database.update_job(
                job.id,
                status=JobStatus.COMPLETED,
                progress=1,
                result={"entry_id": entry.id, "skipped": True, "reason": "already_v2"},
                unlock=True,
            )

        artifacts = dict(job.artifacts)
        segments = [
            TranscriptSegment.model_validate(item)
            for item in artifacts.get("transcript_corrected", [])
        ]
        ocr_items = [self._ocr_model(item) for item in artifacts.get("ocr", [])]
        if self.config.analysis_mode == AnalysisMode.GATEWAY and "analysis" not in artifacts:
            return self.database.update_job(
                job.id,
                status=JobStatus.AWAITING_AGENT_ANALYSIS,
                progress=0.7,
                result={"phase": "analysis", "next_tool": "get_analysis_context"},
                unlock=True,
            )

        self.database.update_job(job.id, status=JobStatus.ANALYZING, progress=0.72)
        if "analysis" not in artifacts:
            metadata = dict(artifacts.get("metadata", {}))
            context_text = "\n".join(
                [
                    *[item.text for item in entry.inspirations],
                    *[item.text for item in segments],
                    str(metadata.get("post_text") or ""),
                    *[item.text for item in ocr_items],
                ]
            )
            metadata["existing_knowledge"] = self.indexer.find_related_claims(context_text)
            async with self.analysis_semaphore:
                result = await self.analysis.analyze(
                    segments, ocr_items, entry.inspirations, metadata
                )
            self._validate_analysis_evidence(
                result,
                {**current_data, **artifacts, "metadata": metadata},
            )
            artifacts["analysis"] = result.model_dump(mode="json")
            self.database.update_job(
                job.id, artifacts={"analysis": artifacts["analysis"]}, progress=0.84
            )

        validated = AnalysisResult.model_validate(artifacts["analysis"])
        self._validate_analysis_evidence(validated, {**current_data, **artifacts})
        normalized = validated.model_dump(mode="json")
        normalized["contradictions"] = self._normalize_contradictions(
            normalized.get("contradictions", []), entry.id
        )
        validated = AnalysisResult.model_validate(normalized)
        data = dict(current_data)
        data["analysis"] = validated.model_dump(mode="json")
        data["provider"] = (
            f"agent:{artifacts.get('analysis_producer', 'gateway')}"
            if self.config.analysis_mode == AnalysisMode.GATEWAY
            else self.analysis.name
        )
        data["model"] = (
            artifacts.get("analysis_model", "agent")
            if self.config.analysis_mode == AnalysisMode.GATEWAY
            else self.analysis.model
        )
        data["prompt_version"] = (
            f"external:{PROMPT_VERSION}"
            if self.config.analysis_mode == AnalysisMode.GATEWAY
            else PROMPT_VERSION
        )
        updated = entry.model_copy(
            update={
                "title": safe_filename(validated.title),
                "summary": validated.one_liner,
                "tags": validated.tags,
                "updated_at": utc_now(),
            }
        )
        chunks, relations, reminders = self._prepare_entry_bundle(updated, data)
        self._persist_entry_documents_and_bundle(
            updated,
            data,
            chunks,
            relations,
            reminders,
            action="reanalyze-v2",
            log_summary=f"复用现有逐字稿与 OCR，由 {data['provider']} 生成 v2 分析",
            commit_message=f"reanalyze-v2: {updated.video_id} {updated.title}",
            require_existing=True,
        )
        source_kind = current_data.get("metadata", {}).get("source_kind", SourceKind.VIDEO.value)
        creator_folder = str(current_data.get("creator", {}).get("folder_path") or "")
        return self.database.update_job(
            job.id,
            status=JobStatus.COMPLETED,
            progress=1,
            result={
                "entry_id": updated.id,
                "source_path": updated.source_path,
                "machine_data_path": str(
                    Path(creator_folder) / ".data" / "sources" / f"{updated.video_id}.md"
                    if creator_folder
                    else Path("wiki") / ".data" / "sources" / f"{updated.video_id}.md"
                ),
                "summary": updated.summary,
                "reused_transcript": source_kind == SourceKind.VIDEO.value,
                "reused_ocr": True,
            },
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
        await asyncio.to_thread(lock.__enter__)
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

        existing = self.database.find_entry_by_video_id(video_id)
        if existing:
            with self.vault.entry_operations_locked():
                # Re-read after the work lock so a preceding capture can win
                # the first-write race and its user data is never lost.
                existing = self.database.get_entry(existing.id)
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
                await self.media.extract_audio(video_path, audio_path)
                try:
                    transcript = await self.transcriber.transcribe(audio_path, work_dir / "whisper")
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
            self.database.update_job(
                job.id,
                artifacts={
                    "transcript_raw": artifacts["transcript_raw"],
                    "ocr": artifacts["ocr"],
                    **(
                        {"ocr_warning": artifacts["ocr_warning"]}
                        if artifacts.get("ocr_warning")
                        else {}
                    ),
                },
                progress=0.58,
            )
        transcript_raw = [
            TranscriptSegment.model_validate(item) for item in artifacts["transcript_raw"]
        ]
        ocr_items = artifacts.get("ocr", [])

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
            async with self.analysis_semaphore:
                corrected, llm_issues = await self.analysis.correct_transcript(
                    transcript_raw,
                    [self._ocr_model(item) for item in ocr_items],
                )
            issues = _deduplicate_issues([*detect_review_issues(transcript_raw), *llm_issues])
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

        self.database.update_job(job.id, status=JobStatus.ANALYZING, progress=0.7)
        if "analysis" not in artifacts:
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
            analysis_metadata["existing_knowledge"] = self.indexer.find_related_claims(context_text)
            async with self.analysis_semaphore:
                analysis = await self.analysis.analyze(
                    corrected,
                    [self._ocr_model(item) for item in ocr_items],
                    effective_inspirations,
                    analysis_metadata,
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
        if self.config.analysis_mode != AnalysisMode.GATEWAY and not self.analysis.configured:
            warnings.append("模型未配置，使用本地降级分析")
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
        existing = self.database.find_entry_by_video_id(work_id)
        if existing:
            for inspiration in request.inspirations:
                existing = self.add_inspiration(existing.id, inspiration)
            existing_data = self.database.get_entry_data(existing.id)
            if self._image_note_files_intact(existing_data):
                existing = self._repair_entry_if_needed(existing)
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
            self.database.update_job(
                job.id,
                artifacts={
                    "ocr": artifacts["ocr"],
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
                "reacquired": bool(existing),
            },
            unlock=True,
        )

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
                cover_path = self._persist_video_cover(
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

    def _prepare_entry_bundle(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        *,
        reminders: list[ReminderCandidate] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ReminderCandidate]]:
        self.indexer.ensure_embedding_compatibility()
        relations = self.indexer.build_relations(entry, data, persist=False)
        data["relations"] = relations
        chunks = self.indexer.index_entry(entry, data, persist=False)
        reminder_values = reminders or [
            ReminderCandidate.model_validate(item)
            for item in data.get("analysis", {}).get("reminders", [])
        ]
        return chunks, relations, reminder_values

    def _write_entry_documents(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        *,
        action: str,
        log_summary: str,
        commit_message: str,
    ) -> None:
        entries = [item for item in self.database.list_entries() if item.id != entry.id]
        entries.append(entry)
        with self.vault.locked():
            written = self.vault.write_entry(entry, data)
            index = self.vault.rebuild_index(entries)
            changed = [*written.changed_paths, index]
            if not data.get("creator", {}).get("folder_path"):
                log = self.vault.append_log(
                    action,
                    entry.title,
                    log_summary,
                    [*written.changed_paths, index],
                )
                changed.append(log)
            self._commit_vault(changed, commit_message)

    def _persist_entry_documents_and_bundle(
        self,
        entry: EntryRecord,
        data: dict[str, Any],
        chunks: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        reminders: list[ReminderCandidate],
        *,
        action: str,
        log_summary: str,
        commit_message: str,
        require_existing: bool = False,
    ) -> EntryRecord:
        """Commit one entry mutation under the cross-process operation lock."""
        with self.vault.entry_operations_locked():
            if require_existing:
                self.database.get_entry(entry.id)
            self._write_entry_documents(
                entry,
                data,
                action=action,
                log_summary=log_summary,
                commit_message=commit_message,
            )
            return self.database.persist_entry_bundle(
                entry, data, chunks, relations, reminders
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
    def _creator_context(creator_id: str, folder_path: str, work_id: str) -> dict[str, Any]:
        return {
            "id": creator_id,
            "folder_path": folder_path,
            "parent_job_id": "",
            "work_id": work_id,
            "batch_silent": False,
        }

    def _adopt_creator_capture(
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
        return self._creator_context(creator.id, creator.folder_path, work_id), metadata, target_dir

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

    def _refresh_creator_documents(self, creator_id: str, *, action: str) -> None:
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
                self._commit_vault(changed, f"creator: {creator.nickname} {action}")

    def _commit_vault(self, changed: list[Path], message: str) -> bool:
        if not (self.config.vault_path / ".git").exists():
            return True
        committed = self.vault.commit(changed, message)
        if not committed:
            warnings.warn(
                f"Vault 与 SQLite 已保存，但 Git 提交失败，文件保持为待提交状态：{message}",
                RuntimeWarning,
                stacklevel=2,
            )
        return committed

    def _refresh_creator_parent(self, parent_job_id: str) -> None:
        if not parent_job_id:
            return
        with suppress(JobStateError):
            parent = self.database.get_job(parent_job_id)
            if parent.kind != "creator_import" or parent.status != JobStatus.MONITORING:
                return
            child_ids = list(parent.result.get("child_job_ids", []))
            children = [self.database.get_job(job_id) for job_id in child_ids]
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
                    progress=0.65 + 0.35 * finished / len(children),
                    result=result,
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
            )

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
            return self._repair_entry_if_needed_locked(entry)

    def _repair_entry_if_needed_locked(self, entry: EntryRecord) -> EntryRecord:
        # Re-read under the operation lock so a concurrent delete cannot be
        # undone by a stale worker repair.
        entry = self.database.get_entry(entry.id)
        if self._entry_documents_intact(entry) and self.database.entry_chunk_count(entry.id) > 0:
            return entry
        data = self.database.get_entry_data(entry.id)
        chunks, relations, reminders = self._prepare_entry_bundle(entry, data)
        self._write_entry_documents(
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
        report = {
            "dry_run": not apply,
            "entry_count": len(loaded),
            "entry_ids": [entry.id for entry, _ in loaded],
            "creator_count": len(loaded_creators),
            "creator_ids": [creator.id for creator, _ in loaded_creators],
            "topic_count": len(loaded_topics),
            "topic_ids": [topic.id for topic, _ in loaded_topics],
            "entry_errors": list(self.vault.last_entry_load_errors),
        }
        if not apply:
            return report
        if self.vault.last_entry_load_errors:
            raise JobStateError(
                "Vault 中存在无法解析的资料；请根据 dry-run 的 entry_errors 修复后再重建"
            )
        if self.database.has_unfinished_creator_jobs():
            raise JobStateError("存在未完成的博主清点或批量任务，完成后再重建 SQLite")
        prepared: list[
            tuple[
                EntryRecord,
                dict[str, Any],
                list[dict[str, Any]],
                list[ReminderCandidate],
            ]
        ] = []
        self.database.clear_knowledge_cache(include_creators=True)
        # Insert base entries first so relation foreign keys can be restored in pass two.
        for entry, data in loaded:
            self.database.upsert_entry(entry, data)
            chunks = self.indexer.index_entry(entry, data, persist=False)
            reminders = [
                ReminderCandidate.model_validate(item)
                for item in data.get("analysis", {}).get("reminders", [])
            ]
            prepared.append((entry, data, chunks, reminders))
        for entry, data, chunks, reminders in prepared:
            relations = []
            for relation in data.get("relations", []):
                try:
                    self.database.get_entry(relation["target_entry_id"])
                except (EntryNotFoundError, KeyError):
                    continue
                relations.append(relation)
            self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
        for creator, works in loaded_creators:
            self.database.restore_creator_bundle(creator, works)
        for topic, artifacts in loaded_topics:
            self.database.restore_topic_bundle(topic, artifacts)
        self.indexer.record_embedding_signature()
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
                    chunks, relations, reminders = self._prepare_entry_bundle(entry, data)
                    current_relations = json.dumps(relations, ensure_ascii=False, sort_keys=True)
                    if current_relations == previous_relations:
                        continue
                    written = self.vault.write_entry(entry, data)
                    changed.extend(written.changed_paths)
                    self.database.persist_entry_bundle(entry, data, chunks, relations, reminders)
                    report["relation_updates"].append(entry.id)
                for entry in expired:
                    self._trash_assets(entry)
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

    def _trash_assets(self, entry: EntryRecord, *, mark_database: bool = True) -> None:
        raw_parent = Path(entry.raw_path).parent
        raw_root = raw_parent.parent if raw_parent.name == "records" else raw_parent
        assets = self.config.vault_path / raw_root / "assets" / entry.video_id
        if assets.exists():
            send2trash(str(assets))
        if mark_database:
            self.database.mark_media_removed(entry.id)

    def _vault_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.config.vault_path / path

    def _vault_relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.config.vault_path.resolve()))

    def _metadata_image_paths_exist(self, metadata: VideoMetadata) -> bool:
        return bool(metadata.image_paths) and all(
            (path := self._vault_path(value)).is_file() and path.stat().st_size > 0
            for value in metadata.image_paths
        )

    def _image_note_files_intact(self, data: dict[str, Any]) -> bool:
        metadata_data = data.get("metadata", {})
        if metadata_data.get("source_kind") != SourceKind.IMAGE_NOTE.value:
            return False
        try:
            return self._metadata_image_paths_exist(VideoMetadata.model_validate(metadata_data))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _detect_image_review_issues(
        observations: list[OCRObservation], post_text: str
    ) -> list[ReviewIssue]:
        normalized_post = re.sub(r"\s+", "", post_text).lower()
        critical = re.compile(
            r"(?:\d|[%￥¥$€]|(?:年|月|日|号|点|时|分)|[A-Za-z][A-Za-z0-9._+-]{1,})"
        )
        issues: list[ReviewIssue] = []
        for index, observation in enumerate(observations):
            confidence = observation.confidence
            normalized_text = re.sub(r"\s+", "", observation.text).lower()
            if (
                confidence is None
                or confidence >= 0.65
                or not critical.search(observation.text)
                or (normalized_text and normalized_text in normalized_post)
            ):
                continue
            issues.append(
                ReviewIssue(
                    id=f"image-{observation.image_index or 0}-{index}",
                    image_index=observation.image_index,
                    raw_text=observation.text,
                    reason="图片中的数字、日期或专有名词 OCR 置信度较低，且作品正文没有相同依据",
                    suggestions=[],
                )
            )
        return issues

    @staticmethod
    def _apply_image_review_resolutions(
        observations: list[OCRObservation], issues: list[ReviewIssue]
    ) -> list[OCRObservation]:
        resolved = {
            (issue.image_index, issue.raw_text): issue.resolution
            for issue in issues
            if issue.resolution
        }
        return [
            observation.model_copy(
                update={
                    "text": resolved.get(
                        (observation.image_index, observation.text), observation.text
                    )
                }
            )
            for observation in observations
        ]

    def _validate_analysis_evidence(
        self, analysis: AnalysisResult, context: dict[str, Any]
    ) -> None:
        """Reject source claims whose locator or quote cannot be traced to captured evidence."""
        metadata = context.get("metadata", {}) or {}
        source_kind = metadata.get("source_kind", SourceKind.VIDEO.value)
        is_image_note = source_kind == SourceKind.IMAGE_NOTE.value
        transcript_values = context.get("transcript_corrected") or context.get("transcript_raw", [])
        transcript_text = "\n".join(
            str(item.get("text", "") if isinstance(item, dict) else item.text)
            for item in transcript_values
        )
        ocr_values = context.get("ocr", [])
        ocr_text = "\n".join(
            str(item.get("text", "") if isinstance(item, dict) else item.text)
            for item in ocr_values
        )
        post_text = str(metadata.get("post_text") or "")
        transcript_ranges = [
            (
                int(item.get("start_ms", 0) if isinstance(item, dict) else item.start_ms),
                int(item.get("end_ms", 0) if isinstance(item, dict) else item.end_ms),
            )
            for item in transcript_values
        ]
        ocr_timestamps = {
            int(value)
            for item in ocr_values
            if (value := item.get("timestamp_ms") if isinstance(item, dict) else item.timestamp_ms)
            is not None
        }
        ocr_image_indices = {
            int(value)
            for item in ocr_values
            if (value := item.get("image_index") if isinstance(item, dict) else item.image_index)
            is not None
        }
        evidence_text = {
            "audio": transcript_text,
            "ocr": ocr_text,
            "audio+ocr": f"{transcript_text}\n{ocr_text}",
            "post_text": post_text,
            "image_ocr": ocr_text,
            "post_text+image_ocr": f"{post_text}\n{ocr_text}",
        }
        allowed = (
            {"post_text", "image_ocr", "post_text+image_ocr", "ai_inference"}
            if is_image_note
            else {"audio", "ocr", "audio+ocr", "ai_inference"}
        )
        image_count = len(metadata.get("image_paths") or metadata.get("images") or [])
        duration_ms = int(float(metadata.get("duration_seconds") or 0) * 1000)
        errors: list[str] = []
        atom_ids: set[str] = set()

        def validate_item(item: Any, *, label: str, atom: bool = False) -> None:
            provenance = item.provenance if atom else item.evidence_type
            timestamp_ms = getattr(item, "timestamp_ms", None)
            image_index = getattr(item, "image_index", None)
            quote = item.quote
            if provenance not in allowed:
                errors.append(f"{label} 使用了与作品类型不符的来源 {provenance}")
                return
            if atom and ((item.atom_type == "inference") != (provenance == "ai_inference")):
                errors.append(f"{label} 的 inference 类型与 ai_inference 来源不一致")
            if is_image_note:
                if timestamp_ms is not None:
                    errors.append(f"{label} 是图文证据，不能包含 timestamp_ms")
                if provenance in {"image_ocr", "post_text+image_ocr"} and image_index is None:
                    errors.append(f"{label} 缺少图片编号")
                elif provenance in {"image_ocr", "post_text+image_ocr"} and (
                    image_index not in ocr_image_indices
                ):
                    errors.append(f"{label} 的图片编号没有对应 OCR 证据")
                if image_index is not None and (image_count == 0 or image_index > image_count):
                    errors.append(f"{label} 的图片编号超出采集范围")
            else:
                if image_index is not None:
                    errors.append(f"{label} 是视频证据，不能包含 image_index")
                if provenance != "ai_inference" and timestamp_ms is None:
                    errors.append(f"{label} 缺少视频时间戳")
                elif timestamp_ms is not None and provenance != "ai_inference":
                    audio_match = any(
                        start - 1000 <= timestamp_ms <= end + 1000
                        for start, end in transcript_ranges
                    )
                    ocr_match = any(
                        abs(timestamp_ms - observed) <= 1500 for observed in ocr_timestamps
                    )
                    locator_matches = {
                        "audio": audio_match,
                        "ocr": ocr_match,
                        "audio+ocr": audio_match or ocr_match,
                    }
                    if not locator_matches.get(provenance, False):
                        errors.append(f"{label} 的时间戳没有对应 ASR/OCR 证据")
                if duration_ms and timestamp_ms is not None and timestamp_ms > duration_ms + 5000:
                    errors.append(f"{label} 的时间戳超出视频时长")
            if quote and provenance != "ai_inference":
                source = _normalize_evidence_text(evidence_text.get(provenance, ""))
                normalized_quote = _normalize_evidence_text(quote)
                if not normalized_quote or normalized_quote not in source:
                    errors.append(f"{label} 的引文无法在原始 ASR/OCR/正文中定位")

        if is_image_note and analysis.chapters:
            errors.append("静态图文不能包含视频时间轴图解")
        for index, chapter in enumerate(analysis.chapters, start=1):
            if duration_ms and chapter.start_ms > duration_ms + 5000:
                errors.append(f"时间轴章节 {index} 的开始时间超出视频时长")
            if chapter.end_ms is not None and duration_ms and chapter.end_ms > duration_ms + 5000:
                errors.append(f"时间轴章节 {index} 的结束时间超出视频时长")
            if not chapter.evidence:
                errors.append(f"时间轴章节 {index} 缺少可核验证据")
            for evidence_index, evidence in enumerate(chapter.evidence, start=1):
                validate_item(
                    evidence,
                    label=f"时间轴章节 {index} 证据 {evidence_index}",
                )
        for index, atom in enumerate(analysis.knowledge_atoms, start=1):
            if atom.id in atom_ids:
                errors.append(f"知识原子 id 重复：{atom.id}")
            atom_ids.add(atom.id)
            validate_item(atom, label=f"知识原子 {atom.id or index}", atom=True)
        corpus = _normalize_evidence_text(f"{transcript_text}\n{ocr_text}\n{post_text}")
        for reminder in analysis.reminders:
            if reminder.source_quote:
                quote = _normalize_evidence_text(reminder.source_quote)
                if not quote or quote not in corpus:
                    errors.append(f"提醒候选 {reminder.id} 的依据无法在原始证据中定位")
        if errors:
            raise JobStateError("分析证据校验失败：" + "；".join(errors[:8]))

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

    @staticmethod
    def _ocr_model(value: dict[str, Any]):
        from .models import OCRObservation

        return OCRObservation.model_validate(value)

    def _normalize_contradictions(
        self, values: list[dict[str, Any]], current_entry_id: str
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for value in values:
            target_id = value.get("conflicts_with_entry_id")
            if not target_id or target_id == current_entry_id:
                continue
            try:
                target = self.database.get_entry(target_id)
            except EntryNotFoundError:  # reject hallucinated or removed entry ids
                continue
            normalized.append(
                {
                    **value,
                    "target_title": target.title,
                    "target_source_path": target.source_path,
                    "target_original_url": target.original_url,
                }
            )
        return normalized


def _deduplicate_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    result: list[ReviewIssue] = []
    seen: set[tuple[int, int, int | None, str]] = set()
    for issue in issues:
        key = (issue.start_ms, issue.end_ms, issue.image_index, issue.raw_text)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _normalize_evidence_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()
