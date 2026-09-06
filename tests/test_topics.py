from __future__ import annotations

import shutil
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from douyin_wiki.models import EntryRecord, RetentionPolicy
from douyin_wiki.webapp.chat import ChatChunk, ChatContextBuilder


def _analysis(title: str, statement: str, atom_id: str) -> dict:
    return {
        "analysis_version": 2,
        "title": title,
        "one_liner": statement,
        "relevance_to_inspiration": "与专题研究目标直接相关。",
        "takeaways": [statement],
        "content_type": "explanation",
        "content_card": {"kind": "explanation", "question": title},
        "chapters": [
            {
                "start_ms": 5000,
                "title": title,
                "summary": statement,
                "evidence": [
                    {
                        "timestamp_ms": 5000,
                        "quote": statement,
                        "evidence_type": "audio",
                    }
                ],
            }
        ],
        "knowledge_atoms": [
            {
                "id": atom_id,
                "statement": statement,
                "atom_type": "fact",
                "provenance": "audio",
                "timestamp_ms": 5000,
                "quote": statement,
                "confidence": 0.95,
            }
        ],
        "tags": ["测试"],
    }


def _add_entry(service, entry_id: str, title: str, statement: str) -> EntryRecord:
    now = datetime.now(UTC)
    work_id = entry_id.removeprefix("dy-")
    entry = EntryRecord(
        id=entry_id,
        video_id=work_id,
        title=title,
        original_url=f"https://www.douyin.com/video/{work_id}",
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        raw_path=f"raw/{work_id}.md",
        source_path=f"wiki/sources/{work_id}.md",
        status="active",
        media_status="present",
        retention=RetentionPolicy.KEEP,
        summary=statement,
        tags=["测试"],
        created_at=now,
        updated_at=now,
    )
    data = {"analysis": _analysis(title, statement, f"atom-{work_id}")}
    service.database.upsert_entry(entry, data)
    service.indexer.index_entry(entry, data)
    return entry


class TopicProvider:
    model = "专题测试模型"
    configured = True

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        joined = "\n".join(message["content"] for message in messages)
        assert "只能使用给出的专题来源" in joined
        yield ChatChunk(text=f"## 核心结论\n\n采用渐进部署。〔{self.entry_id}〕")
        yield ChatChunk(
            usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
        )


class SourceChangingTopicProvider(TopicProvider):
    def __init__(
        self,
        service,
        topic_id: str,
        cited_entry_id: str,
        replacement_entry_id: str,
    ) -> None:
        super().__init__(cited_entry_id)
        self.service = service
        self.topic_id = topic_id
        self.replacement_entry_id = replacement_entry_id

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        self.service.set_topic_sources(
            self.topic_id,
            [{"entry_id": self.replacement_entry_id, "enabled": True}],
        )
        async for chunk in super().stream(messages):
            yield chunk


class TopicDeletingProvider(TopicProvider):
    def __init__(self, service, topic_id: str, cited_entry_id: str) -> None:
        super().__init__(cited_entry_id)
        self.service = service
        self.topic_id = topic_id

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        with self.service.database.connect() as conn:
            conn.execute("DELETE FROM research_topics WHERE id=?", (self.topic_id,))
        async for chunk in super().stream(messages):
            yield chunk


class EntryRevisionChangingTopicProvider(TopicProvider):
    def __init__(self, service, entry_id: str) -> None:
        super().__init__(entry_id)
        self.service = service

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatChunk]:
        entry = self.service.database.get_entry(self.entry_id)
        data = self.service.database.get_entry_data(self.entry_id)
        changed = entry.model_copy(
            update={"updated_at": entry.updated_at + timedelta(minutes=1)}
        )
        self.service.database.upsert_entry(changed, data)
        async for chunk in super().stream(messages):
            yield chunk


def _start_topic_mutation_after_artifact_save(
    service,
    mutation,
    original_save,
):
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run_mutation() -> None:
        started.set()
        try:
            mutation()
        except BaseException as exc:  # pragma: no cover - surfaced by the test below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_mutation)

    def save_with_mutation(artifact):
        worker.start()
        assert started.wait(timeout=2)
        # The old implementation has no entry-operation fence here, so the
        # mutation completes before the stale topic snapshot is persisted.
        # The fixed implementation deliberately waits until finalization ends.
        finished.wait(timeout=2)
        return original_save(artifact)

    service.database.save_topic_artifact = save_with_mutation
    return worker, finished, errors


def test_topic_search_is_filtered_before_ranking_and_source_disable_is_immediate(service) -> None:
    ai_one = _add_entry(service, "dy-ai-1", "企业 AI 部署", "企业 AI 部署需要先做流程梳理")
    ai_two = _add_entry(service, "dy-ai-2", "本地模型", "本地模型量化会影响显存占用")
    coffee = _add_entry(service, "dy-coffee", "聪明杯教程", "聪明杯使用十五克咖啡粉冲煮")
    topic = service.create_topic("企业 AI 决策", [ai_one.id, ai_two.id], goal="选择部署方案")
    topic_id = topic["topic"]["id"]

    assert service.search_topic(topic_id, "聪明杯怎么冲") == []
    assert all(
        item.entry_id in {ai_one.id, ai_two.id}
        for item in service.search_topic(topic_id, "AI 部署")
    )
    assert service.search_knowledge("聪明杯", entry_ids=[coffee.id])[0].entry_id == coffee.id
    assert service.search_knowledge("聪明杯", entry_ids=[]) == []

    service.set_topic_sources(
        topic_id,
        [
            {"entry_id": ai_one.id, "enabled": False},
            {"entry_id": ai_two.id, "enabled": True},
        ],
    )
    assert all(item.entry_id == ai_two.id for item in service.search_topic(topic_id, "本地模型"))


def test_entry_and_topic_chat_contexts_never_fall_back_to_library(service) -> None:
    ai = _add_entry(service, "dy-ai", "AI 文章", "AI 项目先验证高价值流程")
    coffee = _add_entry(service, "dy-coffee", "咖啡文章", "聪明杯使用十五克咖啡粉")
    topic_id = service.create_topic("AI 专题", [ai.id])["topic"]["id"]
    builder = ChatContextBuilder(service)

    _, entry_citations = builder.build(
        "聪明杯", context_entry_id=ai.id, history=[]
    )
    assert {item.entry_id for item in entry_citations} == {ai.id}

    messages, topic_citations = builder.build(
        "聪明杯怎么冲",
        context_entry_id=None,
        context_topic_id=topic_id,
        history=[],
    )
    assert topic_citations == []
    assert "当前专题没有相关证据" in "\n".join(item["content"] for item in messages)
    assert coffee.id not in "\n".join(item["content"] for item in messages)


@pytest.mark.asyncio
async def test_topic_artifact_provenance_staleness_and_literal_note(service) -> None:
    entry = _add_entry(service, "dy-ai", "AI 文章", "企业应先验证一个高价值流程")
    topic_id = service.create_topic("决策专题", [entry.id], goal="决定试点范围")["topic"]["id"]
    artifact = await service.generate_topic_artifact(
        topic_id, "decision_brief", provider=TopicProvider(entry.id)
    )
    assert artifact["status"] == "current"
    assert artifact["total_tokens"] == 50
    artifact_path = (
        service.config.vault_path
        / "topics"
        / topic_id
        / "artifacts"
        / f"{artifact['id']}.md"
    )
    assert artifact_path.exists()
    assert "专题测试模型" in artifact_path.read_text(encoding="utf-8")

    data = service.database.get_entry_data(entry.id)
    changed = entry.model_copy(update={"updated_at": entry.updated_at + timedelta(minutes=1)})
    service.database.upsert_entry(changed, data)
    refreshed = service.get_topic(topic_id)
    assert refreshed["artifacts"][0]["status"] == "needs_update"
    assert "需要更新" in artifact_path.read_text(encoding="utf-8")

    before = len(refreshed["artifacts"])
    with pytest.raises(ValueError, match="明确确认"):
        service.save_topic_note(topic_id, "逐字笔记", confirmed=False)
    assert len(service.get_topic(topic_id)["artifacts"]) == before
    note = service.save_topic_note(topic_id, "逐字笔记", confirmed=True)
    assert note["content_markdown"] == "逐字笔记"
    assert note["user_authored"] is True
    assert (service.config.vault_path / "topics" / topic_id / ".data" / "topic.md").exists()


@pytest.mark.asyncio
async def test_source_changes_during_generation_mark_artifact_stale_and_keep_latest_topic(
    service,
) -> None:
    original = _add_entry(service, "dy-original", "原始文章", "原始专题证据")
    replacement = _add_entry(service, "dy-replacement", "替换文章", "替换专题证据")
    topic_id = service.create_topic("交错专题", [original.id])["topic"]["id"]
    before = service.get_topic(topic_id)["topic"]

    artifact = await service.generate_topic_artifact(
        topic_id,
        "overview",
        provider=SourceChangingTopicProvider(
            service, topic_id, original.id, replacement.id
        ),
    )

    assert artifact["status"] == "needs_update"
    assert artifact["source_revision"] == before["source_revision"]
    assert artifact["source_revisions"] == [
        {
            "entry_id": original.id,
            "updated_at": before["sources"][0]["source_revision"],
            "enabled": True,
        }
    ]
    assert [source["entry_id"] for source in service.get_topic(topic_id)["topic"]["sources"]] == [
        replacement.id
    ]
    topic_data = (
        service.config.vault_path / "topics" / topic_id / ".data" / "topic.md"
    ).read_text(encoding="utf-8")
    assert replacement.id in topic_data


@pytest.mark.asyncio
async def test_entry_revision_changes_during_generation_mark_artifact_stale(
    service,
) -> None:
    entry = _add_entry(service, "dy-revision", "版本变化文章", "版本变化专题证据")
    topic_id = service.create_topic("版本交错专题", [entry.id])["topic"]["id"]
    before = service.get_topic(topic_id)["topic"]

    artifact = await service.generate_topic_artifact(
        topic_id,
        "overview",
        provider=EntryRevisionChangingTopicProvider(service, entry.id),
    )

    assert artifact["status"] == "needs_update"
    assert artifact["source_revision"] == before["source_revision"]
    assert artifact["source_revisions"][0]["entry_id"] == entry.id
    assert artifact["source_revisions"][0]["updated_at"] == before["sources"][0][
        "source_revision"
    ]


@pytest.mark.asyncio
async def test_source_update_at_finalization_does_not_revert_vault_topic(service) -> None:
    original = _add_entry(service, "dy-final-original", "最终化原始文章", "原始证据")
    replacement = _add_entry(service, "dy-final-replacement", "最终化替换文章", "替换证据")
    topic_id = service.create_topic("最终化交错专题", [original.id])["topic"]["id"]
    original_save = service.database.save_topic_artifact

    worker, finished, errors = _start_topic_mutation_after_artifact_save(
        service,
        lambda: service.set_topic_sources(
            topic_id,
            [{"entry_id": replacement.id, "enabled": True}],
        ),
        original_save,
    )

    artifact = await service.generate_topic_artifact(
        topic_id,
        "overview",
        provider=TopicProvider(original.id),
    )
    worker.join(timeout=2)

    assert finished.is_set()
    assert errors == []
    assert artifact["status"] == "current"
    topic_index = (
        service.config.vault_path / "topics" / topic_id / "index.md"
    ).read_text(encoding="utf-8")
    assert replacement.source_path.removesuffix(".md") in topic_index
    assert original.source_path.removesuffix(".md") not in topic_index


@pytest.mark.asyncio
async def test_topic_delete_at_finalization_is_serialized_without_sqlite_error(service) -> None:
    entry = _add_entry(service, "dy-final-delete", "最终化待删除专题来源", "专题证据")
    topic_id = service.create_topic("最终化删除交错专题", [entry.id])["topic"]["id"]
    topic_dir = service.config.vault_path / "topics" / topic_id
    original_save = service.database.save_topic_artifact

    def delete_topic() -> None:
        with service.vault.entry_operations_locked():
            with service.database.connect() as conn:
                conn.execute("DELETE FROM research_topics WHERE id=?", (topic_id,))
            shutil.rmtree(topic_dir)

    worker, finished, errors = _start_topic_mutation_after_artifact_save(
        service, delete_topic, original_save
    )

    artifact = await service.generate_topic_artifact(
        topic_id,
        "overview",
        provider=TopicProvider(entry.id),
    )
    worker.join(timeout=2)

    assert finished.is_set()
    assert errors == []
    assert artifact["status"] == "current"
    with service.database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM research_topics WHERE id=?", (topic_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM topic_artifacts WHERE topic_id=?", (topic_id,)
        ).fetchone()[0] == 0
    assert not topic_dir.exists()


@pytest.mark.asyncio
async def test_deleted_topic_during_generation_does_not_save_or_recreate_artifact(
    service,
) -> None:
    entry = _add_entry(service, "dy-deleted-topic", "待删除专题来源", "专题证据")
    topic_id = service.create_topic("删除交错专题", [entry.id])["topic"]["id"]

    with pytest.raises(KeyError, match="专题不存在"):
        await service.generate_topic_artifact(
            topic_id,
            "overview",
            provider=TopicDeletingProvider(service, topic_id, entry.id),
        )

    with service.database.connect() as conn:
        topic_count = conn.execute(
            "SELECT COUNT(*) FROM research_topics WHERE id=?", (topic_id,)
        ).fetchone()[0]
        artifact_count = conn.execute(
            "SELECT COUNT(*) FROM topic_artifacts WHERE topic_id=?", (topic_id,)
        ).fetchone()[0]
    assert topic_count == 0
    assert artifact_count == 0
