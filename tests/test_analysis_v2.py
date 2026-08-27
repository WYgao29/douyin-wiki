from __future__ import annotations

import copy

import pytest

from douyin_wiki.errors import JobStateError
from douyin_wiki.models import AnalysisMode, AnalysisResult, JobStatus
from douyin_wiki.vault import encode_markdown_path
from douyin_wiki.worker import Worker


def analysis_payload(kind: str, card: dict) -> dict:
    return {
        "analysis_version": 2,
        "title": f"{kind} 示例",
        "one_liner": "这是一条用于验证自适应视频知识卡片的简短摘要。",
        "relevance_to_inspiration": "这是 AI 根据用户灵感作出的关联判断。",
        "takeaways": ["收获一", "收获二", "收获三"],
        "content_type": kind,
        "facets": ["comparison"] if kind in {"tutorial", "recommendation"} else [],
        "content_card": {"kind": kind, **card},
        "chapters": [
            {
                "start_ms": 0,
                "end_ms": 5000,
                "title": "问题背景",
                "summary": "视频先说明筛选财经信息源的目标。",
                "key_points": ["减少噪声", "保留一手证据"],
                "evidence": [
                    {
                        "timestamp_ms": 0,
                        "quote": "财经媒体筛选",
                        "evidence_type": "ocr",
                    }
                ],
            },
            {
                "start_ms": 5000,
                "title": "筛选方法",
                "summary": "作者总结适用场景。",
                "comparison_table": {
                    "headers": ["标准", "要求"],
                    "rows": [["证据", "提供一手证据"]],
                },
                "evidence": [
                    {
                        "timestamp_ms": 5000,
                        "quote": "筛选信息源要看它能否提供一手证据",
                        "evidence_type": "audio+ocr",
                    }
                ],
            },
        ],
        "knowledge_atoms": [
            {
                "id": "atom-parameter",
                "statement": "高质量信息源应提供一手证据",
                "atom_type": "method",
                "provenance": "audio",
                "timestamp_ms": 5000,
                "quote": "筛选信息源要看它能否提供一手证据",
                "context": "作者说明筛选财经信息源的方法时",
                "confidence": 0.96,
            }
        ],
        "actions": ["按参数完成一次冲煮记录"],
        "open_questions": [],
        "reminders": [],
        "tags": ["测试", kind],
        "concepts": [],
        "entities": [],
        "contradictions": [],
    }


CARD_CASES = [
    ("tutorial", {"goal": "完成手冲", "parameters": ["15克粉"], "steps": ["注水"]}, "目标"),
    ("explanation", {"question": "为什么萃取不同", "mechanism": ["流速改变萃取"]}, "问题"),
    ("opinion", {"thesis": "信息源质量更重要", "reasons": ["减少噪声"]}, "结论"),
    ("recommendation", {"subjects": ["咖啡壶"], "criteria": ["控流"]}, "对象"),
    ("news_event", {"event": "新品发布", "impact": ["价格变化"]}, "事件"),
    ("story_case", {"context": "创业初期", "outcome": "完成转型"}, "背景"),
    (
        "collection",
        {"items": [{"name": "方案一", "traits": ["稳定"], "scenarios": ["日常"]}]},
        "项目",
    ),
    ("other", {"notes": ["暂时无法可靠归类"]}, "要点"),
]


def test_markdown_path_encodes_obsidian_unsafe_filename_characters() -> None:
    encoded = encode_markdown_path("../../raw/手冲 咖啡?!🏆.md")
    assert encoded == "../../raw/%E6%89%8B%E5%86%B2%20%E5%92%96%E5%95%A1%3F%21%F0%9F%8F%86.md"


def test_analysis_schema_replaces_key_moments_with_timeline_chapters() -> None:
    schema = AnalysisResult.model_json_schema()
    assert "chapters" in schema["properties"]
    assert "key_moments" not in schema["properties"]


def test_legacy_key_moments_upgrade_to_timeline_chapters() -> None:
    result = AnalysisResult.model_validate(
        {
            "title": "旧分析",
            "key_moments": [
                {
                    "timestamp_ms": 5000,
                    "title": "旧片段",
                    "summary": "旧片段摘要",
                    "quote": "筛选信息源要看它能否提供一手证据",
                    "evidence_type": "audio",
                }
            ],
        }
    )
    assert len(result.chapters) == 1
    assert result.chapters[0].start_ms == 5000
    assert result.chapters[0].title == "旧片段"
    assert result.chapters[0].evidence[0].quote == "筛选信息源要看它能否提供一手证据"


def test_legacy_key_moments_with_duplicate_timestamps_remain_readable() -> None:
    result = AnalysisResult.model_validate(
        {
            "title": "旧分析",
            "key_moments": [
                {
                    "timestamp_ms": 5000,
                    "title": "语音结论",
                    "summary": "语音摘要",
                    "quote": "筛选信息源要看它能否提供一手证据",
                    "evidence_type": "audio",
                },
                {
                    "timestamp_ms": 5000,
                    "title": "画面补充",
                    "summary": "画面摘要",
                    "quote": "财经媒体筛选",
                    "evidence_type": "ocr",
                },
            ],
        }
    )
    assert len(result.chapters) == 2
    assert all(chapter.start_ms == 5000 for chapter in result.chapters)
    assert all(chapter.end_ms is None for chapter in result.chapters)


def test_timeline_chapter_evidence_rejects_blank_quote() -> None:
    with pytest.raises(ValueError, match="引文不能为空"):
        AnalysisResult.model_validate(
            {
                "title": "无效分析",
                "chapters": [
                    {
                        "start_ms": 0,
                        "title": "章节",
                        "summary": "摘要",
                        "evidence": [
                            {"timestamp_ms": 0, "quote": "   ", "evidence_type": "audio"}
                        ],
                    }
                ],
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "card", "label"), CARD_CASES)
async def test_v2_adaptive_cards_are_compact(service, kind, card, label) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.submit_analysis(
        completed.result["entry_id"],
        analysis_payload(kind, card),
        producer="test-agent",
    )
    source = (service.config.vault_path / entry.source_path).read_text(encoding="utf-8")
    body = source.split("---", 2)[-1]
    assert "内容卡片 ·" in body
    assert f"**{label}**" in body
    assert "## 摘要" not in body
    assert "## AI 判断" not in body
    assert "## 维护建议" not in body
    assert "## 时间轴图解" in body
    assert "## 关键片段" not in body
    assert "### ▶ 00:00\u3000一、问题背景" in body
    assert "| 标准 | 要求 |" in body
    assert len(body.splitlines()) <= 70


@pytest.mark.asyncio
async def test_knowledge_atoms_drive_timestamped_search(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    service.submit_analysis(
        completed.result["entry_id"],
        analysis_payload("tutorial", {"goal": "完成手冲", "parameters": ["15克粉"]}),
        producer="test-agent",
    )
    evidence = service.search_knowledge("一手证据")
    assert evidence
    assert any(item.timestamp_ms == 5000 for item in evidence)
    assert any("作者说明筛选财经信息源的方法时" in item.snippet for item in evidence)


@pytest.mark.asyncio
async def test_analysis_rejects_untraceable_quote_and_locator(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    payload = analysis_payload("tutorial", {"goal": "测试证据"})
    payload["knowledge_atoms"][0]["quote"] = "作品中从未出现的参数"
    payload["knowledge_atoms"][0]["timestamp_ms"] = 55000
    with pytest.raises(JobStateError, match="分析证据校验失败"):
        service.submit_analysis(completed.result["entry_id"], payload, producer="test-agent")


@pytest.mark.asyncio
async def test_analysis_rejects_ungrounded_timeline_chapter(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    payload = analysis_payload("tutorial", {"goal": "测试章节证据"})
    payload["chapters"][0]["evidence"] = []
    with pytest.raises(JobStateError, match="时间轴章节 1 缺少可核验证据"):
        service.submit_analysis(completed.result["entry_id"], payload, producer="test-agent")


@pytest.mark.asyncio
async def test_reanalysis_reuses_evidence_and_is_idempotent(service, monkeypatch) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    legacy = copy.deepcopy(data)
    legacy["analysis"].pop("analysis_version", None)
    service.database.upsert_entry(entry, legacy)
    raw_path = service.config.vault_path / entry.raw_path
    raw_before = raw_path.read_bytes()

    async def forbidden(*args, **kwargs):
        raise AssertionError("reanalysis must not download, transcribe, or OCR")

    monkeypatch.setattr(service.downloader, "download", forbidden)
    monkeypatch.setattr(service.transcriber, "transcribe", forbidden)
    monkeypatch.setattr(service.ocr, "recognize", forbidden)

    job = service.reanalyze_entry(entry.id)
    result = await Worker(service).run_once()
    assert result.id == job.id
    assert result.status == JobStatus.COMPLETED
    assert result.result["reused_transcript"] is True
    assert result.result["reused_ocr"] is True
    assert raw_path.read_bytes() == raw_before
    assert service.database.get_entry_data(entry.id)["analysis"]["analysis_version"] == 2

    skipped_job = service.reanalyze_entry(entry.id)
    skipped = await Worker(service).run_once()
    assert skipped.id == skipped_job.id
    assert skipped.result["skipped"] is True
    batch = service.reanalyze_all()
    assert batch["queued_job_ids"] == []
    assert entry.id in batch["skipped_entry_ids"]


@pytest.mark.asyncio
async def test_gateway_reanalysis_starts_at_analysis_phase(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    data = service.database.get_entry_data(entry.id)
    data["analysis"].pop("analysis_version", None)
    service.database.upsert_entry(entry, data)
    service.config.analysis_mode = AnalysisMode.GATEWAY

    job = service.reanalyze_entry(entry.id)
    waiting = await Worker(service).run_once()
    assert waiting.status == JobStatus.AWAITING_AGENT_ANALYSIS
    assert waiting.result["phase"] == "analysis"
    context = service.get_analysis_context(job.id)
    assert context["transcript_corrected"]
    service.submit_gateway_analysis(
        job.id,
        analysis_payload("tutorial", {"goal": "完成手冲", "parameters": ["15克粉"]}),
        producer="hermes",
    )
    finished = await Worker(service).run_once()
    assert finished.status == JobStatus.COMPLETED
    assert service.database.get_entry_data(entry.id)["provider"] == "agent:hermes"


@pytest.mark.asyncio
async def test_render_failure_keeps_previous_document_pair(service, monkeypatch) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry = service.database.get_entry(completed.result["entry_id"])
    source = service.config.vault_path / entry.source_path
    machine = service.config.vault_path / "wiki" / ".data" / "sources" / f"{entry.video_id}.md"
    before = (source.read_bytes(), machine.read_bytes())
    data_before = copy.deepcopy(service.database.get_entry_data(entry.id))

    def fail_render(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(service.vault, "_render_machine", fail_render)
    with pytest.raises(RuntimeError, match="synthetic"):
        service.submit_analysis(
            entry.id,
            analysis_payload("tutorial", {"goal": "不会写入"}),
            producer="test-agent",
        )
    assert (source.read_bytes(), machine.read_bytes()) == before
    assert service.database.get_entry_data(entry.id) == data_before


@pytest.mark.asyncio
async def test_failed_initial_vault_write_does_not_create_duplicate_cache_entry(
    service, monkeypatch
) -> None:
    original = service.vault.write_entry
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic disk failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.vault, "write_entry", fail_once)
    job = service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    failed = await Worker(service).run_once()
    assert failed.status == JobStatus.FAILED
    assert service.database.find_entry_by_video_id("7672717300746907078") is None

    service.retry_job(job.id)
    completed = await Worker(service).run_once()
    assert completed.status == JobStatus.COMPLETED
    assert service.database.find_entry_by_video_id("7672717300746907078") is not None


@pytest.mark.asyncio
async def test_database_can_be_rebuilt_from_machine_markdown(service) -> None:
    service.capture_douyin("https://v.douyin.com/uvHsRpXIn8s/")
    completed = await Worker(service).run_once()
    entry_id = completed.result["entry_id"]
    original = service.database.get_entry(entry_id)
    topic_id = service.create_topic(
        "重建测试专题", [entry_id], goal="验证 Markdown 是长期事实源"
    )["topic"]["id"]
    service.save_topic_note(topic_id, "重建后仍应存在", confirmed=True)

    preview = service.rebuild_database_from_vault()
    assert preview["entry_ids"] == [entry_id]
    assert preview["topic_ids"] == [topic_id]
    service.database.clear_knowledge_cache()
    assert service.database.list_entries() == []

    rebuilt = service.rebuild_database_from_vault(apply=True)
    assert rebuilt["status"] == "rebuilt"
    restored = service.database.get_entry(entry_id)
    assert restored.video_id == original.video_id
    assert service.database.entry_chunk_count(entry_id) > 0
    restored_topic = service.get_topic(topic_id)
    assert restored_topic["topic"]["sources"][0]["entry_id"] == entry_id
    assert restored_topic["artifacts"][0]["content_markdown"] == "重建后仍应存在"
