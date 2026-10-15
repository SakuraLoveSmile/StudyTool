import { api, sessionTransport } from './client';
import type {
  LibraryAnswer, LibraryAskRequest, LibraryChapter, LibraryGuide, LibraryGuideRequest,
} from '../types/library';

async function write<T>(action: string, payload: unknown): Promise<T> {
  const response = await api.post<T>(`/library/${action}`, payload, {
    timeout: 180000,
    headers: { 'Idempotency-Key': sessionTransport.idempotencyKey(`library:${action}`, payload) },
  });
  sessionTransport.clearIdempotencyKey(`library:${action}`, payload);
  return response.data;
}

export const libraryApi = {
  async chapters(docId: string): Promise<LibraryChapter[]> {
    return (await api.get('/library/chapters', { params: { doc_id: docId } })).data;
  },
  async guides(docId: string, chapterId: string): Promise<LibraryGuide[]> {
    return (await api.get('/library/guides', { params: { doc_id: docId, chapter_id: chapterId } })).data;
  },
  generateGuide(payload: LibraryGuideRequest): Promise<LibraryGuide> {
    return write('guides', payload);
  },
  ask(payload: LibraryAskRequest): Promise<LibraryAnswer> {
    return write('ask', payload);
  },
  async answers(docId?: string): Promise<LibraryAnswer[]> {
    return (await api.get('/library/answers', { params: { doc_id: docId || undefined } })).data;
  },
};
