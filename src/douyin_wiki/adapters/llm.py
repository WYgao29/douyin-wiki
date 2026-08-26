from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config import LLMSettings, llm_api_key_required, llm_is_configured
from ..errors import ExternalToolError, ModelConfigurationError
from ..models import (
    AnalysisResult,
    InspirationInput,
    OCRObservation,
    ReviewIssue,
    TranscriptSegment,
)
from ..secrets import get_secret

PROMPT_VERSION = "v2.1-image-note"


class AnalysisProvider(ABC):
    configured: bool = True
    name: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    async def correct_transcript(
        self, segments: list[TranscriptSegment], ocr: list[OCRObservation]
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]: ...

    @abstractmethod
    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
    ) -> AnalysisResult: ...


class OpenAICompatibleProvider(AnalysisProvider):
    name = "openai-compatible"

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.model = settings.model
        stored_key = get_secret(settings.api_key_env)
        self.api_key = stored_key if llm_api_key_required(settings.base_url) else ""
        self.configured = llm_is_configured(settings, self.api_key)

    def _require_configured(self) -> None:
        if not self.configured:
            raise ModelConfigurationError(
                f"请配置 llm.model；云端接口还需配置环境变量 {self.settings.api_key_env}"
            )

    async def _json_call(self, system: str, user: str) -> dict[str, Any]:
        self._require_configured()
        url = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=body)
                    if response.status_code == 400 and "response_format" in response.text:
                        body.pop("response_format", None)
                        response = await client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                    payload = response.json()
                    content = payload["choices"][0]["message"]["content"]
                    return _parse_json_content(content)
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(2**attempt)
        raise ExternalToolError("云端模型调用失败", details={"cause": str(last_error)})

    async def correct_transcript(
        self, segments: list[TranscriptSegment], ocr: list[OCRObservation]
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]:
        self._require_configured()
        corrected: list[TranscriptSegment] = []
        issues: list[ReviewIssue] = []
        for chunk in _chunk_segments(segments, max_chars=12_000):
            nearby_ocr = [
                item.model_dump(mode="json")
                for item in ocr
                if not chunk
                or item.timestamp_ms is None
                or (chunk[0].start_ms - 5000 <= item.timestamp_ms <= chunk[-1].end_ms + 5000)
            ]
            result = await self._json_call(
                """你是中文逐字稿校对器。只能修正明显的同音字、断句、数字、人名和专有名词错误；
不得摘要、删句、补充视频没有说过的内容。OCR 只是辅助证据，冲突时保留不确定性。
输出 JSON：{\"segments\":[{\"id\":整数,\"text\":字符串}],
\"review_issues\":[{\"segment_id\":整数,\"reason\":字符串,\"suggestions\":[字符串]}]}。""",
                json.dumps(
                    {
                        "segments": [item.model_dump(mode="json") for item in chunk],
                        "ocr": nearby_ocr,
                    },
                    ensure_ascii=False,
                ),
            )
            by_id = {
                int(item["id"]): str(item["text"]).strip() for item in result.get("segments", [])
            }
            for segment in chunk:
                updated = segment.model_copy(update={"text": by_id.get(segment.id, segment.text)})
                corrected.append(updated)
            for index, item in enumerate(result.get("review_issues", [])):
                segment_id = int(item.get("segment_id", -1))
                source = next((segment for segment in chunk if segment.id == segment_id), None)
                if source:
                    issues.append(
                        ReviewIssue(
                            id=f"llm-{segment_id}-{index}",
                            start_ms=source.start_ms,
                            end_ms=source.end_ms,
                            raw_text=source.text,
                            reason=str(item.get("reason", "模型认为该片段需要人工确认")),
                            suggestions=[str(value) for value in item.get("suggestions", [])],
                        )
                    )
        return corrected, issues

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
    ) -> AnalysisResult:
        self._require_configured()
        chunks = _chunk_segments(segments, max_chars=22_000)
        if len(chunks) == 1:
            payload = await self._analysis_call(chunks[0], ocr, inspirations, metadata)
            return AnalysisResult.model_validate(payload)

        partials: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            partials.append(
                await self._analysis_call(
                    chunk,
                    [
                        item
                        for item in ocr
                        if item.timestamp_ms is None
                        or chunk[0].start_ms - 5000 <= item.timestamp_ms <= chunk[-1].end_ms + 5000
                    ],
                    inspirations,
                    {**metadata, "part": index + 1, "parts": len(chunks)},
                )
            )
        result = await self._json_call(
            _analysis_system_prompt(),
            json.dumps(
                {
                    "task": (
                        "把分段分析合并成一个完整分析；去重，但不要丢失时间敏感主张和提醒候选。"
                    ),
                    "metadata": metadata,
                    "user_inspirations_verbatim": [
                        item.model_dump(mode="json") for item in inspirations
                    ],
                    "partial_analyses": partials,
                },
                ensure_ascii=False,
            ),
        )
        return AnalysisResult.model_validate(result)

    async def _analysis_call(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._json_call(
            _analysis_system_prompt(),
            json.dumps(
                {
                    "metadata": metadata,
                    "user_inspirations_verbatim": [
                        item.model_dump(mode="json") for item in inspirations
                    ],
                    "transcript": [s.model_dump(mode="json") for s in segments],
                    "ocr": [item.model_dump(mode="json") for item in ocr],
                },
                ensure_ascii=False,
            ),
        )


class FallbackAnalysisProvider(AnalysisProvider):
    configured = False
    name = "local-fallback"
    model = "heuristic-v1"

    async def correct_transcript(
        self, segments: list[TranscriptSegment], ocr: list[OCRObservation]
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]:
        return segments, []

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
    ) -> AnalysisResult:
        transcript = "".join(segment.text for segment in segments).strip()
        post_text = str(metadata.get("post_text") or "").strip()
        ocr_text = "\n".join(item.text for item in ocr).strip()
        title = str(metadata.get("title") or "抖音作品")
        summary = (transcript or post_text or ocr_text)[:500] or "作品没有可用的文字内容。"
        return AnalysisResult(
            title=title,
            one_liner=summary[:120],
            relevance_to_inspiration=(
                "；".join(inspiration.text for inspiration in inspirations) if inspirations else ""
            ),
            takeaways=[summary] if summary else [],
            content_type="other",
            content_card={"kind": "other", "notes": [summary] if summary else []},
            risks=["未配置云端分析模型，当前为未经深度分析的降级结果。"],
            ai_judgment="需要在配置模型后重新分析。",
        )


def provider_from_settings(settings: LLMSettings) -> AnalysisProvider:
    provider = OpenAICompatibleProvider(settings)
    return provider if provider.configured else FallbackAnalysisProvider()


def _chunk_segments(
    segments: list[TranscriptSegment], *, max_chars: int
) -> list[list[TranscriptSegment]]:
    if not segments:
        return [[]]
    chunks: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    size = 0
    for segment in segments:
        if current and size + len(segment.text) > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(segment)
        size += len(segment.text)
    if current:
        chunks.append(current)
    return chunks


def _parse_json_content(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _analysis_system_prompt() -> str:
    return """你是个人知识库的抖音作品分析器。作品可能是视频，也可能是静态图文。一次完成内容分类与
结构化分析。严格区分视频原话、作品正文、OCR 画面信息、AI 推断和用户灵感。用户灵感必须逐字保留，
只能用于调整分析重点，禁止改写或补造。metadata.source_kind=image_note 时，正文位于 post_text，OCR
按 image_index 对应原图；此时不要伪造 00:00 时间戳。
选择唯一 content_type：tutorial、explanation、opinion、recommendation、news_event、story_case、
collection、other；facets 只能选 comparison、personal_experience、time_sensitive、promotion。
content_card.kind 必须等于 content_type，并按类型填写：
tutorial(goal, prerequisites, parameters, steps, pitfalls)；
explanation(question, concepts, mechanism, examples)；opinion(thesis, reasons, assumptions,
counterpoints)；recommendation(subjects, criteria, pros, cons, best_for)；news_event(event,
absolute_time, impact, actions, valid_until)；
story_case(context, turning_points, outcome, lessons)；
collection(items[{name,traits,scenarios}])；other(notes)。
one_liner 不超过 120 个中文字符；takeaways 输出 3–5 条；key_moments 输出 3–5 条，每条必须有
timestamp_ms（图文为 null）、image_index（视频为 null）、title、summary、quote（可空）和
evidence_type(audio/ocr/audio+ocr/post_text/image_ocr/post_text+image_ocr/ai_inference)。
knowledge_atoms 把可检索知识拆成原子，字段为 id、statement、atom_type、provenance、timestamp_ms、
image_index、quote、context、confidence、valid_until、review_after、stale。事实、数字、日期、参数和
方法必须尽可能带时间戳或图片编号和原文；AI 推断必须使用 provenance=ai_inference，且不得伪装成
作品原话。
用户灵感必须逐字保留，只能用于调整分析重点，禁止替用户发明灵感。
metadata.existing_knowledge 是从本地知识库召回的既有主张；只有新旧主张明确不兼容时才写入
contradictions，必须引用其中真实存在的 entry_id/claim_id，保留双方来源，不要擅自裁决或覆盖。
日期、促销、活动或待办放入 reminders；不能确定绝对时间时 due_at=null、needs_clarification=true。
输出一个 JSON 对象，字段必须兼容：title, analysis_version=2, one_liner,
relevance_to_inspiration, takeaways[], content_type, facets[], content_card, key_moments[],
knowledge_atoms[], actions[], open_questions[], tags[], concepts[],
entities[{name,kind,description}],
contradictions[{id,claim_id,conflicts_with_entry_id,conflicts_with_claim_id,reason,confidence,status}],
reminders[{id,title,due_at,timezone,reason,source_quote,confidence,
needs_clarification}]。时间使用 ISO 8601，未知字段使用 null、空字符串或空数组。"""
