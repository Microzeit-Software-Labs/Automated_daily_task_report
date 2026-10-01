"""The background service: restart back-off, restarts, stopping, one at a time.

Children here are throwaway ``python -c`` programs -- no database, no WhatsApp --
so this is fast and safe to run anywhere. They do start real processes, which
is the point: what is being tested is exactly that a crashed child comes back
and a stopped supervisor leaves nothing behind.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from interlock.supervisor import (
    STATUS_FILE,
    STOP_FILE,
    AlreadyRunningError,
    Backoff,
    ChildSpec,
    SingleInstance,
    Supervisor,
    console_python,
    default_specs,
    process_creation_time,
)

pytestmark = pytest.mark.unit

def _fast() -> Backoff:
    return Backoff(initial=0.05, maximum=0.2, reset_after=30)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _wait_for(condition: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class _Running:
    """A supervisor running in a background thread, stopped on exit."""

    def __init__(self, specs: list[ChildSpec], log_dir: Path) -> None:
        self.log_dir = log_dir
        self.supervisor = Supervisor(
            specs, log_dir=log_dir, backoff_factory=_fast, poll_seconds=0.05
        )
        self._thread = threading.Thread(target=self.supervisor.run, daemon=True)

    def __enter__(self) -> _Running:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.supervisor.request_stop()
        self._thread.join(timeout=30)

    def status(self) -> dict[str, object]:
        path = self.log_dir / STATUS_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (OSError, ValueError):
            return {}

    def child(self, name: str) -> dict[str, object]:
        children = self.status().get("children", {})
        assert isinstance(children, dict)
        return children.get(name, {})  # type: ignore[no-any-return]

    def log(self, name: str) -> str:
        path = self.log_dir / f"{name}.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    ).stdout
    return str(pid) in out


class TestBackoff:
    def test_doubles_up_to_the_maximum(self) -> None:
        backoff = Backoff(initial=2, maximum=60, reset_after=60)

        assert [backoff.after_exit(1) for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]

    def test_a_long_healthy_run_starts_it_over(self) -> None:
        backoff = Backoff(initial=2, maximum=60, reset_after=60)
        for _ in range(4):
            backoff.after_exit(1)

        assert backoff.after_exit(3600) == 2

    def test_a_run_just_under_the_threshold_does_not_reset(self) -> None:
        backoff = Backoff(initial=2, maximum=60, reset_after=60)
        backoff.after_exit(1)

        assert backoff.after_exit(59) == 4

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"initial": 0},
            {"initial": 10, "maximum": 5},
            {"reset_after": -1},
        ],
    )
    def test_rejects_nonsense(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            Backoff(**kwargs)


class TestRestarts:
    def test_a_child_that_exits_is_started_again(self, tmp_path: Path) -> None:
        spec = ChildSpec("crashy", _py("import sys; print('boom'); sys.exit(7)"), tmp_path)

        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: int(run.child("crashy").get("restarts", 0)) >= 2)  # type: ignore[call-overload]

            assert run.child("crashy")["last_exit_code"] == 7
        assert "crashy stopped (exit code 7)" in run.log("supervisor")

    def test_a_child_that_stays_up_is_left_alone(self, tmp_path: Path) -> None:
        spec = ChildSpec("steady", _py("import time; time.sleep(60)"), tmp_path)

        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: run.child("steady").get("state") == "running")
            pid = run.child("steady")["pid"]
            time.sleep(0.5)

            assert run.child("steady")["pid"] == pid
            assert run.child("steady")["restarts"] == 0

    def test_one_failing_child_does_not_affect_the_others(self, tmp_path: Path) -> None:
        specs = [
            ChildSpec("bad", _py("import sys; sys.exit(1)"), tmp_path),
            ChildSpec("good", _py("import time; time.sleep(60)"), tmp_path),
        ]

        with _Running(specs, tmp_path) as run:
            assert _wait_for(lambda: int(run.child("bad").get("restarts", 0)) >= 2)  # type: ignore[call-overload]

            assert run.child("good")["state"] == "running"
            assert run.child("good")["restarts"] == 0

    def test_a_program_that_cannot_start_is_retried_not_fatal(self, tmp_path: Path) -> None:
        spec = ChildSpec("ghost", ["definitely-not-a-real-program-xyz"], tmp_path)

        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: "could not start" in run.log("supervisor"))
            assert _wait_for(lambda: int(run.child("ghost").get("restarts", 0)) >= 2)  # type: ignore[call-overload]


class TestLogs:
    def test_each_childs_output_goes_to_its_own_log(self, tmp_path: Path) -> None:
        def talker(who: str) -> list[str]:
            return _py(f"import time; print('hello from {who}', flush=True); time.sleep(60)")

        specs = [ChildSpec("a", talker("a"), tmp_path), ChildSpec("b", talker("b"), tmp_path)]

        with _Running(specs, tmp_path) as run:
            assert _wait_for(
                lambda: "hello from a" in run.log("a") and "hello from b" in run.log("b")
            )

            assert "hello from b" not in run.log("a")

    def test_non_ascii_output_does_not_break_the_log(self, tmp_path: Path) -> None:
        spec = ChildSpec(
            "unicode",
            _py(
                "import sys, time; "
                "sys.stdout.buffer.write('caf\\u00e9 \\u2713\\n'.encode()); "
                "sys.stdout.flush(); time.sleep(60)"
            ),
            tmp_path,
        )

        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: "café ✓" in run.log("unicode"))


class TestStopping:
    def test_stopping_takes_every_child_down(self, tmp_path: Path) -> None:
        specs = [
            ChildSpec("one", _py("import time; time.sleep(60)"), tmp_path),
            ChildSpec("two", _py("import time; time.sleep(60)"), tmp_path),
        ]
        run = _Running(specs, tmp_path)
        with run:
            assert _wait_for(
                lambda: run.child("one").get("state") == "running"
                and run.child("two").get("state") == "running"
            )
            pids = [int(run.child(n)["pid"]) for n in ("one", "two")]  # type: ignore[call-overload]

        assert _wait_for(lambda: not any(_alive(p) for p in pids), timeout=10)
        assert run.status()["stopped"] is True

    def test_the_stop_file_shuts_it_down_cleanly(self, tmp_path: Path) -> None:
        spec = ChildSpec("steady", _py("import time; time.sleep(60)"), tmp_path)
        run = _Running([spec], tmp_path)
        run._thread.start()
        assert _wait_for(lambda: run.child("steady").get("state") == "running")
        pid = int(run.child("steady")["pid"])  # type: ignore[call-overload]

        (tmp_path / STOP_FILE).write_text("stop")
        run._thread.join(timeout=20)

        assert not run._thread.is_alive()
        assert _wait_for(lambda: not _alive(pid), timeout=10)
        assert not (tmp_path / STOP_FILE).exists()  # consumed, so it can't stop the next run

    def test_a_leftover_stop_file_does_not_stop_a_fresh_start(self, tmp_path: Path) -> None:
        (tmp_path / STOP_FILE).write_text("stale")
        spec = ChildSpec("steady", _py("import time; time.sleep(60)"), tmp_path)

        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: run.child("steady").get("state") == "running")
            time.sleep(0.3)

            assert run._thread.is_alive()

    def test_a_grandchild_is_stopped_too(self, tmp_path: Path) -> None:
        """The venv's python.exe is a launcher that starts the real interpreter;
        stopping only the launcher would leave the real one running."""
        code = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('grandchild', p.pid, flush=True); time.sleep(60)"
        )
        spec = ChildSpec("parent", _py(code), tmp_path)
        run = _Running([spec], tmp_path)
        with run:
            assert _wait_for(lambda: "grandchild" in run.log("parent"))
            line = next(x for x in run.log("parent").splitlines() if "grandchild" in x)
            grandchild = int(line.split()[-1])
            assert _alive(grandchild)

        assert _wait_for(lambda: not _alive(grandchild), timeout=10)


class TestOneAtATime:
    def test_a_second_instance_is_refused_until_the_first_lets_go(self, tmp_path: Path) -> None:
        first = SingleInstance(tmp_path / "x.lock")
        second = SingleInstance(tmp_path / "x.lock")
        first.acquire()
        try:
            with pytest.raises(AlreadyRunningError):
                second.acquire()
        finally:
            first.release()

        second.acquire()  # free again
        second.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process APIs")
class TestNothingIsLeftBehind:
    """If the supervisor is killed hard its children keep running; the next
    start must stop exactly those, and nothing else."""

    @staticmethod
    def _leftover(tmp_path: Path, *, created: int | None) -> subprocess.Popen[bytes]:
        """A stand-in for a child an earlier supervisor left running, recorded in
        the status file the way that supervisor would have."""
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        real_created = process_creation_time(orphan.pid)
        assert real_created is not None
        status = {
            "pid": 1,
            "stopped": False,
            "children": {
                "agent": {"pid": orphan.pid, "created": created or real_created, "state": "running"}
            },
        }
        (tmp_path / STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")
        return orphan

    @staticmethod
    def _start_and_stop(tmp_path: Path) -> None:
        with _Running([], tmp_path):
            time.sleep(1.0)  # long enough for the start-up sweep

    def test_a_child_left_by_a_killed_supervisor_is_stopped_on_the_next_start(
        self, tmp_path: Path
    ) -> None:
        orphan = self._leftover(tmp_path, created=None)
        try:
            self._start_and_stop(tmp_path)

            assert _wait_for(lambda: not _alive(orphan.pid), timeout=10)
            assert "was left running by an earlier supervisor" in (
                tmp_path / "supervisor.log"
            ).read_text(encoding="utf-8")
        finally:
            orphan.kill()
            orphan.wait()

    def test_an_unrelated_process_that_reused_the_pid_is_left_alone(self, tmp_path: Path) -> None:
        # Same pid, but a creation time that is not this process's: not ours.
        orphan = self._leftover(tmp_path, created=1234)
        try:
            self._start_and_stop(tmp_path)

            assert _alive(orphan.pid)
        finally:
            orphan.kill()
            orphan.wait()

    def test_a_clean_shutdown_leaves_nothing_to_sweep(self, tmp_path: Path) -> None:
        spec = ChildSpec("steady", _py("import time; time.sleep(60)"), tmp_path)
        with _Running([spec], tmp_path) as run:
            assert _wait_for(lambda: run.child("steady").get("state") == "running")

        recorded = json.loads((tmp_path / STATUS_FILE).read_text(encoding="utf-8"))
        assert recorded["children"]["steady"]["pid"] is None

    def test_the_kill_on_close_job_can_be_set_up(self) -> None:
        """Joining the job is irreversible, so try it in a throwaway process.
        (Whether it then kills the children is up to Windows and whatever job
        started us, so that part is covered by the sweep above.)"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from interlock.supervisor import join_kill_on_close_job; "
                "print(join_kill_on_close_job())",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.stdout.strip() == "True", result.stderr


class TestWhatItRuns:
    def test_runs_the_api_worker_and_agent(self, tmp_path: Path) -> None:
        specs = default_specs(root=tmp_path, port=8123)

        assert [s.name for s in specs] == ["api", "worker", "agent"]
        api = specs[0]
        assert api.argv[1:3] == ["-m", "uvicorn"]
        assert api.argv[-2:] == ["--port", "8123"]
        assert specs[1].argv[1:] == ["-m", "interlock.workers.loop"]
        assert specs[2].argv[-1] == "dist/src/index.js"
        assert specs[2].cwd == tmp_path / "apps" / "agent"

    def test_the_agent_can_be_left_out(self, tmp_path: Path) -> None:
        assert [s.name for s in default_specs(root=tmp_path, with_agent=False)] == ["api", "worker"]

    def test_children_use_a_console_python_not_pythonw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "pythonw.exe"
        fake.write_text("")
        (tmp_path / "python.exe").write_text("")
        monkeypatch.setattr(sys, "executable", str(fake))

        assert console_python() == str(tmp_path / "python.exe")
