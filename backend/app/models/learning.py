"""Pydantic contracts for the Phase B learning/content API.

The learning API is intentionally independent from the legacy ``schemas.py``
contracts.  The models accept both the canonical names used by the backend and
the small set of aliases used by the existing web client (``name``/``title``,
``prompt``/``question``, and ``source_citations``/``source_refs``).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _LearningModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ChapterStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class RecallCardLifecycle(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    PAUSED = "paused"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class RecallCardOrigin(str, Enum):
    MANUAL = "manual"
    GENERATED = "generated"
    IMPORTED = "imported"


class RecallCardStudyStatus(str, Enum):
    NEW = "new"
    LEARNING = "learning"
    REVIEW = "review"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class RevisionStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"


class RevisionDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class GenerationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CardCitation(_LearningModel):
    """Immutable source snapshot attached to a card revision.

    ``doc_name_snapshot`` and ``chunk_content_hash`` are snapshots, not live
    joins.  The service still verifies them against the referenced chunk at
    write time so a citation cannot silently point at a different document or
    version of the text.
    """

    doc_id: str
    chunk_id: str
    doc_name_snapshot: str
    page_number: Optional[int] = None
    paragraph_ref: Optional[str] = None
    snippet: str
    chunk_content_hash: str

    @model_validator(mode="before")
    @classmethod
    def _accept_client_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        aliases = {
            "doc_name": "doc_name_snapshot",
            "document_name": "doc_name_snapshot",
            "filename": "doc_name_snapshot",
            "page": "page_number",
            "reference": "paragraph_ref",
            "quote": "snippet",
            "text": "snippet",
            "content_hash": "chunk_content_hash",
            "hash": "chunk_content_hash",
        }
        for old, new in aliases.items():
            if new not in value and old in value:
                value[new] = value[old]
        return value

    @model_validator(mode="after")
    def _validate_values(self) -> "CardCitation":
        for field_name in ("doc_id", "chunk_id", "doc_name_snapshot", "snippet", "chunk_content_hash"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} 不能为空")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number 必须为正整数")
        return self


# Friendly aliases for callers that use the terminology from the product plan.
SourceCitation = CardCitation
SourceCitationSnapshot = CardCitation


class LearningChapterCreate(_LearningModel):
    name: Optional[str] = None
    title: Optional[str] = None
    source_doc_id: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: int = Field(default=0, ge=0)
    enabled: bool = True
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _name_required(self) -> "LearningChapterCreate":
        title = (self.title or self.name or "").strip()
        if not title:
            raise ValueError("章节标题不能为空")
        self.title = title
        self.name = title
        return self


class LearningChapterUpdate(_LearningModel):
    name: Optional[str] = None
    title: Optional[str] = None
    source_doc_id: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None
    status: Optional[ChapterStatus] = None
    expected_version: Optional[int] = Field(default=None, ge=1)
    version: Optional[int] = Field(default=None, ge=1)
    metadata: Optional[Dict[str, Any]] = None

    @property
    def effective_version(self) -> Optional[int]:
        return self.expected_version if self.expected_version is not None else self.version

    @model_validator(mode="after")
    def _title_nonempty(self) -> "LearningChapterUpdate":
        if self.title is not None and not self.title.strip():
            raise ValueError("章节标题不能为空")
        if self.name is not None and not self.name.strip():
            raise ValueError("章节标题不能为空")
        if self.title is not None:
            self.title = self.title.strip()
        if self.name is not None:
            self.name = self.name.strip()
        return self


class LearningChapterResponse(_LearningModel):
    id: str
    name: str
    title: str
    source_doc_id: Optional[str] = None
    normalized_title: str
    parent_id: Optional[str] = None
    sort_order: int = 0
    enabled: bool = True
    status: ChapterStatus = ChapterStatus.ACTIVE
    version: int = 1
    card_count: int = 0
    due_count: int = 0
    metadata: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str
    archived_at: Optional[str] = None


# Short aliases commonly imported by API tests / integrations.
ChapterCreate = LearningChapterCreate
ChapterUpdate = LearningChapterUpdate
ChapterResponse = LearningChapterResponse


class LearningSettingsUpdate(_LearningModel):
    daily_new_limit: Optional[int] = Field(default=None, ge=1, le=100)
    daily_review_limit: Optional[int] = Field(default=None, ge=1, le=100)
    new_cards_per_day: Optional[int] = Field(default=None, ge=1, le=100)
    review_cards_per_day: Optional[int] = Field(default=None, ge=1, le=100)
    reveal_after_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    session_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    auto_advance: Optional[bool] = None
    show_hints: Optional[bool] = None
    timezone: Optional[str] = None
    expected_version: Optional[int] = Field(default=None, ge=1)
    version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_new_limit(self) -> Optional[int]:
        return self.daily_new_limit if self.daily_new_limit is not None else self.new_cards_per_day

    @property
    def effective_review_limit(self) -> Optional[int]:
        return self.daily_review_limit if self.daily_review_limit is not None else self.review_cards_per_day

    @property
    def effective_version(self) -> Optional[int]:
        return self.expected_version if self.expected_version is not None else self.version

    @model_validator(mode="after")
    def _timezone_valid(self) -> "LearningSettingsUpdate":
        if self.timezone is not None and not self.timezone.strip():
            raise ValueError("timezone 不能为空")
        return self


class LearningSettingsResponse(_LearningModel):
    id: str
    daily_new_limit: Optional[int] = Field(default=None, ge=1, le=100)
    daily_review_limit: Optional[int] = Field(default=None, ge=1, le=100)
    new_cards_per_day: Optional[int] = Field(default=None, ge=1, le=100)
    review_cards_per_day: Optional[int] = Field(default=None, ge=1, le=100)
    reveal_after_seconds: int = 0
    session_minutes: int = 20
    auto_advance: bool = False
    show_hints: bool = True
    timezone: str = "Asia/Shanghai"
    version: int = 1
    updated_at: str


LearningSettings = LearningSettingsResponse


class RecallCardCreate(_LearningModel):
    chapter_id: str
    prompt: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    back: Optional[str] = None
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_citations: List[CardCitation] = Field(default_factory=list)
    source_refs: Optional[List[CardCitation]] = None
    citations: Optional[List[CardCitation]] = None
    card_type: str = "basic"
    origin: RecallCardOrigin = RecallCardOrigin.MANUAL
    status: RecallCardStudyStatus = RecallCardStudyStatus.NEW
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _content_required(self) -> "RecallCardCreate":
        prompt = (self.prompt or self.question or "").strip()
        answer = (self.answer or self.back or "").strip()
        if not prompt:
            raise ValueError("问题 / 提示面不能为空")
        if not answer:
            raise ValueError("参考答案不能为空")
        self.prompt = prompt
        self.answer = answer
        if self.source_refs is not None:
            self.source_citations = self.source_refs
        if self.citations is not None:
            self.source_citations = self.citations
        self.tags = [str(tag).strip() for tag in self.tags if str(tag).strip()][:50]
        return self


class RecallCardUpdate(_LearningModel):
    chapter_id: Optional[str] = None
    prompt: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    back: Optional[str] = None
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: Optional[List[str]] = None
    source_citations: Optional[List[CardCitation]] = None
    source_refs: Optional[List[CardCitation]] = None
    citations: Optional[List[CardCitation]] = None
    card_type: Optional[str] = None
    status: Optional[RecallCardStudyStatus] = None
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expected_version: Optional[int] = Field(default=None, ge=1)
    version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_version(self) -> Optional[int]:
        return self.expected_version if self.expected_version is not None else self.version

    @model_validator(mode="after")
    def _content_nonempty(self) -> "RecallCardUpdate":
        prompt = self.prompt if self.prompt is not None else self.question
        answer = self.answer if self.answer is not None else self.back
        if prompt is not None:
            if not prompt.strip():
                raise ValueError("问题 / 提示面不能为空")
            self.prompt = prompt.strip()
        if answer is not None:
            if not answer.strip():
                raise ValueError("参考答案不能为空")
            self.answer = answer.strip()
        if self.source_refs is not None:
            self.source_citations = self.source_refs
        if self.citations is not None:
            self.source_citations = self.citations
        if self.tags is not None:
            self.tags = [str(tag).strip() for tag in self.tags if str(tag).strip()][:50]
        return self


class RecallCardRevisionCreate(_LearningModel):
    prompt: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    back: Optional[str] = None
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: Optional[List[str]] = None
    source_citations: List[CardCitation] = Field(default_factory=list)
    source_refs: Optional[List[CardCitation]] = None
    citations: Optional[List[CardCitation]] = None
    concise_content: Optional[str] = None
    detailed_content: Optional[str] = None
    answer_structure: Optional[str] = None
    confusing_points_and_cases: Optional[str] = None
    note: Optional[str] = None
    base_revision_id: Optional[str] = None
    expected_card_version: Optional[int] = Field(default=None, ge=1)
    card_version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_card_version(self) -> Optional[int]:
        return self.expected_card_version if self.expected_card_version is not None else self.card_version

    @model_validator(mode="after")
    def _revision_content(self) -> "RecallCardRevisionCreate":
        prompt = (self.prompt or self.question or "").strip()
        answer = (self.answer or self.back or "").strip()
        if not prompt:
            raise ValueError("问题 / 提示面不能为空")
        if not answer:
            raise ValueError("参考答案不能为空")
        self.prompt = prompt
        self.answer = answer
        if self.source_refs is not None:
            self.source_citations = self.source_refs
        if self.citations is not None:
            self.source_citations = self.citations
        if self.tags is not None:
            self.tags = [str(tag).strip() for tag in self.tags if str(tag).strip()][:50]
        return self


CardRevisionCreate = RecallCardRevisionCreate


class RecallCardRevisionUpdate(_LearningModel):
    prompt: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    back: Optional[str] = None
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: Optional[List[str]] = None
    source_citations: Optional[List[CardCitation]] = None
    source_refs: Optional[List[CardCitation]] = None
    citations: Optional[List[CardCitation]] = None
    concise_content: Optional[str] = None
    detailed_content: Optional[str] = None
    answer_structure: Optional[str] = None
    confusing_points_and_cases: Optional[str] = None
    note: Optional[str] = None
    expected_version: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _revision_nonempty(self) -> "RecallCardRevisionUpdate":
        prompt = self.prompt if self.prompt is not None else self.question
        answer = self.answer if self.answer is not None else self.back
        if prompt is not None:
            if not prompt.strip():
                raise ValueError("问题 / 提示面不能为空")
            self.prompt = prompt.strip()
        if answer is not None:
            if not answer.strip():
                raise ValueError("参考答案不能为空")
            self.answer = answer.strip()
        if self.source_refs is not None:
            self.source_citations = self.source_refs
        if self.citations is not None:
            self.source_citations = self.citations
        return self


class RevisionReviewItem(_LearningModel):
    revision_id: str
    decision: RevisionDecision
    note: Optional[str] = None
    expected_card_version: Optional[int] = Field(default=None, ge=1)
    card_version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_card_version(self) -> Optional[int]:
        return self.expected_card_version if self.expected_card_version is not None else self.card_version


class RevisionReviewRequest(_LearningModel):
    revision_id: Optional[str] = None
    decision: Optional[RevisionDecision] = None
    note: Optional[str] = None
    revisions: List[RevisionReviewItem] = Field(default_factory=list)
    items: List[RevisionReviewItem] = Field(default_factory=list)
    decisions: List[RevisionReviewItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalise_items(self) -> "RevisionReviewRequest":
        if self.revision_id and self.decision:
            self.revisions = [RevisionReviewItem(revision_id=self.revision_id, decision=self.decision, note=self.note)]
        elif self.items:
            self.revisions = self.items
        elif self.decisions:
            self.revisions = self.decisions
        if not self.revisions:
            raise ValueError("至少需要一条 revision 审核决定")
        return self


class RecallCardRevisionResponse(_LearningModel):
    id: str
    card_id: str
    revision_number: int
    status: RevisionStatus
    prompt: str
    answer: str
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_citations: List[CardCitation] = Field(default_factory=list)
    concise_content: Optional[str] = None
    detailed_content: Optional[str] = None
    answer_structure: Optional[str] = None
    confusing_points_and_cases: Optional[str] = None
    base_revision_id: Optional[str] = None
    note: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_by: Optional[str] = None
    reviewed_by: Optional[str] = None
    created_at: str
    updated_at: str
    published_at: Optional[str] = None


CardRevisionResponse = RecallCardRevisionResponse


class RecallCardResponse(_LearningModel):
    id: str
    chapter_id: str
    chapter_name: Optional[str] = None
    source_doc_id: Optional[str] = None
    title: Optional[str] = None
    prompt: str
    answer: str
    hint: Optional[str] = None
    source_excerpt: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_citations: List[CardCitation] = Field(default_factory=list)
    source_refs: List[CardCitation] = Field(default_factory=list)
    status: RecallCardStudyStatus = RecallCardStudyStatus.NEW
    lifecycle_status: RecallCardLifecycle = RecallCardLifecycle.DRAFT
    origin: RecallCardOrigin = RecallCardOrigin.MANUAL
    is_confirmed: bool = False
    is_ai_generated: bool = False
    due_at: Optional[str] = None
    last_reviewed_at: Optional[str] = None
    review_count: int = 0
    interval_days: Optional[int] = None
    ease_factor: Optional[float] = None
    revision_id: Optional[str] = None
    published_revision_id: Optional[str] = None
    version: int = 1
    sort_order: int = 0
    created_at: str
    updated_at: str
    published_at: Optional[str] = None
    archived_at: Optional[str] = None


RecallCard = RecallCardResponse


class RecallCardQuery(_LearningModel):
    chapter_id: Optional[str] = None
    status: Optional[str] = None
    lifecycle_status: Optional[RecallCardLifecycle] = None
    search: Optional[str] = None
    include_candidates: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class GenerationCreate(_LearningModel):
    chapter_ids: List[str] = Field(default_factory=list)
    source_doc_id: Optional[str] = None
    card_count: int = Field(default=0, ge=0, le=10000)
    instructions: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class GenerationResponse(_LearningModel):
    id: str
    status: GenerationStatus
    chapter_ids: List[str] = Field(default_factory=list)
    source_doc_id: Optional[str] = None
    card_count: int = 0
    created_count: int = 0
    task_id: Optional[str] = None
    error_msg: Optional[str] = None
    created_at: str
    updated_at: str


class PublishCardRequest(_LearningModel):
    revision_id: Optional[str] = None
    reset_progress: bool = False
    expected_version: Optional[int] = Field(default=None, ge=1)
    version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_version(self) -> Optional[int]:
        return self.expected_version if self.expected_version is not None else self.version


class CardStatusRequest(_LearningModel):
    status: RecallCardLifecycle
    expected_version: Optional[int] = Field(default=None, ge=1)
    version: Optional[int] = Field(default=None, ge=1)

    @property
    def effective_version(self) -> Optional[int]:
        return self.expected_version if self.expected_version is not None else self.version


class CardHistoryItem(_LearningModel):
    id: str
    card_id: str
    action: str
    revision_id: Optional[str] = None
    snapshot: Optional[RecallCardResponse] = None
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    created_at: str


class LearningContentResponse(_LearningModel):
    chapters: List[LearningChapterResponse] = Field(default_factory=list)
    cards: List[RecallCardResponse] = Field(default_factory=list)
    total_cards: int = 0
    total_chapters: int = 0


class ScopeUpdate(_LearningModel):
    chapter_ids: Optional[List[str]] = None
    include_unassigned: bool = True


class ScopeResponse(_LearningModel):
    chapter_ids: List[str] = Field(default_factory=list)
    include_unassigned: bool = True
    updated_at: Optional[str] = None
