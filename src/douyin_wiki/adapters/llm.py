from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config import LLMSettings, llm_is_configured
from ..errors import ExternalToolError, ModelConfigurationError
from ..models import (
    AnalysisResult,
    InspirationInput,
    OCRObservation,
    ReviewIssue,
    TranscriptSegment,
)
from ..secrets import get_secret

PROMPT_VERSION = "v2.2-timeline-chapters"
TRANSCRIPT_CORRECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        },
        "review_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "integer"},
                    "reason": {"type": "string"},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["segment_id", "reason", "suggestions"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["segments", "review_issues"],
    "additionalProperties": False,
}


def _analysis_response_schema() -> dict[str, Any]:
    schema = AnalysisResult.model_json_schema()
    # Legacy fields remain readable in stored entries but need not be generated.
    for name in (
        "summary",
        "core_points",
        "evidence",
        "steps",
        "applicable_scenarios",
        "risks",
        "claims",
        "ai_judgment",
    ):
        schema["properties"].pop(name, None)
    schema["required"] = [
        "analysis_version",
        "title",
        "one_liner",
        "relevance_to_inspiration",
        "takeaways",
        "content_type",
        "facets",
        "content_card",
        "chapters",
        "knowledge_atoms",
        "actions",
        "open_questions",
        "reminders",
        "tags",
        "concepts",
        "entities",
        "contradictions",
    ]
    schema["properties"]["takeaways"]["minItems"] = 1
    schema["properties"]["knowledge_atoms"]["maxItems"] = 30

    def bound(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            if node.get("type") == "array":
                node.setdefault("maxItems", 12)
            if node.get("type") == "string":
                node.setdefault("maxLength", 1000)
            for value in node.values():
                bound(value)
        elif isinstance(node, list):
            for value in node:
                bound(value)

    bound(schema)
    return _openai_strict_schema(schema)


def _openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON schema into an OpenAI ``strict``-compatible shape.

    OpenAI/oMLX ``json_schema`` + ``strict: true`` rejects nullable ``anyOf``,
    missing ``required`` entries, free ``additionalProperties``, remote ``$ref``,
    and several keywords backends reject (``const``, ``default``, ``title``,
    ``discriminator``, ``maxLength``/``maxItems``/``minItems``, …). Inline defs,
    require every property, encode nullability as ``type: [T, "null"]``, keep
    multi-way unions (e.g. ``content_card``) as ``anyOf``, and map ``const`` to
    ``enum``.
    """
    import copy

    strip_keys = {
        "default",
        "discriminator",
        "title",
        "maxLength",
        "minLength",
        "maxItems",
        "minItems",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
    }

    root = copy.deepcopy(schema)
    defs = root.pop("$defs", None) or root.pop("definitions", None) or {}

    def resolve(ref: str) -> dict[str, Any]:
        name = ref.rsplit("/", 1)[-1]
        if name not in defs:
            raise KeyError(f"unknown schema ref: {ref}")
        return copy.deepcopy(defs[name])

    def convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = convert(resolve(node["$ref"]))
            for key, value in node.items():
                if key == "$ref" or key in strip_keys or key == "const":
                    continue
                target[key] = convert(value) if isinstance(value, dict) else value
            if "const" in node and "enum" not in target:
                target["enum"] = [node["const"]]
            return target

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in strip_keys:
                continue
            if key == "const":
                if "enum" not in out and "enum" not in node:
                    out["enum"] = [value]
                continue
            if key in {"anyOf", "oneOf"}:
                variants = [convert(item) for item in value]
                non_null = [
                    item
                    for item in variants
                    if not (isinstance(item, dict) and item.get("type") == "null")
                ]
                has_null = len(non_null) != len(variants)
                # Collapse Optional/nullable unions; keep multi-way unions (content_card).
                if has_null and len(non_null) == 1:
                    base = dict(non_null[0])
                    type_value = base.get("type")
                    if isinstance(type_value, str):
                        base["type"] = [type_value, "null"]
                    elif isinstance(type_value, list):
                        if "null" not in type_value:
                            base["type"] = [*type_value, "null"]
                    elif "properties" in base:
                        base["type"] = ["object", "null"]
                    elif "items" in base:
                        base["type"] = ["array", "null"]
                    for extra_key, extra_value in node.items():
                        if (
                            extra_key in {"anyOf", "oneOf", "const"}
                            or extra_key in strip_keys
                            or extra_key in base
                        ):
                            continue
                        base[extra_key] = extra_value
                    out.update(base)
                    continue
                out["anyOf"] = variants
                continue
            if key == "properties" and isinstance(value, dict):
                out["properties"] = {name: convert(child) for name, child in value.items()}
                continue
            if key == "items":
                out["items"] = convert(value)
                continue
            if isinstance(value, dict):
                out[key] = convert(value)
            elif isinstance(value, list):
                out[key] = [convert(item) if isinstance(item, dict) else item for item in value]
            else:
                out[key] = value

        is_object = (
            out.get("type") == "object"
            or (isinstance(out.get("type"), list) and "object" in out["type"])
            or "properties" in out
        )
        if is_object:
            properties = out.setdefault("properties", {})
            out["required"] = list(properties.keys())
            out["additionalProperties"] = False
            if "type" not in out:
                out["type"] = "object"
        return out

    return convert(root)


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
        # Loopback servers may be unauthenticated (for example LM Studio/Ollama),
        # but some local servers such as OMLX still require an API key. Preserve a
        # configured key while allowing local endpoints to work without one.
        self.api_key = stored_key
        self.configured = llm_is_configured(settings, self.api_key)
        self.last_usage: dict[str, int] = {}

    def _require_configured(self) -> None:
        if not self.configured:
            raise ModelConfigurationError(
                f"请配置 llm.model；云端接口还需配置环境变量 {self.settings.api_key_env}"
            )

    async def _json_call(
        self, system: str, user: str, *, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_configured()
        url = f"{self.settings.base_url.rstrip('/')}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "douyin_wiki_result",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
                if self.settings.response_format == "json_schema"
                else {"type": "json_object"}
            ),
        }
        if self.settings.enable_thinking is not None:
            body["enable_thinking"] = self.settings.enable_thinking
        if self.settings.thinking_budget is not None:
            body["thinking_budget"] = self.settings.thinking_budget
        if self.settings.max_output_tokens is not None:
            body["max_tokens"] = self.settings.max_output_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=body)
                    if response.status_code == 400 and "response_format" in response.text:
                        current = body.get("response_format")
                        if (
                            isinstance(current, dict)
                            and current.get("type") == "json_schema"
                        ):
                            # oMLX/OpenAI may reject an incompatible strict schema; relax.
                            body["response_format"] = {"type": "json_object"}
                            response = await client.post(url, headers=headers, json=body)
                        elif self.settings.response_format == "json_object":
                            body.pop("response_format", None)
                            response = await client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                    payload = response.json()
                    usage = payload.get("usage") or {}
                    self.last_usage = {
                        key: int(value) for key, value in usage.items() if isinstance(value, int)
                    }
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("模型输出达到 token 上限")
                    content = choice["message"]["content"]
                    return _parse_json_content(content)
            except (
                httpx.HTTPError,
                AttributeError,
                KeyError,
                IndexError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
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
                response_schema=TRANSCRIPT_CORRECTION_SCHEMA,
            )
            by_id: dict[int, str] = {}
            for item in result.get("segments", []):
                try:
                    identifier = int(item["id"])
                    text = str(item["text"]).strip()
                except (KeyError, TypeError, ValueError):
                    continue
                if text:
                    by_id[identifier] = text
            for segment in chunk:
                updated = segment.model_copy(update={"text": by_id.get(segment.id, segment.text)})
                corrected.append(updated)
            for index, item in enumerate(result.get("review_issues", [])):
                try:
                    segment_id = int(item.get("segment_id", -1))
                except (TypeError, ValueError):
                    continue
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
            response_schema=_analysis_response_schema(),
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
            response_schema=_analysis_response_schema(),
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
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return parsed


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
one_liner 不超过 120 个中文字符；takeaways 输出 3–5 条。视频必须输出 chapters 时间轴图解，按
内容实际展开顺序完整覆盖有信息量的部分，通常 4–10 章，不要只挑结论片段。每章包含 start_ms、
end_ms、title、summary、key_points，以及可选 comparison_table{headers,rows}；章节标题应概括主题，
start_ms 应定位主题开始处而不是结论出现处。每章 evidence 输出 1–6 条可核验依据，每条包含
timestamp_ms、quote 和 evidence_type(audio/ocr/audio+ocr)。只有画面存在清晰、结构化对比数据时才
生成 comparison_table，不得根据推断补表。静态图文没有视频时间轴，chapters 必须为空数组。
knowledge_atoms 把可检索知识拆成原子，字段为 id、statement、atom_type、provenance、timestamp_ms、
image_index、quote、context、confidence、valid_until、review_after、stale。事实、数字、日期、参数和
方法必须尽可能带时间戳或图片编号和原文；AI 推断必须使用 provenance=ai_inference，且不得伪装成
作品原话。
用户灵感必须逐字保留，只能用于调整分析重点，禁止替用户发明灵感。
metadata.existing_knowledge 是从本地知识库召回的既有主张；只有新旧主张明确不兼容时才写入
contradictions，必须引用其中真实存在的 entry_id/claim_id，保留双方来源，不要擅自裁决或覆盖。
日期、促销、活动或待办放入 reminders；不能确定绝对时间时 due_at=null、needs_clarification=true。
输出一个 JSON 对象，字段必须兼容：title, analysis_version=2, one_liner,
relevance_to_inspiration, takeaways[], content_type, facets[], content_card, chapters[],
knowledge_atoms[], actions[], open_questions[], tags[], concepts[],
entities[{name,kind,description}],
contradictions[{id,claim_id,conflicts_with_entry_id,conflicts_with_claim_id,reason,confidence,status}],
reminders[{id,title,due_at,timezone,reason,source_quote,confidence,
needs_clarification}]。时间使用 ISO 8601，未知字段使用 null、空字符串或空数组。"""
