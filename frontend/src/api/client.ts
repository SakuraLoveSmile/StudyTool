import axios, { AxiosError } from 'axios';
import type {
  DocumentItem, DocumentChunk, QuestionItem, KnowledgePoint,
  KnowledgePointStats, OverallStats, StudyCard, PracticeSession, TaskStatus, AnswerCandidate,
  TaskEventsResponse
} from '../types';
import type {
  CardRevision,
  CardRevisionPayload,
  LearningCardHistoryItem,
  LearningCardPayload,
  LearningCardQuery,
  LearningChapter,
  LearningChapterPayload,
  LearningGeneration,
  LearningGenerationPayload,
  LearningGenerationQuery,
  LearningScope,
  LearningScopeUpdate,
  LearningSettings,
  LearningSettingsUpdate,
  RecallCard,
  RevisionReviewPayload,
  TodayLearning,
  TodayNextPayload,
  TodayOpenPayload,
  TodayOverridePayload,
  AttemptRatePayload,
  LearningActionResponse
} from '../types/learning';
import {
  learningArray,
  normaliseChapter,
  normaliseGeneration,
  normaliseRecallCard,
  normaliseScope,
  normaliseSettings,
  normaliseToday,
  normaliseLearningAction,
  unwrapLearningPayload
} from '../types/learning';

const api = axios.create({
  baseURL: '/api',
  timeout: 60000,
  withCredentials: true,
});

let csrfToken: string | null = null;
let requestEpoch = 0;
let requestController = new AbortController();
const idempotencyKeys = new Map<string, string>();
const writeMethods = new Set(['post', 'put', 'patch', 'delete']);

export const sessionTransport = {
  get epoch() { return requestEpoch; },
  setCsrfToken(token: string | null) { csrfToken = token; },
  reset() {
    requestEpoch += 1;
    requestController.abort();
    requestController = new AbortController();
    csrfToken = null;
    idempotencyKeys.clear();
  },
  idempotencyKey(action: string, payload: unknown): string {
    const fingerprint = `${requestEpoch}:${action}:${JSON.stringify(payload)}`;
    const existing = idempotencyKeys.get(fingerprint);
    if (existing) return existing;
    const key = `${requestEpoch}-${crypto.randomUUID()}`;
    idempotencyKeys.set(fingerprint, key);
    return key;
  },
  clearIdempotencyKey(action: string, payload: unknown) {
    idempotencyKeys.delete(`${requestEpoch}:${action}:${JSON.stringify(payload)}`);
  },
  isCancelled(error: unknown) {
    return axios.isCancel(error) || (error instanceof AxiosError && error.code === 'ERR_CANCELED');
  },
};

api.interceptors.request.use((config) => {
  (config as typeof config & { ragEpoch?: number }).ragEpoch = requestEpoch;
  config.signal = requestController.signal;
  if (writeMethods.has((config.method ?? 'get').toLowerCase()) && csrfToken) {
    config.headers = config.headers ?? {};
    config.headers['X-CSRF-Token'] = csrfToken;
  }
  return config;
});

api.interceptors.response.use((response) => {
  if ((response.config as typeof response.config & { ragEpoch?: number }).ragEpoch !== requestEpoch) {
    return Promise.reject(new axios.CanceledError('Stale session response'));
  }
  return response;
}, (error) => {
  if (error?.config?.ragEpoch !== undefined && error.config.ragEpoch !== requestEpoch) {
    return Promise.reject(new axios.CanceledError('Stale session failure'));
  }
  const requestUrl = String(error?.config?.url ?? '');
  if (error?.response?.status === 401 && !requestUrl.includes('/auth/') && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('rag:auth-required'));
  }
  return Promise.reject(error);
});

export interface AuthUser { id: string; username: string; must_change_password: boolean; }
export interface AuthResponse { user: AuthUser | null; csrf_token: string | null; auth_required?: boolean; }

export const apiClient = {
  // Authentication. Session credentials stay in the HttpOnly cookie.
  login: async (username: string, password: string): Promise<AuthResponse> => {
    const res = await api.post('/auth/login', { username, password }, { signal: undefined });
    return res.data;
  },
  getCurrentUser: async (): Promise<AuthResponse> => {
    const res = await api.get('/auth/me');
    return res.data;
  },
  logout: async (): Promise<void> => {
    await api.post('/auth/logout');
  },
  changePassword: async (current_password: string, new_password: string): Promise<AuthResponse> => {
    const res = await api.post('/auth/change-password', { current_password, new_password });
    return res.data;
  },
  // Materials
  uploadMaterial: async (formData: FormData): Promise<DocumentItem> => {
    const res = await api.post('/materials/upload', formData);
    return res.data;
  },
  listMaterials: async (file_type?: string): Promise<DocumentItem[]> => {
    const res = await api.get('/materials', { params: { file_type } });
    return res.data;
  },
  getChunks: async (docId: string): Promise<DocumentChunk[]> => {
    const res = await api.get(`/materials/${docId}/chunks`);
    return res.data;
  },
  documentFileUrl: (docId: string): string => `/api/materials/${docId}/file`,
  updateChunk: async (chunkId: string, content: string): Promise<DocumentChunk> => {
    const res = await api.put(`/materials/chunks/${chunkId}`, { content });
    return res.data;
  },
  deleteDocument: async (docId: string): Promise<void> => {
    await api.delete(`/materials/${docId}`);
  },
  getTaskStatus: async (taskId: string): Promise<TaskStatus> => {
    const res = await api.get(`/materials/tasks/${taskId}`);
    return res.data;
  },
  getTaskEvents: async (taskId: string, afterId?: number): Promise<TaskEventsResponse> => {
    const res = await api.get(`/materials/tasks/${taskId}/events`, {
      params: afterId !== undefined ? { after_id: afterId } : undefined
    });
    return res.data;
  },
  getActiveTasks: async (): Promise<TaskStatus[]> => {
    const res = await api.get('/materials/tasks/active/list');
    return res.data;
  },
  cancelTask: async (taskId: string): Promise<TaskStatus> => {

    const res = await api.post(`/materials/tasks/${taskId}/cancel`);
    return res.data;
  },
  deleteTask: async (taskId: string): Promise<void> => {
    await api.delete(`/materials/tasks/${taskId}`);
  },
  retryTask: async (taskId: string): Promise<TaskStatus> => {
    const res = await api.post(`/materials/tasks/${taskId}/retry`);
    return res.data;
  },
  clearTasks: async (scope: 'completed' | 'terminal' = 'completed'): Promise<{ cleared_count: number; clear_token: string }> => {
    const res = await api.post('/materials/tasks/clear', { scope });
    return res.data;
  },
  restoreTasks: async (clear_token: string): Promise<{ restored_count: number }> => {
    const res = await api.post('/materials/tasks/restore', { clear_token });
    return res.data;
  },
  getTaskHistory: async (params?: { category?: string; page?: number; limit?: number }): Promise<{
    tasks: TaskStatus[];
    total: number;
    page: number;
    limit: number;
    active_count: number;
    actionable_count: number;
    history_count: number;
  }> => {
    const res = await api.get('/materials/tasks/history', { params });
    return res.data;
  },

  // Questions
  listQuestions: async (params?: { is_confirmed?: number; year?: number; doc_id?: string; knowledge_point_id?: string }): Promise<QuestionItem[]> => {
    const res = await api.get('/questions', { params });
    return res.data;
  },
  getQuestion: async (id: string): Promise<QuestionItem> => {
    const res = await api.get(`/questions/${id}`);
    return res.data;
  },
  updateQuestion: async (id: string, data: Partial<QuestionItem>): Promise<QuestionItem> => {
    const res = await api.put(`/questions/${id}`, data);
    return res.data;
  },
  confirmQuestion: async (id: string): Promise<QuestionItem> => {
    const res = await api.post(`/questions/${id}/confirm`);
    return res.data;
  },
  batchConfirmQuestions: async (ids: string[]): Promise<{ status: string; confirmed_count: number }> => {
    const res = await api.post('/questions/batch-confirm', ids);
    return res.data;
  },
  deleteQuestion: async (id: string): Promise<void> => {
    await api.delete(`/questions/${id}`);
  },
  listAnswerCandidates: async (): Promise<AnswerCandidate[]> => {
    const res = await api.get('/questions/answer-candidates');
    return res.data;
  },
  attachAnswerCandidate: async (questionId: string, candidateId: string): Promise<QuestionItem> => {
    const res = await api.post(`/questions/${questionId}/attach-answer/${candidateId}`);
    return res.data;
  },

  // Analysis & Knowledge Points
  getOverallStats: async (): Promise<OverallStats> => {
    const res = await api.get('/analysis/stats');
    return res.data;
  },
  getKnowledgePointsStats: async (): Promise<KnowledgePointStats[]> => {
    const res = await api.get('/analysis/point-stats');
    return res.data;
  },
  listKnowledgePoints: async (): Promise<KnowledgePoint[]> => {
    const res = await api.get('/analysis/knowledge-points');
    return res.data;
  },
  getLegacyKnowledgePoints: async (): Promise<KnowledgePoint[]> => {
    const res = await api.get('/analysis/knowledge-points/legacy');
    return res.data;
  },
  createKnowledgePoint: async (data: Partial<KnowledgePoint>): Promise<KnowledgePoint> => {
    const res = await api.post('/analysis/knowledge-points', data);
    return res.data;
  },
  updateKnowledgePoint: async (id: string, data: Partial<KnowledgePoint>): Promise<KnowledgePoint> => {
    const res = await api.put(`/analysis/knowledge-points/${id}`, data);
    return res.data;
  },
  mergeKnowledgePoints: async (target_id: string, source_ids: string[]): Promise<{ status: string; message: string }> => {
    const res = await api.post('/analysis/knowledge-points/merge', { target_id, source_ids });
    return res.data;
  },
  deleteKnowledgePoint: async (id: string): Promise<void> => {
    await api.delete(`/analysis/knowledge-points/${id}`);
  },
  restoreKnowledgePoint: async (id: string): Promise<void> => {
    await api.post(`/analysis/knowledge-points/${id}/restore`);
  },

  // Extraction
  createExtraction: async (doc_ids: string[]) => {
    const res = await api.post('/analysis/extractions', { doc_ids });
    return res.data;
  },
  getExtraction: async (extractionId: string) => {
    const res = await api.get(`/analysis/extractions/${extractionId}`);
    return res.data;
  },
  getExtractionCandidates: async (extractionId: string) => {
    const res = await api.get(`/analysis/extractions/${extractionId}/candidates`);
    return res.data;
  },
  applyCandidates: async (extractionId: string, decisions: any[]) => {
    const res = await api.post(`/analysis/extractions/${extractionId}/apply`, { decisions });
    return res.data;
  },
  getQuestionLinks: async (extractionId: string) => {
    const res = await api.get(`/analysis/extractions/${extractionId}/question-links`);
    return res.data;
  },
  applyQuestionLinks: async (extractionId: string, links: any[]) => {
    const res = await api.post(`/analysis/extractions/${extractionId}/question-links/apply`, { links });
    return res.data;
  },

  // Study Cards
  getStudyCard: async (kpid: string): Promise<StudyCard> => {
    const res = await api.get(`/cards/${kpid}`);
    return res.data;
  },
  generateStudyCard: async (kpid: string): Promise<TaskStatus> => {
    const res = await api.post('/cards/generate', { knowledge_point_id: kpid });
    return res.data;
  },
  answerAssistant: async (data: { question_id?: string; custom_stem?: string }, idempotencyKey?: string) => {
    const res = await api.post('/cards/assistant', data, idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : undefined);
    return res.data;
  },

  // Learning / Recall V1
  getLearningSettings: async (): Promise<LearningSettings> => {
    const res = await api.get('/learning/settings');
    return normaliseSettings(res.data);
  },
  updateLearningSettings: async (data: LearningSettingsUpdate): Promise<LearningSettings> => {
    const res = await api.put('/learning/settings', data);
    return normaliseSettings(res.data);
  },
  updateLearningScope: async (data: LearningScopeUpdate): Promise<LearningScope> => {
    const res = await api.put('/learning/scope', data);
    return normaliseScope(res.data);
  },
  listLearningChapters: async (): Promise<LearningChapter[]> => {
    const res = await api.get('/learning/chapters');
    return learningArray<unknown>(res.data).map((item, index) => normaliseChapter(item, index));
  },
  createLearningChapter: async (data: LearningChapterPayload): Promise<LearningChapter> => {
    const res = await api.post('/learning/chapters', data);
    return normaliseChapter(unwrapLearningPayload<unknown>(res.data));
  },
  updateLearningChapter: async (id: string, data: Partial<LearningChapterPayload>): Promise<LearningChapter> => {
    const res = await api.patch(`/learning/chapters/${encodeURIComponent(id)}`, data);
    return normaliseChapter(unwrapLearningPayload<unknown>(res.data));
  },
  createLearningGeneration: async (data: LearningGenerationPayload): Promise<LearningGeneration> => {
    const res = await api.post('/learning/generations', data);
    return normaliseGeneration(unwrapLearningPayload<unknown>(res.data));
  },
  listLearningGenerations: async (params?: LearningGenerationQuery): Promise<LearningGeneration[]> => {
    const res = await api.get('/learning/generations', { params });
    return learningArray<unknown>(res.data).map((item, index) => normaliseGeneration(item, index));
  },
  listRecallCards: async (params?: LearningCardQuery): Promise<RecallCard[]> => {
    const res = await api.get('/learning/cards', { params });
    return learningArray<unknown>(res.data).map((item, index) => normaliseRecallCard(item, index));
  },
  createRecallCard: async (data: LearningCardPayload): Promise<RecallCard> => {
    const res = await api.post('/learning/cards', data);
    return normaliseRecallCard(unwrapLearningPayload<unknown>(res.data));
  },
  createCardRevision: async (cardId: string, data: CardRevisionPayload): Promise<CardRevision> => {
    const res = await api.post(`/learning/cards/${encodeURIComponent(cardId)}/revisions`, data);
    return unwrapLearningPayload<CardRevision>(res.data);
  },
  reviewCardRevision: async (data: RevisionReviewPayload): Promise<CardRevision> => {
    const res = await api.post('/learning/revisions/review', data);
    const value = unwrapLearningPayload<unknown>(res.data);
    return (Array.isArray(value) ? value[0] : value) as CardRevision;
  },
  updateRecallCard: async (cardId: string, data: Partial<LearningCardPayload>): Promise<RecallCard> => {
    const res = await api.patch(`/learning/cards/${encodeURIComponent(cardId)}`, data);
    return normaliseRecallCard(unwrapLearningPayload<unknown>(res.data));
  },
  publishRecallCard: async (cardId: string, data?: { revision_id?: string; reset_progress?: boolean; expected_version?: number }): Promise<RecallCard> => {
    const res = await api.post(`/learning/cards/${encodeURIComponent(cardId)}/publish`, data ?? {});
    return normaliseRecallCard(unwrapLearningPayload<unknown>(res.data));
  },
  setRecallCardLifecycle: async (cardId: string, status: string, expected_version?: number): Promise<RecallCard> => {
    const res = await api.post(`/learning/cards/${encodeURIComponent(cardId)}/lifecycle`, { status, expected_version });
    return normaliseRecallCard(unwrapLearningPayload<unknown>(res.data));
  },
  getRecallCardHistory: async (cardId: string): Promise<LearningCardHistoryItem[]> => {
    const res = await api.get(`/learning/cards/${encodeURIComponent(cardId)}/history`);
    return learningArray<LearningCardHistoryItem>(res.data, ['items', 'history', 'revisions']);
  },
  getTodayLearning: async (): Promise<TodayLearning> => {
    const res = await api.get('/learning/today');
    return normaliseToday(res.data);
  },
  openTodayLearning: async (data?: TodayOpenPayload): Promise<TodayLearning> => {
    const res = await api.post('/learning/today/open', data ?? {});
    return normaliseToday(res.data);
  },
  nextTodayLearning: async (data?: TodayNextPayload): Promise<TodayLearning> => {
    const res = await api.post('/learning/today/next', data ?? {});
    return normaliseToday(res.data);
  },
  overrideTodayLearning: async (data: TodayOverridePayload): Promise<TodayLearning> => {
    const res = await api.post('/learning/today/override', data);
    return normaliseToday(res.data);
  },
  revealLearningAttempt: async (attemptId: string, data?: { day_unit_id?: string; day_version?: number }): Promise<LearningActionResponse> => {
    const res = await api.post(`/learning/attempts/${encodeURIComponent(attemptId)}/reveal`, data ?? {});
    return normaliseLearningAction(res.data);
  },
  rateLearningAttempt: async (attemptId: string, data: AttemptRatePayload & { day_unit_id?: string; day_version?: number }): Promise<LearningActionResponse> => {
    const res = await api.post(`/learning/attempts/${encodeURIComponent(attemptId)}/rate`, data);
    return normaliseLearningAction(res.data);
  },
  advanceTodayLearning: async (data?: { day_version?: number }): Promise<TodayLearning> => {
    const res = await api.post('/learning/today/advance', data ?? {});
    return normaliseToday(res.data);
  },
  pauseTodayLearning: async (data?: { day_version?: number }): Promise<TodayLearning> => {
    const res = await api.post('/learning/today/pause', data ?? {});
    return normaliseToday(res.data);
  },

  // Practice & Grading
  generatePractice: async (knowledge_point_id: string, question_count: number = 1, difficulty: string = 'basic', idempotencyKey?: string): Promise<PracticeSession> => {
    const res = await api.post('/practice/generate', { knowledge_point_id, question_count, difficulty }, idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : undefined);
    return res.data;
  },
  listPracticeSessions: async (): Promise<PracticeSession[]> => {
    const res = await api.get('/practice/sessions');
    return res.data;
  },
  getPracticeSession: async (id: string): Promise<PracticeSession> => {
    const res = await api.get(`/practice/sessions/${id}`);
    return res.data;
  },
  submitPractice: async (sessionId: string, answers: Array<{ question_id: string; answer_text: string }>, idempotencyKey?: string): Promise<PracticeSession> => {
    const res = await api.post(`/practice/sessions/${sessionId}/submit`, { answers }, idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : undefined);
    return res.data;
  },

  // Config status
  getConfigStatus: async () => {
    const res = await api.get('/config/status');
    return res.data;
  }
};
