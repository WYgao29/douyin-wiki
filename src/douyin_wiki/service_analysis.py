from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .adapters.llm import (
    PROMPT_VERSION,
)
from .errors import (
    JobStateError,
)
from .models import (
    AnalysisMode,
    AnalysisResult,
    EntryRecord,
    GatewayContext,
    JobRecord,
    JobStatus,
    OCRObservation,
    ReviewIssue,
    SourceKind,
    TranscriptCorrection,
    TranscriptSegment,
)
from .review import _deduplicate_issues, detect_review_issues
from .time_utils import utc_now
from .vault import safe_filename


class AnalysisMixin:
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
            "media_provenance": artifacts.get("media_provenance", {}),
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
                "缺少转录置信度分数不等于识别质量低；仅数字或英文出现不构成人工复核理由。"
                "有具体识别歧义或与对应画面证据冲突时，说明原文和疑点，不编造置信度。",
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

    def submit_analysis(
        self,
        entry_id: str,
        analysis: dict[str, Any],
        *,
        producer: str,
        model: str = "agent",
    ) -> EntryRecord:
        with self.vault.entry_operations_locked():
            return self._submit_analysis_locked(entry_id, analysis, producer=producer, model=model)

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
        with self.vault.entry_operations_locked():
            # The model call intentionally happens before this lock. Reload both
            # projections so concurrent user mutations are merged into the
            # final analysis write instead of being overwritten by the snapshot
            # captured before analysis started.
            latest_entry = self.database.get_entry(entry_id)
            latest_data = self.database.get_entry_data(entry_id)
            latest_data["analysis"] = validated.model_dump(mode="json")
            latest_data["provider"] = (
                f"agent:{artifacts.get('analysis_producer', 'gateway')}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.name
            )
            latest_data["model"] = (
                artifacts.get("analysis_model", "agent")
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else self.analysis.model
            )
            latest_data["prompt_version"] = (
                f"external:{PROMPT_VERSION}"
                if self.config.analysis_mode == AnalysisMode.GATEWAY
                else PROMPT_VERSION
            )
            updated = latest_entry.model_copy(
                update={
                    "title": safe_filename(validated.title),
                    "summary": validated.one_liner,
                    "tags": validated.tags,
                    "updated_at": utc_now(),
                }
            )
            chunks, relations, reminders = self._prepare_entry_bundle(updated, latest_data)
            self._persist_entry_documents_and_bundle_locked(
                updated,
                latest_data,
                chunks,
                relations,
                reminders,
                action="reanalyze-v2",
                log_summary=(f"复用现有逐字稿与 OCR，由 {latest_data['provider']} 生成 v2 分析"),
                commit_message=f"reanalyze-v2: {updated.video_id} {updated.title}",
            )
            source_kind = latest_data.get("metadata", {}).get("source_kind", SourceKind.VIDEO.value)
            creator_folder = str(latest_data.get("creator", {}).get("folder_path") or "")
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

    def _prune_unverified_analysis_evidence(
        self,
        analysis: AnalysisResult,
        context: dict[str, Any],
        *,
        audit: list[dict[str, Any]] | None = None,
    ) -> tuple[AnalysisResult, int]:
        """Keep only model citations that pass the existing source evidence checks."""
        try:
            self._validate_analysis_evidence(analysis, context)
            return analysis, 0
        except JobStateError:
            pass

        base = analysis.model_copy(update={"chapters": [], "knowledge_atoms": [], "reminders": []})

        def error(candidate: AnalysisResult) -> str | None:
            try:
                self._validate_analysis_evidence(candidate, context)
                return None
            except JobStateError as exc:
                return str(exc)

        def record(kind: str, item: Any, reason: str, *, chapter: Any = None) -> None:
            if audit is not None:
                audit.append(
                    {
                        "kind": kind,
                        "id": getattr(item, "id", None),
                        "chapter_title": getattr(chapter, "title", None),
                        "chapter_start_ms": getattr(chapter, "start_ms", None),
                        "timestamp_ms": getattr(item, "timestamp_ms", None),
                        "quote": getattr(item, "quote", None),
                        "reason": reason,
                    }
                )

        def repair_timestamp(item: Any, provenance: str) -> Any:
            quote = _normalize_evidence_text(getattr(item, "quote", "") or "")
            if not quote or provenance not in {"audio", "ocr", "audio+ocr"}:
                return item
            candidates: list[tuple[int, str]] = []
            if provenance in {"audio", "audio+ocr"}:
                for source in context.get("transcript_corrected") or context.get(
                    "transcript_raw", []
                ):
                    candidates.append(
                        (
                            int(
                                source.get("start_ms", 0)
                                if isinstance(source, dict)
                                else source.start_ms
                            ),
                            str(
                                source.get("text", "") if isinstance(source, dict) else source.text
                            ),
                        )
                    )
            if provenance in {"ocr", "audio+ocr"}:
                for source in context.get("ocr", []):
                    timestamp = (
                        source.get("timestamp_ms")
                        if isinstance(source, dict)
                        else source.timestamp_ms
                    )
                    if timestamp is not None:
                        candidates.append(
                            (
                                int(timestamp),
                                str(
                                    source.get("text", "")
                                    if isinstance(source, dict)
                                    else source.text
                                ),
                            )
                        )
            matching = [
                timestamp
                for timestamp, text in candidates
                if quote in _normalize_evidence_text(text)
            ]
            if not matching:
                return item
            original = getattr(item, "timestamp_ms", None)
            best = min(matching, key=lambda value: abs(value - (original or 0)))
            return item.model_copy(update={"timestamp_ms": best})

        removed = 0
        chapters = []
        for chapter in analysis.chapters:
            evidence = []
            for item in chapter.evidence:
                candidate_chapter = chapter.model_copy(update={"evidence": [item]})
                reason = error(base.model_copy(update={"chapters": [candidate_chapter]}))
                if reason and "时间戳" in reason:
                    repaired = repair_timestamp(item, item.evidence_type)
                    repaired_chapter = chapter.model_copy(update={"evidence": [repaired]})
                    if error(base.model_copy(update={"chapters": [repaired_chapter]})) is None:
                        item = repaired
                        reason = None
                if reason is None:
                    evidence.append(item)
                else:
                    removed += 1
                    record("chapter_evidence", item, reason, chapter=chapter)
            if evidence:
                chapters.append(chapter.model_copy(update={"evidence": evidence}))
            else:
                removed += 1
                record("chapter", chapter, "章节没有可核验证据", chapter=chapter)

        atoms = []
        seen_atom_ids: set[str] = set()
        for atom in analysis.knowledge_atoms:
            if atom.provenance in {"audio", "ocr", "audio+ocr"} and atom.image_index is not None:
                atom = atom.model_copy(update={"image_index": None})
            if atom.provenance == "ai_inference" and atom.atom_type != "inference":
                atom = atom.model_copy(update={"atom_type": "inference"})
            reason = error(base.model_copy(update={"knowledge_atoms": [atom]}))
            if atom.id in seen_atom_ids:
                reason = f"知识原子 id 重复：{atom.id}"
            if reason and "时间戳" in reason:
                repaired = repair_timestamp(atom, atom.provenance)
                if error(base.model_copy(update={"knowledge_atoms": [repaired]})) is None:
                    atom = repaired
                    reason = None
            if reason is None:
                atoms.append(atom)
                seen_atom_ids.add(atom.id)
            else:
                removed += 1
                record("knowledge_atom", atom, reason)

        reminders = []
        for reminder in analysis.reminders:
            reason = error(base.model_copy(update={"reminders": [reminder]}))
            if reason is None:
                reminders.append(reminder)
            else:
                removed += 1
                record("reminder", reminder, reason)

        cleaned = analysis.model_copy(
            update={
                "chapters": chapters,
                "knowledge_atoms": atoms,
                "reminders": reminders,
            }
        )
        self._validate_analysis_evidence(cleaned, context)
        return cleaned, removed

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


def _normalize_evidence_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()
