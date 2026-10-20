"""One TOOLS engine process per project (engine_process plan, slice 2a).

Every op the editor asks of a project is a frame to that project's persistent
``_execute_op_subprocess.py --role tools``: started once, reused, replaced when it
died or its bundle moved, killed and retried once when a warm one is lost, and
gone with this process. Real daemons run on this interpreter where the pin is
about the process; a fake process stands in where the pin needs a hang or a death
on a chosen frame.
"""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.core import temp_root  # noqa: E402
from procedo.studio import _project_local_state  # noqa: E402
from procedo.studio.llm import _engine_ops as engine_ops  # noqa: E402
from procedo.studio.pack_ops import engine, pack_parsers  # noqa: E402

_DAEMON = Path(pack_parsers.__file__).resolve().parent.parent / "llm" / "_execute_op_subprocess.py"
_REAL_POPEN = subprocess.Popen


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose venv is this interpreter, and a registry of its own."""
    root = tmp_path / "proj"
    (root / "bundle").mkdir(parents=True)
    (root / "bundle" / "bundle.json").write_text("{}", encoding="utf-8")
    venv = _project_local_state.ProjectVenv(Path(sys.executable), root / "bundle")
    monkeypatch.setattr(pack_parsers, "_resolve_project_python", lambda _root: venv)
    monkeypatch.setattr(engine, "_slots", {})
    monkeypatch.setattr(engine, "_closed", False)   # an earlier test's exit hook closed it
    yield root
    for slot in engine._slots.values():
        if slot.session is not None:
            slot.session.kill()


@pytest.fixture
def spawns(monkeypatch):
    """Every engine process started, as (argv, kwargs); the real process runs."""
    seen: list = []

    def spy(argv, **kwargs):
        seen.append((argv, kwargs))
        return _REAL_POPEN(argv, **kwargs)

    monkeypatch.setattr(pack_parsers.subprocess, "Popen", spy)
    return seen


def _session(root: Path) -> pack_parsers.RemoteSession:
    return engine._slots[("tools", os.path.normcase(os.path.abspath(root)))].session


def _available(root: Path, timeout: float = 30) -> dict:
    return engine.tools_op(root, {"op": "is_available"}, timeout)


# --- the registry over real processes ---------------------------------------


def test_many_ops_start_one_process(project, spawns):
    for _ in range(20):
        assert _available(project)["ok"] is True
    assert len(spawns) == 1
    argv, kwargs = spawns[0]
    assert argv[1:] == [str(_DAEMON), "--role", "tools"]
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_a_moved_bundle_gets_a_fresh_process_and_the_old_one_exits(project, spawns):
    assert _available(project)["ok"]
    old = _session(project)._proc
    os.utime(project / "bundle" / "bundle.json", (1_000_000, 1_000_000))
    assert _available(project)["ok"]
    assert len(spawns) == 2
    assert old.wait(timeout=10) is not None


def test_a_process_that_died_idle_is_replaced(project, spawns):
    assert _available(project)["ok"]
    proc = _session(project)._proc
    proc.kill()
    proc.wait(timeout=10)
    assert _available(project)["ok"]
    assert len(spawns) == 2


def _tree(proc) -> list:
    """The engine process and what it started (a venv's python.exe is a launcher)."""
    psutil = pytest.importorskip("psutil")
    root = psutil.Process(proc.pid)
    return [root, *root.children(recursive=True)]


def test_the_process_exits_on_stdin_eof_and_leaves_no_orphan(project, spawns):
    """However the app ends, its pipe closes: the process leaves on its own."""
    assert _available(project)["ok"]
    proc = _session(project)._proc
    tree = _tree(proc)
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
    _gone, alive = pytest.importorskip("psutil").wait_procs(tree, timeout=10)
    assert alive == []


def test_the_exit_hook_lets_the_process_leave_on_its_own_and_no_orphan(project, spawns):
    """Graceful (exit code 0, not a kill): a venv launcher exits only after its
    python, so the working folder is free once the hook returns."""
    assert _available(project)["ok"]
    proc = _session(project)._proc
    tree = _tree(proc)
    engine.stop_all()
    assert proc.poll() == 0
    _gone, alive = pytest.importorskip("psutil").wait_procs(tree, timeout=10)
    assert alive == []


def test_stop_ends_the_instances_running_out_of_a_venv_and_only_those(project, spawns, tmp_path):
    assert _available(project)["ok"]
    proc = _session(project)._proc
    engine.stop(venv_folder=tmp_path / "another_venv")
    assert proc.poll() is None
    engine.stop(venv_folder=Path(sys.executable).parent.parent)   # the venv this test's interpreter is in
    assert proc.poll() is not None
    assert _session(project) is None
    assert _available(project)["ok"] and len(spawns) == 2


def test_stop_never_waits_on_a_busy_slot_s_lock(project, spawns):
    """The GUI thread calls ``stop`` (Studio's project switch, a bundle change): it
    closes the instance without the slot's lock, which a worker's op holds."""
    assert _available(project)["ok"]
    slot = next(iter(engine._slots.values()))
    proc = slot.session._proc
    done = threading.Event()
    with slot.lock:
        t = threading.Thread(target=lambda: (engine.stop(project_root=project), done.set()))
        t.start()
        assert done.wait(15), "stop waited on the slot's lock"
        assert slot.session is None and proc.wait(timeout=15) is not None
    t.join(5)


def test_a_dead_instance_is_not_restarted_to_be_stopped(project, spawns):
    assert _available(project)["ok"]
    proc = _session(project)._proc
    proc.kill()
    proc.wait(timeout=10)
    engine.stop(venv_folder=Path(sys.executable).parent.parent)
    assert len(spawns) == 1


def test_a_dead_instance_is_not_restarted_to_be_replaced(project, spawns):
    assert _available(project)["ok"]
    proc = _session(project)._proc
    proc.kill()
    proc.wait(timeout=10)
    os.utime(project / "bundle" / "bundle.json", (1_000_000, 1_000_000))
    assert _available(project)["ok"]
    assert len(spawns) == 2


def test_a_tools_instance_claims_its_venv_for_its_pid_until_it_ends(project, spawns, monkeypatch):
    seen: list = []
    monkeypatch.setattr(_project_local_state, "claim_engine",
                        lambda root, pid, role: seen.append(("claim", pid, role)) or project / "venv")
    monkeypatch.setattr(_project_local_state, "release_engine",
                        lambda folder, pid, role: seen.append(("release", pid, role)))
    assert _available(project)["ok"] and _available(project)["ok"]
    pid = _session(project)._proc.pid
    assert seen == [("claim", pid, "tools")]
    engine.stop_all()
    assert seen == [("claim", pid, "tools"), ("release", pid, "tools")]


@pytest.fixture
def claims(monkeypatch, project):
    seen: list = []
    monkeypatch.setattr(_project_local_state, "claim_engine",
                        lambda root, pid, role: seen.append(("claim", pid)) or project / "venv")
    monkeypatch.setattr(_project_local_state, "release_engine",
                        lambda folder, pid, role: seen.append(("release", pid)))
    return seen


@pytest.mark.parametrize("end", ["kill", "shutdown"])
def test_every_way_an_instance_ends_releases_its_claim(project, spawns, claims, end):
    assert _available(project)["ok"]
    session = _session(project)
    pid = session._proc.pid
    getattr(session, end)()
    assert claims == [("claim", pid), ("release", pid)]


def test_a_restarted_process_releases_the_old_claim_before_it_claims(project, spawns, claims):
    assert _available(project)["ok"]
    first = _session(project)._proc
    first.kill()
    first.wait(timeout=10)
    assert _available(project)["ok"]                  # the same session starts it again
    second = _session(project)._proc
    assert claims == [("claim", first.pid), ("release", first.pid), ("claim", second.pid)]


def test_close_kills_a_process_that_ignores_stdin_eof(project):
    sess = pack_parsers.RemoteSession(project, role="tools")
    sess._proc = _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.PIPE)
    started = time.monotonic()
    sess.close(grace=0.5)
    assert sess._proc.poll() is not None and time.monotonic() - started < 10


# --- the roles ----------------------------------------------------------------

_INSTRUMENT = ("exec_ops", "safe_off", "raw", "acquire_run", "release_run", "run_test",
               "cancel_run", "prompt_response")


@pytest.mark.parametrize("cmd", _INSTRUMENT)
def test_tools_refuses_every_instrument_and_bench_frame(cmd):
    refused = engine_ops.refusal("tools", {"cmd": cmd})
    assert refused["kind"] == "Protocol" and refused["ok"] is False


@pytest.mark.parametrize("op", ["export_rows", "refresh_media", "parse_text", "validate",
                                "display_snapshot", "extract"])
def test_run_refuses_every_op_but_its_own(op):
    assert engine_ops.refusal("run", {"cmd": "op", "op": op})["kind"] == "Protocol"


def test_each_role_takes_its_own_frames():
    for frame in ({"cmd": "op", "op": "export_rows"}, {"cmd": "ping"}, {"cmd": "shutdown"}):
        assert engine_ops.refusal("tools", frame) is None
    for frame in ({"cmd": "op", "op": "build_manual_run"}, {"cmd": "op", "op": "build_manual_result"},
                  {"cmd": "ping"}, {"cmd": "shutdown"}, *({"cmd": c} for c in _INSTRUMENT)):
        assert engine_ops.refusal("run", frame) is None


def _talk(role: str, frames: list[dict]) -> list[dict]:
    out, _err = _REAL_POPEN([sys.executable, str(_DAEMON), "--role", role], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True).communicate(
        "".join(json.dumps(f) + "\n" for f in frames), timeout=60)
    return [json.loads(line) for line in out.splitlines()]


def test_the_tools_daemon_refuses_an_instrument_frame_before_it_runs():
    answers = _talk("tools", [{"cmd": "exec_ops", "ops": [], "bench_map": {}},
                                 {"cmd": "op", "op": "is_available"}])
    assert answers[0]["kind"] == "Protocol" and "exec_ops" in answers[0]["error"]
    assert answers[1]["ok"] is True


def test_the_run_daemon_refuses_a_tools_op():
    answers = _talk("run", [{"cmd": "op", "op": "refresh_media", "procedure_json": {}},
                               {"cmd": "ping"}])
    assert answers[0]["kind"] == "Protocol" and "refresh_media" in answers[0]["error"]
    assert answers[1] == {"ok": True, "pong": True}


def test_the_daemon_will_not_start_without_a_role():
    proc = _REAL_POPEN([sys.executable, str(_DAEMON)], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _out, err = proc.communicate("", timeout=60)
    assert proc.returncode == 2 and "--role" in err


# --- a hang or a death on a chosen frame (fake process) -----------------------


class _Fake:
    """A daemon process answering frame N per ``plan[N]``: ``ok``, ``hang`` (never
    answers) or ``die`` (closes its pipe after ``delay``). Counts overlapping frames."""

    overlap = 0

    def __init__(self, argv, plan: list[str], delay: float = 0.0) -> None:
        self.argv, self.plan, self.delay = argv, list(plan), delay
        self.frames: list[dict] = []
        self.returncode = None
        self.pid = 4242
        self.terminated = False
        self.hung = False   # a hung process ignores stdin EOF: only a kill ends it
        self._out: queue.Queue = queue.Queue()
        self._pending = 0
        self.stdin, self.stdout, self.stderr = self, self, io.StringIO("")

    def write(self, line: str) -> None:
        frame = json.loads(line)
        self.frames.append(frame)
        self._pending += 1
        _Fake.overlap = max(_Fake.overlap, self._pending)
        step = self.plan.pop(0) if self.plan else "ok"
        if step == "ok":
            self._out.put(json.dumps({"ok": True, "frame": len(self.frames)}) + "\n")
        elif step == "hang":
            self.hung = True
        elif step == "die":
            threading.Timer(self.delay, self._die).start()
        elif step.startswith("{"):
            self._out.put(step + "\n")

    def _die(self) -> None:
        self.returncode = 1
        self._out.put("")

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        line = self._out.get()
        time.sleep(0.005)
        self._pending -= 1
        return line

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self.hung:   # it waits out the grace, as a real one would
            time.sleep(timeout or 0)
            raise subprocess.TimeoutExpired("fake engine", timeout)
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def close(self) -> None:   # stdin closed: a hung process does not notice
        pass

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1
        self._out.put("")

    kill = terminate


@pytest.fixture
def fakes(project, monkeypatch):
    """``plans``: one frame plan per process the registry starts, in order."""
    started: list[_Fake] = []
    plans: list = []
    _Fake.overlap = 0

    def popen(argv, **_kwargs):
        plan = plans.pop(0) if plans else (["ok"], 0.0)
        started.append(_Fake(argv, *plan))
        return started[-1]

    monkeypatch.setattr(pack_parsers.subprocess, "Popen", popen)
    return started, plans


def test_a_warm_process_past_its_deadline_is_killed_and_not_retried(project, fakes):
    """A slow op would only cost the wait twice: kill it and say so."""
    started, plans = fakes
    plans += [(["ok", "hang"], 0.0), (["ok"], 0.0)]
    assert _available(project, 0.5)["ok"]
    result = _available(project, 0.5)
    assert (result["ok"], result["kind"]) == (False, "ParserUnavailable")
    assert "unresponsive" in result["error"]
    assert len(started) == 1 and started[0].terminated
    assert _available(project, 0.5)["ok"]          # the next op starts afresh
    assert len(started) == 2


def test_a_warm_process_that_dies_mid_op_is_retried_once(project, fakes):
    started, plans = fakes
    plans += [(["ok", "die"], 0.0), (["ok"], 0.0)]
    assert _available(project)["ok"]
    assert _available(project)["ok"] is True
    assert len(started) == 2


def test_the_retry_happens_once_only(project, fakes):
    started, plans = fakes
    plans += [(["ok", "die"], 0.0), (["die"], 0.0), (["ok"], 0.0)]
    assert _available(project)["ok"]
    result = _available(project)
    assert (result["ok"], result["kind"]) == (False, "ParserUnavailable")
    assert "closed pipe" in result["error"]
    assert len(started) == 2


@pytest.mark.parametrize("step", ["hang", "die"])
def test_a_fresh_process_lost_on_its_first_op_is_not_retried(project, fakes, step):
    started, plans = fakes
    plans += [([step], 0.0), (["ok"], 0.0)]
    result = _available(project, 0.5)
    assert result["kind"] == "ParserUnavailable"
    assert len(started) == 1 and started[0].returncode is not None


@pytest.mark.parametrize("answer", [
    {"ok": False, "kind": "ParseFailure", "error": "bad line", "code": "X", "findings": []},
    {"ok": False, "kind": "Other", "error": "ValueError: boom"},
    {"ok": False, "kind": "Protocol", "error": "the tools engine does not take 'raw'"},
])
def test_an_op_failure_reaches_the_caller_unchanged(project, fakes, answer):
    _started, plans = fakes
    plans.append(([json.dumps(answer)], 0.0))
    assert _available(project) == answer


def test_is_available_answers_no_with_the_engine_s_error(project, fakes):
    _started, plans = fakes
    plans.append((["die"], 0.0))
    available, reason = pack_parsers.is_available(project)
    assert available is False and "closed pipe" in reason


def test_a_lost_engine_is_parser_unavailable_to_the_bridges(project, fakes):
    _started, plans = fakes
    plans.append((["die"], 0.0))
    with pytest.raises(pack_parsers.ParserUnavailable, match="closed pipe"):
        pack_parsers.render_text({"steps": []}, project_root=project)


def test_two_threads_share_one_process_one_frame_at_a_time(project, fakes):
    """Thread B waits for thread A's whole call, retry included: it never sends a
    frame beside A's, nor replaces the instance A just replaced."""
    started, plans = fakes
    plans += [(["ok", "die"], 0.2), (["ok"] * 50, 0.0)]
    assert _available(project)["ok"]
    gate = threading.Barrier(2)
    results: list = []

    def caller():
        gate.wait()
        results.extend(_available(project) for _ in range(5))

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert [r["ok"] for r in results] == [True] * 10
    assert len(started) == 2
    assert _Fake.overlap == 1


# --- no traces ------------------------------------------------------------------


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    """``tmp_path/TEMP`` is %TEMP% for this test, and this process has no root yet."""
    folder = tmp_path / "TEMP"
    folder.mkdir()
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(folder))
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    monkeypatch.setattr(temp_root, "_root", None)
    yield folder
    temp_root.remove()


def test_the_process_works_and_leaves_nothing_outside_the_temp_root(temp_dir, project, spawns):
    assert _available(project)["ok"]
    _argv, kwargs = spawns[0]
    root = temp_root.root()
    assert kwargs["cwd"] == kwargs["env"]["TEMP"] == kwargs["env"]["TMP"] == str(root)
    proc = _session(project)._proc
    engine.stop_all()             # the exit hook, before the temp root goes
    assert proc.poll() is not None
    temp_root.remove()
    assert list(temp_dir.iterdir()) == []


# --- the lifecycle hooks (slice 2b) -----------------------------------------


def _other_project(tmp_path: Path) -> Path:
    root = tmp_path / "other"
    (root / "bundle").mkdir(parents=True)
    (root / "bundle" / "bundle.json").write_text("{}", encoding="utf-8")
    return root


def test_stopping_a_project_ends_its_process_only_and_the_next_op_starts_afresh(
    project, spawns, tmp_path
):
    other = _other_project(tmp_path)
    assert _available(project)["ok"] and _available(other)["ok"]
    mine, theirs = _session(project)._proc, _session(other)._proc
    engine.stop(project_root=project)
    assert mine.wait(timeout=10) is not None
    assert theirs.poll() is None
    assert _available(project)["ok"]
    assert len(spawns) == 3


def test_switching_project_stops_the_old_ones_process(project, spawns, tmp_path):
    from procedo.studio.core.project_manager import ProjectManager

    other = _other_project(tmp_path)
    (project / "tests").mkdir()
    (other / "tests").mkdir()
    manager = ProjectManager()
    assert manager.set_project_root(project)
    assert _available(project)["ok"]
    proc = _session(project)._proc
    assert manager.set_project_root(project)       # the same project again: nothing stops
    assert proc.poll() is None
    assert manager.set_project_root(other)
    assert proc.wait(timeout=10) is not None


def test_a_bundle_change_in_studio_stops_the_projects_process(project, spawns):
    from types import SimpleNamespace

    from procedo.studio.controllers import project_controller

    assert _available(project)["ok"]
    proc = _session(project)._proc
    window = SimpleNamespace(
        project_manager=SimpleNamespace(project_root=project),
        _update_project_rules_indicators=lambda: None,
        _handle_config_change=lambda: None,
    )
    project_controller.on_bundle_change_debounced(window)
    assert proc.wait(timeout=10) is not None


def test_the_temp_roots_removal_stops_the_processes_first(temp_dir, project, spawns):
    """aboutToQuit calls ``remove`` too: the engine must already be gone, or Windows
    refuses to delete its working folder."""
    assert _available(project)["ok"]
    proc = _session(project)._proc
    temp_root.remove()
    assert proc.poll() is not None
    assert list(temp_dir.iterdir()) == []


def test_after_the_exit_hook_nothing_starts_again(project, spawns):
    """A worker caught mid-frame at exit must not respawn into the root being removed."""
    assert _available(project)["ok"]
    engine.stop_all()
    assert _session_or_none(project) is None
    answer = _available(project)
    assert (answer["ok"], answer["kind"]) == (False, "ParserUnavailable")
    assert len(spawns) == 1


def _session_or_none(root: Path):
    slot = engine._slots.get(("tools", os.path.normcase(os.path.abspath(root))))
    return None if slot is None else slot.session


def test_stop_locks_only_the_slots_it_ends(project, spawns, tmp_path):
    other = _other_project(tmp_path)
    assert _available(other)["ok"]
    theirs = engine._slots[("tools", os.path.normcase(os.path.abspath(other)))]
    with theirs.lock:                         # busy with a frame
        engine.stop(project_root=project)     # would hang if it locked every slot
    assert theirs.session is not None


def test_stop_under_a_hung_op_ends_it_and_the_op_is_not_retried(project, fakes):
    """A wedged engine: ``stop`` returns at once (the close's grace, then a kill),
    the op in flight ends lost instead of waiting out its 30 s deadline, and no
    retry starts an instance for a project that just closed."""
    started, plans = fakes
    plans += [(["ok", "hang"], 0.0), (["ok"], 0.0)]
    assert _available(project)["ok"]
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(res=_available(project, 30)))
    worker.start()
    while not started[0].hung:
        time.sleep(0.01)
    begun = time.monotonic()
    engine.stop(project_root=project)
    worker.join(20)
    assert time.monotonic() - begun < 15 and not worker.is_alive()
    assert box["res"]["kind"] == "ParserUnavailable" and started[0].terminated
    assert len(started) == 1


# --- the RUN instance (slice 3) ---------------------------------------------


def test_run_is_taken_once_while_live_and_fresh_once_its_holder_ended_it(project, spawns):
    """Only a holder ends RUN: while one is live a second holder gets None (never a
    second process on the instruments); once it ended, the next take is fresh."""
    first = engine.run(project)
    assert first.ping()["pong"] and engine.run(project) is None
    assert engine.run_for_recovery(project) == (first, False)
    first.shutdown()
    second = engine.run(project)
    assert second is not None and second is not first and not second.alive
    second.shutdown()                       # never started: ended all the same
    assert engine.run(project) is not None
    assert [argv[1:] for argv, _kw in spawns] == [[str(_DAEMON), "--role", "run"]]


def test_a_run_whose_process_died_is_not_restarted_by_the_registry(project, spawns):
    first = engine.run(project)
    assert first.ping()["pong"]
    first._proc.kill()
    first._proc.wait(timeout=10)
    assert first.ping()["kind"] == "SessionDead"     # the holder learns it: its lost path
    taken, fresh = engine.run_for_recovery(project)
    assert fresh and taken is not first and len(spawns) == 1
    taken.shutdown()


def test_stop_and_the_exit_hook_leave_a_live_run_to_its_holder(project, spawns):
    run = engine.run(project)
    assert run.ping()["pong"]
    engine.stop(project_root=project)
    engine.stop(venv_folder=Path(sys.executable).parent.parent)
    engine.stop_all()
    assert run.alive and run.ping()["pong"]
    assert engine.run_for_recovery(project) == (run, False)   # a recovery may still drive it
    run.shutdown()


def test_run_claims_its_venv_as_a_bench_holder_tools_as_this_app_s_engine(monkeypatch):
    from procedo.core import venv_pool

    seen = []
    monkeypatch.setattr(venv_pool, "claim_child", lambda root, pid, token: seen.append(token))
    _project_local_state.claim_engine(Path("p"), 1, "run")
    _project_local_state.claim_engine(Path("p"), 1, "tools")
    assert seen == [venv_pool.BENCH, venv_pool.engine_token()]


def test_a_recovery_safe_off_never_starts_a_second_run_beside_a_live_one(project, spawns):
    """Recovery and RUN are never two live processes on the instruments: with a
    holder's RUN still live, the off goes through it, and it stays its holder's."""
    run = engine.run(project)
    assert run.ping()["pong"]
    pack_parsers.safe_off_remote([], {}, project)   # no packs here: the frame's answer is moot
    assert len(spawns) == 1 and run.alive
    run.shutdown()
    pack_parsers.safe_off_remote([], {}, project)   # a fresh one, ended
    assert len(spawns) == 2 and engine.run(project) is not None


def test_a_project_stop_returns_at_once_even_on_a_wedged_engine(project, fakes):
    """Studio's project switch and bundle change call it on the GUI thread: the
    close (its grace, then the kill) happens on a thread; the op in flight still
    ends lost, unretried."""
    started, plans = fakes
    plans += [(["ok", "hang"], 0.0)]
    assert _available(project)["ok"]
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(res=_available(project, 30)))
    worker.start()
    while not started[0].hung:
        time.sleep(0.01)
    begun = time.monotonic()
    engine.stop(project_root=project)
    assert time.monotonic() - begun < 1.0
    worker.join(20)
    assert box["res"]["kind"] == "ParserUnavailable" and started[0].terminated


def test_a_stop_landing_while_an_instance_is_replaced_leaves_none_running(project, spawns, monkeypatch):
    """``stop`` takes no lock, so it can land between ``_replace`` retiring the old
    instance and placing the new one: that new one is retired again, never left
    running for a project that just closed."""
    real = engine._replace

    def replace_under_a_stop(slot, key, role, root):
        engine.stop(project_root=project)      # the GUI thread's stop, mid-replace
        real(slot, key, role, root)

    monkeypatch.setattr(engine, "_replace", replace_under_a_stop)
    answer = _available(project)
    assert (answer["ok"], answer["kind"]) == (False, "ParserUnavailable")
    assert spawns == [] and _session(project) is None


# --- the extract op ------------------------------------------------------------


def test_extract_runs_what_the_bundle_declares_and_explains_the_rest(tmp_path):
    """Each document's keys by the extractors ``defaults.json`` declares; an undeclared
    key is explained in that result's ``errors``; a document of the wrong shape is
    a validator's finding, never a failed op."""
    pytest.importorskip("rules_packager_base.rules.v2_0_2.extractors")
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "defaults.json").write_text(json.dumps({"extractors": {
        key: {"module": "rules_packager_base.rules.v2_0_2.extractors", "function": function}
        for key, function in (("meta", "extract_meta"), ("steps_validate", "validate_steps"))}}), encoding="utf-8")
    out = engine_ops.run({"op": "extract", "project": str(tmp_path), "keys": ["meta", "steps_validate", "equipment"],
                          "procedures": [{"id": "T1", "title": "Title"}, ["not", "an", "object"]]})
    assert out["ok"] is True and len(out["results"]) == 2
    first, second = out["results"]
    assert first["data"]["meta"]["display_name"] == "Title (T1)"
    assert set(first["errors"]) == set(second["errors"]) == {"equipment"}  # the bundle declares none
    assert "PROC_NOT_OBJECT" in {finding["code"] for finding in second["data"]["steps_validate"]}
