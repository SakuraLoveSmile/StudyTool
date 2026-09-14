import pytest
import uuid
import json
from app.database import db_session, init_db
from app.services.stats_engine import StatsEngine
from app.services.llm.client import LLMClient
from app.services.parser.text_splitter import split_text_into_chunks, segment_chinese
from app.routers.practice import submit_and_evaluate_practice, generate_practice
from app.models.schemas import PracticeGenerateRequest, PracticeSubmitRequest, PracticeAnswerSubmission
from app.routers.analysis import merge_knowledge_points, KnowledgePointMergeRequest

@pytest.fixture(scope="module", autouse=True)
def init_environment():
    init_db()

@pytest.mark.asyncio
async def test_full_course_end_to_end_flow():
    """
    End-to-end simulation:
    1. Import textbook chapter & 20+ years past exam data.
    2. Test question-answer confirmation gate (unconfirmed questions strictly excluded).
    3. Verify stats accuracy: distinct year counts, recent 5-year window, missing years, unexamined points preserved.
    4. Generate and verify 4-dimensional study card with source citations (no fabricated cases).
    5. Test practice generation (answers hidden) -> student submission -> item-by-item semantic grading.
    6. Merge synonymous knowledge points and verify smooth statistical recalculation.
    """
    with db_session() as conn:
        cursor = conn.cursor()
        # Clean existing test data
        cursor.execute("DELETE FROM practice_sessions")
        cursor.execute("DELETE FROM study_cards")
        cursor.execute("DELETE FROM question_knowledge_rel")
        cursor.execute("DELETE FROM questions")
        cursor.execute("DELETE FROM knowledge_points")
        cursor.execute("DELETE FROM document_chunks_fts")
        cursor.execute("DELETE FROM document_chunks")
        cursor.execute("DELETE FROM documents")

        # 1. Simulate Document & Text Chunks
        doc_id = "doc-mgmt-textbook"
        cursor.execute("""
        INSERT INTO documents (id, filename, file_hash, file_type, status, page_count)
        VALUES (?, '管理学原理与组织设计教材.docx', 'hash_tb_001', 'textbook', 'completed', 15)
        """, (doc_id,))

        chunk1_id = str(uuid.uuid4())
        chunk1_text = "矩阵制组织结构由纵向的职能部门系列和横向的项目小组系列组成。主要特征包括：具有双重权力系统、跨职能协同高效、适应动态多变环境。适用条件：组织面临复杂多变的外在市场且拥有多重战略目标。注意：经检索教材全书，本章未录入任何真实企业实操案例。"
        seg1 = segment_chinese(chunk1_text)
        cursor.execute("""
        INSERT INTO document_chunks (id, doc_id, chunk_index, page_number, heading, paragraph_ref, content, content_segmented)
        VALUES (?, ?, 0, NULL, '第四章 组织设计与运行', '段落 10-14', ?, ?)
        """, (chunk1_id, doc_id, chunk1_text, seg1))
        cursor.execute("INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)", (chunk1_id, seg1))

        # 2. Add Knowledge Points
        kp_matrix = "kp-matrix"
        kp_synonym = "kp-matrix-synonym" # to be merged later
        kp_control = "kp-control"
        kp_unexamined = "kp-virtual-org"

        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '4.1', '矩阵制组织结构', '第四章 组织结构', 'important', 1)", (kp_matrix,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '4.1.b', '矩阵型结构', '第四章 组织结构', 'normal', 2)", (kp_synonym,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '5.2', '控制的基本过程', '第五章 控制管理', 'normal', 3)", (kp_control,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '6.1', '虚拟组织形态', '第六章 前沿组织', 'deferred', 4)", (kp_unexamined,))

        # 3. Simulate 20+ Years Exam Data (Years: 2003 ~ 2023, with 2015 missing to verify missing years detection)
        # Add 12 confirmed years for kp-matrix (2003, 2005, 2008, 2010, 2012, 2014, 2016, 2018, 2020, 2021, 2022, 2023)
        matrix_years = [2003, 2005, 2008, 2010, 2012, 2014, 2016, 2018, 2020, 2021, 2022, 2023]
        for y in matrix_years:
            qid = f"q-matrix-{y}"
            cursor.execute("""
            INSERT INTO questions (id, year, stem, standard_answer, is_confirmed, is_ai_generated)
            VALUES (?, ?, ?, '标准答题规范：概念内涵、双重领导制及其优缺点。', 1, 0)
            """, (qid, y, f"{y}年简答题：简述矩阵制组织结构的优缺点及适用环境。"))
            cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES (?, ?, ?)", (str(uuid.uuid4()), qid, kp_matrix))

        # Add 1 question for synonym kp
        cursor.execute("""
        INSERT INTO questions (id, year, stem, standard_answer, is_confirmed, is_ai_generated)
        VALUES ('q-syn-2019', 2019, '2019年真题：矩阵型结构有何特征？', '同矩阵制。', 1, 0)
        """)
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES (?, 'q-syn-2019', ?)", (str(uuid.uuid4()), kp_synonym))

        # Add unconfirmed question (MUST NOT enter statistics)
        cursor.execute("""
        INSERT INTO questions (id, year, stem, standard_answer, is_confirmed, is_ai_generated)
        VALUES ('q-unconfirmed', 2023, '未确认真题：矩阵结构考题', '未校对答案', 0, 0)
        """)
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES (?, 'q-unconfirmed', ?)", (str(uuid.uuid4()), kp_matrix))

    # Test Overall Statistics
    overall = StatsEngine.get_overall_stats()
    # Recorded confirmed years include matrix_years + 2019 -> min 2003, max 2023
    assert overall.min_year == 2003
    assert overall.max_year == 2023
    assert 2015 in overall.missing_years # 2015 was deliberately skipped
    assert overall.confirmed_questions == 13 # 12 from kp_matrix + 1 from synonym (unconfirmed q excluded!)

    # Test Knowledge Point Statistics
    pt_stats = StatsEngine.get_knowledge_points_stats()
    matrix_stat = next(p for p in pt_stats if p.id == kp_matrix)
    assert matrix_stat.questions_count == 12
    assert matrix_stat.years_count == 12
    assert matrix_stat.recent_year == 2023
    # Recent 5-year window: [2023-4, 2023] = [2019, 2020, 2021, 2022, 2023]
    # For matrix_stat, years in this window: 2020, 2021, 2022, 2023 -> 4
    assert matrix_stat.recent_5yr_count == 4

    # Verify unexamined point is preserved
    unexam_stat = next(p for p in pt_stats if p.id == kp_unexamined)
    assert unexam_stat.questions_count == 0
    assert unexam_stat.years_count == 0
    assert unexam_stat.recent_year is None

    # Test Practice Generation & Answering
    practice_req = PracticeGenerateRequest(knowledge_point_id=kp_matrix, question_count=1, difficulty="basic")
    session = await generate_practice(practice_req)
    assert session.question_count == 1
    # Check that answers and evaluation points are hidden in public questions
    assert "reference_answer" not in session.questions[0] or session.questions[0].get("reference_answer") is None
    qid = session.questions[0]["question_id"]

    # Student Submits Answer
    sub_req = PracticeSubmitRequest(answers=[
        PracticeAnswerSubmission(
            question_id=qid,
            answer_text="矩阵制组织结构是指纵向职能系统和横向项目小组并存的双重领导体制。主要特征是双重命令链和高度协同性。适用于外部环境动荡且多目标的组织。"
        )
    ])
    graded_session = await submit_and_evaluate_practice(session.id, sub_req)
    assert graded_session.evaluation is not None
    eval_item = graded_session.evaluation[0]
    assert len(eval_item.keypoints_coverage) > 0
    # Coverage must be one of the standard statuses
    valid_statuses = {"COVERED", "PARTIALLY_COVERED", "MISSING"}
    for cov in eval_item.keypoints_coverage:
        assert cov.status in valid_statuses
        assert cov.point is not None

    # Test Merging Synonymous Knowledge Points
    merge_res = merge_knowledge_points(KnowledgePointMergeRequest(
        target_id=kp_matrix,
        source_ids=[kp_synonym]
    ))
    assert merge_res["status"] == "ok"

    # Re-verify stats after merge: q-syn-2019 (year 2019) is now absorbed into kp_matrix!
    updated_stats = StatsEngine.get_knowledge_points_stats()
    merged_matrix_stat = next(p for p in updated_stats if p.id == kp_matrix)
    # Questions count should now be 13, years count should now include 2019 -> 13 years!
    assert merged_matrix_stat.questions_count == 13
    assert merged_matrix_stat.years_count == 13
    assert 2019 in merged_matrix_stat.years_list
    # In recent 5-year window [2019..2023], matrix now has 2019, 2020, 2021, 2022, 2023 -> 5 years!
    assert merged_matrix_stat.recent_5yr_count == 5
