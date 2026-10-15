export interface DocumentItem {
  id: string;
  filename: string;
  file_hash: string;
  file_type: 'textbook' | 'exam' | 'answer' | 'exam_and_answer' | 'supplement';
  year: number | null;
  session_name: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  error_msg: string | null;
  page_count: number;
  created_at: string;
  task_id?: string | null;
  is_duplicate?: boolean;
}

export interface DocumentChunk {
  id: string;
  doc_id: string;
  chunk_index: number;
  page_number: number | null;
  heading: string | null;
  paragraph_ref: string | null;
  content: string;
  original_content?: string | null;
  parse_error?: string | null;
  embedding_status?: string | null;
  created_at: string;
}

export interface QuestionItem {
  id: string;
  doc_id: string | null;
  year: number | null;
  session_name: string | null;
  question_num: string | null;
  question_type: string;
  score: number | null;
  stem: string;
  standard_answer: string | null;
  source_location: string | null;
  source_chunk_id?: string | null;
  source_page?: number | null;
  source_paragraph_ref?: string | null;
  answer_source_chunk_id?: string | null;
  answer_source_doc_id?: string | null;
  answer_source_page?: number | null;
  answer_source_paragraph_ref?: string | null;
  needs_review?: number;
  review_reason?: string | null;
  is_confirmed: number; // 0 or 1
  is_ai_generated: number;
  knowledge_point_ids: string[];
  knowledge_point_names: string[];
  created_at: string;
}

export interface AnswerCandidate {
  id: string;
  doc_id: string;
  year: number | null;
  session_name: string | null;
  question_num: string | null;
  answer_text: string;
  source_chunk_id: string | null;
  source_page: number | null;
  source_paragraph_ref: string | null;
  status: string;
  question_id: string | null;
  created_at: string;
}

export interface KnowledgePoint {
  id: string;
  code: string | null;
  name: string;
  chapter: string | null;
  parent_id: string | null;
  priority_tag: 'important' | 'normal' | 'deferred';
  sort_order: number;
  source_type?: 'manual' | 'extracted' | 'pending_verification';
  source_status?: 'valid' | 'supplement_needed';
  source_reference?: string | null;
  is_deleted?: number;
  deleted_at?: string | null;
  created_at: string;
  children?: KnowledgePoint[];
}

export interface KnowledgePointStats {
  id: string;
  code: string | null;
  name: string;
  chapter: string | null;
  priority_tag: 'important' | 'normal' | 'deferred';
  source_type?: 'manual' | 'extracted' | 'pending_verification';
  source_status?: 'valid' | 'supplement_needed';
  source_reference?: string | null;
  questions_count: number;
  years_count: number;
  recent_year: number | null;
  recent_5yr_count: number;
  years_list: number[];
  questions: QuestionItem[];
}

export interface OverallStats {
  total_recorded_years: number;
  min_year: number | null;
  max_year: number | null;
  recorded_years: number[];
  missing_years: number[];
  recent_5yr_window: number[];
  total_questions: number;
  confirmed_questions: number;
  knowledge_points_count: number;
}

export interface CitationItem {
  chunk_id: string;
  doc_name: string;
  page_or_ref: string;
  snippet: string;
}

export interface StudyCard {
  id: string;
  knowledge_point_id: string;
  knowledge_point_name: string;
  concise_content: string;
  detailed_content: string;
  answer_structure: string;
  confusing_points_and_cases: string;
  citations: CitationItem[];
  needs_update: number;
  updated_at: string;
}

export interface KeypointCoverage {
  point: string;
  status: 'COVERED' | 'PARTIALLY_COVERED' | 'MISSING';
  detail: string;
  basis: string;
}

export interface QuestionEvaluation {
  question_id: string;
  score_estimate: number | null;
  max_score: number | null;
  score_basis: string | null;
  keypoints_coverage: KeypointCoverage[];
  conceptual_errors: string[];
  ungrounded_claims: string[];
  expression_rewrite: string;
}

export interface PracticeSession {
  id: string;
  knowledge_point_id: string;
  knowledge_point_name: string;
  question_count: number;
  difficulty: 'basic' | 'comprehensive';
  questions: Array<{
    question_id: string;
    stem: string;
    reference_answer?: string;
    basis?: string;
  }>;
  user_answers?: Array<{
    question_id: string;
    answer_text: string;
  }>;
  evaluation?: QuestionEvaluation[];
  created_at: string;
}

export interface TimingCategory {
  wall_time_ms: number;
  elapsed_time_ms: number;
  count: number;
}

export interface TaskTimingBreakdown {
  local_parse?: TimingCategory;
  ocr_request?: TimingCategory;
  llm_extract_request?: TimingCategory;
  embedding_request?: TimingCategory;
  validation?: TimingCategory;
  db_write?: TimingCategory;
  retry_wait?: TimingCategory;
}

export interface ExecUnitStatus {
  id: string;
  kind: 'batch' | 'page' | 'chunk' | 'card' | string;
  index?: number | null;
  scope?: string | null;
  service?: 'llm' | 'ocr' | 'embedding' | string | null;
  state: 'queued' | 'waiting_slot' | 'awaiting_response' | 'processing' | 'saving' | 'retry_wait'
    | 'succeeded' | 'failed' | 'skipped' | 'cancelled' | string;
  action?: string | null;
  requestStartedAt?: string | null;
  attempt?: number;
  retryReason?: string | null;
  retryWaitSeconds?: number | null;
  error?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface TaskUnitSummary {
  total?: number | null;
  succeeded: number;
  failed: number;
  skipped: number;
  activeCount: number;
  retryingCount: number;
  waitingReason?: string | null;
  waitingSince?: string | null;
}

export interface TaskProgressDetail {
  stage: string;
  phase: 'idle' | 'working' | 'waiting_provider' | 'waiting_api' | 'retry_wait';
  unit: 'page' | 'chunk' | 'batch' | 'question' | string;
  total?: number | null;
  succeeded: number;
  failed: number;
  skipped: number;
  activeUnits: number[];
  resultCount: number;
  attempt: number;
  currentScope?: string | null;
  currentAction?: string | null;
  requestStartedAt?: string | null;
  heartbeatAt?: string | null;
  lastProgressAt?: string | null;
  /** 逐执行单元状态（页/批次级），仅由更新后的执行器产生；旧任务为空数组 */
  execUnits?: ExecUnitStatus[];
  /** 由执行单元汇总的任务摘要；旧任务为 null */
  summary?: TaskUnitSummary | null;
}

export interface TaskEventItem {
  id: number;
  task_id: string;
  event_type: string;
  stage?: string | null;
  unit_index?: number | null;
  message: string;
  data?: Record<string, any> | null;
  created_at: string;
}

export interface TaskEventsResponse {
  events: TaskEventItem[];
  last_id?: number | null;
}

export interface TaskStatus {
  id: string;
  task_type: string;
  target_id: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'partial' | 'cancel_requested' | 'cancelled';
  progress: number;
  message: string | null;
  error_msg: string | null;
  created_at: string;
  updated_at: string;
  batch_errors_json?: string | null;
  retry_of?: string | null;
  hidden_at?: string | null;
  parent_task_id?: string | null;
  started_at?: string | null;
  elapsed_seconds?: number | null;
  queued_seconds?: number | null;
  queue_position?: number | null;
  progress_detail?: TaskProgressDetail | null;
  timing_breakdown?: TaskTimingBreakdown | null;
}

export interface TaskHistoryResponse {
  tasks: TaskStatus[];
  total: number;
  page: number;
  limit: number;
  active_count: number;
  actionable_count: number;
  history_count: number;
}

export interface TaskClearResponse {
  cleared_count: number;
  clear_token: string;
}

export interface TaskRestoreResponse {
  restored_count: number;
}

export interface CandidateCitation {
  chunk_id: string;
  doc_id: string;
  doc_name: string;
  page_number?: number | null;
  paragraph_ref?: string | null;
  quote: string;
  source_hash?: string | null;
}

export interface ExtractionCandidate {
  id: string;
  extraction_id: string;
  name: string;
  chapter?: string | null;
  definition?: string | null;
  citations: CandidateCitation[];
  status: 'pending' | 'accepted' | 'ignored' | 'merged' | 'invalid';
  match_type: 'new' | 'existing' | 'source_changed' | 'deleted_or_ignored';
  matched_kp_id?: string | null;
  matched_kp_name?: string | null;
  target_kp_id?: string | null;
  source_status?: 'valid' | 'stale' | 'missing' | 'supplement_needed' | string;
  requires_source_review?: boolean;
  version?: number;
  created_at: string;
}

export interface CandidateDecisionItem {
  candidate_id: string;
  version?: number;
  action: 'accept' | 'ignore' | 'merge';
  target_kp_id?: string;
  custom_name?: string;
  custom_chapter?: string;
}

export interface ExtractionBatch {
  id: string;
  task_id?: string | null;
  status: 'processing' | 'completed' | 'cancelled' | 'failed' | 'partial';
  doc_ids: string[];
  coverage_info?: {
    total_chunks: number;
    processed_chunks: number;
    candidates_count: number;
  } | null;
  error_msg?: string | null;
  candidates_count: number;
  pending_candidates?: number;
  task_status?: string | null;
  accepted_candidates?: number;
  merged_candidates?: number;
  ignored_candidates?: number;
  invalid_candidates?: number;
  can_retry?: boolean;
  recovery_reason?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApplyCandidatesResponse {
  applied_count: number;
  ignored_count: number;
  merged_count: number;
  replayed_count?: number;
  results?: Array<{ candidate_id: string; status: string; target_kp_id: string | null; replayed: boolean }>;
}

export interface CandidateSourceReview {
  candidate_id: string;
  version: number;
  name: string;
  chapter: string | null;
  citations: Array<CandidateCitation & { content: string }>;
  can_confirm: boolean;
  reason: string | null;
}

export interface CandidateSourceReviewPayload {
  version: number;
  name: string;
  chapter?: string | null;
  citations: CandidateCitation[];
}

export interface QuestionLinkItem {
  question_id: string;
  knowledge_point_id: string;
  question_stem: string;
  year?: number | null;
  session_name?: string | null;
  question_num?: string | null;
  knowledge_point_name: string;
  confidence: number;
  reason?: string | null;
}

export interface QuestionLinkSuggestionResponse {
  links: QuestionLinkItem[];
  total_confirmed_questions: number;
  message?: string | null;
}

export interface QuestionLinkDecision {
  question_id: string;
  knowledge_point_id: string;
  action: 'accept' | 'reject';
}

export interface QuestionLinkApplyResponse {
  applied_count: number;
}
