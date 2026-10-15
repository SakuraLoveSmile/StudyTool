export interface LibraryCitation {
  chunk_id: string;
  doc_id: string;
  doc_name: string;
  location: string;
  quote: string;
}

export interface LibraryChapter {
  id: string;
  doc_id: string;
  title: string;
  chunk_count: number;
  character_count: number;
}

export interface LibraryMindMap {
  title: string;
  children: { title: string; children: { title: string }[] }[];
}

export interface LibraryConcept {
  name: string;
  explanation: string;
  recitation: string;
  keywords: string[];
  citations: LibraryCitation[];
}

export interface LibraryConnection {
  accounting_concept: string;
  management_concept: string;
  explanation: string;
  application: string;
  evidence_level: 'suggestion' | 'textbook_supported';
  citations: LibraryCitation[];
}

export interface LibraryExamPoint {
  concept: string;
  question_id: string;
  year: number | null;
  stem: string;
  doc_name: string;
  location: string;
}

export interface LibraryGuide {
  id: string;
  doc_id: string;
  chapter_id: string;
  chapter_title: string;
  summary: string;
  concepts: LibraryConcept[];
  mind_map: LibraryMindMap;
  connections: LibraryConnection[];
  exam_points: LibraryExamPoint[];
  citations: LibraryCitation[];
  warnings: string[];
  created_at: string;
  source_stale: boolean;
}

export interface LibraryGuideRequest {
  doc_id: string;
  chapter_id: string;
  management_doc_ids: string[];
  exam_doc_ids: string[];
}

export interface LibraryAskRequest {
  question: string;
  doc_ids: string[];
  chapter_id?: string | null;
  previous_answer_id?: string | null;
}

export interface LibraryAnswer {
  id: string;
  question: string;
  answer: string;
  citations: LibraryCitation[];
  warnings: string[];
  doc_ids: string[];
  chapter_id: string | null;
  previous_answer_id: string | null;
  created_at: string;
  source_stale: boolean;
}
