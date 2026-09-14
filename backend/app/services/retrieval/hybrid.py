import logging
import numpy as np
from typing import List, Dict, Any, Optional
from app.database import db_session
from app.services.retrieval.fts_search import FTSSearchService
from app.services.retrieval.embedding import EmbeddingService

logger = logging.getLogger(__name__)

class HybridSearchService:
    @classmethod
    async def search(
        cls, 
        query: str, 
        top_k: int = 6, 
        doc_type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Hybrid retrieval combining FTS5 (BM25) and vector cosine similarity with RRF.
        Optionally filters by document type ('textbook', 'exam', 'answer').
        """
        # 1. FTS5 Search
        fts_results = FTSSearchService.search(query, limit=top_k * 2)
        
        # 2. Vector Search
        query_vec = await EmbeddingService.get_embedding(query)
        vec_results = cls._vector_search(query_vec, limit=top_k * 2, doc_type_filter=doc_type_filter)

        # 3. Reciprocal Rank Fusion (RRF)
        k = 60
        rrf_scores: Dict[str, float] = {}
        chunk_map: Dict[str, Dict[str, Any]] = {}

        for rank, item in enumerate(fts_results, start=1):
            cid = item["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
            chunk_map[cid] = item

        for rank, item in enumerate(vec_results, start=1):
            cid = item["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (k + rank))
            if cid not in chunk_map:
                chunk_map[cid] = item

        # Sort by RRF score descending
        sorted_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)
        
        final_results = []
        for cid in sorted_ids[:top_k]:
            item = chunk_map[cid]
            item["rrf_score"] = rrf_scores[cid]
            final_results.append(item)

        return final_results

    @classmethod
    def _vector_search(
        cls, 
        query_vec: np.ndarray, 
        limit: int = 15,
        doc_type_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Scans embeddings from document_chunks and calculates cosine similarity."""
        with db_session() as conn:
            cursor = conn.cursor()
            query_sql = """
            SELECT 
                c.id as chunk_id,
                c.doc_id,
                d.filename as doc_name,
                d.file_type,
                c.page_number,
                c.heading,
                c.paragraph_ref,
                c.content,
                c.embedding
            FROM document_chunks c
            JOIN documents d ON c.doc_id = d.id
            WHERE c.embedding IS NOT NULL
            """
            params = []
            if doc_type_filter:
                query_sql += " AND d.file_type = ?"
                params.append(doc_type_filter)

            cursor.execute(query_sql, tuple(params))
            rows = cursor.fetchall()
            if not rows:
                return []

            chunk_ids = []
            metadata_list = []
            embeddings_list = []

            for r in rows:
                blob = r["embedding"]
                if not blob:
                    continue
                vec = EmbeddingService.deserialize_embedding(blob)
                embeddings_list.append(vec)
                chunk_ids.append(r["chunk_id"])
                metadata_list.append({
                    "chunk_id": r["chunk_id"],
                    "doc_id": r["doc_id"],
                    "doc_name": r["doc_name"],
                    "page_number": r["page_number"],
                    "heading": r["heading"],
                    "paragraph_ref": r["paragraph_ref"],
                    "content": r["content"],
                    "snippet": r["content"][:140] + "..."
                })

            valid_embeddings = []
            valid_metadata = []
            for vec, meta in zip(embeddings_list, metadata_list):
                if vec.shape == query_vec.shape:
                    valid_embeddings.append(vec)
                    valid_metadata.append(meta)

            if not valid_embeddings:
                return []

            matrix = np.stack(valid_embeddings) # shape: (N, D)
            # Dot product for normalized vectors = cosine similarity
            sims = np.dot(matrix, query_vec)
            top_indices = np.argsort(-sims)[:limit]

            results = []
            for idx in top_indices:
                item = valid_metadata[idx]
                item["vec_sim"] = float(sims[idx])
                results.append(item)

            return results
