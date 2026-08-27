from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


class RetentionPolicy(StrEnum):
    TEMPORARY = "temporary"
    KEEP = "keep"
    DISCARD = "discard"


class AnalysisMode(StrEnum):
    GATEWAY = "gateway"
    PROVIDER = "provider"
    LOCAL = "local"


class SourceKind(StrEnum):
    VIDEO = "video"
    IMAGE_NOTE = "image_note"


class CreatorWorkDecision(StrEnum):
    PENDING = "pending"
    SELECTED = "selected"
    SKIPPED = "skipped"
    IMPORTED = "imported"


class CreatorWorkAvailability(StrEnum):
    AVAILABLE = "available"
    POSSIBLY_UNAVAILABLE = "possibly_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    INVENTORYING = "inventorying"
    NEEDS_SELECTION = "needs_selection"
    DISPATCHING = "dispatching"
    MONITORING = "monitoring"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    AWAITING_AGENT_ANALYSIS = "awaiting_agent_analysis"
    NEEDS_AUTH = "needs_auth"
    NEEDS_REVIEW = "needs_review"
    WAITING_CONFIRMATION = "waiting_confirmation"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


AuthState = Literal[
    "ready",
    "available",
    "unverified",
    "missing",
    "expired",
    "needs_login",
    "unavailable",
    "error",
]


class AuthCheckResult(BaseModel):
    """Non-secret authentication health returned to CLI and MCP callers."""

    scope: Literal["video", "image_note", "creator"]
    state: AuthState
    ok: bool
    server_verified: bool = False
    cookie_source: str
    message: str
    action: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InspirationInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    quote: str | None = Field(default=None, max_length=4000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> InspirationInput:
        if self.end_ms is not None and self.start_ms is None:
            raise ValueError("end_ms requires start_ms")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class CaptureOptions(BaseModel):
    retention: RetentionPolicy = RetentionPolicy.TEMPORARY
    allow_long: bool = False
    approve_cloud_analysis: bool = False


class GatewayContext(BaseModel):
    gateway: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=512)
    message_id: str | None = Field(default=None, max_length=512)
    reply_target: str | None = Field(default=None, max_length=512)


class CaptureRequest(BaseModel):
    share_text: str = Field(min_length=1, max_length=20_000)
    inspirations: list[InspirationInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("inspirations", "purposes"),
    )
    options: CaptureOptions = Field(default_factory=CaptureOptions)
    gateway_context: GatewayContext | None = None


class CreatorProfile(BaseModel):
    sec_uid: str = Field(min_length=8, max_length=256)
    canonical_url: str
    original_url: str
    nickname: str = "抖音博主"
    uid: str | None = None
    unique_id: str | None = None
    signature: str = ""
    avatar_url: str | None = None
    avatar_path: str | None = None
    reported_work_count: int | None = Field(default=None, ge=0)


class CreatorInventoryWork(BaseModel):
    work_id: str
    source_kind: SourceKind = SourceKind.VIDEO
    canonical_url: str
    original_url: str
    title: str = "抖音作品"
    published_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    thumbnail_url: str | None = None
    thumbnail_path: str | None = None
    is_pinned: bool = False


class CreatorInventoryResult(BaseModel):
    profile: CreatorProfile
    works: list[CreatorInventoryWork] = Field(default_factory=list)
    complete: bool = True
    reported_count: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)


class CreatorRecord(BaseModel):
    id: str
    sec_uid: str
    canonical_url: str
    original_url: str
    nickname: str
    folder_path: str
    uid: str | None = None
    unique_id: str | None = None
    signature: str = ""
    avatar_path: str | None = None
    inspirations: list[InspirationInput] = Field(default_factory=list)
    reported_work_count: int | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CreatorWorkRecord(BaseModel):
    creator_id: str
    work_id: str
    source_kind: SourceKind
    canonical_url: str
    original_url: str
    title: str
    published_at: datetime | None = None
    duration_seconds: float | None = None
    thumbnail_path: str | None = None
    is_pinned: bool = False
    decision: CreatorWorkDecision = CreatorWorkDecision.PENDING
    availability: CreatorWorkAvailability = CreatorWorkAvailability.AVAILABLE
    missing_sync_count: int = 0
    entry_id: str | None = None
    last_job_id: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime


class CreatorInventoryItem(BaseModel):
    job_id: str
    ordinal: int = Field(ge=1)
    is_new: bool = True
    work: CreatorWorkRecord


class TranscriptSegment(BaseModel):
    id: int
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptCorrection(BaseModel):
    id: int
    text: str = Field(min_length=1, max_length=20_000)


class ReviewIssue(BaseModel):
    id: str
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    image_index: int | None = Field(default=None, ge=1)
    raw_text: str
    reason: str
    suggestions: list[str] = Field(default_factory=list)
    resolution: str | None = None


class OCRObservation(BaseModel):
    timestamp_ms: int | None = Field(default=None, ge=0)
    image_index: int | None = Field(default=None, ge=1)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    image_path: str | None = None


class Claim(BaseModel):
    id: str
    text: str
    source_quote: str | None = None
    start_ms: int | None = None
    valid_until: datetime | None = None
    review_after: datetime | None = None
    stale: bool = False


class ReminderCandidate(BaseModel):
    id: str
    title: str
    due_at: datetime | None = None
    timezone: str = "Asia/Shanghai"
    reason: str
    source_quote: str
    confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False

    @model_validator(mode="after")
    def ensure_timezone(self) -> ReminderCandidate:
        if self.due_at is not None and self.due_at.tzinfo is None:
            self.due_at = self.due_at.replace(tzinfo=ZoneInfo(self.timezone))
        return self


class Entity(BaseModel):
    name: str
    kind: str = "entity"
    description: str = ""


class ContradictionRecord(BaseModel):
    id: str
    claim_id: str
    conflicts_with_entry_id: str
    conflicts_with_claim_id: str | None = None
    reason: str
    confidence: float = Field(ge=0, le=1)
    status: Literal["open", "resolved"] = "open"
    target_title: str | None = None
    target_source_path: str | None = None
    target_original_url: str | None = None


ContentType = Literal[
    "tutorial",
    "explanation",
    "opinion",
    "recommendation",
    "news_event",
    "story_case",
    "collection",
    "other",
]
ContentFacet = Literal["comparison", "personal_experience", "time_sensitive", "promotion"]


class TutorialCard(BaseModel):
    kind: Literal["tutorial"] = "tutorial"
    goal: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)


class ExplanationCard(BaseModel):
    kind: Literal["explanation"] = "explanation"
    question: str = ""
    concepts: list[str] = Field(default_factory=list)
    mechanism: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class OpinionCard(BaseModel):
    kind: Literal["opinion"] = "opinion"
    thesis: str = ""
    reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    counterpoints: list[str] = Field(default_factory=list)


class RecommendationCard(BaseModel):
    kind: Literal["recommendation"] = "recommendation"
    subjects: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)


class NewsEventCard(BaseModel):
    kind: Literal["news_event"] = "news_event"
    event: str = ""
    absolute_time: datetime | None = None
    impact: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None


class StoryCaseCard(BaseModel):
    kind: Literal["story_case"] = "story_case"
    context: str = ""
    turning_points: list[str] = Field(default_factory=list)
    outcome: str = ""
    lessons: list[str] = Field(default_factory=list)


class CollectionItem(BaseModel):
    name: str
    traits: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)


class CollectionCard(BaseModel):
    kind: Literal["collection"] = "collection"
    items: list[CollectionItem] = Field(default_factory=list)


class OtherCard(BaseModel):
    kind: Literal["other"] = "other"
    notes: list[str] = Field(default_factory=list)


ContentCard = Annotated[
    TutorialCard
    | ExplanationCard
    | OpinionCard
    | RecommendationCard
    | NewsEventCard
    | StoryCaseCard
    | CollectionCard
    | OtherCard,
    Field(discriminator="kind"),
]


class ChapterEvidence(BaseModel):
    timestamp_ms: int = Field(ge=0)
    quote: str = Field(min_length=1)
    evidence_type: Literal["audio", "ocr", "audio+ocr"] = "audio"

    @field_validator("quote")
    @classmethod
    def validate_quote(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("时间轴图解证据的引文不能为空")
        return normalized


class ChapterTable(BaseModel):
    headers: list[str] = Field(min_length=2, max_length=6)
    rows: list[list[str]] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_row_widths(self) -> ChapterTable:
        expected = len(self.headers)
        if any(len(row) != expected for row in self.rows):
            raise ValueError("时间轴图解表格的每行列数必须与表头一致")
        return self


class TimelineChapter(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    title: str
    summary: str
    key_points: list[str] = Field(default_factory=list, max_length=5)
    comparison_table: ChapterTable | None = None
    evidence: list[ChapterEvidence] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_time_range(self) -> TimelineChapter:
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("时间轴图解章节的结束时间必须晚于开始时间")
        return self


class KnowledgeAtom(BaseModel):
    id: str
    statement: str
    atom_type: Literal[
        "fact", "method", "parameter", "opinion", "event", "recommendation", "inference"
    ] = "fact"
    provenance: Literal[
        "audio",
        "ocr",
        "audio+ocr",
        "post_text",
        "image_ocr",
        "post_text+image_ocr",
        "ai_inference",
    ] = "audio"
    timestamp_ms: int | None = Field(default=None, ge=0)
    image_index: int | None = Field(default=None, ge=1)
    quote: str | None = None
    context: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)
    valid_until: datetime | None = None
    review_after: datetime | None = None
    stale: bool = False


class AnalysisResultV2(BaseModel):
    analysis_version: Literal[2] = 2
    title: str
    one_liner: str = Field(default="", max_length=120)
    relevance_to_inspiration: str = ""
    takeaways: list[str] = Field(default_factory=list, max_length=5)
    content_type: ContentType = "other"
    facets: list[ContentFacet] = Field(default_factory=list)
    content_card: ContentCard = Field(default_factory=OtherCard)
    chapters: list[TimelineChapter] = Field(default_factory=list, max_length=12)
    knowledge_atoms: list[KnowledgeAtom] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    reminders: list[ReminderCandidate] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    contradictions: list[ContradictionRecord] = Field(default_factory=list)

    # Kept in the wire/storage model so v1 Agents and old Vault entries remain readable.
    summary: str = ""
    core_points: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    applicable_scenarios: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    ai_judgment: str = ""

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data.setdefault("one_liner", data.get("summary", ""))
        data.setdefault("relevance_to_inspiration", data.get("ai_judgment", ""))
        data.setdefault("takeaways", list(data.get("core_points", []))[:5])
        if len(data.get("takeaways", [])) < 3:
            extras = [
                *data.get("steps", []),
                *data.get("evidence", []),
                data.get("summary", ""),
            ]
            for item in extras:
                if item and item not in data["takeaways"]:
                    data["takeaways"].append(item)
                if len(data["takeaways"]) >= 3:
                    break
        data["takeaways"] = data.get("takeaways", [])[:5]
        data.setdefault("content_type", "other")
        data.setdefault("facets", [])
        data.setdefault("content_card", {"kind": data["content_type"]})
        if data.get("content_card", {}).get("kind") != data["content_type"]:
            data["content_card"] = {"kind": data["content_type"]}
        if "knowledge_atoms" not in data:
            legacy_claims = [
                item.model_dump(mode="python") if isinstance(item, BaseModel) else item
                for item in data.get("claims", [])
            ]
            data["knowledge_atoms"] = [
                {
                    "id": claim.get("id", f"legacy-{index}"),
                    "statement": claim.get("text", ""),
                    "atom_type": "fact",
                    "provenance": "audio",
                    "timestamp_ms": claim.get("start_ms"),
                    "quote": claim.get("source_quote"),
                    "confidence": 0.7,
                    "valid_until": claim.get("valid_until"),
                    "review_after": claim.get("review_after"),
                    "stale": claim.get("stale", False),
                }
                for index, claim in enumerate(legacy_claims)
                if claim.get("text")
            ]
        legacy_moments = [
            item.model_dump(mode="python") if isinstance(item, BaseModel) else item
            for item in data.pop("key_moments", [])
        ]
        if "chapters" not in data and legacy_moments:
            ordered = sorted(
                (
                    item
                    for item in legacy_moments
                    if isinstance(item, dict) and item.get("timestamp_ms") is not None
                ),
                key=lambda item: int(item["timestamp_ms"]),
            )
            chapters: list[dict[str, Any]] = []
            for index, moment in enumerate(ordered):
                timestamp_ms = int(moment["timestamp_ms"])
                next_timestamp_candidate = (
                    int(ordered[index + 1]["timestamp_ms"])
                    if index + 1 < len(ordered)
                    else None
                )
                next_timestamp = (
                    next_timestamp_candidate
                    if next_timestamp_candidate is not None
                    and next_timestamp_candidate > timestamp_ms
                    else None
                )
                evidence_type = moment.get("evidence_type", "audio")
                quote = str(moment.get("quote") or "").strip()
                evidence = []
                if quote and evidence_type in {"audio", "ocr", "audio+ocr"}:
                    evidence.append(
                        {
                            "timestamp_ms": timestamp_ms,
                            "quote": quote,
                            "evidence_type": evidence_type,
                        }
                    )
                chapters.append(
                    {
                        "start_ms": timestamp_ms,
                        "end_ms": next_timestamp,
                        "title": moment.get("title", "章节"),
                        "summary": moment.get("summary", ""),
                        "key_points": [],
                        "evidence": evidence,
                    }
                )
            data["chapters"] = chapters[:12]
        raw_chapters = data.get("chapters")
        if isinstance(raw_chapters, list):
            data["chapters"] = sorted(
                raw_chapters,
                key=lambda item: int(
                    (item.model_dump(mode="python") if isinstance(item, BaseModel) else item).get(
                        "start_ms", 0
                    )
                ),
            )
        return data

    @model_validator(mode="after")
    def synchronize_legacy_fields(self) -> AnalysisResultV2:
        if not self.summary:
            self.summary = self.one_liner
        if not self.core_points:
            self.core_points = list(self.takeaways)
        if not self.ai_judgment:
            self.ai_judgment = self.relevance_to_inspiration
        if not self.claims:
            self.claims = [
                Claim(
                    id=atom.id,
                    text=atom.statement,
                    source_quote=atom.quote,
                    start_ms=atom.timestamp_ms,
                    valid_until=atom.valid_until,
                    review_after=atom.review_after,
                    stale=atom.stale,
                )
                for atom in self.knowledge_atoms
                if atom.atom_type != "inference"
            ]
        return self


# Compatibility name used by integrations released before v2.
AnalysisResult = AnalysisResultV2


class VideoMetadata(BaseModel):
    video_id: str
    original_url: str
    canonical_url: str
    title: str = "抖音视频"
    author: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    description: str | None = None
    media_path: str | None = None
    thumbnail_path: str | None = None
    thumbnail_kind: str | None = None
    source_kind: SourceKind = SourceKind.VIDEO
    image_paths: list[str] = Field(default_factory=list)
    post_text: str | None = None
    music_metadata: dict[str, Any] | None = None
    live_photo: bool = False
    creator_sec_uid: str | None = None
    creator_uid: str | None = None
    creator_unique_id: str | None = None
    creator_url: str | None = None


class Evidence(BaseModel):
    entry_id: str
    snippet: str
    timestamp_ms: int | None = None
    image_index: int | None = Field(default=None, ge=1)
    original_url: str
    inspirations: list[InspirationInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("inspirations", "purposes"),
    )
    relation_reason: str | None = None
    status: str
    confidence: float = Field(ge=0, le=1)
    score: float = 0
    title: str = ""


class LibraryItem(BaseModel):
    entry_id: str
    work_id: str
    title: str
    author: str = "未知作者"
    cover_path: str | None = None
    cover_url: str | None = None
    summary: str = ""
    content_type: str = "other"
    tags: list[str] = Field(default_factory=list)
    inspirations: list[InspirationInput] = Field(default_factory=list)
    published_at: datetime | None = None
    captured_at: datetime | None = None
    status: str = "已入库"
    source_kind: SourceKind = SourceKind.VIDEO
    source_path: str
    original_url: str = ""
    creator_id: str | None = None
    body_markdown: str = ""


class Citation(BaseModel):
    entry_id: str
    article_title: str
    snippet: str
    timestamp_ms: int | None = None
    image_index: int | None = Field(default=None, ge=1)
    original_url: str = ""


class SourceRevision(BaseModel):
    entry_id: str
    updated_at: datetime
    enabled: bool = True


class TopicSource(BaseModel):
    entry_id: str
    position: int = Field(ge=1)
    enabled: bool = True
    source_revision: datetime
    title: str = ""


TopicArtifactKind = Literal[
    "overview",
    "comparison",
    "evidence_map",
    "consensus",
    "decision_brief",
    "faq",
    "note",
]


class TopicArtifact(BaseModel):
    id: str
    topic_id: str
    kind: TopicArtifactKind
    title: str
    content_markdown: str
    source_revision: str
    source_revisions: list[SourceRevision] = Field(default_factory=list)
    status: Literal["current", "needs_update"] = "current"
    model: str | None = None
    prompt_version: str = "topic-research-v1"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    user_authored: bool = False
    created_at: datetime
    updated_at: datetime


class ResearchTopic(BaseModel):
    id: str
    title: str
    goal: str = ""
    instructions: str = ""
    sources: list[TopicSource] = Field(default_factory=list)
    source_revision: str
    created_at: datetime
    updated_at: datetime


class ChatSession(BaseModel):
    id: str
    title: str
    scope: Literal["library", "entry", "topic"] = "library"
    context_entry_id: str | None = None
    context_topic_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessage(BaseModel):
    id: int | None = None
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    citations: list[Citation] = Field(default_factory=list)
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    created_at: datetime


class InspirationDraft(BaseModel):
    entry_id: str
    text: str = Field(min_length=1, max_length=4000)
    quote: str | None = Field(default=None, max_length=4000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> InspirationDraft:
        InspirationInput(
            text=self.text,
            quote=self.quote,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
        )
        return self


class JobRecord(BaseModel):
    id: str
    kind: str = "capture"
    status: JobStatus
    progress: float = Field(ge=0, le=1)
    request: CaptureRequest
    artifacts: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class JobEvent(BaseModel):
    id: int
    job_id: str
    status: JobStatus
    gateway_context: GatewayContext | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    acknowledged_at: datetime | None = None
    superseded_at: datetime | None = None


class EntryRecord(BaseModel):
    id: str
    video_id: str
    title: str
    original_url: str
    canonical_url: str
    raw_path: str
    source_path: str
    status: str
    media_status: str
    retention: RetentionPolicy
    media_expires_at: datetime | None = None
    summary: str = ""
    inspirations: list[InspirationInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("inspirations", "purposes"),
    )
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# Backward-compatible import alias for integrations built before the terminology change.
PurposeInput = InspirationInput
