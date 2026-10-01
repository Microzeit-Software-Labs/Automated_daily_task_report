"""The background service: one process that keeps Interlock running.

It starts the API (which also serves the web UI), the worker and the WhatsApp
agent as hidden child processes, restarts any that stops, and writes each one's
output to its own log file. Run it with ``python -m interlock.supervisor`` --
normally through ``scripts/start-interlock.ps1`` or the "Interlock" scheduled
task (``scripts/register-autostart.ps1``), both of which use ``pythonw.exe`` so
there is no console window.

Deliberately plain, like ``workers/loop.py``: no state of its own beyond the
children it is watching. Everything that matters is already crash-safe
(the worker's leases, the agent's outbox), so "restart whatever died" is enough.

* **Restarts back off** (``Backoff``): 2 s, 4 s, ... up to 60 s, and reset once a
  child has stayed up for a minute. A child that is broken (database down at
  boot, a bad ``.env``) retries calmly instead of spinning.
* **One supervisor at a time.** A lock held by the running supervisor (released by
  the OS if it dies) makes a second one exit at once, so a double-click or a
  second logon task can never start duplicate children. The WhatsApp agent has
  its own lock too; two agents on one session is the one thing that must never
  happen.
* **Nothing is left behind.** A clean stop takes every child (and its children)
  down. If the supervisor is killed hard instead, two things clean up: on Windows
  it has joined a job object that kills its children with it (best effort: a
  parent job that allows breakaway, as Node- and Electron-launched shells have,
  silently defeats it), and the *next* start stops any child the status file says
  was left running -- matched by pid *and* process creation time, so a recycled
  pid is never mistaken for one of ours.
* **A stop file** (``logs/supervisor.stop``) asks it to shut down cleanly; that is
  how ``start-interlock.ps1 -Stop`` works, since a hidden process has no window
  to close.
* **A status file** (``logs/supervisor-status.json``) says what is running, for
  ``start-interlock.ps1 -Status``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import IO

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"

STOP_FILE = "supervisor.stop"
STATUS_FILE = "supervisor-status.json"
LOCK_FILE = "supervisor.lock"

LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3

# A Windows flag: don't open a console window for the child.
_CREATE_NO_WINDOW = 0x08000000


# -- restart back-off ---------------------------------------------------------


class Backoff:
    """How long to wait before restarting a child that stopped.

    The wait doubles each time it stops again quickly, and starts over once a
    run lasted at least ``reset_after`` seconds -- a child that ran for an hour
    and then died gets a prompt restart, not the delay its last bad streak earned.
    """

    def __init__(
        self, *, initial: float = 2.0, maximum: float = 60.0, reset_after: float = 60.0
    ) -> None:
        if initial <= 0 or maximum < initial or reset_after < 0:
            raise ValueError("Backoff needs 0 < initial <= maximum and reset_after >= 0.")
        self._initial = initial
        self._maximum = maximum
        self._reset_after = reset_after
        self._next = initial

    def after_exit(self, ran_for: float) -> float:
        """Seconds to wait before restarting a child that just stopped after
        running for ``ran_for`` seconds."""
        if ran_for >= self._reset_after:
            self._next = self._initial
        delay = self._next
        self._next = min(self._next * 2, self._maximum)
        return delay


# -- children -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ChildSpec:
    name: str
    argv: Sequence[str]
    cwd: Path
    env: Mapping[str, str] | None = None
    """Extra environment on top of the supervisor's own."""


class _Child:
    """One managed process and the bookkeeping around restarting it."""

    def __init__(self, spec: ChildSpec, backoff: Backoff, logger: logging.Logger) -> None:
        self.spec = spec
        self.backoff = backoff
        self.logger = logger
        self.process: subprocess.Popen[bytes] | None = None
        self.pump: threading.Thread | None = None
        self.started_at = 0.0
        self.restart_at = 0.0
        self.restarts = 0
        self.last_exit: int | None = None
        self.created: int | None = None
        """The running process's creation time, to recognise it later."""


def process_creation_time(pid: int) -> int | None:
    """When process ``pid`` was created (a Windows FILETIME), or None if there
    is no such process. Together with the pid this identifies one process for
    good: a pid can be reused later, the pair cannot."""
    if sys.platform != "win32":  # pragma: no cover - the project is Windows-first
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pointer = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, pointer, pointer, pointer, pointer]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        times = (created, exited, kernel, user)
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _tree_kill(pid: int) -> None:
    """Stop a process and everything it started. On Windows the venv's
    ``python.exe`` is a launcher that starts the real interpreter as its child,
    so stopping only the launcher would leave the real one running."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    else:  # pragma: no cover - the project is Windows-first
        os.kill(pid, signal.SIGKILL)


class Supervisor:
    """Starts ``specs``, keeps them running, stops them on request."""

    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        log_dir: Path = LOG_DIR,
        backoff_factory: Callable[[], Backoff] = Backoff,
        poll_seconds: float = 0.5,
        echo: bool = False,
    ) -> None:
        self._log_dir = log_dir
        self._poll = poll_seconds
        self._echo = echo
        self._stop = threading.Event()
        self._started = time.time()
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = _make_logger("supervisor", log_dir)
        self._children = [
            _Child(spec, backoff_factory(), _make_logger(spec.name, log_dir)) for spec in specs
        ]

    # -- control ----------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def note(self, message: str) -> None:
        """Add a line to ``supervisor.log``."""
        self._log.warning("%s", message)

    def run(self) -> None:
        """Run until a stop is requested (``request_stop``, or the stop file)."""
        stop_file = self._log_dir / STOP_FILE
        stop_file.unlink(missing_ok=True)  # a leftover from last time must not stop us at once
        self._log.info("supervisor started (pid %s): %s", os.getpid(), self._summary())
        try:
            self._stop_leftovers()
            while not self._stop.is_set():
                if stop_file.exists():
                    self._log.info("stop file found; shutting down")
                    break
                self._tick(time.monotonic())
                self._write_status()
                self._stop.wait(self._poll)
        except Exception:
            self._log.exception("supervisor crashed")
            raise
        finally:
            self._shutdown()
            stop_file.unlink(missing_ok=True)
            self._log.info("supervisor stopped")
            for logger in [self._log, *(c.logger for c in self._children)]:
                _close_logger(logger)

    def _summary(self) -> str:
        return ", ".join(c.spec.name for c in self._children)

    def _stop_leftovers(self) -> None:
        """Stop children an earlier supervisor left running (it was killed hard).
        Only a process whose pid *and* creation time match what the status file
        recorded is touched, so an unrelated process that was handed the same pid
        since is safe."""
        try:
            previous = json.loads((self._log_dir / STATUS_FILE).read_text(encoding="utf-8"))
            recorded = previous.get("children", {})
        except (OSError, ValueError, AttributeError):
            return
        for name, info in recorded.items():
            pid, created = info.get("pid"), info.get("created")
            if pid and created is not None and process_creation_time(int(pid)) == created:
                self._log.warning(
                    "%s (pid %s) was left running by an earlier supervisor; stopping it", name, pid
                )
                _tree_kill(int(pid))

    # -- one pass ---------------------------------------------------------

    def _tick(self, now: float) -> None:
        for child in self._children:
            process = child.process
            if process is None:
                if now >= child.restart_at:
                    self._start(child, now)
                continue
            code = process.poll()
            if code is None:
                continue
            ran_for = now - child.started_at
            delay = child.backoff.after_exit(ran_for)
            child.last_exit = code
            child.process = None
            child.restart_at = now + delay
            child.restarts += 1
            self._log.warning(
                "%s stopped (exit code %s) after %.0f s; restarting in %.0f s",
                child.spec.name,
                code,
                ran_for,
                delay,
            )

    def _start(self, child: _Child, now: float) -> None:
        spec = child.spec
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        env.update(spec.env or {})
        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=spec.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except OSError as error:
            # e.g. node is not installed. Back off like any other failure.
            delay = child.backoff.after_exit(0.0)
            child.restart_at = now + delay
            child.restarts += 1
            self._log.error(
                "%s could not start (%s); trying again in %.0f s", spec.name, error, delay
            )
            return
        child.process = process
        child.created = process_creation_time(process.pid)
        child.started_at = now
        child.pump = threading.Thread(
            target=self._pump,
            args=(process.stdout, child.logger, spec.name),
            name=f"pump-{spec.name}",
            daemon=True,
        )
        child.pump.start()
        self._log.info("%s started (pid %s)", spec.name, process.pid)

    def _pump(self, stream: IO[bytes] | None, logger: logging.Logger, name: str) -> None:
        """Copy a child's output into its own rotating log."""
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.info("%s", line)
                if self._echo:
                    print(f"[{name}] {line}", flush=True)

    # -- shutdown ---------------------------------------------------------

    def _shutdown(self) -> None:
        running = [c for c in self._children if c.process is not None]
        for child in running:
            assert child.process is not None
            self._log.info("stopping %s (pid %s)", child.spec.name, child.process.pid)
            _tree_kill(child.process.pid)
        for child in running:
            assert child.process is not None
            try:
                child.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - taskkill /F is final
                self._log.error("%s did not stop", child.spec.name)
            if child.pump is not None:
                child.pump.join(timeout=2)
            child.process = None
        self._write_status(final=True)

    # -- status -----------------------------------------------------------

    def _write_status(self, *, final: bool = False) -> None:
        now = time.monotonic()
        children = {}
        for child in self._children:
            running = child.process is not None and child.process.poll() is None
            children[child.spec.name] = {
                "state": "running" if running else "starting" if not final else "stopped",
                "pid": child.process.pid if running and child.process else None,
                "created": child.created if running else None,
                "running_for_seconds": round(now - child.started_at) if running else 0,
                "restarts": child.restarts,
                "last_exit_code": child.last_exit,
            }
        status = {
            "pid": os.getpid(),
            "started_at": self._started,
            "updated_at": time.time(),
            "stopped": final,
            "children": children,
        }
        target = self._log_dir / STATUS_FILE
        scratch = target.with_suffix(".tmp")
        try:
            scratch.write_text(json.dumps(status), encoding="utf-8")
            scratch.replace(target)  # atomic: a reader never sees half a file
        except OSError:  # pragma: no cover - the status file is a convenience only
            pass


def _make_logger(name: str, log_dir: Path) -> logging.Logger:
    """A logger that writes ``<log_dir>/<name>.log``, rotating by size. Keyed by
    the directory as well as the name so two supervisors (the tests run several)
    never share a file handle."""
    logger = logging.getLogger(f"interlock.supervisor.{log_dir}.{name}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{name}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    return logger


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


# -- one supervisor at a time -------------------------------------------------


class AlreadyRunningError(Exception):
    pass


class SingleInstance:
    """An exclusive lock on a file, held for the life of the process and
    released by the OS if the process dies -- so a crash never leaves a stale
    lock behind the way a pid file would."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            _lock(handle)
        except OSError as error:
            handle.close()
            raise AlreadyRunningError(str(self._path)) from error
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            try:
                _unlock(self._file)
            finally:
                self._file.close()
                self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - the project is Windows-first
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# -- Windows job object: nothing outlives the supervisor -----------------------


def join_kill_on_close_job() -> bool:
    """Put this process in a job that kills all its members when it ends.

    Children inherit membership, so if the supervisor is killed hard (Task
    Manager, a crash) the OS takes the API, worker and agent down with it,
    rather than leaving an orphaned agent holding the WhatsApp session. Returns
    False when it can't (not Windows, or the platform refused), which only
    loses that safety net.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # Handles are 64-bit: without these, ctypes passes them as 32-bit ints and
    # raises OverflowError.
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)  # 9 = JobObjectExtendedLimitInformation
    )
    if not ok:
        return False
    # The handle is deliberately never closed: closing it is what kills the job.
    return bool(kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()))


# -- what to run --------------------------------------------------------------


def console_python() -> str:
    """The interpreter for children: ``python.exe``, even when the supervisor
    itself runs under ``pythonw.exe`` (children need a real stdout to pipe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if base and (Path(base) / "nodejs" / "node.exe").exists():
            return str(Path(base) / "nodejs" / "node.exe")
    return None


def default_specs(
    *, root: Path = ROOT, host: str = "127.0.0.1", port: int = 8000, with_agent: bool = True
) -> list[ChildSpec]:
    python = console_python()
    specs = [
        ChildSpec(
            "api",
            [
                python,
                "-m",
                "uvicorn",
                "interlock.api.main:app",
                "--host",
                host,
                "--port",
                str(port),
            ],
            root,
        ),
        ChildSpec("worker", [python, "-m", "interlock.workers.loop"], root),
    ]
    if with_agent:
        agent_dir = root / "apps" / "agent"
        # "node" not found is reported (and retried) by the supervisor like any
        # other failure to start, so the agent can be installed later.
        specs.append(ChildSpec("agent", [find_node() or "node", "dist/src/index.js"], agent_dir))
    return specs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Interlock in the background.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-agent", action="store_true", help="don't run the WhatsApp agent")
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR)
    parser.add_argument(
        "--echo", action="store_true", help="also print children's output (console use)"
    )
    args = parser.parse_args(argv)

    lock = SingleInstance(args.log_dir / LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunningError:
        # Success, not an error: the job is done. (A non-zero exit would make
        # the logon task retry for nothing.)
        print("Interlock is already running.", file=sys.stderr)
        return 0

    supervisor = Supervisor(
        default_specs(host=args.host, port=args.port, with_agent=not args.no_agent),
        log_dir=args.log_dir,
        echo=args.echo,
    )
    # Under pythonw there is no console, so anything that goes wrong must reach
    # supervisor.log or it is invisible.
    try:
        joined = join_kill_on_close_job()
    except Exception as error:  # a missing safety net must not stop the service
        supervisor.note(f"could not set up the kill-on-close job ({error!r})")
        joined = True
    if not joined:
        supervisor.note("could not join a kill-on-close job; children rely on a clean stop")

    def _on_signal(_signum: int, _frame: FrameType | None) -> None:
        supervisor.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_signal)
    try:
        supervisor.run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
