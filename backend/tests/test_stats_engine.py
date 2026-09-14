import pytest
import uuid
from app.database import db_session, init_db
from app.services.stats_engine import StatsEngine

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    with db_session() as conn:
        cursor = conn.cursor()
        # Clean test tables
        cursor.execute("DELETE FROM question_knowledge_rel")
        cursor.execute("DELETE FROM questions")
        cursor.execute("DELETE FROM knowledge_points")

        # Insert 3 knowledge points: KP1 (high freq), KP2 (low freq), KP3 (unexamined)
        kp1_id = "kp-matrix-struct"
        kp2_id = "kp-control-process"
        kp3_id = "kp-zero-exam"

        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '1.1', '矩阵制组织结构', '第一章', 'important', 1)", (kp1_id,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '2.1', '控制的基本过程', '第二章', 'normal', 2)", (kp2_id,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '3.1', '未考查的边缘知识点', '第三章', 'deferred', 3)", (kp3_id,))

        # Insert confirmed questions:
        # 2018: Q1 -> KP1
        # 2020: Q2 -> KP1, KP2 (multi-point question)
        # 2021: Q3 -> KP1 (same year duplicate check)
        # 2022: Q4 -> KP1
        # 2023: Q5 (unconfirmed, must NOT enter stats)
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q1', 2018, '2018年真题', 1, 0)")
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q2', 2020, '2020年真题跨考点', 1, 0)")
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q3', 2021, '2021年真题', 1, 0)")
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q4', 2022, '2022年真题', 1, 0)")
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q5_unconfirmed', 2023, '未确认真题', 0, 0)")
        cursor.execute("INSERT INTO questions (id, year, stem, is_confirmed, is_ai_generated) VALUES ('q6_simulated', 2024, 'AI模拟题', 1, 1)")

        # Link questions to KP
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r1', 'q1', 'kp-matrix-struct')")
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r2', 'q2', 'kp-matrix-struct')")
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r3', 'q2', 'kp-control-process')") # q2 linked to both
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r4', 'q3', 'kp-matrix-struct')")
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r5', 'q4', 'kp-matrix-struct')")
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r6', 'q5_unconfirmed', 'kp-matrix-struct')")
        cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES ('r7', 'q6_simulated', 'kp-matrix-struct')")

def test_overall_stats_calculation():
    overall = StatsEngine.get_overall_stats()
    # Confirmed years are: 2018, 2020, 2021, 2022 (4 distinct years)
    assert overall.total_recorded_years == 4
    assert overall.min_year == 2018
    assert overall.max_year == 2022
    # Missing year in [2018, 2022] is 2019
    assert 2019 in overall.missing_years
    # Confirmed questions count is 4 (q1, q2, q3, q4)
    assert overall.confirmed_questions == 4

def test_knowledge_point_frequency_and_sorting():
    stats = StatsEngine.get_knowledge_points_stats()
    assert len(stats) == 3 # KP3 (unexamined) must be preserved in catalog

    kp1 = next(s for s in stats if s.id == "kp-matrix-struct")
    kp2 = next(s for s in stats if s.id == "kp-control-process")
    kp3 = next(s for s in stats if s.id == "kp-zero-exam")

    # KP1 should have 4 confirmed questions (q1, q2, q3, q4)
    assert kp1.questions_count == 4
    assert kp1.years_count == 4
    assert kp1.recent_year == 2022

    # Recent 5-year window is [2022-4, 2022] = [2018, 2019, 2020, 2021, 2022]
    # In this window, recorded years for KP1 are: 2018, 2020, 2021, 2022 -> 4 years
    assert kp1.recent_5yr_count == 4

    # KP2 should have 1 question (q2), year 2020
    assert kp2.questions_count == 1
    assert kp2.years_count == 1
    assert kp2.recent_year == 2020
    assert kp2.recent_5yr_count == 1

    # KP3 unexamined
    assert kp3.questions_count == 0
    assert kp3.years_count == 0
    assert kp3.recent_year is None

    # Default sort verification: KP1 first, KP2 second, KP3 third
    assert stats[0].id == "kp-matrix-struct"
    assert stats[1].id == "kp-control-process"
    assert stats[2].id == "kp-zero-exam"
