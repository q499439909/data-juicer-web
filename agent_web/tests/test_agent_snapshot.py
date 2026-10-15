import pytest

from data_juicer_agents.capabilities.session.orchestrator import DJSessionAgent


def test_session_agent_snapshot_round_trip_restores_state_and_memory():
    class _Memory:
        def __init__(self):
            self.loaded = None

        def state_dict(self):
            return {"messages": [{"role": "assistant", "content": "saved"}]}

        def load_state_dict(self, state, strict=True):
            assert strict is True
            self.loaded = state

    class _ReactAgent:
        def __init__(self, memory):
            self.memory = memory

    source = DJSessionAgent(use_llm_router=False, working_dir="/tmp/user-a")
    source.state.history = [{"role": "user", "content": "hello"}]
    source.state.draft_plan = {"plan_id": "plan_1", "recipe": {"process": []}}
    source._react_agent = _ReactAgent(_Memory())  # pylint: disable=protected-access

    snapshot = source.export_snapshot()

    restored_memory = _Memory()
    restored = DJSessionAgent(use_llm_router=False)
    restored._react_agent = _ReactAgent(restored_memory)  # pylint: disable=protected-access
    restored.restore_snapshot(snapshot)

    assert snapshot["version"] == 1
    assert restored.state.working_dir == "/tmp/user-a"
    assert restored.state.history == [{"role": "user", "content": "hello"}]
    assert restored.state.draft_plan["plan_id"] == "plan_1"
    assert restored._tool_runtime.state is restored.state  # pylint: disable=protected-access
    assert restored_memory.loaded == snapshot["memory"]


def test_session_agent_restore_snapshot_rejects_unknown_version():
    agent = DJSessionAgent(use_llm_router=False)

    with pytest.raises(ValueError, match="snapshot version"):
        agent.restore_snapshot({"version": 99, "state": {}})
