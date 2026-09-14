import uuid
import pytest
from app.database import db_session
from app.services.task_worker import TaskWorker
from app.routers.materials import (
    cancel_task, delete_task_record, clear_tasks, restore_cleared_tasks, get_task_history
)
from app.models.schemas import TaskClearRequest, TaskRestoreRequest
from fastapi import HTTPException

def test_cancel_pending_task():
    task_id = TaskWorker.create_task("parse_document", "doc-1", "等待解析")
    res = cancel_task(task_id)
    assert res.status == "cancelled"
    assert res.message == "已取消"

    with db_session() as conn:
        task = conn.execute("SELECT status, message FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert task["status"] == "cancelled"

def test_cancel_processing_task():
    task_id = TaskWorker.create_task("extract_questions", "doc-2", "提取中")
    TaskWorker.update_task_progress(task_id, 30, "正在调用模型", "processing")
    res = cancel_task(task_id)
    assert res.status == "cancel_requested"

    # Simulated worker detects cancellation and finalizes
    assert TaskWorker.is_cancelled(task_id) is True
    TaskWorker.finalize_cancellation(task_id)

    with db_session() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert task["status"] == "cancelled"

def test_completion_race_condition_protection():
    task_id = TaskWorker.create_task("generate_card", "kp-1", "生成中")
    TaskWorker.update_task_progress(task_id, 80, "生成中", "processing")
    cancel_task(task_id)

    # Attempting to mark completed should fail because status is cancel_requested
    completed = TaskWorker.mark_task_completed(task_id, "完成")
    assert completed is False

    with db_session() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert task["status"] == "cancel_requested"

def test_delete_and_clear_tasks():
    t1 = TaskWorker.create_task("parse_document", "d1")
    TaskWorker.mark_task_completed(t1, "完成")
    t2 = TaskWorker.create_task("parse_document", "d2")
    TaskWorker.mark_task_failed(t2, "失败")
    t3 = TaskWorker.create_task("parse_document", "d3")
    TaskWorker.update_task_progress(t3, 50, "进行中", "processing")

    # Cannot delete processing task
    with pytest.raises(HTTPException) as exc_info:
        delete_task_record(t3)
    assert exc_info.value.status_code == 400

    # Can delete completed task
    del_res = delete_task_record(t1)
    assert del_res["status"] == "ok"

    # Clear completed scope
    clear_res = clear_tasks(TaskClearRequest(scope="terminal"))
    assert clear_res.cleared_count >= 1

    # Restore cleared
    restore_res = restore_cleared_tasks(TaskRestoreRequest(clear_token=clear_res.clear_token))
    assert restore_res.restored_count == clear_res.cleared_count

def test_task_history_categories():
    t_active = TaskWorker.create_task("parse_document", "d_active")
    TaskWorker.update_task_progress(t_active, 10, "处理中", "processing")
    t_failed = TaskWorker.create_task("parse_document", "d_failed")
    TaskWorker.mark_task_failed(t_failed, "错误")
    t_done = TaskWorker.create_task("parse_document", "d_done")
    TaskWorker.mark_task_completed(t_done, "完成")

    history = get_task_history()
    assert history.active_count >= 1
    assert history.actionable_count >= 1
    assert history.history_count >= 2

    # Running filter
    running_hist = get_task_history(category="running")
    running_ids = [t.id for t in running_hist.tasks]
    assert t_active in running_ids
    assert t_done not in running_ids
