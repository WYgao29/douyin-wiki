from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import LLMSettings, llm_is_configured
from ..errors import (
    ExternalToolError,
    ModelConfigurationError,
    ModelConnectionError,
    ModelServiceError,
    ModelTimeoutError,
)
from ..models import (
    AnalysisResult,
    InspirationInput,
    OCRObservation,
    ReviewIssue,
    TimelineChapter,
    TranscriptSegment,
)
from ..secrets import get_secret

PROMPT_VERSION = "v2.3-context-correction"


class ModelLimitError(ExternalToolError):
    """A request must be made smaller before it can be retried."""

    code = "model_limit"


class ModelContextError(ModelLimitError):
    code = "model_context_limit"


class ModelOutputError(ModelLimitError):
    code = "model_output_limit"



def _soft_merge_analysis_partials(
    group: list[dict[str, Any]], *, reason: str
) -> dict[str, Any]:
    """Concatenate partial analyses without another model call."""
    open_questions: list[str] = []
    knowledge_atoms: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    actions: list[str] = []
    tags: list[str] = []
    concepts: list[str] = []
    entities: list[dict[str, Any]] = []
    reminders: list[dict[str, Any]] = []
    takeaways: list[str] = []
    titles: list[str] = []
    notes: list[str] = []
    for partial in group:
        titles.append(str(partial.get("title") or "").strip())
        open_questions.extend(
            str(item).strip()
            for item in (partial.get("open_questions") or [])
            if str(item).strip()
        )
        knowledge_atoms.extend(
            item for item in (partial.get("knowledge_atoms") or []) if isinstance(item, dict)
        )
        chapters.extend(
            item for item in (partial.get("chapters") or []) if isinstance(item, dict)
        )
        actions.extend(
            str(item).strip() for item in (partial.get("actions") or []) if str(item).strip()
        )
        tags.extend(str(item).strip() for item in (partial.get("tags") or []) if str(item).strip())
        concepts.extend(
            str(item).strip() for item in (partial.get("concepts") or []) if str(item).strip()
        )
        entities.extend(
            item for item in (partial.get("entities") or []) if isinstance(item, dict)
        )
        reminders.extend(
            item for item in (partial.get("reminders") or []) if isinstance(item, dict)
        )
        takeaways.extend(
            str(item).strip() for item in (partial.get("takeaways") or []) if str(item).strip()
        )
        card = partial.get("content_card") or {}
        if isinstance(card, dict):
            notes.extend(
                str(item).strip() for item in (card.get("notes") or []) if str(item).strip()
            )
    open_questions.append(reason)
    notes.append(reason)
    title = next((item for item in titles if item and not item.startswith("跳过")), None)
    if title is None:
        title = titles[0] if titles and titles[0] else "分段汇总（降级）"
    return AnalysisResult(
        title=title[:200],
        one_liner=reason[:120],
        takeaways=list(dict.fromkeys(takeaways))[:5],
        open_questions=list(dict.fromkeys(open_questions)),
        knowledge_atoms=knowledge_atoms,
        chapters=chapters[:12],
        actions=list(dict.fromkeys(actions)),
        tags=list(dict.fromkeys(tags)),
        concepts=list(dict.fromkeys(concepts)),
        entities=entities,
        reminders=reminders,
        content_card={"kind": "other", "notes": list(dict.fromkeys(notes))},
    ).model_dump(mode="json")



def _normalize_correction_segments(
    chunk: list[TranscriptSegment], returned: Any
) -> list[dict[str, Any]]:
    """Map model correction rows onto local chunk indices ``0..n-1``.

    Live models sometimes emit 1-based ids (``1..n``), unknown extras, or
    duplicates. Prefer exact local ids, then 1-based remapping, then
    positional fallback when the usable row count matches. Gaps keep the
    original chunk text so a single bad id does not fail the whole job.
    """
    if not isinstance(returned, list):
        raise ValueError("模型校正结果缺少 segments 数组")
    size = len(chunk)
    if size == 0:
        return []

    rows: list[dict[str, Any]] = []
    for item in returned:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        rows.append({"id": item["id"], "text": text})

    def filled(mapping: dict[int, str]) -> list[dict[str, Any]]:
        return [
            {"id": index, "text": mapping[index] if index in mapping else chunk[index].text}
            for index in range(size)
        ]

    local: dict[int, str] = {}
    for row in rows:
        identifier = row["id"]
        if 0 <= identifier < size and identifier not in local:
            local[identifier] = row["text"]

    one_based: dict[int, str] = {}
    for row in rows:
        identifier = row["id"] - 1
        if 0 <= identifier < size and identifier not in one_based:
            one_based[identifier] = row["text"]

    returned_ids = [row["id"] for row in rows]
    looks_one_based = (
        bool(returned_ids)
        and min(returned_ids) >= 1
        and max(returned_ids) <= size
        and 0 not in returned_ids
    )

    if looks_one_based and len(one_based) >= max(len(local), 1):
        return filled(one_based)
    if local:
        # Prefer partial local coverage (fill gaps from originals) over positional
        # guessing whenever any in-range id was returned.
        return filled(local)
    if len(rows) == size:
        return [{"id": index, "text": rows[index]["text"]} for index in range(size)]
    if one_based:
        return filled(one_based)
    # Nothing usable — keep originals rather than failing the whole correction.
    return filled({})


def _split_transcript_text(value: str, *, min_chars: int = 12) -> tuple[str, str] | None:
    """Shrink a transcript piece for context/output limits.

    Prefer punctuation boundaries when both halves meet ``min_chars``.
    Otherwise hard-split on characters. Returns ``None`` only when the
    text is irreducible (fewer than 2 characters).
    """
    if len(value) < 2:
        return None
    middle = max(1, len(value) // 2)
    boundaries = [
        match.end()
        for match in re.finditer(r"[。！？!?；;，,、：:\n]", value)
        if min_chars <= match.end() <= len(value) - min_chars
    ]
    boundary = (
        min(boundaries, key=lambda point: abs(point - middle))
        if boundaries
        else middle
    )
    left, right = value[:boundary], value[boundary:]
    if not left or not right:
        return None
    return left, right


def _is_context_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        word in lowered
        for word in (
            "context length",
            "context window",
            "prompt too long",
            "too many tokens",
            "maximum context",
            "上下文",
            "输入过长",
        )
    )


def _is_output_truncation_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "failed to extract valid json from output",
            "json validation failed",
            "output token limit",
            "finish_reason=length",
        )
    )


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
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def _looks_like_json_schema_rejection(body_text: str) -> bool:
    """True when a 400 body likely rejects strict json_schema response_format.

    Backends vary (oMLX / OpenAI-compatible forks). Prefer explicit format tokens
    over bare words like ``schema`` / ``keyword`` / empty bodies, so unrelated
    400s (bad max_tokens, unknown model, blank errors) are not masked.
    """
    lowered = (body_text or "").lower().strip()
    if not lowered:
        return False
    markers = (
        "json_schema",
        "response_format",
        "additionalproperties",
        "additional_properties",
    )
    if any(marker in lowered for marker in markers):
        return True
    # e.g. "strict mode does not support this keyword"
    return "strict" in lowered and ("keyword" in lowered or "schema" in lowered)


def _looks_like_json_object_rejection(body_text: str) -> bool:
    lowered = (body_text or "").lower()
    return "response_format" in lowered or "json_object" in lowered


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

    # Do not write minItems/maxItems/maxLength here: OpenAI-strict stripping
    # removes them, so pre-strict bounds would be dead writes. Keep only
    # additionalProperties=False which survives (and is required by strict).
    def bound(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
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
        self._tokenizer: Any = None
        for path in (Path.home() / ".omlx/models").glob(
            f"*/{Path(self.model).name}/tokenizer.json"
        ):
            try:
                from tokenizers import Tokenizer

                self._tokenizer = Tokenizer.from_file(str(path))
                break
            except (ImportError, OSError, ValueError):
                continue

    def _token_count(self, value: str) -> int:
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(value).ids)
        # UTF-8 byte length is conservative for Chinese and mixed OCR text.
        return len(value.encode("utf-8"))

    def _fits(self, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> bool:
        request = json.dumps(payload, ensure_ascii=False)
        schema_text = json.dumps(schema, ensure_ascii=False)
        output = self.settings.max_output_tokens or 8192
        context = self.settings.context_window_tokens
        reserve = min(output, context // 2) + max(128, context // 50)
        return self._token_count(system + request + schema_text) + reserve <= context

    async def _checkpointed_call(
        self,
        system: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
        checkpoints: dict[str, Any] | None = None,
        on_checkpoint: Callable[[str, dict[str, Any]], None] | None = None,
        checkpoint_scope: Any = None,
    ) -> dict[str, Any]:
        user = json.dumps(payload, ensure_ascii=False)
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    PROMPT_VERSION,
                    self.model,
                    self.settings.max_output_tokens,
                    self.settings.enable_thinking,
                    self.settings.thinking_budget,
                    checkpoint_scope,
                    system,
                    user,
                    schema,
                ],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if checkpoints is not None and fingerprint in checkpoints:
            result = checkpoints[fingerprint]
            if validator is not None:
                try:
                    validator(result)
                except ValueError:
                    del checkpoints[fingerprint]
                else:
                    return result
            else:
                return result
        result = await self._json_call(
            system,
            user,
            response_schema=schema,
            **({"response_validator": validator} if validator else {}),
        )
        if validator is not None:
            validator(result)
        if checkpoints is not None:
            checkpoints[fingerprint] = result
        if on_checkpoint is not None:
            on_checkpoint(fingerprint, result)
        return result

    def _budgeted_chunks(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        system: str,
        schema: dict[str, Any],
        payload_for: Callable[[list[TranscriptSegment], list[OCRObservation]], dict[str, Any]],
        *,
        max_items: int = 80,
        max_text_chars: int | None = None,
    ) -> list[list[TranscriptSegment]]:
        if not segments:
            return [[]]
        chunks: list[list[TranscriptSegment]] = []
        current: list[TranscriptSegment] = []
        for segment in segments:
            candidate = [*current, segment]
            nearby = self._fit_ocr(candidate, ocr, system, schema, payload_for)
            if current and (
                len(candidate) > max_items
                or (
                    max_text_chars is not None
                    and sum(len(item.text) for item in candidate) > max_text_chars
                )
                or not self._fits(
                    system,
                    payload_for(candidate, nearby),
                    schema,
                )
            ):
                chunks.append(current)
                candidate = [segment]
            nearby = self._fit_ocr(candidate, ocr, system, schema, payload_for)
            if not self._fits(system, payload_for(candidate, nearby), schema):
                raise ModelContextError(f"字幕段 {segment.id} 单独超过模型输入预算")
            current = candidate
        if current:
            chunks.append(current)
        return chunks

    def _fit_ocr(
        self,
        chunk: list[TranscriptSegment],
        ocr: list[OCRObservation],
        system: str,
        schema: dict[str, Any],
        payload_for: Callable[[list[TranscriptSegment], list[OCRObservation]], dict[str, Any]],
    ) -> list[OCRObservation]:
        nearby = _nearby_ocr(chunk, ocr)
        while nearby and not self._fits(system, payload_for(chunk, nearby), schema):
            nearby.pop()
        return nearby

    def _budgeted_ocr_chunks(
        self,
        ocr: list[OCRObservation],
        system: str,
        schema: dict[str, Any],
        payload_for: Callable[[list[TranscriptSegment], list[OCRObservation]], dict[str, Any]],
    ) -> list[list[OCRObservation]]:
        chunks: list[list[OCRObservation]] = []
        current: list[OCRObservation] = []
        for item in ocr:
            candidate = [*current, item]
            if current and (
                len(candidate) > 20
                or sum(len(value.text) for value in candidate) > 6000
                or not self._fits(system, payload_for([], candidate), schema)
            ):
                chunks.append(current)
                candidate = [item]
            if not self._fits(system, payload_for([], candidate), schema):
                raise ModelContextError("单条 OCR 超过模型输入预算")
            current = candidate
        if current:
            chunks.append(current)
        return chunks or [[]]

    def _require_configured(self) -> None:
        if not self.configured:
            raise ModelConfigurationError(
                f"请配置 llm.model；云端接口还需配置环境变量 {self.settings.api_key_env}"
            )

    async def _json_call(
        self,
        system: str,
        user: str,
        *,
        response_schema: dict[str, Any],
        response_validator: Callable[[dict[str, Any]], Any] | None = None,
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

        def check_limits(response: httpx.Response) -> None:
            if response.status_code == 400 and _is_context_error(response.text):
                raise ModelContextError("模型输入超过上下文窗口")
            if response.status_code >= 400 and _is_output_truncation_error(response.text):
                raise ModelOutputError("模型输出截断，需缩小批次")

        last_error: Exception | None = None
        request_count = 0
        for attempt in range(self.settings.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                    request_count += 1
                    response = await client.post(url, headers=headers, json=body)
                    check_limits(response)
                    if response.status_code == 400:
                        current = body.get("response_format")
                        if (
                            isinstance(current, dict)
                            and current.get("type") == "json_schema"
                            and _looks_like_json_schema_rejection(response.text)
                        ):
                            # Schema/strict rejection only — do not mask unrelated 400s.
                            body["response_format"] = {"type": "json_object"}
                            request_count += 1
                            response = await client.post(url, headers=headers, json=body)
                            check_limits(response)
                    if response.status_code == 400:
                        current = body.get("response_format")
                        if (
                            isinstance(current, dict)
                            and current.get("type") == "json_object"
                            and _looks_like_json_object_rejection(response.text)
                        ):
                            body.pop("response_format", None)
                            request_count += 1
                            response = await client.post(url, headers=headers, json=body)
                            check_limits(response)
                    response.raise_for_status()
                    payload = response.json()
                    usage = payload.get("usage") or {}
                    self.last_usage = {
                        key: int(value) for key, value in usage.items() if isinstance(value, int)
                    }
                    self.last_usage["request_count"] = request_count
                    self.last_usage["retry_count"] = max(0, request_count - 1)
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") == "length":
                        message = choice.get("message") or {}
                        content = message.get("content") or ""
                        reasoning = (
                            message.get("reasoning_content") or message.get("reasoning") or ""
                        )
                        raise ModelOutputError(
                            "模型输出达到 token 上限",
                            details={
                                "finish_reason": "length",
                                "usage": self.last_usage.copy(),
                                "content_chars": len(content),
                                "reasoning_chars": len(reasoning),
                            },
                        )
                    content = choice["message"]["content"]
                    try:
                        parsed = _parse_json_content(content)
                    except (ValueError, json.JSONDecodeError) as exc:
                        emitted = usage.get("completion_tokens") or usage.get("output_tokens")
                        if (
                            isinstance(emitted, int)
                            and self.settings.max_output_tokens is not None
                            and emitted >= self.settings.max_output_tokens - 1
                        ):
                            raise ModelOutputError(
                                "模型输出达到 token 上限，JSON 未完整返回"
                            ) from exc
                        raise
                    if response_validator is not None:
                        try:
                            response_validator(parsed)
                        except ValueError as exc:
                            if attempt < self.settings.max_retries:
                                body["messages"] = [
                                    {"role": "system", "content": system},
                                    {"role": "user", "content": user},
                                    {
                                        "role": "user",
                                        "content": (
                                            "上一个 JSON 不符合结果模型，"
                                            "请根据原始输入重新生成完整 JSON。"
                                            f"校验错误：{str(exc)[:700]}"
                                        ),
                                    },
                                ]
                            raise
                    return parsed
            except ModelLimitError:
                raise
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
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code >= 500 or exc.response.status_code == 429
                )
                if attempt < self.settings.max_retries and retryable:
                    await asyncio.sleep(2**attempt)
                elif not retryable:
                    break
        details = {"cause": str(last_error), "request_count": request_count}
        if isinstance(last_error, httpx.HTTPStatusError):
            details["status_code"] = last_error.response.status_code
        if isinstance(last_error, httpx.TimeoutException):
            raise ModelTimeoutError("模型请求超时，请检查模型接口状态", details=details)
        if isinstance(last_error, httpx.ConnectError):
            raise ModelConnectionError("无法连接模型服务，请检查接口地址和进程", details=details)
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code >= 500:
            raise ModelServiceError("模型服务暂不可用，请检查模型接口状态", details=details)
        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 400:
            raise ExternalToolError(
                "模型拒绝请求，请检查模型名称、输出上限和响应格式",
                details=details,
            )
        if isinstance(last_error, (ValueError, json.JSONDecodeError)):
            raise ExternalToolError(f"模型结果校验失败：{last_error}", details=details)
        raise ExternalToolError("模型调用失败", details=details)

    async def correct_transcript(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        *,
        checkpoints: dict[str, Any] | None = None,
        on_checkpoint: Callable[[str, dict[str, Any]], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[list[TranscriptSegment], list[ReviewIssue]]:
        self._require_configured()
        corrected: list[TranscriptSegment] = []
        system = """你是中文逐字稿校对器。结合原话上下文与对应时间附近的 OCR，
直接给出最贴近原意的完整逐字稿。
根据语义上下文修复同音词、断句等识别错误；不得摘要、删句、扩写或添加视频没有说过的信息。
OCR 只是辅助证据，不出现对应文字不代表原话错误。数字、金额、人名等没有足够依据时保留原表述。
context_before/context_after 只供理解，不得复制进当前段的 text；
只返回 segments 中当前 text 对应的内容。
对不确定的地方作出保守选择，不请求人工复核。每段 id 必须与输入 segments 的 id 完全一致（本次请求内从 0 起的本地序号），不要改用其它编号。
输出 JSON：{\"segments\":[{\"id\":整数,\"text\":字符串}]}。"""
        source_index = {segment.id: index for index, segment in enumerate(segments)}
        source_by_id = {segment.id: segment for segment in segments}
        bounds_by_id: dict[int, tuple[int, int]] = {}

        def payload_for(
            chunk: list[TranscriptSegment], nearby: list[OCRObservation]
        ) -> dict[str, Any]:
            payload = {
                # Model-facing IDs are small and local to this request. Internal
                # path IDs only identify pieces/checkpoints and never reach the model.
                "segments": [],
                "ocr": [_ocr_prompt_item(item) for item in nearby],
            }
            for index, item in enumerate(chunk):
                source = source_by_id[item.id]
                start, end = bounds_by_id[item.id]
                position = source_index[source.id]
                previous = source.text[:start] or (
                    segments[position - 1].text if position else ""
                )
                following = source.text[end:] or (
                    segments[position + 1].text if position + 1 < len(segments) else ""
                )
                prompt_item = _transcript_prompt_item(item.model_copy(update={"id": index}))
                prompt_item["context_before"] = previous[-40:]
                prompt_item["context_after"] = following[:40]
                payload["segments"].append(prompt_item)
            return payload

        if len({segment.id for segment in segments}) != len(segments):
            raise ExternalToolError("原始逐字稿片段 ID 重复，无法安全校正")
        if not segments:
            if on_progress is not None:
                on_progress("correction", 0, 0)
            return [], []

        # Internal IDs identify text pieces, while the returned segments retain
        # their original IDs and time ranges. Source index and binary split path
        # keep IDs stable even when another branch succeeds on a later retry.
        id_floor = min(segment.id for segment in segments)
        path_by_id: dict[int, int] = {}
        pieces_by_source: dict[int, list[int]] = {segment.id: [] for segment in segments}
        pieces: list[TranscriptSegment] = []
        min_chars = 12
        max_chars = max(48, min(1600, (self.settings.max_output_tokens or 8192) // 4))

        def split_text(value: str) -> tuple[str, str] | None:
            return _split_transcript_text(value, min_chars=min_chars)

        def add_piece(
            source: TranscriptSegment, value: str, path: int, start: int
        ) -> TranscriptSegment:
            identifier = id_floor - ((source_index[source.id] + 1) * (1 << 32) + path)
            piece = source.model_copy(update={"id": identifier, "text": value})
            source_by_id[identifier] = source
            bounds_by_id[identifier] = (start, start + len(value))
            path_by_id[identifier] = path
            return piece

        for source in segments:
            pending = [(source.text, 1, 0)]
            while pending:
                value, path, start = pending.pop()
                candidate = add_piece(source, value, path, start)
                nearby = self._fit_ocr(
                    [candidate], ocr, system, TRANSCRIPT_CORRECTION_SCHEMA, payload_for
                )
                if len(value) <= max_chars and self._fits(
                    system, payload_for([candidate], nearby), TRANSCRIPT_CORRECTION_SCHEMA
                ):
                    pieces.append(candidate)
                    pieces_by_source[source.id].append(candidate.id)
                    continue
                halves = split_text(value)
                if halves is None:
                    raise ModelContextError(f"字幕段 {source.id} 单独超过模型输入预算")
                pending.extend(
                    (
                        (halves[1], path * 2 + 1, start + len(halves[0])),
                        (halves[0], path * 2, start),
                    )
                )

        chunks = self._budgeted_chunks(
            pieces,
            ocr,
            system,
            TRANSCRIPT_CORRECTION_SCHEMA,
            payload_for,
            max_text_chars=max_chars,
        )
        completed = 0
        total = len(pieces) if pieces else 1
        if on_progress is not None:
            on_progress("correction", completed, total)

        def validate_result(chunk: list[TranscriptSegment], result: dict[str, Any]) -> None:
            # Normalize unknown/duplicate/1-based ids onto local 0..n-1 indices.
            # Mutates result so downstream indexing stays simple and safe.
            result["segments"] = _normalize_correction_segments(chunk, result.get("segments"))
        results: dict[int, str] = {}
        limit_splits = 0
        # Character-level hard-splits need headroom proportional to total chars.
        max_limit_splits = max(
            32,
            len(pieces) * 8,
            sum(len(piece.text) for piece in pieces) * 2,
        )

        async def run_chunk(chunk: list[TranscriptSegment]) -> None:
            nonlocal limit_splits, completed, total
            if limit_splits > max_limit_splits:
                raise ModelOutputError("字幕校正连续超限，已达到有界重试上限")
            try:
                result = await self._checkpointed_call(
                    system,
                    payload_for(
                        chunk,
                        self._fit_ocr(
                            chunk,
                            ocr,
                            system,
                            TRANSCRIPT_CORRECTION_SCHEMA,
                            payload_for,
                        ),
                    ),
                    TRANSCRIPT_CORRECTION_SCHEMA,
                    validator=lambda result: validate_result(chunk, result),
                    checkpoints=checkpoints,
                    on_checkpoint=on_checkpoint,
                    checkpoint_scope=[piece.id for piece in chunk],
                )
            except ModelLimitError as exc:
                limit_splits += 1
                if limit_splits > max_limit_splits:
                    raise ModelOutputError(
                        "字幕校正连续超限，已达到有界重试上限",
                        details={"last_response": exc.details},
                    ) from exc
                if len(chunk) > 1:
                    middle = len(chunk) // 2
                    await run_chunk(chunk[:middle])
                    await run_chunk(chunk[middle:])
                    return
                piece = chunk[0]
                halves = split_text(piece.text)
                if halves is None:
                    source = source_by_id[piece.id]
                    raise ModelOutputError(
                        f"字幕段 {source.id} 的最小文本块输出超限，无法完成校正",
                        details={"last_response": exc.details},
                    ) from exc
                source = source_by_id[piece.id]
                path = path_by_id[piece.id]
                start, _end = bounds_by_id[piece.id]
                children = [
                    add_piece(source, halves[0], path * 2, start),
                    add_piece(source, halves[1], path * 2 + 1, start + len(halves[0])),
                ]
                position = pieces_by_source[source.id].index(piece.id)
                pieces_by_source[source.id][position : position + 1] = [
                    child.id for child in children
                ]
                total += 1
                await run_chunk([children[0]])
                await run_chunk([children[1]])
                return
            for item in result["segments"]:
                results[chunk[item["id"]].id] = item["text"]
            completed += len(chunk)
            if on_progress is not None:
                on_progress("correction", completed, total)

        for chunk in chunks:
            await run_chunk(chunk)

        for segment in segments:
            piece_ids = pieces_by_source[segment.id]
            if any(identifier not in results for identifier in piece_ids):
                raise ExternalToolError(f"字幕段 {segment.id} 的子块未全部完成，无法合并")
            text = "".join(results[identifier] for identifier in piece_ids)
            corrected.append(segment.model_copy(update={"text": text}))
        return corrected, []

    async def analyze(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
        *,
        checkpoints: dict[str, Any] | None = None,
        on_checkpoint: Callable[[str, dict[str, Any]], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> AnalysisResult:
        self._require_configured()
        schema = _analysis_response_schema()
        system = _analysis_system_prompt()
        # A split piece still has only its source segment's time range. The
        # model may cite its text, but must not infer a finer timestamp.
        split_system = (
            system
            + "\n同一字幕段的内部文本块可能共用原始起止时间；音频引文只用所给字幕段的"
            "start_ms 作粗粒度定位，不按文字位置推算更精确的时间。"
        )
        max_text_chars = max(48, min(1600, (self.settings.max_output_tokens or 8192) // 4))
        min_chars = 12
        source_index = {segment.id: index for index, segment in enumerate(segments)}
        if len(source_index) != len(segments):
            raise ExternalToolError("原始逐字稿片段 ID 重复，无法安全分析")
        id_floor = min(source_index, default=0)
        path_by_id = {segment.id: 1 for segment in segments}
        source_by_id = {segment.id: segment for segment in segments}
        # Reserve space for part/parts before chunking. The actual values are
        # smaller, so an accepted chunk also fits the later request payload.
        budget_metadata = {**metadata, "part": 999999999, "parts": 999999999}

        def split_text(value: str) -> tuple[str, str] | None:
            return _split_transcript_text(value, min_chars=min_chars)

        def add_piece(source: TranscriptSegment, value: str, path: int) -> TranscriptSegment:
            identifier = id_floor - ((source_index[source.id] + 1) * (1 << 32) + path)
            piece = source.model_copy(update={"id": identifier, "text": value})
            source_by_id[identifier] = source
            path_by_id[identifier] = path
            return piece

        def payload_for(
            chunk: list[TranscriptSegment], nearby: list[OCRObservation]
        ) -> dict[str, Any]:
            return {
                "metadata": budget_metadata,
                "user_inspirations_verbatim": [
                    item.model_dump(mode="json") for item in inspirations
                ],
                "transcript": [
                    {
                        **_transcript_prompt_item(item),
                        "id": index,
                        "source_segment_id": source_by_id[item.id].id,
                    }
                    for index, item in enumerate(chunk)
                ],
                "ocr": [_ocr_prompt_item(item) for item in nearby],
            }

        pieces: list[TranscriptSegment] = []
        for source in segments:
            pending = [(source.text, 1)]
            while pending:
                value, path = pending.pop()
                candidate = add_piece(source, value, path)
                nearby = self._fit_ocr([candidate], ocr, split_system, schema, payload_for)
                if len(value) <= max_text_chars and self._fits(
                    split_system, payload_for([candidate], nearby), schema
                ):
                    pieces.append(source if path == 1 else candidate)
                    continue
                halves = split_text(value)
                if halves is None:
                    raise ModelContextError(f"字幕段 {source.id} 的最小文本块仍超过模型输入预算")
                pending.extend(((halves[1], path * 2 + 1), (halves[0], path * 2)))

        request_system = split_system

        chunks = (
            [
                (chunk, ocr)
                for chunk in self._budgeted_chunks(
                    pieces,
                    ocr,
                    request_system,
                    schema,
                    payload_for,
                    max_items=(
                        min(80, max(20, self.settings.max_output_tokens // 200))
                        if self.settings.max_output_tokens
                        else 80
                    ),
                    max_text_chars=max_text_chars,
                )
            ]
            if segments
            else [
                ([], group)
                for group in self._budgeted_ocr_chunks(
                    ocr,
                    request_system,
                    schema,
                    payload_for,
                )
            ]
        )
        completed_chunks = 0
        total_chunks = len(chunks)

        def report_progress() -> None:
            if on_progress is not None:
                on_progress("analysis", completed_chunks, total_chunks)

        report_progress()
        limit_splits = 0
        max_limit_splits = max(
            256,
            sum(len(piece.text) for piece in pieces) * 2 + max(1, len(ocr)) * 2,
        )

        async def analyze_chunk(
            chunk: list[TranscriptSegment],
            focus_ocr: list[OCRObservation],
            index: int,
        ) -> list[dict[str, Any]]:
            nonlocal completed_chunks, total_chunks, limit_splits
            try:
                result = await self._analysis_call(
                    chunk,
                    self._fit_ocr(
                        chunk,
                        focus_ocr,
                        request_system,
                        schema,
                        payload_for,
                    ),
                    inspirations,
                    {**metadata, "part": index, "parts": len(chunks)}
                    if len(chunks) > 1
                    else metadata,
                    system=request_system,
                    source_ids={item.id: source_by_id[item.id].id for item in chunk},
                    checkpoints=checkpoints,
                    on_checkpoint=on_checkpoint,
                )
                completed_chunks += 1
                report_progress()
                return [result]
            except ModelLimitError as exc:
                limit_splits += 1
                if limit_splits > max_limit_splits:
                    raise ModelOutputError(
                        "内容分析连续超限，已达到有界缩小上限",
                        details={"last_response": exc.details},
                    ) from exc
                if len(chunk) >= 2:
                    middle = len(chunk) // 2
                    # Parent attempt finished; two child batches remain.
                    completed_chunks += 1
                    total_chunks += 2
                    report_progress()
                    return [
                        *await analyze_chunk(chunk[:middle], focus_ocr, index),
                        *await analyze_chunk(chunk[middle:], focus_ocr, index),
                    ]
                if len(chunk) == 1:
                    piece = chunk[0]
                    halves = split_text(piece.text)
                    if halves is None:
                        source = source_by_id[piece.id]
                        raise type(exc)(
                            f"字幕段 {source.id} 的最小文本块仍超过模型预算，无法完成分析",
                            details={
                                "segment_id": source.id,
                                "piece_chars": len(piece.text),
                                "last_response": exc.details,
                            },
                        ) from exc
                    source = source_by_id[piece.id]
                    path = path_by_id[piece.id]
                    children = [
                        add_piece(source, halves[0], path * 2),
                        add_piece(source, halves[1], path * 2 + 1),
                    ]
                    completed_chunks += 1
                    total_chunks += 2
                    report_progress()
                    return [
                        *await analyze_chunk([children[0]], focus_ocr, index),
                        *await analyze_chunk([children[1]], focus_ocr, index),
                    ]
                if not chunk and len(focus_ocr) >= 2:
                    middle = len(focus_ocr) // 2
                    completed_chunks += 1
                    total_chunks += 2
                    report_progress()
                    return [
                        *await analyze_chunk([], focus_ocr[:middle], index),
                        *await analyze_chunk([], focus_ocr[middle:], index),
                    ]
                raise

        partials: list[dict[str, Any]] = []
        for index, (chunk, focus_ocr) in enumerate(chunks, start=1):
            partials.extend(await analyze_chunk(chunk, focus_ocr, index))
        if len(partials) == 1:
            return AnalysisResult.model_validate(partials[0])
        if on_progress is not None:
            on_progress("merge", 0, 0)
        merge_count = 0

        async def merge(group: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal merge_count
            if len(group) == 1:
                return group[0]
            if len(group) > 3:
                size = (len(group) + 2) // 3
                return await merge(
                    [
                        await merge(group[index : index + size])
                        for index in range(0, len(group), size)
                    ]
                )
            inherited: set[tuple[int | None, str]] = set()
            inherited_quotes: set[str] = set()
            for partial in group:
                for chapter in partial.get("chapters", []):
                    for evidence in chapter.get("evidence", []):
                        quote = _citation_key(evidence.get("quote", ""))
                        inherited.add((evidence.get("timestamp_ms"), quote))
                        inherited_quotes.add(quote)
                for atom in partial.get("knowledge_atoms", []):
                    quote = _citation_key(atom.get("quote", ""))
                    inherited.add((atom.get("timestamp_ms"), quote))
                    inherited_quotes.add(quote)
                for reminder in partial.get("reminders", []):
                    inherited_quotes.add(_citation_key(reminder.get("source_quote", "")))

            def validate_merge(value: dict[str, Any]) -> AnalysisResult:
                analysis = AnalysisResult.model_validate(value)
                for chapter in analysis.chapters:
                    for evidence in chapter.evidence:
                        if (evidence.timestamp_ms, _citation_key(evidence.quote)) not in inherited:
                            raise ValueError("汇总证据必须继承分段结果的原始引文与时间戳")
                for atom in analysis.knowledge_atoms:
                    if (
                        atom.quote
                        and atom.provenance != "ai_inference"
                        and (atom.timestamp_ms, _citation_key(atom.quote)) not in inherited
                    ):
                        raise ValueError("汇总知识原子的引文与时间戳未见于分段结果")
                for reminder in analysis.reminders:
                    if (
                        reminder.source_quote
                        and _citation_key(reminder.source_quote) not in inherited_quotes
                    ):
                        raise ValueError("汇总提醒的依据未见于分段结果")
                return analysis

            payload = {
                "task": "合并分段分析；保留原始引文、时间戳和来源，不创造新证据。",
                "metadata": metadata,
                "user_inspirations_verbatim": [
                    item.model_dump(mode="json") for item in inspirations
                ],
                "partial_analyses": group,
            }
            fits_input = self._fits(_analysis_system_prompt(), payload, schema)
            if len(group) <= 3 and fits_input:
                try:
                    result = await self._checkpointed_call(
                        _analysis_system_prompt(),
                        payload,
                        schema,
                        validator=validate_merge,
                        checkpoints=checkpoints,
                        on_checkpoint=on_checkpoint,
                    )
                    merge_count += 1
                    if on_progress is not None:
                        on_progress("merge", merge_count, 0)
                    return result
                except ModelLimitError:
                    pass
            if len(group) == 2:
                # Final fallback: never fail the whole job on an irreducible merge.
                warning = (
                    "两个分段结果仍超过模型输入预算，已降级拼接分段结果"
                    if not fits_input
                    else "两个分段结果仍超过模型汇总预算，已降级拼接分段结果"
                )
                return _soft_merge_analysis_partials(group, reason=warning)
            middle = len(group) // 2
            return await merge(
                [
                    await merge(group[:middle]),
                    await merge(group[middle:]),
                ]
            )

        result = await merge(partials)
        return _preserve_partial_coverage(AnalysisResult.model_validate(result), partials)

    async def _analysis_call(
        self,
        segments: list[TranscriptSegment],
        ocr: list[OCRObservation],
        inspirations: list[InspirationInput],
        metadata: dict[str, Any],
        *,
        system: str | None = None,
        source_ids: dict[int, int] | None = None,
        checkpoints: dict[str, Any] | None = None,
        on_checkpoint: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return await self._checkpointed_call(
            system or _analysis_system_prompt(),
            {
                "metadata": metadata,
                "user_inspirations_verbatim": [
                    item.model_dump(mode="json") for item in inspirations
                ],
                "transcript": [
                    {
                        **_transcript_prompt_item(segment),
                        "id": index,
                        "source_segment_id": (
                            source_ids[segment.id] if source_ids is not None else segment.id
                        ),
                    }
                    for index, segment in enumerate(segments)
                ],
                "ocr": [_ocr_prompt_item(item) for item in ocr],
            },
            _analysis_response_schema(),
            validator=AnalysisResult.model_validate,
            checkpoints=checkpoints,
            on_checkpoint=on_checkpoint,
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


def _transcript_prompt_item(segment: TranscriptSegment) -> dict[str, Any]:
    return segment.model_dump(mode="json", include={"id", "start_ms", "end_ms", "text"})


def _ocr_prompt_item(item: OCRObservation) -> dict[str, Any]:
    return item.model_dump(
        mode="json", include={"timestamp_ms", "image_index", "text", "confidence"}
    )


def _citation_key(value: str | None) -> str:
    return re.sub(r"[\W_]+", "", value or "", flags=re.UNICODE).lower()


def _preserve_partial_coverage(
    merged: AnalysisResult, partials: list[dict[str, Any]]
) -> AnalysisResult:
    """Use the merge for its overview while retaining the source timeline and claims."""
    if len(partials) <= 1:
        return merged
    sources = [AnalysisResult.model_validate(item) for item in partials]
    group_size = max(1, math.ceil(len(sources) / 12))
    chapters: list[TimelineChapter] = []
    for offset in range(0, len(sources), group_size):
        source_chapters = [
            chapter
            for source in sources[offset : offset + group_size]
            for chapter in source.chapters
        ]
        if not source_chapters:
            continue
        first, last = source_chapters[0], source_chapters[-1]
        evidence = [item for chapter in source_chapters for item in chapter.evidence]
        key_points = list(
            dict.fromkeys(point for chapter in source_chapters for point in chapter.key_points)
        )
        chapters.append(
            TimelineChapter(
                start_ms=min(chapter.start_ms for chapter in source_chapters),
                end_ms=max(
                    (chapter.end_ms or chapter.start_ms + 1) for chapter in source_chapters
                ),
                title=(
                    first.title
                    if first.title == last.title
                    else f"{first.title}；{last.title}"
                ),
                summary=(
                    first.summary
                    if first.summary == last.summary
                    else f"{first.summary} {last.summary}".strip()
                ),
                key_points=_sample_evenly(key_points, 5),
                comparison_table=next(
                    (
                        chapter.comparison_table
                        for chapter in source_chapters
                        if chapter.comparison_table
                    ),
                    None,
                ),
                evidence=_sample_evenly(evidence, 6),
            )
        )
    atoms = []
    seen_statements: set[str] = set()
    for source in sources:
        for atom in source.knowledge_atoms:
            key = _citation_key(atom.statement)
            if not key or key in seen_statements:
                continue
            seen_statements.add(key)
            atoms.append(atom.model_copy(update={"id": f"atom_{len(atoms) + 1:03d}"}))
    return merged.model_copy(
        update={
            "chapters": chapters or merged.chapters,
            "knowledge_atoms": atoms or merged.knowledge_atoms,
        }
    )


def _sample_evenly(items: list[Any], limit: int) -> list[Any]:
    if len(items) <= limit:
        return items
    return [items[round(index * (len(items) - 1) / (limit - 1))] for index in range(limit)]


def _nearby_ocr(
    chunk: list[TranscriptSegment],
    ocr: list[OCRObservation],
) -> list[OCRObservation]:
    nearby: list[OCRObservation] = []
    seen: dict[str, int] = {}
    size = 0
    for item in ocr:
        timestamp = item.timestamp_ms
        if (
            chunk
            and timestamp is not None
            and not (chunk[0].start_ms - 5000 <= timestamp <= chunk[-1].end_ms + 5000)
        ):
            continue
        lines: list[str] = []
        keys: set[str] = set()
        for line in item.text.splitlines():
            key = re.sub(r"\s+", "", line).casefold()
            if not key or key in keys:
                continue
            previous = seen.get(key)
            if (
                timestamp is not None
                and previous is not None
                and abs(timestamp - previous) <= 60_000
            ):
                continue
            lines.append(line)
            keys.add(key)
        if not lines:
            continue
        filtered_text = "\n".join(lines)
        if nearby and (size + len(filtered_text) > 6000 or len(nearby) >= 20):
            break
        nearby.append(item.model_copy(update={"text": filtered_text}))
        if timestamp is not None:
            seen.update({key: timestamp for key in keys})
        size += len(filtered_text)
    return nearby


def _chunk_segments(
    segments: list[TranscriptSegment], *, max_chars: int, max_items: int | None = None
) -> list[list[TranscriptSegment]]:
    if not segments:
        return [[]]
    chunks: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    size = 0
    for segment in segments:
        if current and (
            size + len(segment.text) > max_chars
            or (max_items is not None and len(current) >= max_items)
        ):
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
结构化分析。OCR confidence 是识别质量提示而非事实正确率，缺失不表示内容错误。
低置信数字或专名应结合上下文核对；依据不足时保留原表述并说明不确定性，不臆造，不要求人工校对。
严格区分视频原话、作品正文、OCR 画面信息、AI 推断和用户灵感。用户灵感必须逐字保留，
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
生成 comparison_table，不得根据推断补表。rows 必须是二维字符串数组，
每一行的单元格数量必须等于 headers 的数量；不要把单元格拆成不同的行。
无法保证完整矩形表格时省略 comparison_table，用 key_points 表述，禁止补造数据。
静态图文没有视频时间轴，chapters 必须为空数组。
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
