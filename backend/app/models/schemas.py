from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Dict, Any

# --- Document Schemas ---
class DocumentResponse(BaseModel):
    id: str
    filename: str
    file_hash: str
    file_type: str # 'textbook', 'exam', 'answer', 'supplement'
    year: Optional[int] = None
    session_name: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    page_count: int = 0
    created_at: str
    task_id: Optional[str] = None

class DocumentChunkResponse(BaseModel):
    id: str
    doc_id: str
    chunk_index: int
    page_number: Optional[int] = None
    heading: Optional[str] = None
    paragraph_ref: Optional[str] = None
    content: str
    original_content: Optional[str] = None
    parse_error: Optional[str] = None
    embedding_status: Optional[str] = None
    created_at: str

class ChunkUpdateRequest(BaseModel):
    content: str

# --- Knowledge Point Schemas ---
class KnowledgePointBase(BaseModel):
    code: Optional[str] = None
    name: str
    chapter: Optional[str] = None
    parent_id: Optional[str] = None
    priority_tag: str = "normal" # 'important', 'normal', 'deferred'
    sort_order: int = 0
    source_type: Optional[str] = "pending_verification" # 'pending_verification', 'extracted', 'manual'
    source_status: Optional[str] = "valid"              # 'valid', 'invalid', 'supplement_needed'
    source_reference: Optional[str] = None
    is_deleted: int = 0
    deleted_at: Optional[str] = None

class KnowledgePointCreate(KnowledgePointBase):
    pass

class KnowledgePointUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    chapter: Optional[str] = None
    parent_id: Optional[str] = None
    priority_tag: Optional[str] = None
    sort_order: Optional[int] = None
    source_type: Optional[str] = None
    source_status: Optional[str] = None
    source_reference: Optional[str] = None

class KnowledgePointMergeRequest(BaseModel):
    target_id: str # Keep this id
    source_ids: List[str] # Merge these into target_id and soft-delete/reassign

class KnowledgePointResponse(KnowledgePointBase):
    id: str
    created_at: str
    children: List["KnowledgePointResponse"] = []

# --- Question Schemas ---
class QuestionCreate(BaseModel):
    doc_id: Optional[str] = None
    year: Optional[int] = None
    session_name: Optional[str] = None
    question_num: Optional[str] = None
    question_type: str = "short_answer"
    score: Optional[float] = None
    stem: str
    standard_answer: Optional[str] = None
    source_location: Optional[str] = None
    source_chunk_id: Optional[str] = None
    source_page: Optional[int] = None
    source_paragraph_ref: Optional[str] = None
    answer_source_chunk_id: Optional[str] = None
    answer_source_doc_id: Optional[str] = None
    answer_source_page: Optional[int] = None
    answer_source_paragraph_ref: Optional[str] = None
    is_confirmed: int = 0
    knowledge_point_ids: List[str] = []

    @model_validator(mode="after")
    def validate_question(self):
        value = self
        if value.year is not None and not 1950 <= value.year <= 2050:
            raise ValueError("年份必须为空或在 1950-2050 之间")
        if not value.stem.strip():
            raise ValueError("题干不能为空")
        if value.score is not None and value.score < 0:
            raise ValueError("分值不能为负数")
        return value

class QuestionUpdate(BaseModel):
    year: Optional[int] = None
    session_name: Optional[str] = None
    question_num: Optional[str] = None
    question_type: Optional[str] = None
    score: Optional[float] = None
    stem: Optional[str] = None
    standard_answer: Optional[str] = None
    source_location: Optional[str] = None
    source_chunk_id: Optional[str] = None
    source_page: Optional[int] = None
    source_paragraph_ref: Optional[str] = None
    answer_source_chunk_id: Optional[str] = None
    answer_source_doc_id: Optional[str] = None
    answer_source_page: Optional[int] = None
    answer_source_paragraph_ref: Optional[str] = None
    is_confirmed: Optional[int] = None
    knowledge_point_ids: Optional[List[str]] = None

    @model_validator(mode="after")
    def validate_update(self):
        value = self
        if value.year is not None and not 1950 <= value.year <= 2050:
            raise ValueError("年份必须为空或在 1950-2050 之间")
        if value.stem is not None and not value.stem.strip():
            raise ValueError("题干不能为空")
        if value.score is not None and value.score < 0:
            raise ValueError("分值不能为负数")
        return value

class QuestionResponse(BaseModel):
    id: str
    doc_id: Optional[str] = None
    year: Optional[int] = None
    session_name: Optional[str] = None
    question_num: Optional[str] = None
    question_type: str
    score: Optional[float] = None
    stem: str
    standard_answer: Optional[str] = None
    source_location: Optional[str] = None
    source_chunk_id: Optional[str] = None
    source_page: Optional[int] = None
    source_paragraph_ref: Optional[str] = None
    answer_source_chunk_id: Optional[str] = None
    answer_source_doc_id: Optional[str] = None
    answer_source_page: Optional[int] = None
    answer_source_paragraph_ref: Optional[str] = None
    needs_review: int = 1
    review_reason: Optional[str] = None
    is_confirmed: int
    is_ai_generated: int
    knowledge_point_ids: List[str] = []
    knowledge_point_names: List[str] = []
    created_at: str

# --- Stats Schemas ---
class KnowledgePointStats(BaseModel):
    id: str
    code: Optional[str] = None
    name: str
    chapter: Optional[str] = None
    priority_tag: str
    source_type: Optional[str] = "pending_verification"
    source_status: Optional[str] = "valid"
    source_reference: Optional[str] = None
    questions_count: int = 0      # 涉及题目数
    years_count: int = 0          # 出现年份数 (去重)
    recent_year: Optional[int] = None # 最近出现年份
    recent_5yr_count: int = 0     # 近五年出现次数
    years_list: List[int] = []    # 涉及年份明细
    questions: List[QuestionResponse] = [] # 原题与答案明细

class OverallStatsResponse(BaseModel):
    total_recorded_years: int
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    recorded_years: List[int] = []
    missing_years: List[int] = []
    recent_5yr_window: List[int] = [] # [max_year-4, ..., max_year]
    total_questions: int = 0
    confirmed_questions: int = 0
    knowledge_points_count: int = 0

# --- Study Card Schemas ---
class CitationItem(BaseModel):
    chunk_id: str
    doc_name: str
    page_or_ref: str
    snippet: str

class StudyCardResponse(BaseModel):
    id: str
    knowledge_point_id: str
    knowledge_point_name: str
    concise_content: str
    detailed_content: str
    answer_structure: str
    confusing_points_and_cases: str
    citations: List[CitationItem] = []
    needs_update: int = 0
    updated_at: str

class GenerateCardRequest(BaseModel):
    knowledge_point_id: str

# --- Question Answering Assistant Schemas ---
class QuestionAnswerRequest(BaseModel):
    question_id: Optional[str] = None
    custom_stem: Optional[str] = None

class QuestionAnswerResponse(BaseModel):
    stem: str
    official_answer: Optional[str] = None
    ai_answer: str
    thought_process: str
    citations: List[CitationItem] = []
    conflict_notes: Optional[str] = None

# --- Practice & Grading Schemas ---
class PracticeGenerateRequest(BaseModel):
    knowledge_point_id: str
    question_count: int = Field(default=1, ge=1, le=5)
    difficulty: str = "basic" # 'basic', 'comprehensive'

class PracticeAnswerSubmission(BaseModel):
    question_id: str
    answer_text: str

class PracticeSubmitRequest(BaseModel):
    answers: List[PracticeAnswerSubmission]

class KeypointCoverage(BaseModel):
    point: str
    status: str # 'COVERED', 'PARTIALLY_COVERED', 'MISSING'
    detail: str
    basis: str

class QuestionEvaluationResult(BaseModel):
    question_id: str
    score_estimate: Optional[float] = None
    max_score: Optional[float] = None
    score_basis: Optional[str] = None
    keypoints_coverage: List[KeypointCoverage] = []
    conceptual_errors: List[str] = []
    ungrounded_claims: List[str] = []
    expression_rewrite: str

class PracticeSessionResponse(BaseModel):
    id: str
    knowledge_point_id: str
    knowledge_point_name: str
    question_count: int
    difficulty: str
    questions: List[Dict[str, Any]] # Public view hides answers before submission
    user_answers: Optional[List[Dict[str, Any]]] = None
    evaluation: Optional[List[QuestionEvaluationResult]] = None
    created_at: str

# --- Task Schemas ---
class ExecUnitStatus(BaseModel):
    """Per-execution-unit status (page / batch / card), reported independently by concurrent workers."""
    id: str
    kind: str = "batch"            # 'batch', 'page', 'chunk', 'card'
    index: Optional[int] = None    # 1-based display index within its kind
    scope: Optional[str] = None    # e.g. '第 9—12 页'
    service: Optional[str] = None  # 'llm', 'ocr', 'embedding'
    state: str = "queued"          # 'queued','waiting_slot','awaiting_response','processing','saving','retry_wait','succeeded','failed','skipped','cancelled'
    action: Optional[str] = None
    requestStartedAt: Optional[str] = None  # set when the request is actually issued; reset on each retry
    attempt: int = 1
    retryReason: Optional[str] = None       # masked reason
    retryWaitSeconds: Optional[float] = None
    error: Optional[str] = None             # masked error
    startedAt: Optional[str] = None
    finishedAt: Optional[str] = None

class TaskUnitSummary(BaseModel):
    """Task-level summary aggregated from per-unit statuses."""
    total: Optional[int] = None
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    activeCount: int = 0
    retryingCount: int = 0
    waitingReason: Optional[str] = None   # e.g. '等待模型响应', '限流等待', '重试等待'
    waitingSince: Optional[str] = None    # request start (UTC ISO) of the longest-waiting active unit

class TaskProgressDetail(BaseModel):
    stage: str = "init"
    phase: str = "working" # 'working', 'waiting_provider', 'retry_wait'
    unit: str = "batch" # 'page', 'chunk', 'batch'
    total: Optional[int] = None
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    activeUnits: List[int] = []
    resultCount: int = 0
    attempt: int = 1
    currentScope: Optional[str] = None # e.g. '第 19—24 页'
    currentAction: Optional[str] = None # e.g. '等待第 6 批模型响应'
    requestStartedAt: Optional[str] = None
    heartbeatAt: Optional[str] = None
    lastProgressAt: Optional[str] = None
    execUnits: List[ExecUnitStatus] = []    # optional per-unit statuses; empty for legacy tasks
    summary: Optional[TaskUnitSummary] = None  # present only for tasks produced by the updated executor

class TimingCategory(BaseModel):
    wall_time_ms: float = 0.0
    elapsed_time_ms: float = 0.0
    count: int = 0

class TaskTimingBreakdown(BaseModel):
    local_parse: TimingCategory = Field(default_factory=TimingCategory)
    ocr_request: TimingCategory = Field(default_factory=TimingCategory)
    llm_extract_request: TimingCategory = Field(default_factory=TimingCategory)
    embedding_request: TimingCategory = Field(default_factory=TimingCategory)
    validation: TimingCategory = Field(default_factory=TimingCategory)
    db_write: TimingCategory = Field(default_factory=TimingCategory)
    retry_wait: TimingCategory = Field(default_factory=TimingCategory)

class TaskEventItem(BaseModel):
    id: int
    task_id: str
    event_type: str
    stage: Optional[str] = None
    unit_index: Optional[int] = None
    message: str
    data: Optional[Dict[str, Any]] = None
    created_at: str

class TaskEventsResponse(BaseModel):
    events: List[TaskEventItem]
    last_id: Optional[int] = None
    has_more: bool = False

class TaskResponse(BaseModel):
    id: str
    task_type: str
    target_id: Optional[str] = None
    status: str
    progress: int
    message: Optional[str] = None
    error_msg: Optional[str] = None
    created_at: str
    updated_at: str
    batch_errors_json: Optional[str] = None
    retry_of: Optional[str] = None
    hidden_at: Optional[str] = None
    parent_task_id: Optional[str] = None
    started_at: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    queued_seconds: Optional[float] = None
    queue_position: Optional[int] = None
    progress_detail: Optional[TaskProgressDetail] = None
    timing_breakdown: Optional[TaskTimingBreakdown] = None

class TaskClearRequest(BaseModel):
    scope: str = "completed" # 'completed' or 'terminal'

class TaskClearResponse(BaseModel):
    cleared_count: int
    clear_token: str

class TaskRestoreRequest(BaseModel):
    clear_token: str

class TaskRestoreResponse(BaseModel):
    restored_count: int

class TaskHistoryResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int
    page: int
    limit: int
    active_count: int
    actionable_count: int
    history_count: int


class AnswerCandidateResponse(BaseModel):
    id: str
    doc_id: str
    year: Optional[int] = None
    session_name: Optional[str] = None
    question_num: Optional[str] = None
    answer_text: str
    source_chunk_id: Optional[str] = None
    source_page: Optional[int] = None
    source_paragraph_ref: Optional[str] = None
    status: str
    question_id: Optional[str] = None
    created_at: str


# --- Knowledge Point Extraction Schemas ---
class ExtractionCreateRequest(BaseModel):
    doc_ids: List[str]

class ExtractionBatchResponse(BaseModel):
    id: str
    task_id: Optional[str] = None
    status: str
    doc_ids: List[str] = []
    coverage_info: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    candidates_count: int = 0
    created_at: str
    updated_at: str

class CandidateCitation(BaseModel):
    chunk_id: str
    doc_id: str
    doc_name: str
    page_number: Optional[int] = None
    paragraph_ref: Optional[str] = None
    quote: str

class CandidateResponse(BaseModel):
    id: str
    extraction_id: str
    name: str
    chapter: Optional[str] = None
    definition: Optional[str] = None
    citations: List[CandidateCitation] = []
    status: str = "pending" # 'pending', 'accepted', 'ignored', 'merged'
    match_type: str = "new" # 'new', 'existing', 'source_changed', 'deleted_or_ignored'
    matched_kp_id: Optional[str] = None
    matched_kp_name: Optional[str] = None
    target_kp_id: Optional[str] = None
    created_at: str

class CandidateDecisionItem(BaseModel):
    candidate_id: str
    action: str # 'accept', 'ignore', 'merge'
    target_kp_id: Optional[str] = None
    custom_name: Optional[str] = None
    custom_chapter: Optional[str] = None

class ApplyCandidatesRequest(BaseModel):
    decisions: List[CandidateDecisionItem]

class ApplyCandidatesResponse(BaseModel):
    applied_count: int
    ignored_count: int
    merged_count: int

class QuestionLinkItem(BaseModel):
    question_id: str
    knowledge_point_id: str
    question_stem: str
    year: Optional[int] = None
    session_name: Optional[str] = None
    question_num: Optional[str] = None
    knowledge_point_name: str
    confidence: float = 1.0
    reason: Optional[str] = None

class QuestionLinkSuggestionResponse(BaseModel):
    links: List[QuestionLinkItem] = []
    total_confirmed_questions: int = 0
    message: Optional[str] = None

class QuestionLinkDecision(BaseModel):
    question_id: str
    knowledge_point_id: str
    action: str = "accept" # 'accept' | 'reject'

class QuestionLinkApplyRequest(BaseModel):
    links: List[QuestionLinkDecision]

class QuestionLinkApplyResponse(BaseModel):
    applied_count: int
