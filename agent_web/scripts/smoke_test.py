"""Exercise a real HTTP server and Data-Juicer task in a disposable workspace.

No API key required: seed a deterministic plan instead of requesting an LLM plan.
Use --executor ray only with an existing, same-host Ray head.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executor", choices=("local", "ray"), default="local")
    parser.add_argument("--ray-address", default="http://127.0.0.1:8265")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="dja-web-smoke-") as directory:
        data = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = dict(os.environ)
        # Do not read production .env, database, credentials, or start a Ray head.
        for key in tuple(env):
            if key.startswith("AGENT_WEB_"):
                del env[key]
        env.update({
            "AGENT_WEB_ENV_FILE": str(data / "absent.env"),
            "AGENT_WEB_DATA_DIR": str(data),
            "AGENT_WEB_HOST": "127.0.0.1",
            "AGENT_WEB_PORT": str(port),
            "AGENT_WEB_COOKIE_SECURE": "false",
            "AGENT_WEB_RAY_AUTO_START": "false",
            "AGENT_WEB_SCHEDULER_CPU_CAPACITY": "2",
            "AGENT_WEB_SCHEDULER_GPU_CAPACITY": "0",
            "AGENT_WEB_TASK_TIMEOUT": str(args.timeout),
            "RAY_ADDRESS": args.ray_address,
            "DASHSCOPE_API_KEY": "",
            "OPENAI_API_KEY": "",
            "MODELSCOPE_API_TOKEN": "",
            "PYTHONUTF8": "1",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        })
        password = secrets.token_urlsafe(20)
        catalog = subprocess.run(
            [str(Path(sys.executable).parent / ("djx.exe" if os.name == "nt" else "djx")),
             "tool", "run", "list_operator_catalog", "--input-json", "{}"],
            cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if catalog.returncode != 0 or 'text_length_filter' not in catalog.stdout:
            raise RuntimeError("Operator catalog preflight failed: " + catalog.stderr[-3000:] + catalog.stdout[-3000:])
        subprocess.run(
            [sys.executable, "-m", "agent_web.scripts.create_admin", "smoke_admin", "--password-stdin"],
            input=password + "\n", text=True, env=env, cwd=root, check=True,
        )
        with (data / "server.log").open("w+", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "agent_web.app"], cwd=root, env=env, stdout=log, stderr=log,
            )
            task_id = None
            session_cookies = None
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        if server.poll() is not None:
                            raise RuntimeError("Web server exited during startup")
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Web server readiness timed out")
                    response = client.get("/login")
                    assert response.status_code == 200
                    response = client.post("/api/auth/login", json={"username": "smoke_admin", "password": password})
                    response.raise_for_status()
                    user = response.json()
                    session_cookies = client.cookies
                    assert user["role"] == "admin"
                    assert client.get("/").status_code == 200
                    assert client.get("/static/workspace.js").status_code == 200
                    assert client.get("/admin/runs").status_code == 200
                    source = b'{"text":"x"}\n{"text":"hello world"}\n'
                    response = client.post("/api/workspace/upload", files=[("files", ("sample.jsonl", source, "application/json"))], data={"relative_paths": "sample.jsonl", "extract_archives": "false"})
                    response.raise_for_status()
                    response = client.post("/api/conversations", json={"title": "Deployment smoke"})
                    response.raise_for_status()
                    conversation = response.json()
                    # The DB is disposable and belongs only to this smoke server.
                    from agent_web.db.store import AppStore
                    store = AppStore(data / "agent_web.db")
                    dataset = data / "workspaces" / user["id"] / "uploads" / "sample.jsonl"
                    assert dataset.exists(), f"Upload did not create {dataset}"
                    plan = {
                        "plan_id": "deployment-smoke",
                        "recipe": {
                            "project_name": "deployment_smoke", "dataset_path": str(dataset),
                            "text_keys": "text", "np": 1, "use_cache": False,
                            "process": [{"text_length_filter": {"min_len": 5, "max_len": 1000}}],
                        },
                    }
                    store.save_conversation_state(user["id"], conversation["id"], {}, plan)
                    response = client.post("/api/tasks", json={"conversation_id": conversation["id"], "executor": args.executor})
                    response.raise_for_status()
                    task_id = response.json()["id"]
                    deadline = time.monotonic() + args.timeout
                    while time.monotonic() < deadline:
                        response = client.get(f"/api/tasks/{task_id}")
                        response.raise_for_status()
                        task = response.json()
                        if task["status"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.5)
                    assert task["status"] == "succeeded", json.dumps(task, ensure_ascii=False)
                    result = client.get(f"/api/tasks/{task_id}/result/download")
                    result.raise_for_status()
                    if args.executor == "local":
                        rows = [json.loads(line) for line in result.text.splitlines() if line.strip()]
                    else:
                        import io
                        import zipfile
                        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
                            rows = [json.loads(line) for name in archive.namelist() if name.endswith(".jsonl")
                                    for line in archive.read(name).decode().splitlines() if line.strip()]
                    assert [row["text"] for row in rows] == ["hello world"], rows
                    print(f"PASS: operator catalog, HTTP health, admin login, pages, static assets, upload, {args.executor} task, filtered result download")
                    return 0
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read()[-5000:], file=sys.stderr)
                raise
            finally:
                # A failed/aborted check must not leave its detached worker behind.
                if task_id and session_cookies and server.poll() is None:
                    try:
                        httpx.post(
                            f"http://127.0.0.1:{port}/api/tasks/{task_id}/cancel",
                            cookies=session_cookies, timeout=10,
                        )
                    except httpx.HTTPError:
                        pass
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
