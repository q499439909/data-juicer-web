import json
from pathlib import Path

import pytest

from agent_web.results import ResultPresenter
from agent_web.workspaces import WorkspaceManager


def test_result_presenter_builds_media_records_and_numeric_charts(tmp_path: Path):
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    workspace = workspaces.ensure_user_workspace("usr_alice")
    result_dir = workspace / "outputs" / "task-result"
    result_dir.mkdir(parents=True)
    image = result_dir / "sample.png"
    image.write_bytes(b"not-a-real-png")
    output = result_dir / "result.jsonl"
    output.write_text("\n".join([
        json.dumps({"image": "sample.png", "caption": "一只猫", "score": 0.8}),
        json.dumps({"image": "sample.png", "caption": "一只狗", "score": 0.4}),
    ]), encoding="utf-8")
    task = {"id": "task_1", "name": "图文清洗", "result_relative_path": str(output)}

    manifest = ResultPresenter(workspaces).build("usr_alice", task)

    assert manifest["available"] is True
    assert manifest["records"][0]["media"][0]["path"].endswith("sample.png")
    score = next(item for item in manifest["numeric_stats"] if item["field"] == "score")
    assert score["count"] == 2
    assert score["mean"] == pytest.approx(0.6)


def test_result_file_resolution_cannot_escape_user_workspace(tmp_path: Path):
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    presenter = ResultPresenter(workspaces)
    workspaces.ensure_user_workspace("usr_alice")

    with pytest.raises(ValueError, match="outside user workspace"):
        presenter.resolve_file("usr_alice", "../../secret.txt")
