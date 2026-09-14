import logging
import sqlite3
from typing import List, Dict, Any
from app.database import db_session
from app.services.parser.text_splitter import segment_chinese

logger = logging.getLogger(__name__)

class FTSSearchService:
    @classmethod
    def search(cls, query: str, limit: int = 15) -> List[Dict[str, Any]]:
        """
        Runs full-text search on document_chunks_fts using jieba segmented query.
        Returns matched chunks with snippet and BM25 rank score.
        """
        if not query or not query.strip():
            return []

        # Tokenize query using jieba
        seg_terms = segment_chinese(query).split()
        if not seg_terms:
            return []

        # Format FTS5 query: quote each term to prevent syntax errors with punctuation
        fts_query = " OR ".join([f'"{term}"' for term in seg_terms if len(term.strip()) > 0])
        if not fts_query:
            return []

        with db_session() as conn:
            cursor = conn.cursor()
            try:
                # Rank in FTS5: lower rank value means more relevant in default bm25
                cursor.execute("""
                SELECT 
                    c.id as chunk_id,
                    c.doc_id,
                    d.filename as doc_name,
                    c.page_number,
                    c.heading,
                    c.paragraph_ref,
                    c.content,
                    snippet(document_chunks_fts, 1, '<b>', '</b>', '...', 15) as matched_snippet,
                    bm25(document_chunks_fts) as score
                FROM document_chunks_fts fts
                JOIN document_chunks c ON fts.chunk_id = c.id
                JOIN documents d ON c.doc_id = d.id
                WHERE document_chunks_fts MATCH ?
                ORDER BY score ASC
                LIMIT ?
                """, (fts_query, limit))
                
                rows = cursor.fetchall()
                results = []
                for r in rows:
                    results.append({
                        "chunk_id": r["chunk_id"],
                        "doc_id": r["doc_id"],
                        "doc_name": r["doc_name"],
                        "page_number": r["page_number"],
                        "heading": r["heading"],
                        "paragraph_ref": r["paragraph_ref"],
                        "content": r["content"],
                        "snippet": r["matched_snippet"] or (r["content"][:120] + "..."),
                        "fts_score": float(r["score"])
                    })
                return results
            except Exception as e:
                logger.error("FTS search error: %s (query: %s)", str(e), fts_query)
                return []
