import os
import sys
import uuid
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.database import init_db, db_session
from app.services.parser.text_splitter import segment_chinese

def seed_demo_data(force_reset: bool = False):
    print("Initializing database...")
    init_db()

    with db_session() as conn:
        cursor = conn.cursor()

        if force_reset or "--reset" in sys.argv:
            print("Resetting database for demo data...")
            cursor.execute("DELETE FROM practice_sessions")
            cursor.execute("DELETE FROM study_cards")
            cursor.execute("DELETE FROM question_knowledge_rel")
            cursor.execute("DELETE FROM questions")
            cursor.execute("DELETE FROM knowledge_points")
            cursor.execute("DELETE FROM document_chunks_fts")
            cursor.execute("DELETE FROM document_chunks")
            cursor.execute("DELETE FROM documents")
        else:
            # Check if already seeded
            cursor.execute("SELECT COUNT(*) as cnt FROM knowledge_points")
            if cursor.fetchone()["cnt"] > 0:
                print("Database already contains data, skipping demo seed. (Pass --reset to re-seed)")
                return

        print("Seeding textbook document & chunks...")
        doc_id = str(uuid.uuid4())
        cursor.execute("""
        INSERT INTO documents (id, filename, file_hash, file_type, status, page_count)
        VALUES (?, '管理学基础教程（第四版）.docx', 'hash_seed_tb_001', 'textbook', 'completed', 45)
        """, (doc_id,))

        # Chunks for Matrix Structure & Control
        chunks_data = [
            (
                "第四章 组织设计与运作 / 第一节 常见组织结构类型",
                "段落 12-16",
                "【矩阵制组织结构】\n1. 定义：矩阵制组织结构又称规划-目标结构，是由纵向的职能部门系列和横向的项目小组系列复合构成的矩阵式管理组织形式。\n2. 主要特征：\n(1) 具有双重权力系统：项目成员既向职能部门经理汇报日常业务，又向项目负责人汇报项目专项工作；\n(2) 跨部门协同高效：能将不同专业的专家迅速组织在一起攻关复杂任务；\n(3) 灵活性与动态适应性强：项目完成后机构即解散，人员各归其位。\n3. 适用条件：组织面临复杂多变的不确定性环境，需要同时实现多重战略目标，且各部门资源依赖性强。\n4. 局限性：双重命令链容易造成多头领导、职权边界模糊及责任推诿。\n【特别说明：本教材本节未引用具体企业实操案例，考场答题紧扣上述四维内涵即可】"
            ),
            (
                "第五章 控制职能 / 第二节 控制的基本过程",
                "段落 24-28",
                "【控制的基本工作步骤】\n控制是管理者监控实际活动以确保其按计划完成并纠正重大偏差的过程。基本程序包含三个不可分割的阶段：\n1. 确立标准：以计划目标为依据，制定具有可操作性、可量化或可衡量的绩效标准；\n2. 衡量绩效：获取实际工作执行情况的真实信息，注重信息的前馈与实时性；\n3. 纠正偏差：深入分析偏差产生的主客观根源，采取维持现状或调整战略的针对性改进措施。"
            ),
            (
                "第二章 计划与目标管理 / 第三节 MBO核心机制",
                "段落 40-44",
                "【目标管理 (MBO)】\n由管理学大师彼得·德鲁克提出。核心要义在于‘目标制定必须上下结合，通过员工的自我控制与自我激励实现组织整体绩效’。基本要素包括明确目标、参与决策、规定期限和反馈绩效。"
            )
        ]

        for idx, (heading, pref, content) in enumerate(chunks_data):
            cid = str(uuid.uuid4())
            seg = segment_chinese(content)
            cursor.execute("""
            INSERT INTO document_chunks (id, doc_id, chunk_index, page_number, heading, paragraph_ref, content, content_segmented)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """, (cid, doc_id, idx, heading, pref, content, seg))
            cursor.execute("INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)", (cid, seg))

        print("Seeding knowledge point catalog...")
        kp1_id = "kp-matrix-org"
        kp2_id = "kp-control-process"
        kp3_id = "kp-mbo-theory"
        kp4_id = "kp-virtual-org"

        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '4.1', '矩阵制组织结构', '第四章 组织设计与运作', 'important', 1)", (kp1_id,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '5.2', '控制的基本过程', '第五章 控制职能', 'normal', 2)", (kp2_id,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '2.3', '目标管理理论 (MBO)', '第二章 计划与目标管理', 'normal', 3)", (kp3_id,))
        cursor.execute("INSERT INTO knowledge_points (id, code, name, chapter, priority_tag, sort_order) VALUES (?, '6.1', '网络型与无边界组织', '第六章 现代前沿组织形态', 'deferred', 4)", (kp4_id,))

        print("Seeding past exam questions & confirmation links...")
        # Add 3 recent years + older historical exam questions
        sample_questions = [
            (
                2023, "A卷", "简答题 2", 10.0,
                "结合实际管理情境，简述矩阵制组织结构的主要优缺点及其实用条件。",
                "【考点精解】一、矩阵制结构的内涵（由职能系列和项目系列复合构成）；二、三大优点（跨部门高效协同、灵活性高、人力资源共享）；三、两大缺点（双重指挥导致摩擦与多头领导）；四、适用条件（外部复杂动荡、多目标复合项目制企业）。",
                kp1_id
            ),
            (
                2022, "A卷", "简答题 4", 10.0,
                "简述控制工作的基本流程及其各个步骤的核心要点。",
                "【考点精解】控制的基本流程包含：1. 确立标准（可量化、可检验）；2. 衡量绩效（及时获得客观执行信息）；3. 纠正偏差（分析原因并实施纠偏对策）。",
                kp2_id
            ),
            (
                2021, "B卷", "论述题 1", 15.0,
                "论述组织设计中双重命令链的利弊，并分析矩阵制如何平衡职能与项目冲突。",
                "【考点精解】双重命令链打破了传统的统一指挥原则。优势在于加强横向沟通与专业深度，劣势在于权责边界模糊易推诿。平衡冲突的机制在于清晰界定项目经理的‘做什么/何时做’与职能主管的‘谁来做/如何做’。",
                kp1_id
            ),
            (
                2019, "A卷", "简答题 3", 10.0,
                "什么是矩阵型组织？企业在何种情况下适合采用矩阵制结构？",
                "【考点精解】重点回答定义与适用条件（外部环境不确定性高、技术复杂、具有多重关键目标）。",
                kp1_id
            ),
            (
                2018, "A卷", "简答题 1", 10.0,
                "试述德鲁克目标管理理论（MBO）的基本内涵及其实施步骤。",
                "【考点精解】目标管理是以目标为导向、以人为中心。要素：上下结合共同制定、自我控制与评价、注重成果与反馈。",
                kp3_id
            ),
            (
                2016, "A卷", "简答题 2", 10.0,
                "简述现代组织中矩阵制结构的运行机理与局限性。",
                "【考点精解】运行机理围绕纵横交叉网络，局限性在于多头领导成本较高。",
                kp1_id
            ),
            (
                2012, "B卷", "简答题 3", 10.0,
                "简答控制过程的三大主要环节。",
                "【考点精解】制定控制标准、衡量实际绩效、采取纠偏措施。",
                kp2_id
            )
        ]

        for yr, sess, qnum, score, stem, ans, kpid in sample_questions:
            qid = str(uuid.uuid4())
            cursor.execute("""
            INSERT INTO questions (id, year, session_name, question_num, question_type, score, stem, standard_answer, source_location, is_confirmed, is_ai_generated)
            VALUES (?, ?, ?, ?, 'short_answer', ?, ?, ?, '历年真题库已校对', 1, 0)
            """, (qid, yr, sess, qnum, score, stem, ans))
            cursor.execute("INSERT INTO question_knowledge_rel (id, question_id, knowledge_point_id) VALUES (?, ?, ?)", (str(uuid.uuid4()), qid, kpid))

        # Add 1 unconfirmed question for the user to review in the workbench
        cursor.execute("""
        INSERT INTO questions (id, year, session_name, question_num, question_type, score, stem, standard_answer, source_location, is_confirmed, is_ai_generated)
        VALUES ('q-unconfirmed-demo', 2024, '最新模拟卷', '一、3', 'short_answer', 10.0, '【待确认试题】简述组织变革的阻力来源及克服策略。', '【解析】个体阻力（习惯、安全感、经济因素）；组织阻力（结构惯性、群体规范）。克服策略包括沟通教育、广泛参与与谈判妥协。', '2024年最新模拟卷 P4', 0, 0)
        """)

        print("Seeding sample study card for kp1 (矩阵制组织结构)...")
        card_id = str(uuid.uuid4())
        concise = "【核心定义】由纵向职能部门系列与横向项目小组系列复合构成的双重管理组织结构。\n【三大特征】1. 双重权力系统（职能主管+项目主管）；2. 跨职能高效协同；3. 弹性动态适应。\n【核心关键词】双重命令链、项目攻关、跨部门协同、边界模糊。"
        detailed = "一、概念与内涵界定\n矩阵制组织结构（Matrix Structure）是为了完成某项专门任务，由不同职能部门抽调人员组成项目团队，形成的既有横向指挥链又有纵向指挥链的管理结构。\n\n二、核心特征分析\n1. 双重命令系统：组织成员同时接受职能经理和项目经理的指挥；\n2. 任务导向与资源弹性：跨越部门界限，项目完成后人员各自归位；\n3. 信息沟通畅通：纵横双向交织，促进专业人员深度交流。\n\n三、适用条件与局限性\n适用条件：外部环境变动频繁、技术复杂且组织拥有多重战略重点。\n主要局限：双重领导易引发指挥冲突与权力摩擦，对成员沟通协调能力要求极高。"
        answer_struct = "【标准四步答题框架】：\n第一步：给出概念定义与双重领导基本模型\n第二步：展开阐述三大核心优点（协同、敏捷、资源共享）\n第三步：指出两大固有缺陷（多头领导冲突、责任不清）\n第四步：给出适用条件及边界总结"
        confusing_cases = "【易混点辨析】矩阵制与事业部制的区别：事业部制按产品或地区实行相对独立的集中决策/分散经营，具有独立的利润核算中心；而矩阵制侧重临时或动态跨部门项目协同，并不形成永久独立核算实体。\n【企业案例情况说明】经检索上传教材《管理学基础教程》全书，本章未录入企业实操案例，标记为【无资料案例，不予补充】，杜绝编造。"
        
        import json
        cites = [
            {"chunk_id": "chunk-1", "doc_name": "管理学基础教程（第四版）.docx", "page_or_ref": "段落 12-16", "snippet": "矩阵制组织结构由纵向的职能部门系列和横向的项目小组系列复合构成..."}
        ]
        cursor.execute("""
        INSERT INTO study_cards (id, knowledge_point_id, concise_content, detailed_content, answer_structure, confusing_points_and_cases, citations_json, needs_update)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (card_id, kp1_id, concise, detailed, answer_struct, confusing_cases, json.dumps(cites, ensure_ascii=False)))

    print("Demo dataset seeded successfully!")

if __name__ == "__main__":
    seed_demo_data()
