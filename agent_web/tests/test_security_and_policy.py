from pathlib import Path

import pytest

from agent_web.auth.passwords import hash_password, verify_password
from agent_web.execution.policy import ExecutionPolicy
from agent_web.workspaces import WorkspaceManager


def test_password_hash_does_not_store_plaintext():
    encoded = hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_workspace_rejects_path_escape(tmp_path: Path):
    manager = WorkspaceManager(tmp_path)
    workspace = manager.ensure_user_workspace("user-a")

    assert manager.resolve_user_path("user-a", "uploads/data.jsonl").is_relative_to(workspace)
    with pytest.raises(ValueError, match="outside user workspace"):
        manager.resolve_user_path("user-a", "../../user-b/private.jsonl")


def test_execution_policy_enforces_ray_only_operator():
    result = ExecutionPolicy().recommend(
        operator_names=["ray_document_deduplicator"],
        total_bytes=1_000,
        file_count=1,
    )

    assert result.recommended == "ray"
    assert result.allowed == ("ray",)


def test_execution_policy_defaults_api_operator_to_local():
    result = ExecutionPolicy().recommend(
        operator_names=["api_mapper"],
        total_bytes=20_000_000_000,
        file_count=20_000,
    )

    assert result.recommended == "local"
    assert "API" in result.reason
