from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from .errors import (
    ExternalToolError,
    JobStateError,
)
from .models import (
    CaptureOptions,
    CaptureRequest,
    EntryRecord,
    InspirationInput,
    ReminderCandidate,
    ResearchTopic,
    RetentionPolicy,
    SourceKind,
    SourceRevision,
    TopicArtifact,
    TopicArtifactKind,
)
from .time_utils import utc_now


class EntriesMixin:
    def add_inspiration(self, entry_id: str, inspiration: InspirationInput) -> EntryRecord:
        with self.vault.entry_operations_locked():
            return self._add_inspiration_locked(entry_id, inspiration)

    def _add_inspiration_locked(self, entry_id: str, inspiration: InspirationInput) -> EntryRecord:
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
            source_kind = data.get("metadata", {}).get("source_kind", SourceKind.VIDEO.value)
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
            (self.database.get_entry(source.entry_id), source.enabled) for source in topic.sources
        ]
        revision, _ = self._topic_revision(entries)
        if revision != topic.source_revision:
            topic = self.database.update_topic_revision(
                topic_id,
                source_revision=revision,
                source_versions={entry.id: entry.updated_at.isoformat() for entry, _ in entries},
            )
            self._persist_topic(topic)
        return topic

    def _persist_topic(self, topic: ResearchTopic) -> list[Path]:
        artifacts = self.database.list_topic_artifacts(topic.id)
        entries = {
            source.entry_id: self.database.get_entry(source.entry_id) for source in topic.sources
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
            return self._create_topic_locked(title, entry_ids, goal=goal, instructions=instructions)

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
            [(entry.id, enabled, entry.updated_at.isoformat()) for entry, enabled in ordered],
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
        enabled_entries = [self.database.get_entry(source.entry_id) for source in enabled_sources]
        _, revisions = self._topic_revision([(entry, True) for entry in enabled_entries])
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
        with self.vault.entry_operations_locked():
            latest_topic = self._refresh_topic(topic_id)
            now = utc_now()
            artifact = TopicArtifact(
                id=f"{kind}-{uuid.uuid4().hex[:12]}",
                topic_id=topic.id,
                kind=kind,
                title=labels[kind],
                content_markdown=answer.strip(),
                source_revision=topic.source_revision,
                source_revisions=revisions,
                status=(
                    "current"
                    if latest_topic.source_revision == topic.source_revision
                    else "needs_update"
                ),
                model=provider.model or None,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                created_at=now,
                updated_at=now,
            )
            artifact = self.database.save_topic_artifact(artifact)
            self._persist_topic(latest_topic)
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
