from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from typing import Any

from .adapters.embeddings import EmbeddingService, cosine_similarity
from .database import Database
from .errors import EntryNotFoundError
from .models import AnalysisResult, EntryRecord, Evidence, OCRObservation, TranscriptSegment


class KnowledgeIndexer:
    def __init__(self, database: Database, embeddings: EmbeddingService) -> None:
        self.database = database
        self.embeddings = embeddings

    def ensure_embedding_compatibility(self) -> int:
        """Rebuild cached vectors if the effective local embedding model changed."""
        signature = self.embeddings.signature()
        previous = self.database.get_index_metadata("embedding_signature")
        if previous == signature:
            return 0
        entries = self.database.list_entries()
        for entry in entries:
            self.index_entry(entry, self.database.get_entry_data(entry.id))
        self.database.set_index_metadata("embedding_signature", signature)
        return len(entries)

    def record_embedding_signature(self) -> None:
        self.database.set_index_metadata("embedding_signature", self.embeddings.signature())

    def index_entry(
        self, entry: EntryRecord, data: dict[str, Any], *, persist: bool = True
    ) -> list[dict[str, Any]]:
        analysis = AnalysisResult.model_validate(data["analysis"])
        inspirations_text = "\n".join(inspiration.text for inspiration in entry.inspirations)
        tags_text = " ".join(entry.tags)
        chunks: list[dict[str, Any]] = []

        def add(
            kind: str,
            text: str,
            timestamp_ms: int | None = None,
            image_index: int | None = None,
            stale: bool = False,
            chunk_id: str | None = None,
        ) -> None:
            clean = text.strip()
            if clean:
                chunks.append(
                    {
                        "id": chunk_id or uuid.uuid4().hex,
                        "kind": kind,
                        "text": clean,
                        "timestamp_ms": timestamp_ms,
                        "image_index": image_index,
                        # The physical SQLite column keeps its legacy name for a
                        # non-destructive schema migration.
                        "purposes_text": inspirations_text,
                        "tags_text": tags_text,
                        "stale": stale,
                    }
                )

        summary_context = "\n".join(
            filter(
                None,
                [
                    entry.title,
                    f"内容类型：{analysis.content_type}",
                    analysis.one_liner,
                    analysis.relevance_to_inspiration,
                ],
            )
        )
        add("summary", summary_context)
        for inspiration in entry.inspirations:
            inspiration_text = inspiration.text
            if inspiration.quote:
                inspiration_text += f"\n指定原句：{inspiration.quote}"
            add("inspiration", inspiration_text, inspiration.start_ms)
        for value in analysis.takeaways:
            add("takeaway", value)
        for value in analysis.actions:
            add("action", value)
        for atom in analysis.knowledge_atoms:
            contextual_text = "\n".join(
                filter(
                    None,
                    [
                        entry.title,
                        f"内容类型：{analysis.content_type}",
                        f"灵感：{inspirations_text}" if inspirations_text else "",
                        atom.context,
                        atom.statement,
                        f"原文：{atom.quote}" if atom.quote else "",
                    ],
                )
            )
            add(
                "knowledge_atom",
                contextual_text,
                atom.timestamp_ms,
                atom.image_index,
                stale=atom.stale,
                chunk_id=f"{entry.id}:atom:{atom.id}",
            )
        transcript = [
            TranscriptSegment.model_validate(item) for item in data.get("transcript_corrected", [])
        ]
        current: list[TranscriptSegment] = []
        size = 0
        for segment in transcript:
            if current and size + len(segment.text) > 800:
                add("transcript", "".join(item.text for item in current), current[0].start_ms)
                current = []
                size = 0
            current.append(segment)
            size += len(segment.text)
        if current:
            add("transcript", "".join(item.text for item in current), current[0].start_ms)

        post_text = str(data.get("metadata", {}).get("post_text") or "").strip()
        if post_text:
            add("post_text", post_text)
        for observation_data in data.get("ocr", []):
            observation = OCRObservation.model_validate(observation_data)
            add(
                "image_ocr" if observation.image_index is not None else "ocr",
                observation.text,
                observation.timestamp_ms,
                observation.image_index,
            )

        vectors = self.embeddings.embed(
            [chunk["text"] + "\n" + inspirations_text for chunk in chunks]
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk["embedding"] = vector
        if persist:
            self.database.replace_chunks(entry.id, chunks)
        return chunks

    def build_relations(
        self, entry: EntryRecord, data: dict[str, Any], *, persist: bool = True
    ) -> list[dict[str, Any]]:
        analysis = AnalysisResult.model_validate(data["analysis"])
        source_text = "\n".join(
            [
                analysis.one_liner,
                *analysis.takeaways,
                *[atom.statement for atom in analysis.knowledge_atoms],
                *[inspiration.text for inspiration in entry.inspirations],
            ]
        )
        source_vector = self.embeddings.embed([source_text])[0]
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in self.database.fetch_chunks(include_stale=False):
            if row["entry_id"] == entry.id or row["kind"] != "summary" or not row["embedding_json"]:
                continue
            vector = json.loads(row["embedding_json"])
            score = cosine_similarity(source_vector, vector)
            if score >= 0.65:
                target = self.database.get_entry(row["entry_id"])
                relation_type = "semantic" if score >= 0.82 else "maintenance_suggestion"
                candidates.append(
                    (
                        score,
                        {
                            "target_entry_id": target.id,
                            "target_title": target.title,
                            "target_source_path": target.source_path,
                            "relation_type": relation_type,
                            "reason": (
                                "灵感、摘要或核心观点高度相关"
                                if relation_type == "semantic"
                                else "语义可能相关，建议在维护时人工确认"
                            ),
                            "confidence": score,
                        },
                    )
                )
        relations: list[dict[str, Any]] = []
        contradiction_targets: set[str] = set()
        for contradiction in analysis.contradictions:
            try:
                target = self.database.get_entry(contradiction.conflicts_with_entry_id)
            except EntryNotFoundError:  # invalid model output must not break ingestion
                continue
            contradiction_targets.add(target.id)
            relations.append(
                {
                    "target_entry_id": target.id,
                    "target_title": target.title,
                    "target_source_path": target.source_path,
                    "relation_type": "contradiction",
                    "reason": contradiction.reason,
                    "confidence": contradiction.confidence,
                }
            )
        semantic = [
            item
            for _, item in sorted(candidates, reverse=True)
            if item["target_entry_id"] not in contradiction_targets
        ]
        relations.extend(semantic[:5])
        if persist:
            self.database.replace_relations(entry.id, relations)
        return relations

    def find_related_claims(self, text: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """Provide local prior claims as bounded context for contradiction detection."""
        if not text.strip():
            return []
        query_vector = self.embeddings.embed([text[:12_000]])[0]
        candidates: list[tuple[float, dict[str, Any]]] = []
        for row in self.database.fetch_chunks(include_stale=False):
            if row["kind"] not in {"claim", "knowledge_atom"} or not row["embedding_json"]:
                continue
            score = cosine_similarity(query_vector, json.loads(row["embedding_json"]))
            if score < 0.45:
                continue
            entry = self.database.get_entry(row["entry_id"])
            entry_data = self.database.get_entry_data(entry.id)
            analysis_data = entry_data.get("analysis", {})
            claim = next(
                (
                    item
                    for item in analysis_data.get("knowledge_atoms", [])
                    if f":atom:{item.get('id')}" in row["id"]
                ),
                None,
            ) or next(
                (
                    item
                    for item in analysis_data.get("claims", [])
                    if item.get("text") == row["text"]
                ),
                {},
            )
            candidates.append(
                (
                    score,
                    {
                        "entry_id": entry.id,
                        "entry_title": entry.title,
                        "source_path": entry.source_path,
                        "original_url": entry.original_url,
                        "claim_id": claim.get("id"),
                        "claim_text": claim.get("statement") or claim.get("text") or row["text"],
                        "source_quote": claim.get("quote") or claim.get("source_quote"),
                        "similarity": round(score, 4),
                    },
                )
            )
        return [item for _, item in sorted(candidates, reverse=True)[:limit]]


class KnowledgeSearch:
    def __init__(self, database: Database, embeddings: EmbeddingService) -> None:
        self.database = database
        self.embeddings = embeddings

    def search(self, query: str, *, include_stale: bool = False, limit: int = 10) -> list[Evidence]:
        cleaned = query.strip()
        if not cleaned:
            return []
        fts_query = _fts_query(cleaned)
        lexical_rows = self.database.fts_search(
            fts_query, include_stale=include_stale, limit=max(limit * 5, 50)
        )
        lexical_scores: dict[str, float] = {}
        lexical_ranks: dict[str, int] = {}
        for index, row in enumerate(lexical_rows):
            rank = float(row.get("rank", 0))
            lexical_scores[row["id"]] = 1.0 / (1.0 + abs(rank))
            lexical_ranks[row["id"]] = index

        query_vector = self.embeddings.embed([cleaned])[0]
        rows = self.database.fetch_chunks(include_stale=include_stale)
        results: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            vector_score = 0.0
            if row["embedding_json"]:
                vector_score = max(
                    0.0, cosine_similarity(query_vector, json.loads(row["embedding_json"]))
                )
            lexical_score = lexical_scores.get(row["id"], 0.0)
            rank_bonus = 1.0 / (60 + lexical_ranks[row["id"]]) if row["id"] in lexical_ranks else 0
            inspiration_bonus = 0.12 if _contains_overlap(cleaned, row["purposes_text"]) else 0.0
            kind_bonus = {
                "knowledge_atom": 0.12,
                "inspiration": 0.08,
                "summary": 0.04,
                "takeaway": 0.03,
                "transcript": 0.0,
                "ocr": 0.0,
                "image_ocr": 0.0,
            }.get(row["kind"], 0.01)
            score = (
                0.49 * lexical_score
                + 0.39 * vector_score
                + inspiration_bonus
                + rank_bonus
                + kind_bonus
            )
            if score > 0.05:
                results.append((score, row))

        per_entry: dict[str, int] = defaultdict(int)
        evidence: list[Evidence] = []
        for score, row in sorted(results, key=lambda item: item[0], reverse=True):
            if per_entry[row["entry_id"]] >= 3:
                continue
            entry = self.database.get_entry(row["entry_id"])
            relation_reason = {
                "knowledge_atom": "知识原子与问题匹配",
                "inspiration": "用户灵感与问题匹配",
                "summary": "作品摘要与问题匹配",
                "takeaway": "核心收获与问题匹配",
                "transcript": "逐字稿片段与问题匹配",
                "ocr": "视频画面文字与问题匹配",
                "image_ocr": "图片文字与问题匹配",
                "post_text": "作品正文与问题匹配",
            }.get(row["kind"])
            evidence.append(
                Evidence(
                    entry_id=entry.id,
                    title=entry.title,
                    snippet=row["text"],
                    timestamp_ms=row["timestamp_ms"],
                    image_index=row.get("image_index"),
                    original_url=entry.original_url,
                    inspirations=entry.inspirations,
                    relation_reason=relation_reason,
                    status=entry.status,
                    confidence=max(0.0, min(1.0, score)),
                    score=score,
                )
            )
            per_entry[entry.id] += 1
            if len(evidence) >= limit:
                break
        return evidence


def _fts_query(value: str) -> str:
    tokens = [token for token in re.split(r"\s+", value) if token]
    if len(tokens) == 1:
        return f'"{tokens[0].replace(chr(34), "")}"'
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)


def _contains_overlap(query: str, text: str) -> bool:
    if not text:
        return False
    normalized_query = re.sub(r"\s+", "", query.lower())
    normalized_text = re.sub(r"\s+", "", text.lower())
    if normalized_query in normalized_text or normalized_text in normalized_query:
        return True
    bigrams = {normalized_query[index : index + 2] for index in range(len(normalized_query) - 1)}
    return bool(
        bigrams and sum(token in normalized_text for token in bigrams) / len(bigrams) >= 0.4
    )
