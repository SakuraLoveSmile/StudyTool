import logging
from typing import List, Dict, Any, Optional
from app.database import db_session
from app.models.schemas import KnowledgePointStats, OverallStatsResponse, QuestionResponse

logger = logging.getLogger(__name__)

class StatsEngine:
    @classmethod
    def get_overall_stats(cls) -> OverallStatsResponse:
        """
        Calculates global dataset statistics:
        - Recorded years and missing years
        - 5-year window based on the latest year in DB
        - Confirmed vs total questions count
        """
        with db_session() as conn:
            cursor = conn.cursor()
            
            # Distinct confirmed years
            cursor.execute("""
            SELECT DISTINCT year 
            FROM questions 
            WHERE is_confirmed = 1 AND is_ai_generated = 0 AND year IS NOT NULL
            ORDER BY year ASC
            """)
            year_rows = cursor.fetchall()
            recorded_years = [r["year"] for r in year_rows]

            # Questions count
            cursor.execute("SELECT COUNT(*) as cnt FROM questions WHERE is_ai_generated = 0")
            total_questions = cursor.fetchone()["cnt"]

            cursor.execute("SELECT COUNT(*) as cnt FROM questions WHERE is_confirmed = 1 AND is_ai_generated = 0")
            confirmed_questions = cursor.fetchone()["cnt"]

            # Knowledge points count
            cursor.execute("SELECT COUNT(*) as cnt FROM knowledge_points WHERE is_deleted = 0")
            kp_count = cursor.fetchone()["cnt"]

            if not recorded_years:
                return OverallStatsResponse(
                    total_recorded_years=0,
                    min_year=None,
                    max_year=None,
                    recorded_years=[],
                    missing_years=[],
                    recent_5yr_window=[],
                    total_questions=total_questions,
                    confirmed_questions=confirmed_questions,
                    knowledge_points_count=kp_count
                )

            min_year = min(recorded_years)
            max_year = max(recorded_years)
            
            # Identify missing years in the range [min_year, max_year]
            all_span_years = list(range(min_year, max_year + 1))
            missing_years = [y for y in all_span_years if y not in recorded_years]

            # Recent 5-year window: [max_year - 4, max_year]
            recent_5yr_window = [y for y in range(max_year - 4, max_year + 1) if y in recorded_years]

            return OverallStatsResponse(
                total_recorded_years=len(recorded_years),
                min_year=min_year,
                max_year=max_year,
                recorded_years=recorded_years,
                missing_years=missing_years,
                recent_5yr_window=recent_5yr_window,
                total_questions=total_questions,
                confirmed_questions=confirmed_questions,
                knowledge_points_count=kp_count
            )

    @classmethod
    def get_knowledge_points_stats(cls) -> List[KnowledgePointStats]:
        """
        Calculates stats for all knowledge points:
        - Sorted by: years_count DESC, recent_5yr_count DESC, recent_year DESC, sort_order ASC
        - All unexamined knowledge points are retained
        """
        overall = cls.get_overall_stats()
        recent_window_set = set(range(overall.max_year - 4, overall.max_year + 1)) if overall.max_year else set()

        with db_session() as conn:
            cursor = conn.cursor()

            # 1. Fetch all non-deleted knowledge points
            cursor.execute("""
            SELECT id, code, name, chapter, priority_tag, sort_order,
                   source_type, source_status, source_reference
            FROM knowledge_points
            WHERE is_deleted = 0
            ORDER BY sort_order ASC, name ASC
            """)
            kp_rows = cursor.fetchall()
            
            # 2. Fetch all confirmed question associations
            cursor.execute("""
            SELECT 
                rel.knowledge_point_id,
                q.id as question_id,
                q.doc_id,
                q.year,
                q.session_name,
                q.question_num,
                q.question_type,
                q.score,
                q.stem,
                q.standard_answer,
                q.source_location,
                q.source_chunk_id,
                q.source_page,
                q.source_paragraph_ref,
                q.answer_source_chunk_id,
                q.answer_source_doc_id,
                q.answer_source_page,
                q.answer_source_paragraph_ref,
                q.needs_review,
                q.review_reason,
                q.is_confirmed,
                q.is_ai_generated,
                q.created_at
            FROM question_knowledge_rel rel
            JOIN questions q ON rel.question_id = q.id
            WHERE q.is_confirmed = 1 AND q.is_ai_generated = 0
            """)
            rel_rows = cursor.fetchall()

            # Group questions by knowledge_point_id
            kp_questions_map: Dict[str, Dict[str, QuestionResponse]] = {}
            for r in rel_rows:
                kpid = r["knowledge_point_id"]
                if kpid not in kp_questions_map:
                    kp_questions_map[kpid] = {}
                
                qid = r["question_id"]
                # De-duplicate questions per knowledge point
                if qid not in kp_questions_map[kpid]:
                    kp_questions_map[kpid][qid] = QuestionResponse(
                        id=r["question_id"],
                        doc_id=r["doc_id"],
                        year=r["year"],
                        session_name=r["session_name"],
                        question_num=r["question_num"],
                        question_type=r["question_type"],
                        score=r["score"],
                        stem=r["stem"],
                        standard_answer=r["standard_answer"],
                        source_location=r["source_location"],
                        source_chunk_id=r["source_chunk_id"],
                        source_page=r["source_page"],
                        source_paragraph_ref=r["source_paragraph_ref"],
                        answer_source_chunk_id=r["answer_source_chunk_id"],
                        answer_source_doc_id=r["answer_source_doc_id"],
                        answer_source_page=r["answer_source_page"],
                        answer_source_paragraph_ref=r["answer_source_paragraph_ref"],
                        needs_review=r["needs_review"],
                        review_reason=r["review_reason"],
                        is_confirmed=r["is_confirmed"],
                        is_ai_generated=r["is_ai_generated"],
                        knowledge_point_ids=[],
                        knowledge_point_names=[],
                        created_at=str(r["created_at"])
                    )

            stats_list: List[KnowledgePointStats] = []
            for kp in kp_rows:
                kpid = kp["id"]
                q_dict = kp_questions_map.get(kpid, {})
                q_list = list(q_dict.values())
                
                # Calculate metrics
                questions_count = len(q_list)
                years_set = set(q.year for q in q_list if q.year is not None)
                years_count = len(years_set)
                recent_year = max(years_set) if years_set else None
                recent_5yr_count = len([y for y in years_set if y in recent_window_set])
                years_list = sorted(list(years_set), reverse=True)

                stats_list.append(KnowledgePointStats(
                    id=kpid,
                    code=kp["code"],
                    name=kp["name"],
                    chapter=kp["chapter"],
                    priority_tag=kp["priority_tag"] or "normal",
                    source_type=kp["source_type"] or "pending_verification",
                    source_status=kp["source_status"] or "valid",
                    source_reference=kp["source_reference"],
                    questions_count=questions_count,
                    years_count=years_count,
                    recent_year=recent_year,
                    recent_5yr_count=recent_5yr_count,
                    years_list=years_list,
                    questions=q_list
                ))

            # Sort: years_count DESC, recent_5yr_count DESC, recent_year DESC, sort_order ASC
            stats_list.sort(
                key=lambda s: (
                    -s.years_count,
                    -s.recent_5yr_count,
                    -(s.recent_year or 0),
                    s.code or ""
                )
            )

            return stats_list
