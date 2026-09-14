export type LearningCardStatus = 'new' | 'learning' | 'review' | 'suspended' | 'archived';
export type LearningGenerationStatus = 'pending' | 'processing' | 'completed' | 'failed' | 'cancelled';
export type LearningTodayStatus = 'idle' | 'active' | 'paused' | 'completed';
export type AttemptRating = 'forgotten' | 'fuzzy' | 'mastered';
export type LearningRound = 'morning' | 'noon' | 'evening' | number;
export type LearningAttemptKind = 'new' | 'review' | 'relearning' | string;
export type LearningUnitType = 'prompt' | 'answer' | 'outline' | 'explanation' | string;
export type LearningProgressState = 'new' | 'learning' | 'review' | 'completed' | 'suspended' | string;

export interface LearningSettings {
  daily_new_limit: number | null;
  daily_review_limit?: number | null;
  new_cards_per_day?: number | null;
  review_cards_per_day?: number | null;
  reveal_after_seconds?: number | null;
  auto_advance?: boolean;
  show_hints?: boolean;
  timezone: string;
  selected_chapter_ids_json?: string | string[] | null;
  selected_knowledge_point_ids_json?: string | string[] | null;
  updated_at?: string | null;
}

export interface LearningSettingsUpdate {
  daily_new_limit?: number | null;
  daily_review_limit?: number | null;
  new_cards_per_day?: number | null;
  review_cards_per_day?: number | null;
  reveal_after_seconds?: number | null;
  auto_advance?: boolean;
  show_hints?: boolean;
  timezone?: string;
  selected_chapter_ids_json?: string | string[] | null;
  selected_knowledge_point_ids_json?: string | string[] | null;
}

export interface LearningScope {
  chapter_ids: string[];
  knowledge_point_ids?: string[];
  include_unassigned: boolean;
  selected_chapter_ids_json?: string | string[] | null;
  selected_knowledge_point_ids_json?: string | string[] | null;
  updated_at?: string | null;
}

export interface LearningScopeUpdate {
  chapter_ids?: string[];
  knowledge_point_ids?: string[];
  include_unassigned?: boolean;
}

export interface LearningChapter {
  id: string;
  name: string;
  title?: string | null;
  parent_id?: string | null;
  sort_order: number;
  enabled: boolean;
  card_count: number;
  due_count?: number;
  knowledge_point_ids?: string[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface LearningChapterPayload {
  name: string;
  title?: string;
  parent_id?: string | null;
  sort_order?: number;
  enabled?: boolean;
}

export interface LearningGeneration {
  id: string;
  status: LearningGenerationStatus;
  chapter_ids: string[];
  card_count: number;
  created_count?: number;
  task_id?: string | null;
  error_msg?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface LearningGenerationPayload {
  chapter_id?: string;
  chapter_ids?: string[];
  knowledge_point_ids?: string[];
  units?: unknown[];
  card_count?: number;
  instructions?: string;
  mode?: string;
  request_id?: string;
}

export interface LearningCitation {
  id?: string;
  chunk_id?: string | null;
  document_name?: string | null;
  page?: number | null;
  reference?: string | null;
  quote: string;
}

export interface RecallCard {
  id: string;
  knowledge_point_id?: string | null;
  chapter_id?: string | null;
  chapter_name?: string | null;
  prompt: string;
  answer: string;
  hint?: string | null;
  source_excerpt?: string | null;
  source_refs: LearningCitation[];
  tags: string[];
  status: LearningCardStatus;
  current_revision_no?: number | null;
  is_paused?: boolean;
  is_archived?: boolean;
  lock_version?: number | null;
  due_at?: string | null;
  last_reviewed_at?: string | null;
  review_count: number;
  interval_days?: number | null;
  ease_factor?: number | null;
  revision_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  lifecycle_status?: 'draft' | 'published' | 'paused' | 'archived' | 'rejected' | string;
  is_confirmed?: boolean;
  is_ai_generated?: boolean;
  published_revision_id?: string | null;
  version?: number;
}

export interface LearningCardPayload {
  knowledge_point_id?: string | null;
  chapter_id?: string | null;
  prompt: string;
  answer: string;
  hint?: string | null;
  source_excerpt?: string | null;
  tags?: string[];
  status?: LearningCardStatus;
  is_paused?: boolean;
  is_archived?: boolean;
  lifecycle_status?: 'draft' | 'published' | 'paused' | 'archived' | 'rejected';
  origin?: 'manual' | 'generated' | 'imported';
  reset_progress?: boolean;
  expected_version?: number;
}

export type RecallCardPayload = LearningCardPayload;

export interface CardRevision {
  id: string;
  card_id: string;
  revision_no?: number | null;
  review_status?: 'pending' | 'approved' | 'rejected';
  question?: string | null;
  card_type?: string | null;
  outline_points_json?: string | unknown[] | null;
  explanation?: string | null;
  expanded_answer?: string | null;
  citations_json?: string | unknown[] | null;
  source_status?: string | null;
  generation_batch_id?: string | null;
  unit_key?: string | null;
  prompt: string;
  answer: string;
  hint?: string | null;
  note?: string | null;
  status: 'pending' | 'approved' | 'rejected';
  created_at?: string | null;
  reviewed_at?: string | null;
}

export interface CardRevisionPayload {
  prompt: string;
  answer: string;
  hint?: string | null;
  note?: string;
  question?: string;
  card_type?: string;
  outline_points_json?: string | unknown[];
  explanation?: string;
  expanded_answer?: string;
  citations_json?: string | unknown[];
  source_status?: string;
  generation_batch_id?: string;
  unit_key?: string;
}

export interface RevisionReviewItem {
  revision_id: string;
  decision: 'approve' | 'reject';
  note?: string;
  expected_card_version?: number;
}

export interface RevisionReviewPayload extends Partial<RevisionReviewItem> {
  revisions?: RevisionReviewItem[];
  items?: RevisionReviewItem[];
}

export interface LearningProgress {
  id?: string;
  card_id: string;
  layer: string;
  content_revision_no?: number | null;
  state: LearningProgressState;
  interval_index?: number | null;
  due_date?: string | null;
  state_version?: number | null;
}

export interface LearningUnit {
  id: string;
  study_date: string;
  card_id: string;
  layer: string;
  content_revision_no?: number | null;
  unit_type: LearningUnitType;
  started_at?: string | null;
  invalid_reason?: string | null;
}

export interface LearningAttemptEvent {
  id?: string;
  request_id?: string | null;
  fingerprint?: string | null;
  unit_id?: string | null;
  card_id?: string | null;
  layer?: string | null;
  version?: number | null;
  snapshots?: string | Record<string, unknown> | null;
  response?: string | Record<string, unknown> | null;
  created_at?: string | null;
}

export interface TodayAttempt {
  id: string;
  day_unit_id?: string | null;
  card_id: string;
  position: number;
  display_order?: number | null;
  total: number;
  study_date?: string | null;
  content_revision_no?: number | null;
  prompt: string;
  answer?: string | null;
  hint?: string | null;
  chapter_name?: string | null;
  round?: LearningRound | null;
  outline_points?: string[];
  explanation?: string | null;
  expanded_answer?: string | null;
  citations?: LearningCitation[];
  source_excerpt?: string | null;
  attempt_kind?: LearningAttemptKind | null;
  attempt_index?: number | null;
  revealed: boolean;
  revealed_at?: string | null;
  rating?: AttemptRating | null;
  submitted_at?: string | null;
  status?: string | null;
  completed: boolean;
  card?: RecallCard | null;
}

export interface TodayLearning {
  date: string;
  study_date?: string;
  status: LearningTodayStatus;
  current_round_number?: number | null;
  round?: LearningRound | null;
  current_round?: LearningRound | null;
  override_enabled?: boolean;
  state_version?: number | null;
  total: number;
  completed: number;
  remaining: number;
  new_count: number;
  review_count: number;
  attempts: TodayAttempt[];
  queue: RecallCard[];
  current_attempt?: TodayAttempt | null;
  current_card?: RecallCard | null;
  scope?: LearningScope | null;
  settings?: LearningSettings | null;
  day_version?: number | null;
}

export interface TodayNextPayload {
  attempt_id?: string;
  day_unit_id?: string;
  day_version?: number;
}

export interface TodayOpenPayload {
  study_date?: string;
  day_version?: number;
  override?: boolean;
  resume?: boolean;
}

export interface LearningActionResponse {
  request_id?: string;
  idempotent_replay?: boolean;
  study_date?: string;
  day_version?: number | null;
  state_version?: number | null;
  round?: number | string | null;
  rating?: AttemptRating | null;
  attempt?: TodayAttempt | null;
  additional_attempt?: TodayAttempt | null;
  unit?: TodayAttempt | null;
  progress?: Record<string, unknown> | null;
}

export interface TodayOverridePayload {
  attempt_id?: string;
  card_id?: string;
  day_unit_id?: string;
  enabled?: boolean;
  position?: number;
  day_version?: number;
}

export interface AttemptRatePayload {
  rating: AttemptRating;
  request_id?: string;
  content_revision_no?: number;
}

export interface LearningCardQuery {
  chapter_id?: string;
  knowledge_point_id?: string;
  kp?: string;
  status?: LearningCardStatus | 'all' | string;
  search?: string;
  include_candidates?: boolean;
  limit?: number;
  offset?: number;
}

export interface LearningGenerationQuery {
  limit?: number;
  offset?: number;
}

export interface LearningCardHistoryItem {
  id: string;
  card_id: string;
  action: string;
  revision_id?: string | null;
  snapshot?: RecallCard | null;
  prompt?: string | null;
  answer?: string | null;
  status?: string | null;
  created_at?: string | null;
}

export type LearningPayload = Record<string, unknown> | unknown[] | null;

const asRecord = (value: unknown): Record<string, unknown> | null => {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>;
  return null;
};

const unwrap = (value: unknown): unknown => {
  const record = asRecord(value);
  if (!record) return value;
  if ('data' in record && record.data !== undefined) return record.data;
  if ('result' in record && record.result !== undefined) return record.result;
  return value;
};

const firstValue = (record: Record<string, unknown>, keys: string[]): unknown => {
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
};

const stringValue = (value: unknown, fallback = ''): string => {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return fallback;
  return String(value);
};

const numberValue = (value: unknown, fallback = 0): number => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return fallback;
};

const optionalNumber = (value: unknown): number | null => {
  if (value === null || value === undefined || value === '') return null;
  return numberValue(value, 0);
};

const booleanValue = (value: unknown, fallback = false): boolean => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return ['1', 'true', 'yes'].includes(value.toLowerCase());
  return fallback;
};

const stringArray = (value: unknown): string[] => {
  if (Array.isArray(value)) return value.map(item => stringValue(item)).filter(Boolean);
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value);
      return Array.isArray(parsed) ? stringArray(parsed) : [];
    } catch {
      return [];
    }
  }
  return [];
};

export function unwrapLearningPayload<T>(value: unknown): T {
  return unwrap(value) as T;
}

export function learningArray<T>(value: unknown, keys: string[] = ['items', 'cards', 'chapters', 'generations', 'attempts']): T[] {
  const unwrapped = unwrap(value);
  if (Array.isArray(unwrapped)) return unwrapped as T[];
  const record = asRecord(unwrapped);
  if (!record) return [];
  for (const key of keys) if (Array.isArray(record[key])) return record[key] as T[];
  return [];
}

export function normaliseSettings(value: unknown): LearningSettings {
  const record = asRecord(unwrap(value)) ?? {};
  return {
    daily_new_limit: optionalNumber(firstValue(record, ['daily_new_limit', 'new_limit'])),
    daily_review_limit: optionalNumber(firstValue(record, ['daily_review_limit', 'review_limit'])),
    reveal_after_seconds: optionalNumber(firstValue(record, ['reveal_after_seconds', 'reveal_delay_seconds'])),
    auto_advance: booleanValue(record.auto_advance, false),
    show_hints: booleanValue(record.show_hints, true),
    timezone: stringValue(record.timezone, 'Asia/Shanghai'),
    selected_chapter_ids_json: firstValue(record, ['selected_chapter_ids_json', 'selected_chapter_ids']) as string | string[] | null ?? null,
    selected_knowledge_point_ids_json: firstValue(record, ['selected_knowledge_point_ids_json', 'selected_knowledge_point_ids']) as string | string[] | null ?? null,
    updated_at: stringValue(record.updated_at, '') || null,
  };
}

export function normaliseScope(value: unknown): LearningScope {
  const record = asRecord(unwrap(value)) ?? {};
  const chapterValue = firstValue(record, ['chapter_ids', 'selected_chapter_ids_json', 'chapters']);
  const knowledgePointValue = firstValue(record, ['knowledge_point_ids', 'selected_knowledge_point_ids_json']);
  return {
    chapter_ids: stringArray(chapterValue),
    knowledge_point_ids: stringArray(knowledgePointValue),
    include_unassigned: booleanValue(record.include_unassigned, true),
    selected_chapter_ids_json: chapterValue as string | string[] | null ?? null,
    selected_knowledge_point_ids_json: knowledgePointValue as string | string[] | null ?? null,
    updated_at: stringValue(record.updated_at, '') || null,
  };
}

export function normaliseChapter(value: unknown, index = 0): LearningChapter {
  const record = asRecord(value) ?? {};
  const name = stringValue(firstValue(record, ['name', 'title', 'label']), `章节 ${index + 1}`);
  return {
    id: stringValue(record.id, `chapter-${index + 1}`),
    name,
    title: stringValue(record.title, '') || null,
    parent_id: stringValue(record.parent_id, '') || null,
    sort_order: numberValue(firstValue(record, ['sort_order', 'order', 'position']), index),
    enabled: booleanValue(record.enabled, true),
    card_count: numberValue(firstValue(record, ['card_count', 'cards_count', 'count']), 0),
    due_count: numberValue(firstValue(record, ['due_count', 'review_count']), 0),
    knowledge_point_ids: stringArray(record.knowledge_point_ids),
    created_at: stringValue(record.created_at, '') || null,
    updated_at: stringValue(record.updated_at, '') || null,
  };
}

export function normaliseGeneration(value: unknown, index = 0): LearningGeneration {
  const record = asRecord(value) ?? {};
  const status = stringValue(record.status, 'pending');
  return {
    id: stringValue(record.id, `generation-${index + 1}`),
    status: ['pending', 'processing', 'completed', 'failed', 'cancelled'].includes(status) ? status as LearningGenerationStatus : 'pending',
    chapter_ids: stringArray(firstValue(record, ['chapter_ids', 'chapters'])),
    card_count: numberValue(firstValue(record, ['card_count', 'requested_count', 'count']), 0),
    created_count: numberValue(firstValue(record, ['created_count', 'generated_count']), 0),
    task_id: stringValue(record.task_id, '') || null,
    error_msg: stringValue(firstValue(record, ['error_msg', 'error', 'message']), '') || null,
    created_at: stringValue(record.created_at, '') || null,
    updated_at: stringValue(record.updated_at, '') || null,
  };
}

const normaliseCitation = (value: unknown, index: number): LearningCitation => {
  const record = asRecord(value) ?? {};
  return {
    id: stringValue(record.id, `citation-${index + 1}`),
    chunk_id: stringValue(record.chunk_id, '') || null,
    document_name: stringValue(firstValue(record, ['document_name', 'doc_name', 'filename']), '') || null,
    page: record.page === null ? null : numberValue(firstValue(record, ['page', 'page_number']), 0) || null,
    reference: stringValue(firstValue(record, ['reference', 'page_or_ref', 'paragraph_ref']), '') || null,
    quote: stringValue(firstValue(record, ['quote', 'snippet', 'text']), ''),
  };
};

export function normaliseRecallCard(value: unknown, index = 0): RecallCard {
  const record = asRecord(value) ?? {};
  const statusValue = stringValue(record.status, 'new');
  const lifecycleStatus = stringValue(record.lifecycle_status, '');
  const sourceRefs = firstValue(record, ['source_refs', 'citations', 'references']);
  return {
    id: stringValue(record.card_id ?? record.id, `card-${index + 1}`),
    knowledge_point_id: stringValue(record.knowledge_point_id, '') || null,
    chapter_id: stringValue(firstValue(record, ['chapter_id', 'chapterId']), '') || null,
    chapter_name: stringValue(firstValue(record, ['chapter_name', 'chapter', 'chapter_title']), '') || null,
    prompt: stringValue(firstValue(record, ['prompt', 'question', 'front', 'title']), '未命名回忆卡'),
    answer: stringValue(firstValue(record, ['answer', 'back', 'content', 'expanded_answer', 'explanation']), ''),
    hint: stringValue(firstValue(record, ['hint', 'cue']), '') || null,
    source_excerpt: stringValue(firstValue(record, ['source_excerpt', 'excerpt']), '') || null,
    source_refs: Array.isArray(sourceRefs) ? sourceRefs.map(normaliseCitation) : [],
    tags: stringArray(record.tags),
    status: ['new', 'learning', 'review', 'suspended', 'archived'].includes(statusValue) ? statusValue as LearningCardStatus : (lifecycleStatus === 'paused' ? 'suspended' : lifecycleStatus === 'archived' ? 'archived' : 'new'),
    current_revision_no: optionalNumber(record.current_revision_no),
    is_paused: booleanValue(record.is_paused, false),
    is_archived: booleanValue(record.is_archived, false),
    lock_version: optionalNumber(record.lock_version),
    due_at: stringValue(firstValue(record, ['due_at', 'due']), '') || null,
    last_reviewed_at: stringValue(firstValue(record, ['last_reviewed_at', 'reviewed_at']), '') || null,
    review_count: numberValue(firstValue(record, ['review_count', 'reviews']), 0),
    interval_days: optionalNumber(record.interval_days),
    ease_factor: optionalNumber(record.ease_factor),
    revision_id: stringValue(record.revision_id, '') || null,
    lifecycle_status: lifecycleStatus || undefined,
    is_confirmed: booleanValue(record.is_confirmed, false),
    is_ai_generated: booleanValue(record.is_ai_generated, false),
    published_revision_id: stringValue(record.published_revision_id, '') || null,
    version: optionalNumber(record.version) ?? undefined,
    created_at: stringValue(record.created_at, '') || null,
    updated_at: stringValue(record.updated_at, '') || null,
  };
}

export function normaliseAttempt(value: unknown, index = 0): TodayAttempt {
  const record = asRecord(value) ?? {};
  const nestedAttempt = asRecord(record.attempt) ?? {};
  const source = { ...record, ...nestedAttempt };
  const cardValue = firstValue(record, ['card', 'recall_card']) ?? firstValue(nestedAttempt, ['card', 'recall_card']);
  const card = cardValue ? normaliseRecallCard(cardValue, index) : null;
  const prompt = stringValue(firstValue(source, ['prompt', 'question', 'front']), card?.prompt ?? '');
  const ratingValue = stringValue(source.rating, '');
  const rawRound = firstValue(source, ['round', 'current_round', 'session', 'time_slot']);
  const roundValue = stringValue(rawRound, '');
  const numericRound = typeof rawRound === 'number' || (typeof rawRound === 'string' && /^\\d+$/.test(rawRound)) ? numberValue(rawRound, 0) : null;
  const attemptId = firstValue(record, ['attempt_id']) ?? firstValue(nestedAttempt, ['attempt_id', 'id']) ?? firstValue(nestedAttempt, ['id']);
  const dayUnitId = firstValue(record, ['day_unit_id', 'unit_id']) ?? firstValue(nestedAttempt, ['day_unit_id', 'unit_id']);
  const revealedAnswer = firstValue(record, ['answer', 'back', 'expanded_answer', 'explanation']) ?? firstValue(nestedAttempt, ['answer', 'back', 'expanded_answer', 'explanation']);
  return {
    id: stringValue(attemptId, `attempt-${index + 1}`),
    day_unit_id: stringValue(dayUnitId, '') || null,
    card_id: stringValue(firstValue(source, ['card_id', 'cardId']), card?.id ?? ''),
    study_date: stringValue(source.study_date, '') || null,
    position: numberValue(firstValue(source, ['position', 'index', 'sequence', 'display_order']), index + 1),
    display_order: optionalNumber(source.display_order),
    total: numberValue(source.total, 0),
    prompt,
    answer: stringValue(revealedAnswer, card?.answer ?? '') || null,
    hint: stringValue(firstValue(source, ['hint', 'cue']), card?.hint ?? '') || null,
    chapter_name: stringValue(firstValue(source, ['chapter_name', 'chapter', 'chapter_title']), card?.chapter_name ?? '') || null,
    outline_points: Array.isArray(source.outline_points) ? source.outline_points.map(item => stringValue(item)).filter(Boolean) : [],
    explanation: stringValue(source.explanation, '') || null,
    expanded_answer: stringValue(source.expanded_answer, '') || null,
    citations: Array.isArray(source.citations) ? source.citations.map(normaliseCitation) : [],
    source_excerpt: stringValue(source.source_excerpt, '') || null,
    round: numericRound ?? (['morning', 'noon', 'evening'].includes(roundValue) ? roundValue as LearningRound : null),
    attempt_kind: stringValue(source.attempt_kind, '') || null,
    attempt_index: optionalNumber(source.attempt_index),
    revealed: booleanValue(firstValue(source, ['revealed', 'is_revealed', 'revealed_at']), false) || Boolean(revealedAnswer),
    revealed_at: stringValue(source.revealed_at, '') || null,
    rating: ['forgotten', 'fuzzy', 'mastered'].includes(ratingValue) ? ratingValue as AttemptRating : null,
    submitted_at: stringValue(source.submitted_at, '') || null,
    status: stringValue(source.status, '') || null,
    completed: booleanValue(firstValue(source, ['completed', 'is_completed']), false) || stringValue(source.status, '') === 'rated',
    card,
  };
}

export function normaliseToday(value: unknown): TodayLearning {
  const unwrapped = unwrap(value);
  const record = asRecord(unwrapped) ?? {};
  const attemptsValue = firstValue(record, ['attempts', 'items', 'queue']);
  const attempts = Array.isArray(attemptsValue) ? attemptsValue.map(normaliseAttempt) : [];
  const queueValue = firstValue(record, ['cards', 'queue_cards', 'units']);
  const queue = Array.isArray(queueValue)
    ? queueValue.map(item => normaliseRecallCard(item))
    : [];
  const currentValue = firstValue(record, ['current_attempt', 'active_attempt', 'current', 'next_item']);
  const currentAttempt = currentValue ? normaliseAttempt(currentValue) : attempts.find(item => Boolean(item.id) && !item.completed && Boolean(item.day_unit_id)) ?? null;
  const currentCardValue = firstValue(record, ['current_card', 'active_card']);
  const currentCard = currentCardValue ? normaliseRecallCard(currentCardValue) : currentAttempt?.card ?? queue.find(card => card.id === currentAttempt?.card_id) ?? null;
  const total = numberValue(firstValue(record, ['total', 'total_count']), attempts.length || queue.length);
  const completed = numberValue(firstValue(record, ['completed', 'completed_count']), attempts.filter(item => item.completed).length);
  const remaining = numberValue(firstValue(record, ['remaining', 'remaining_count']), Math.max(total - completed, 0));
  const statusValue = stringValue(record.status, currentAttempt ? 'active' : remaining === 0 && total > 0 ? 'completed' : 'idle');
  const roundRaw = firstValue(record, ['round', 'current_round', 'session', 'time_slot']);
  const roundValue = stringValue(roundRaw, '');
  const numericRound = typeof roundRaw === 'number' || (typeof roundRaw === 'string' && /^\\d+$/.test(roundRaw)) ? numberValue(roundRaw, 0) : null;
  return {
    date: stringValue(firstValue(record, ['date', 'day', 'study_date']), new Date().toISOString().slice(0, 10)),
    study_date: stringValue(record.study_date, '') || undefined,
    status: ['idle', 'active', 'paused', 'completed'].includes(statusValue) ? statusValue as LearningTodayStatus : 'idle',
    round: numericRound ?? (['morning', 'noon', 'evening'].includes(roundValue) ? roundValue as LearningRound : null),
    current_round: numericRound ?? (['morning', 'noon', 'evening'].includes(stringValue(record.current_round, '')) ? stringValue(record.current_round) as LearningRound : null),
    current_round_number: optionalNumber(record.current_round),
    override_enabled: booleanValue(record.override_enabled, false),
    state_version: optionalNumber(record.state_version),
    total,
    completed,
    remaining,
    new_count: numberValue(firstValue(record, ['new_count', 'new_cards']), 0),
    review_count: numberValue(firstValue(record, ['review_count', 'reviews_due']), 0),
    attempts,
    queue,
    current_attempt: currentAttempt,
    current_card: currentCard,
    scope: record.scope ? normaliseScope(record.scope) : null,
    settings: record.settings ? normaliseSettings(record.settings) : null,
    day_version: optionalNumber(firstValue(record, ['day_version', 'state_version'])),
  };
}

export function normaliseLearningAction(value: unknown): LearningActionResponse {
  const record = asRecord(unwrap(value)) ?? {};
  const unit = record.unit ? normaliseAttempt({ ...(asRecord(record.unit) ?? {}), attempt: record.attempt }) : null;
  const attempt = record.attempt ? normaliseAttempt(record.attempt) : null;
  const additional = record.additional_attempt ? normaliseAttempt(record.additional_attempt) : null;
  return {
    request_id: stringValue(record.request_id, '') || undefined,
    idempotent_replay: booleanValue(record.idempotent_replay, false),
    study_date: stringValue(record.study_date, '') || undefined,
    day_version: optionalNumber(record.day_version),
    state_version: optionalNumber(record.state_version),
    round: firstValue(record, ['round', 'current_round']) as number | string | null | undefined,
    rating: ['forgotten', 'fuzzy', 'mastered'].includes(stringValue(record.rating, '')) ? stringValue(record.rating) as AttemptRating : null,
    attempt,
    additional_attempt: additional,
    unit,
    progress: asRecord(record.progress),
  };
}
