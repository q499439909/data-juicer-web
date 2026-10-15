from pathlib import Path
import json

from fastapi.testclient import TestClient

from agent_web.app import create_app
from agent_web.config import Settings


def build_client(tmp_path: Path) -> TestClient:
    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        root=root,
        database_path=tmp_path / "app.db",
        workspace_root=tmp_path / "workspaces",
        host="127.0.0.1",
        port=8090,
        cookie_secure=False,
        ray_address="http://127.0.0.1:8265",
        task_timeout_seconds=30,
        ray_bytes_threshold=1024,
        ray_file_threshold=10,
    )
    return TestClient(create_app(settings))


def register(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "display_name": username.title(), "password": "password-123"},
    )
    assert response.status_code == 200


def test_pages_redirect_until_authenticated(tmp_path: Path):
    client = build_client(tmp_path)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert "让数据处理从一句话开始" in client.get("/login").text


def test_registration_rejects_short_password_without_server_error(tmp_path: Path):
    client = build_client(tmp_path)

    response = client.post(
        "/api/auth/register",
        json={"username": "alice", "display_name": "Alice", "password": "short"},
    )

    assert response.status_code == 422


def test_authenticated_page_identifies_account_and_offers_logout(tmp_path: Path):
    client = build_client(tmp_path)
    register(client, "alice")

    response = client.get("/")

    assert response.status_code == 200
    assert "Alice" in response.text
    assert "@alice" in response.text
    assert "普通用户" in response.text
    assert 'id="logoutButton"' in response.text
    assert "退出登录" in response.text


def test_conversation_route_blocks_cross_user_access(tmp_path: Path):
    alice = build_client(tmp_path)
    register(alice, "alice")
    conversation = alice.post("/api/conversations", json={"title": "Alice only"}).json()

    bob = TestClient(alice.app)
    register(bob, "bob")
    response = bob.get(f"/api/conversations/{conversation['id']}/messages")

    assert response.status_code == 404


def test_upload_stays_in_authenticated_users_workspace(tmp_path: Path):
    client = build_client(tmp_path)
    register(client, "alice")

    response = client.post(
        "/api/workspace/upload",
        files=[("files", ("sample.jsonl", b'{"text":"hello"}\n', "application/json"))],
        data={"relative_paths": "folder/sample.jsonl", "extract_archives": "false"},
    )

    assert response.status_code == 200
    saved = response.json()[0]
    assert saved["relative_path"] == "uploads/folder/sample.jsonl"
    assert (tmp_path / "workspaces" / saved["owner_id"] / saved["relative_path"]).is_file()


def test_durable_run_events_can_be_replayed_after_completion(tmp_path: Path):
    client = build_client(tmp_path)
    register(client, "alice")
    conversation = client.post("/api/conversations", json={"title": "Replay"}).json()
    store = client.app.state.container.store
    owner_id = conversation["owner_id"]
    run = store.create_run(owner_id, conversation["id"], "hello")
    store.add_run_event(run["id"], "status", {"message": "分析中"})
    store.add_run_event(run["id"], "chunk", {"text": "回答"})

    active = client.get(f"/api/conversations/{conversation['id']}/active-run").json()
    assert active["run"]["id"] == run["id"]
    store.finish_run(run["id"], "回答", "success", 10)
    response = client.get(f"/api/agent-runs/{run['id']}/events")

    assert response.status_code == 200
    assert '"type": "status"' in response.text
    assert '"type": "chunk"' in response.text


def test_result_manifest_route_is_owner_scoped(tmp_path: Path):
    client = build_client(tmp_path)
    register(client, "alice")
    conversation = client.post("/api/conversations", json={"title": "Result"}).json()
    store = client.app.state.container.store
    output = tmp_path / "workspaces" / conversation["owner_id"] / "outputs" / "result.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"caption": "sample", "score": 0.75}) + "\n", encoding="utf-8")
    task = store.create_task(
        conversation["owner_id"], conversation["id"], "Result", "local", "local", "test",
        {"recipe": {"dataset_path": str(output), "process": []}},
    )
    store.update_task(task["id"], status="succeeded", result_relative_path=str(output))

    manifest = client.get(f"/api/tasks/{task['id']}/result/manifest")

    assert manifest.status_code == 200
    assert manifest.json()["records"][0]["fields"]["caption"] == "sample"
