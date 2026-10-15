from pathlib import Path

import pytest

from agent_web.db.store import AppStore, ResourceNotFound


def test_conversation_store_enforces_owner(tmp_path: Path):
    store = AppStore(tmp_path / "app.db")
    store.initialize()
    user_a = store.create_user("alice", "Alice", "hash-a")
    user_b = store.create_user("bob", "Bob", "hash-b")
    conversation = store.create_conversation(user_a["id"], "Private chat")

    assert store.require_conversation(user_a["id"], conversation["id"])["title"] == "Private chat"
    with pytest.raises(ResourceNotFound):
        store.require_conversation(user_b["id"], conversation["id"])


def test_hidden_conversation_can_be_restored(tmp_path: Path):
    store = AppStore(tmp_path / "app.db")
    store.initialize()
    user = store.create_user("alice", "Alice", "hash-a")
    conversation = store.create_conversation(user["id"], "Recoverable")

    store.set_conversation_hidden(user["id"], conversation["id"], True)
    assert store.list_conversations(user["id"], include_hidden=False) == []
    assert store.list_conversations(user["id"], include_hidden=True)[0]["hidden"] == 1

    store.set_conversation_hidden(user["id"], conversation["id"], False)
    assert store.list_conversations(user["id"], include_hidden=False)[0]["title"] == "Recoverable"


def test_run_events_are_replayable_and_owner_isolated(tmp_path: Path):
    store = AppStore(tmp_path / "app.db")
    store.initialize()
    alice = store.create_user("alice", "Alice", "hash-a")
    bob = store.create_user("bob", "Bob", "hash-b")
    conversation = store.create_conversation(alice["id"], "Durable run")
    run = store.create_run(alice["id"], conversation["id"], "clean data")
    first = store.add_run_event(run["id"], "status", {"message": "分析中"})
    second = store.add_run_event(run["id"], "chunk", {"text": "结果"})

    replay = store.list_run_events(alice["id"], run["id"], first["sequence"])
    assert [event["sequence"] for event in replay] == [second["sequence"]]
    assert replay[0]["payload"] == {"text": "结果"}
    with pytest.raises(ResourceNotFound):
        store.list_run_events(bob["id"], run["id"])
