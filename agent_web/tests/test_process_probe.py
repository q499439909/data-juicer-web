import subprocess
import sys

from agent_web.execution.runner import TaskRunner


def test_task_liveness_probe_does_not_signal_or_kill_the_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert TaskRunner._pid_exists(process.pid)
        assert process.poll() is None
        assert not TaskRunner._pid_exists(0)
        assert not TaskRunner._pid_exists(-1)
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert not TaskRunner._pid_exists(process.pid)
