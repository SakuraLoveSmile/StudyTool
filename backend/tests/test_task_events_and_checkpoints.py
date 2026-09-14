import pytest
import uuid
from fastapi.testclient import TestClient
from app.main import app
from app.database import db_session
from app.services.task_worker import TaskWorker

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

def test_task_checkpoints_persistence():
    task_type = "extract_knowledge_points"
    target_id = f"ext-{uuid.uuid4().hex[:8]}"

    # Save checkpoints for units 0 and 1
    TaskWorker.save_checkpoint(task_type, target_id, "batch_0", 0, "completed", result_count=5)
    TaskWorker.save_checkpoint(task_type, target_id, "batch_1", 1, "completed", result_count=8)

    checkpoints = TaskWorker.get_completed_checkpoints(task_type, target_id)
    assert "batch_0" in checkpoints
    assert "batch_1" in checkpoints
    assert checkpoints["batch_0"]["result_count"] == 5
    assert checkpoints["batch_1"]["result_count"] == 8

def test_task_events_api_and_pagination(client):
    task_id = TaskWorker.create_task("parse_document", "doc-100", "初始进入队列")

    TaskWorker.log_event(task_id, "stage_change", "阶段1: 读取文档", stage="read_doc")
    TaskWorker.log_event(task_id, "request_start", "第 1 批请求已发送", stage="parse_file", unit_index=1)
    TaskWorker.log_event(task_id, "response_received", "第 1 批收到响应", stage="parse_file", unit_index=1)

    # 1. Fetch all events
    res = client.get(f"/api/materials/tasks/{task_id}/events")
    assert res.status_code == 200
    data = res.json()
    assert len(data["events"]) >= 4 # created + 3 logged
    first_id = data["events"][0]["id"]
    last_id = data["last_id"]
    assert last_id is not None

    # 2. Fetch with after_id
    second_id = data["events"][1]["id"]
    res_after = client.get(f"/api/materials/tasks/{task_id}/events?after_id={second_id}")
    assert res_after.status_code == 200
    after_events = res_after.json()["events"]
    assert all(ev["id"] > second_id for ev in after_events)

def test_task_response_rich_diagnostics(client):
    task_id = TaskWorker.create_task("extract_knowledge_points", "ext-test", "正在测试")

    TaskWorker.update_progress_detail(
        task_id,
        stage="batch_extract",
        phase="working",
        unit="batch",
        total=10,
        succeeded=3,
        failed=1,
        activeUnits=[4],
        resultCount=12,
        currentScope="第 10—15 页",
        currentAction="正在处理第 4 批",
        force=True
    )
    TaskWorker.record_timing(task_id, "llm_extract_request", 1250.5)

    res = client.get(f"/api/materials/tasks/{task_id}")
    assert res.status_code == 200
    body = res.json()

    assert body["id"] == task_id
    assert body["progress_detail"] is not None
    assert body["progress_detail"]["stage"] == "batch_extract"
    assert body["progress_detail"]["succeeded"] == 3
    assert body["progress_detail"]["activeUnits"] == [4]
    assert body["progress_detail"]["resultCount"] == 12

    assert body["timing_breakdown"] is not None
    assert body["timing_breakdown"]["llm_extract_request"]["wall_time_ms"] == 1250.5
    assert body["timing_breakdown"]["llm_extract_request"]["count"] == 1
