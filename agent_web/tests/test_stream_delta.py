from agent_web.agent_runtime.runtime import StreamDelta


def test_cumulative_stream_snapshots_become_true_deltas():
    normalizer = StreamDelta()

    deltas = [normalizer.push(value) for value in ("好", "好的", "好的，我", "好的，我看到")]

    assert deltas == ["好", "的", "，我", "看到"]
    assert "".join(deltas) == "好的，我看到"


def test_native_delta_stream_is_preserved():
    normalizer = StreamDelta()

    deltas = [normalizer.push(value) for value in ("好的", "，我", "看到", "图片")]

    assert deltas == ["好的", "，我", "看到", "图片"]
