"""Phase-2 OpenCode server-lifecycle tests.

Cover the own-server-only behaviour added in the lifecycle refactor:
- the launch command passes OPENCODE_CONFIG explicitly (C1),
- find_free_port OS-assigns when nothing is preferred (Q1),
- the PID-file orphan-sweep kills OUR opencode serve but NEVER an unrelated /
  recycled pid (C3),
- is_available() is a pure install check (no server probe).

All WSL/HTTP work is mocked — these are pure-logic tests.
"""
import json
import shlex
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from workflow_editor.llm.opencode_backend import OpenCodeConfig
from workflow_editor.llm.server_health import find_free_port
from workflow_editor.llm.server_manager import OpenCodeServerManager, PidIdentity


def _proc_probe_stdout(*, alive=True, cmdline=None, environ=None):
    """Build a stdout blob matching the shape ``_pid_is_our_opencode`` parses:
    an ALIVE/DEAD marker line, the NUL-joined cmdline, the env separator, then
    the NUL-joined environ. ``cmdline``/``environ`` are token lists."""
    marker = OpenCodeServerManager._ID_ALIVE if alive else OpenCodeServerManager._ID_DEAD
    cmd_blob = "\0".join(cmdline or [])
    env_blob = "\0".join(environ or [])
    sep = OpenCodeServerManager._ID_SEP
    # Mirrors the real shell: "<marker>\n<cmdline>\n<SEP>\n<environ>".
    return f"{marker}\n{cmd_blob}\n{sep}\n{env_blob}"


def _mgr(tmp_path: Path) -> OpenCodeServerManager:
    cfg = OpenCodeConfig(working_directory=str(tmp_path))
    return OpenCodeServerManager(cfg)


# --------------------------------------------------------------------------- #
# C1 — launch command carries OPENCODE_CONFIG explicitly                       #
# --------------------------------------------------------------------------- #

def test_serve_command_includes_opencode_config(tmp_path):
    # working_directory is a Windows-style path → translated to /mnt/c form.
    cfg = OpenCodeConfig(working_directory=r"C:\Users\me\.workflow_editor\opencode\launch")
    mgr = OpenCodeServerManager(cfg)
    cmd = mgr._build_serve_command(5123)
    assert "OPENCODE_CONFIG=" in cmd
    assert "/mnt/c/Users/me/.workflow_editor/opencode/launch/opencode.json" in cmd
    assert "OPENCODE_ENABLE_EXA=1" in cmd
    assert "opencode serve --port 5123" in cmd


def test_serve_command_omits_config_without_launch_dir():
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    cmd = mgr._build_serve_command(6000)
    assert "OPENCODE_CONFIG=" not in cmd
    assert "opencode serve --port 6000" in cmd


def test_serve_command_translates_posix_launch_dir():
    cfg = OpenCodeConfig(working_directory="/mnt/c/foo/launch")
    cmd = OpenCodeServerManager(cfg)._build_serve_command(7000)
    assert "OPENCODE_CONFIG=/mnt/c/foo/launch/opencode.json" in cmd


def test_serve_command_shell_quotes_spaced_launch_dir():
    # A space in the home/launch path must be shell-quoted so it cannot break
    # the bash -ic command into two tokens (MAJOR fix).
    cfg = OpenCodeConfig(working_directory=r"C:\Users\John Doe\.workflow_editor\launch")
    cmd = OpenCodeServerManager(cfg)._build_serve_command(7100)
    # The config token is quoted as a single shell word; the raw space-bearing
    # path never appears unquoted.
    assert "OPENCODE_CONFIG='/mnt/c/Users/John Doe/.workflow_editor/launch/opencode.json'" in cmd
    # Round-trips through the shell lexer as ONE token (no split on the space).
    tokens = shlex.split(cmd)
    assert "/mnt/c/Users/John Doe/.workflow_editor/launch/opencode.json" in \
        [t.split("=", 1)[1] for t in tokens if t.startswith("OPENCODE_CONFIG=")]


def test_serve_command_shell_quotes_metachar_hostname():
    # A metacharacter in the hostname cannot inject a second command — it is
    # quoted, so it survives as a single literal --hostname value (MAJOR fix).
    cfg = OpenCodeConfig(working_directory=None,
                         server_hostname="127.0.0.1; rm -rf ~")
    cmd = OpenCodeServerManager(cfg)._build_serve_command(7200)
    tokens = shlex.split(cmd)
    assert "rm" not in tokens  # the injection never becomes its own token
    assert "127.0.0.1; rm -rf ~" in tokens


# --------------------------------------------------------------------------- #
# Q1 — find_free_port OS-assigns                                               #
# --------------------------------------------------------------------------- #

def test_find_free_port_os_assigns_by_default():
    port = find_free_port()  # preferred=0
    assert isinstance(port, int) and port > 0


def test_find_free_port_zero_never_returns_zero():
    # preferred=0 must NOT be honoured verbatim (0 = "any port"); the OS picks one.
    assert find_free_port(0) != 0


# --------------------------------------------------------------------------- #
# C3 — PID-file orphan sweep                                                   #
# --------------------------------------------------------------------------- #

def test_write_then_read_pid_file_roundtrip(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(4242, 5005)
    pid_file = tmp_path / "opencode.pid"
    assert pid_file.exists()
    data = json.loads(pid_file.read_text())
    assert data["pid"] == 4242 and data["port"] == 5005
    assert mgr._read_pid_file()["pid"] == 4242


def test_remove_pid_file_is_safe_when_absent(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._remove_pid_file()  # no file yet → must not raise
    assert not (tmp_path / "opencode.pid").exists()


def test_pid_file_path_none_without_launch_dir():
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    assert mgr._pid_file_path() is None


def test_sweep_orphan_kills_our_opencode_pid(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(9999, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        return r

    # Identity is OURS on the first check and STILL ours on the post-TERM
    # re-check → graceful TERM, then escalate to KILL -9.
    with patch.object(mgr, "_pid_is_our_opencode", return_value=PidIdentity.OURS), \
         patch("workflow_editor.llm.server_manager.time.sleep"), \
         patch("workflow_editor.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._sweep_orphan()

    flat = [" ".join(c) for c in calls]
    # A graceful TERM AND an escalated KILL -9 were issued against OUR pid.
    assert any("kill 9999" in f and "kill -9" not in f for f in flat)
    assert any("kill -9 9999" in f for f in flat)
    # The stale file is gone after a conclusive sweep.
    assert not (tmp_path / "opencode.pid").exists()


def test_sweep_orphan_spares_non_opencode_pid(tmp_path):
    # The recorded pid was recycled to an unrelated process: cmdline check fails
    # → we must NOT kill it (only clear the stale file).
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(1234, 5005)

    with patch.object(mgr, "_pid_is_our_opencode", return_value=PidIdentity.NOT_OURS), \
         patch("workflow_editor.llm.server_manager.subprocess.run") as run:
        mgr._sweep_orphan()
        run.assert_not_called()
    assert not (tmp_path / "opencode.pid").exists()


def test_sweep_orphan_noop_without_pid_file(tmp_path):
    mgr = _mgr(tmp_path)
    with patch("workflow_editor.llm.server_manager.subprocess.run") as run:
        mgr._sweep_orphan()  # no pid file at all
        run.assert_not_called()


def test_pid_is_our_opencode_ours_on_environ_config_match(tmp_path):
    # cmdline is an opencode serve AND environ carries OUR launch-dir
    # opencode.json as an EXACT OPENCODE_CONFIG=<our_cfg> token → OURS.
    mgr = _mgr(tmp_path)
    our_cfg = mgr._our_config_wsl_path()
    assert our_cfg is not None
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005", "--hostname", "127.0.0.1"],
        environ=[f"OPENCODE_CONFIG={our_cfg}", "PATH=/usr/bin"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.OURS


def test_pid_is_our_opencode_ours_on_argv_config_match(tmp_path):
    # Config asserted via argv `--config <our_cfg>` (two exact tokens) → OURS.
    mgr = _mgr(tmp_path)
    our_cfg = mgr._our_config_wsl_path()
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["/usr/bin/opencode", "serve", "--config", our_cfg, "--port", "5005"],
        environ=["PATH=/usr/bin"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.OURS


def test_pid_is_our_opencode_not_ours_when_pid_dead(tmp_path):
    # /proc/<pid> absent → DEAD marker → conclusively NOT_OURS (safe to forget).
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(alive=False)
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_not_ours_for_other_process(tmp_path):
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["/usr/bin/python3", "some_other_server.py"],
        environ=["PATH=/usr/bin"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_not_ours_for_users_own_opencode(tmp_path):
    # The recycled / coexisting pid IS an `opencode serve`, but pointed at a
    # DIFFERENT config (the user's own). Requiring OUR exact OPENCODE_CONFIG
    # token spares it (MAJOR-2).
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "9000", "--hostname", "127.0.0.1"],
        environ=["OPENCODE_CONFIG=/home/someone/.config/opencode/opencode.json"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_not_ours_for_backup_config_substring(tmp_path):
    # The pid IS an opencode serve and its OPENCODE_CONFIG is `<our_cfg>.backup`
    # — a SUPERSTRING of ours. The old `our_cfg in out` substring check matched
    # and would have killed a non-owned server; the EXACT-token match rejects it
    # (MAJOR-2).
    mgr = _mgr(tmp_path)
    our_cfg = mgr._our_config_wsl_path()
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005"],
        environ=[f"OPENCODE_CONFIG={our_cfg}.backup"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_unknown_on_read_timeout(tmp_path):
    # A /proc read timeout / WSL hiccup is INCONCLUSIVE → UNKNOWN, never
    # NOT_OURS (MAJOR-1). The TimeoutExpired path.
    mgr = _mgr(tmp_path)
    import subprocess as _sp
    with patch("workflow_editor.llm.server_manager.subprocess.run",
               side_effect=_sp.TimeoutExpired(cmd="wsl", timeout=3)):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.UNKNOWN


def test_pid_is_our_opencode_unknown_when_no_marker(tmp_path):
    # The shell ran but emitted NEITHER alive nor dead marker (a WSL-level
    # failure, e.g. bash not found in the relay) → UNKNOWN, not NOT_OURS.
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = "some unrelated noise without any marker"
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.UNKNOWN


def test_pid_is_our_opencode_falls_back_to_serve_without_launch_dir():
    # No launch dir → no config identity to assert → an `opencode serve` is
    # OURS on the serve-token check alone (the strongest marker available).
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    assert mgr._our_config_wsl_path() is None
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005", "--hostname", "127.0.0.1"],
        environ=["PATH=/usr/bin"],
    )
    with patch("workflow_editor.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._pid_is_our_opencode(4242) is PidIdentity.OURS


def test_sweep_orphan_keeps_pid_file_on_unknown(tmp_path):
    # Identity is INCONCLUSIVE (UNKNOWN): no kill, AND the pid file is KEPT so
    # the next launch retries — never permanently lose a still-running orphan
    # (MAJOR-1).
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(4321, 5005)
    with patch.object(mgr, "_pid_is_our_opencode", return_value=PidIdentity.UNKNOWN), \
         patch("workflow_editor.llm.server_manager.subprocess.run") as run:
        mgr._sweep_orphan()
        run.assert_not_called()
    # Pid file SURVIVES the inconclusive sweep.
    assert (tmp_path / "opencode.pid").exists()


def test_sweep_orphan_skips_kill9_when_recheck_not_ours(tmp_path):
    # OURS on the first check → graceful TERM issued; but the post-TERM re-check
    # is NOT_OURS (pid gone / recycled in the gap) → KILL -9 must be SKIPPED
    # (PID-reuse TOCTOU, MAJOR-3). Exactly one subprocess.run (the TERM).
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(7777, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(" ".join(cmd))
        r = MagicMock()
        r.returncode = 0
        return r

    with patch.object(mgr, "_pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.NOT_OURS]), \
         patch("workflow_editor.llm.server_manager.time.sleep"), \
         patch("workflow_editor.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._sweep_orphan()

    # The graceful TERM ran; the -9 did NOT.
    assert any("kill 7777" in c and "kill -9" not in c for c in calls)
    assert not any("kill -9" in c for c in calls)
    # A conclusive OURS-then-gone sweep clears the (now stale) pid file.
    assert not (tmp_path / "opencode.pid").exists()


def test_sweep_orphan_keeps_pid_file_when_recheck_unknown(tmp_path):
    # OURS on the first check → graceful TERM; but the post-TERM re-check is
    # UNKNOWN (a /proc read hiccup). The orphan may still be alive, so KILL -9 is
    # SKIPPED *and* the pid file is KEPT for a next-launch retry — never delete on
    # an inconclusive post-TERM check (gpt-5.5 edge).
    mgr = _mgr(tmp_path)
    mgr._write_pid_file(8888, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(" ".join(cmd))
        r = MagicMock()
        r.returncode = 0
        return r

    with patch.object(mgr, "_pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.UNKNOWN]), \
         patch("workflow_editor.llm.server_manager.time.sleep"), \
         patch("workflow_editor.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._sweep_orphan()

    # TERM ran; -9 did NOT (inconclusive re-check).
    assert any("kill 8888" in c and "kill -9" not in c for c in calls)
    assert not any("kill -9" in c for c in calls)
    # Pid file SURVIVES — a possibly-running orphan is never lost.
    assert (tmp_path / "opencode.pid").exists()


# --------------------------------------------------------------------------- #
# is_available() is install-only (no server probe)                            #
# --------------------------------------------------------------------------- #

def test_is_available_is_install_check_only(tmp_path):
    mgr = _mgr(tmp_path)
    healthy = MagicMock()
    healthy.ok = True
    with patch.object(mgr, "_diagnose_installation", return_value=healthy) as diag, \
         patch("workflow_editor.llm.server_manager.requests.get") as get:
        assert mgr.is_available() is True
        diag.assert_called_once()
        # No HTTP server probe — install check only.
        get.assert_not_called()


# --------------------------------------------------------------------------- #
# Liveness — a CRASHED server reads as not-running, and start() relaunches it  #
# (dead-server detection + auto-recovery).                                     #
# --------------------------------------------------------------------------- #

def _live_proc():
    """A process handle that polls as ALIVE (poll() -> None)."""
    proc = MagicMock()
    proc.poll.return_value = None
    proc.pid = 4242
    return proc


def _dead_proc(returncode=1):
    """A process handle that polls as DEAD (poll() -> an exit code)."""
    proc = MagicMock()
    proc.poll.return_value = returncode
    proc.pid = 4242
    return proc


def test_is_running_false_when_process_polled_dead(tmp_path):
    # A server that crashed mid-session: _running is still True and the process
    # handle lingers, but poll() now returns an exit code → is_running must read
    # False AND self-heal the stale flag/handle.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc(returncode=139)  # e.g. SIGSEGV
    assert mgr.is_running is False
    # Stale state was cleared so a later start() relaunches cleanly.
    assert mgr._running is False
    assert mgr._server_process is None


def test_is_running_true_when_process_alive(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _live_proc()
    assert mgr.is_running is True


def test_is_alive_requires_process_and_health(tmp_path):
    # is_alive is process-alive AND /health 200. A live process whose /health
    # fails (wedged server) is NOT alive.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _live_proc()
    ok = MagicMock(); ok.status_code = 200
    bad = MagicMock(); bad.status_code = 500
    with patch("workflow_editor.llm.server_manager.requests.get", return_value=ok):
        assert mgr.is_alive is True
    with patch("workflow_editor.llm.server_manager.requests.get", return_value=bad):
        assert mgr.is_alive is False


def test_is_alive_false_when_process_dead_without_http(tmp_path):
    # A dead process short-circuits is_alive — no HTTP probe is even attempted.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()
    with patch("workflow_editor.llm.server_manager.requests.get") as get:
        assert mgr.is_alive is False
        get.assert_not_called()


def test_start_relaunches_when_prior_process_dead(tmp_path):
    # The reuse-guard must consult REAL liveness: a crashed prior process must
    # NOT short-circuit start() into a no-op "already running" — it must fall
    # through and spawn a fresh server.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()  # crashed — poll() != None

    healthy = MagicMock(); healthy.ok = True
    fresh = _live_proc()
    with patch.object(mgr, "_diagnose_installation", return_value=healthy), \
         patch.object(mgr, "_sweep_orphan"), \
         patch("workflow_editor.llm.server_manager.find_free_port", return_value=5005), \
         patch("workflow_editor.llm.server_manager.subprocess.Popen", return_value=fresh) as popen, \
         patch.object(mgr, "_start_stderr_drain"), \
         patch.object(mgr, "_write_pid_file"), \
         patch.object(mgr, "_wait_for_server", return_value=True):
        assert mgr.start() is True
        # A FRESH process was spawned (the dead one did not satisfy reuse).
        popen.assert_called_once()
    assert mgr._server_process is fresh
    assert mgr._running is True


def test_start_reuses_when_prior_process_alive(tmp_path):
    # A genuinely live process IS reused — no respawn.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _live_proc()
    with patch("workflow_editor.llm.server_manager.subprocess.Popen") as popen, \
         patch.object(mgr, "_diagnose_installation") as diag:
        assert mgr.start() is True
        popen.assert_not_called()   # reused the live process
        diag.assert_not_called()    # never even reached the install probe


def test_ensure_running_noop_when_alive(tmp_path):
    # ensure_running short-circuits on a live+healthy server — it does NOT call
    # start().
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _live_proc()
    ok = MagicMock(); ok.status_code = 200
    with patch("workflow_editor.llm.server_manager.requests.get", return_value=ok), \
         patch.object(mgr, "start") as start:
        assert mgr.ensure_running() is True
        start.assert_not_called()


def test_ensure_running_relaunches_when_dead(tmp_path):
    # A crashed (process-dead) server → ensure_running delegates to start() and
    # returns its verdict.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()
    with patch.object(mgr, "start", return_value=True) as start:
        assert mgr.ensure_running() is True
        start.assert_called_once()


def test_ensure_running_respects_retirement(tmp_path):
    # A retired manager stays down: ensure_running calls start(), which refuses
    # (returns False) — no relaunch on a single-use manager.
    mgr = _mgr(tmp_path)
    mgr.stop()  # retires
    with patch("workflow_editor.llm.server_manager.subprocess.Popen") as popen:
        assert mgr.ensure_running() is False
        popen.assert_not_called()


def test_ensure_running_relaunches_wedged_server(tmp_path):
    # A WEDGED server: the process is ALIVE (poll() -> None) but /health returns
    # 500. is_alive is False, so start()'s poll-only reuse-guard would
    # short-circuit on the live-but-wedged handle and NEVER respawn. ensure_running
    # must tear the corpse down first so start() spawns a FRESH process and the
    # wedged handle is replaced.
    mgr = _mgr(tmp_path)
    mgr._running = True
    wedged = _live_proc()           # process polls alive
    mgr._server_process = wedged

    bad = MagicMock(); bad.status_code = 500   # /health dead -> not alive
    healthy = MagicMock(); healthy.ok = True
    fresh = _live_proc()
    with patch("workflow_editor.llm.server_manager.requests.get", return_value=bad), \
         patch.object(mgr, "_diagnose_installation", return_value=healthy), \
         patch.object(mgr, "_sweep_orphan"), \
         patch.object(mgr, "_stop_process") as stop_process, \
         patch("workflow_editor.llm.server_manager.find_free_port", return_value=5005), \
         patch("workflow_editor.llm.server_manager.subprocess.Popen", return_value=fresh) as popen, \
         patch.object(mgr, "_start_stderr_drain"), \
         patch.object(mgr, "_write_pid_file"), \
         patch.object(mgr, "_wait_for_server", return_value=True):
        assert mgr.ensure_running() is True
        # The wedged corpse was torn down (force-stop) AND a fresh server spawned.
        stop_process.assert_called()
        popen.assert_called_once()
    # The wedged handle was replaced by the respawned process.
    assert mgr._server_process is fresh
    assert mgr._server_process is not wedged
    assert mgr._running is True


def test_ensure_running_defers_when_start_in_flight(tmp_path):
    # is_alive reads False for EITHER "process dead" OR "a start/stop is in
    # flight" (is_running try-acquires _lock and reports not-running while it is
    # held). ensure_running must NOT relaunch on the in-flight case: another
    # thread is already booting this server. We simulate an in-flight boot by
    # holding _lock, then assert ensure_running bails (returns False, no start()).
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()  # is_alive -> False (process polled dead)
    mgr._lock.acquire()  # model "a start/stop is in flight"
    try:
        with patch.object(mgr, "start") as start, \
             patch.object(mgr, "_force_stop_for_relaunch") as force_stop:
            assert mgr.ensure_running() is False
            # Deferred to the in-flight boot: neither relaunched nor torn down.
            start.assert_not_called()
            force_stop.assert_not_called()
    finally:
        mgr._lock.release()


def test_ensure_running_relaunches_once_lock_free(tmp_path):
    # The lock-busy guard is transient: once the in-flight start/stop releases
    # _lock, a later ensure_running on a dead server relaunches as normal (the
    # guard never permanently wedges recovery).
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()
    assert not mgr._lock.locked()  # lock free -> proceed
    with patch.object(mgr, "start", return_value=True) as start:
        assert mgr.ensure_running() is True
        start.assert_called_once()


# --------------------------------------------------------------------------- #
# Retirement — the prewarm/manager-swap orphan race + the synchronous-stop UI  #
# freeze (BLOCKER A).                                                          #
# --------------------------------------------------------------------------- #

def test_retired_manager_start_returns_false_without_spawning(tmp_path):
    # stop() retires the manager permanently. A later start() — e.g. a stale
    # prewarm daemon thread that only NOW enters start() on a manager the swap
    # already retired — must refuse to launch: no install probe, no Popen, no
    # orphaned server.
    mgr = _mgr(tmp_path)
    mgr.stop()  # sets _retired = True
    assert mgr._retired is True

    with patch.object(mgr, "_diagnose_installation") as diag, \
         patch("workflow_editor.llm.server_manager.subprocess.Popen") as popen:
        assert mgr.start() is False
        # Retirement short-circuits BEFORE any install check or process spawn.
        diag.assert_not_called()
        popen.assert_not_called()


def test_wait_for_server_aborts_when_retired_mid_wait(tmp_path):
    # A stop() during boot (mid _wait_for_server) must interrupt the wait within
    # one poll tick instead of blocking the caller for the full startup timeout
    # — this is what kills the synchronous-stop UI freeze. We retire the manager
    # from another thread while the health poll is spinning and assert the wait
    # returns False quickly (well under the configured startup_timeout).
    mgr = _mgr(tmp_path)
    mgr._config.startup_timeout = 60.0  # the freeze window we must NOT block for
    mgr._server_process = None  # no process-died path; only the _retired guard fires

    def _retire_soon():
        time.sleep(0.2)
        mgr.stop()  # flips _retired before the lock

    # The health endpoint is never up (connection refused) so the loop relies on
    # the _retired check to bail.
    with patch("workflow_editor.llm.server_manager.requests.get",
               side_effect=__import__("requests").exceptions.ConnectionError()):
        t = threading.Thread(target=_retire_soon, daemon=True)
        started = time.time()
        t.start()
        result = mgr._wait_for_server()
        elapsed = time.time() - started

    assert result is False
    assert mgr._retired is True
    # Aborted within a couple of poll ticks, NOT after the 60s timeout.
    assert elapsed < 5.0


# --------------------------------------------------------------------------- #
# Auto-recovery backoff — give up after N consecutive failures; re-arm on a    #
# live server or a manual action (no crash-loop relaunch-every-tick).          #
# --------------------------------------------------------------------------- #

class _FakeSM:
    """A minimal server-manager stand-in for the MainWindow recovery seam.

    The MainWindow tick + recovery finally now read the strong predicate
    ``is_alive`` (process up AND /health 200), so the fake exposes both. By
    default ``is_alive`` tracks ``is_running``; ``wedged=True`` models a
    hung server (process up, /health dead) where ``is_running`` is True but
    ``is_alive`` is False.
    """
    def __init__(self, alive=False, retired=False, wedged=False):
        self._alive = alive
        self._retired = retired
        self._wedged = wedged
        self.ensure_calls = 0

    @property
    def is_running(self):
        # A wedged server's PROCESS is up even though /health is dead.
        return self._alive or self._wedged

    @property
    def is_alive(self):
        # Strong verdict: a wedged server reads NOT alive.
        return self._alive

    def ensure_running(self):
        self.ensure_calls += 1
        return self._alive  # stays down unless a test flips _alive


def _fake_window(sm):
    """A bare object carrying just what the recovery methods touch, so the
    MainWindow methods can be driven unbound (no Qt)."""
    import types
    from workflow_editor.main_window import MainWindow
    w = types.SimpleNamespace()
    w._server_manager = sm
    w._server_recovering = False
    w._server_recovery_attempts = 0
    # Off-thread health-probe state the new tick path reads/writes.
    w._last_server_alive = None
    w._health_probe_inflight = False
    # The cap is a MainWindow class attribute the unbound method reads off self.
    w._MAX_SERVER_RECOVERY_ATTEMPTS = MainWindow._MAX_SERVER_RECOVERY_ATTEMPTS
    # status_bar.showMessage / _refresh_server_indicator are side-effect-only.
    w.status_bar = MagicMock()
    w._refresh_server_indicator = MagicMock()
    return w


def _run_tick_sync(w):
    """Drive one _on_server_health_tick to completion SYNCHRONOUSLY.

    The tick is now fully off-thread: it spawns a daemon that probes
    ``is_alive`` and marshals the verdict back to the UI thread via
    ``_post_to_ui`` (a thread-safe ``_ui_call`` signal emit in production —
    NOT QTimer.singleShot, which is lost when emitted from a non-Qt daemon
    thread). To observe the whole
    probe -> result -> recover -> done chain in one call we (a) run every
    spawned thread body inline on ``.start()`` and (b) run every ``_post_to_ui``
    callback inline. The bound methods resolve off ``w`` (a SimpleNamespace), so
    we install an inline ``_post_to_ui`` on ``w`` itself."""
    from workflow_editor.main_window import MainWindow

    def _inline_thread(target, daemon=False):
        t = MagicMock()
        t.start.side_effect = target  # run the probe/recover body on .start()
        return t

    def _inline_post_to_ui(fn):
        fn()  # run the UI-thread continuation immediately (no event loop)

    w._post_to_ui = _inline_post_to_ui
    # Bind the UI-thread continuations as methods on the SimpleNamespace so the
    # inline _post_to_ui callbacks (which call self._on_*) resolve them.
    w._on_health_probe_result = lambda sm, alive: MainWindow._on_health_probe_result(w, sm, alive)
    w._recover = lambda sm: MainWindow._recover(w, sm)
    w._on_recover_done = lambda sm, ok: MainWindow._on_recover_done(w, sm, ok)

    with patch("workflow_editor.main_window.threading.Thread",
               side_effect=_inline_thread):
        MainWindow._on_server_health_tick(w)


def test_post_to_ui_uses_signal_not_singleshot():
    # The dispatch primitive: _post_to_ui must marshal via the _ui_call SIGNAL
    # (thread-safe, queued to the UI thread), NOT QTimer.singleShot — which,
    # called from a non-Qt daemon thread, creates the timer in the calling
    # thread and the callback can be lost (probe/recover would wedge forever).
    import types
    from workflow_editor.main_window import MainWindow

    w = types.SimpleNamespace()
    w._ui_call = MagicMock()  # stand-in for the bound Signal
    payload = object()
    with patch("PySide6.QtCore.QTimer") as qtimer:
        MainWindow._post_to_ui(w, payload)
    # Marshalled by emitting the signal with the exact callable...
    w._ui_call.emit.assert_called_once_with(payload)
    # ...and NOT by the lost-from-daemon QTimer.singleShot path.
    qtimer.singleShot.assert_not_called()


def test_auto_recovery_gives_up_after_three_failures():
    # A server that stays down: each tick relaunches and increments the failure
    # count; after MAX (3) consecutive failures we STOP spawning recoveries.
    from workflow_editor.main_window import MainWindow
    sm = _FakeSM(alive=False)
    w = _fake_window(sm)

    for _ in range(3):
        _run_tick_sync(w)
    # Three relaunch attempts, three failures recorded.
    assert sm.ensure_calls == 3
    assert w._server_recovery_attempts == 3
    assert w._server_recovery_attempts >= MainWindow._MAX_SERVER_RECOVERY_ATTEMPTS

    # A FOURTH tick must NOT relaunch — auto-recovery has given up.
    _run_tick_sync(w)
    assert sm.ensure_calls == 3  # unchanged: no new attempt


def test_auto_recovery_resets_when_server_back_alive():
    # After the give-up cap, the moment the server reads alive again the tick
    # resets the counter so a FUTURE crash is auto-recovered.
    from workflow_editor.main_window import MainWindow
    sm = _FakeSM(alive=False)
    w = _fake_window(sm)
    for _ in range(3):
        _run_tick_sync(w)
    assert w._server_recovery_attempts == 3

    # Server comes back: the alive branch resets the counter to 0.
    sm._alive = True
    _run_tick_sync(w)
    assert w._server_recovery_attempts == 0
    assert w._server_recovering is False

    # It then crashes again — auto-recovery is re-armed (relaunch fires).
    sm._alive = False
    _run_tick_sync(w)
    assert sm.ensure_calls == 4  # a fresh attempt after the reset


def test_reset_server_recovery_rearms_after_giveup():
    # A manual action (Restart backend / Start server) calls _reset_server_recovery
    # which clears the give-up state so the next tick auto-recovers again.
    from workflow_editor.main_window import MainWindow
    sm = _FakeSM(alive=False)
    w = _fake_window(sm)
    for _ in range(3):
        _run_tick_sync(w)
    assert w._server_recovery_attempts == 3  # gave up

    MainWindow._reset_server_recovery(w)
    assert w._server_recovery_attempts == 0
    assert w._server_recovering is False

    # Next tick relaunches again (no longer capped).
    _run_tick_sync(w)
    assert sm.ensure_calls == 4


def test_auto_recovery_triggers_on_wedged_server():
    # A WEDGED server (process up, /health dead) reads is_running True but
    # is_alive False. The tick must trust is_alive: it relaunches via
    # ensure_running AND counts the still-not-alive outcome as a failed attempt
    # toward the give-up cap (a process-poll-only tick would see "running",
    # never recover it, and show a green lie).
    sm = _FakeSM(alive=False, wedged=True)
    w = _fake_window(sm)
    _run_tick_sync(w)
    assert sm.ensure_calls == 1                 # wedge detected -> relaunch
    assert w._server_recovery_attempts == 1     # still not alive -> counts as a failure


def test_stop_manager_async_sets_retired_synchronously():
    # BLOCKER: the orphan race is only closed if _retired is armed
    # SYNCHRONOUSLY at the swap (on the calling thread), BEFORE the daemon
    # thread that calls stop() runs. stop() also sets it, but a stale prewarm
    # thread that races into the old manager's start() must already see
    # _retired==True the instant _stop_manager_async returns. We block stop()
    # on an event so it provably has NOT run yet, then assert _retired is True.
    from workflow_editor.main_window import MainWindow

    stop_gate = threading.Event()

    class _FakeManager:
        def __init__(self):
            self._retired = False

        def stop(self):
            # Block BEFORE touching _retired. So if _retired is True at the
            # assert, ONLY the synchronous arm in _stop_manager_async could have
            # set it — stop()'s own (idempotent) set has not run yet. This is
            # what proves the arm is synchronous, not a lucky-fast bg thread.
            stop_gate.wait(2.0)
            self._retired = True  # idempotent (mirrors the real stop())

    mgr = _FakeManager()
    # Call the method unbound — it only spawns the stop thread; it touches no
    # MainWindow instance state, so a bare object suffices as `self`.
    MainWindow._stop_manager_async(object(), mgr)

    # SYNCHRONOUS guarantee: _retired is already armed even though stop()'s
    # teardown is still blocked on the gate (deferred off this thread).
    assert mgr._retired is True
    stop_gate.set()  # let the daemon thread finish cleanly


def test_stale_health_probe_does_not_clear_live_inflight_flag():
    # Residual #1: a probe spawned for an OLD manager must not clear the CURRENT
    # manager's in-flight flag — otherwise the next tick stacks a 2nd concurrent
    # probe. The flag clear must happen AFTER the stale-manager guard returns.
    from workflow_editor.main_window import MainWindow
    current_sm = _FakeSM(alive=True)
    w = _fake_window(current_sm)
    w._health_probe_inflight = True  # the CURRENT manager's probe is in flight

    old_sm = _FakeSM(alive=True)  # a different (swapped-out) manager
    # A late result from the OLD manager's probe marshals back.
    MainWindow._on_health_probe_result(w, old_sm, True)

    # Dropped by the stale-manager guard WITHOUT clearing the live flag.
    assert w._health_probe_inflight is True
    # The stale verdict must not have driven the current indicator either.
    w._refresh_server_indicator.assert_not_called()


def test_current_health_probe_clears_inflight_flag():
    # The complement: the CURRENT manager's probe DOES clear its own in-flight
    # flag (so the next tick is unblocked).
    from workflow_editor.main_window import MainWindow
    sm = _FakeSM(alive=True)
    w = _fake_window(sm)
    w._health_probe_inflight = True

    MainWindow._on_health_probe_result(w, sm, True)

    assert w._health_probe_inflight is False
    w._refresh_server_indicator.assert_called_once_with(True)


# --- Settings-dialog probe hardening (residuals #2/#3/#4) -------------------
#
# The dialog methods are driven UNBOUND on a SimpleNamespace carrying just the
# widgets they touch (every widget is a MagicMock — no Qt event loop). The
# in-method ``import threading`` resolves the global ``threading`` module, so
# patching ``threading.Thread`` forces / observes the spawn path.


def _fake_dialog():
    import types
    self = types.SimpleNamespace()
    self.opencode_model = MagicMock()
    self.opencode_model.count.return_value = 0
    self.opencode_models_note = MagicMock()
    self.opencode_start_server = MagicMock()
    self.opencode_host = MagicMock()
    self.opencode_host.text.return_value = "127.0.0.1"
    self.opencode_port = MagicMock()
    self.opencode_port.value.return_value = 4096
    self._settings = {}
    self._server_manager = None
    self._models_probe_in_flight = False
    self._start_server_generation = 0
    self._closed = False
    # _post_to_ui is not reached on the spawn-failure paths under test.
    self._post_to_ui = MagicMock()
    return self


def _raising_thread(*args, **kwargs):
    t = MagicMock()
    t.start.side_effect = RuntimeError("can't start new thread")
    return t


def test_models_probe_spawn_failure_clears_inflight_flag():
    # Residual #3: a Thread.start() failure must clear _models_probe_in_flight
    # (and surface a note) — otherwise the flag wedges True and the note is stuck
    # at "Querying server…" forever, blocking every future Refresh.
    from workflow_editor.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    with patch("threading.Thread", side_effect=_raising_thread):
        SettingsDialog._populate_opencode_models(self)
    assert self._models_probe_in_flight is False
    # The last note is the failure message, not the stuck "Querying server…".
    assert "Querying server" not in self.opencode_models_note.setText.call_args[0][0]


def test_start_probe_spawn_failure_re_enables_button():
    # Residual #4: ensure_running Thread.start() failure must re-enable the Start
    # button and replace the "Starting…" note — not wedge it disabled forever.
    from workflow_editor.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    self._server_manager = _FakeSM(alive=False)
    self.parent = MagicMock(return_value=None)
    # The spawn fails BEFORE the poll timer is armed, so QTimer is never reached;
    # patch it anyway for safety (a SimpleNamespace is not a valid QObject parent).
    with patch("threading.Thread", side_effect=_raising_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsDialog._on_start_server_clicked(self)
    # Button re-enabled (last setEnabled call is True) after the spawn failure.
    assert self.opencode_start_server.setEnabled.call_args[0][0] is True
    assert "Starting" not in self.opencode_models_note.setText.call_args[0][0]


def test_stale_start_probe_result_is_dropped_by_generation():
    # Residual #2: a probe result from an EARLIER Start-server attempt (older
    # generation token) must be dropped — it must not stop a newer poll's timer,
    # re-enable the button, or list models for the wrong state.
    from workflow_editor.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    self._server_manager = _FakeSM(alive=False)
    self.parent = MagicMock(return_value=None)
    # First click installs _on_start_probe_result bound to generation 1.
    captured = {}

    def _capture_thread(target=None, daemon=False):
        t = MagicMock()
        t.start.side_effect = lambda: captured.setdefault("ran", True)
        return t

    with patch("threading.Thread", side_effect=_capture_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsDialog._on_start_server_clicked(self)
    handler_gen1 = self._on_start_probe_result
    assert self._start_server_generation == 1

    # A SECOND click bumps the generation to 2 (a newer poll owns the state).
    with patch("threading.Thread", side_effect=_capture_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsDialog._on_start_server_clicked(self)
    assert self._start_server_generation == 2

    # Reset observable widgets, then deliver the STALE gen-1 result (alive=True).
    self.opencode_start_server.reset_mock()
    self._populate_opencode_models = MagicMock()
    handler_gen1(True, 1)

    # Dropped: no button re-enable, no model re-query for the stale generation.
    self.opencode_start_server.setEnabled.assert_not_called()
    self._populate_opencode_models.assert_not_called()


def test_apply_models_no_ops_after_dialog_closed():
    # Residual #5: a model-fetch worker may post _apply_opencode_models() AFTER
    # the dialog closed; the C++ widgets are deleted. The _closed guard must drop
    # the late callback before it touches any widget.
    from workflow_editor.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    self._models_probe_in_flight = True
    self._closed = True  # done() ran
    SettingsDialog._apply_opencode_models(self, ["a/b"], "")
    # Returned early: no widget mutation, flag left as-is (not cleared on a dead
    # dialog — nothing reads it anymore).
    self.opencode_model.clear.assert_not_called()
    self.opencode_models_note.setText.assert_not_called()
