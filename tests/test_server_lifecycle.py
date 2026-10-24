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
import os
import shlex
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from procedo.studio.llm.opencode_backend import OpenCodeConfig
from procedo.studio.llm.server_health import find_free_port
from procedo.studio.llm.opencode_orphan_sweep import OrphanSweeper, PidIdentity
from procedo.studio.llm.server_manager import OpenCodeServerManager


def _proc_probe_stdout(*, alive=True, cmdline=None, environ=None):
    """Build a stdout blob matching the shape ``pid_is_our_opencode`` parses:
    an ALIVE/DEAD marker line, the NUL-joined cmdline, the env separator, then
    the NUL-joined environ. ``cmdline``/``environ`` are token lists."""
    marker = OrphanSweeper._ID_ALIVE if alive else OrphanSweeper._ID_DEAD
    cmd_blob = "\0".join(cmdline or [])
    env_blob = "\0".join(environ or [])
    sep = OrphanSweeper._ID_SEP
    # Mirrors the real shell: "<marker>\n<cmdline>\n<SEP>\n<environ>".
    return f"{marker}\n{cmd_blob}\n{sep}\n{env_blob}"


def _pid_file(launch_dir: Path) -> Path:
    """THIS process's pid file in *launch_dir* (one per Studio process)."""
    from procedo.studio.llm import launch_files
    return launch_files.launch_file_path(launch_dir, launch_files.PID_FILE)


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
    assert f"/mnt/c/Users/me/.workflow_editor/opencode/launch/opencode-{os.getpid()}.json" in cmd
    assert "OPENCODE_ENABLE_EXA=1" in cmd
    assert "opencode serve --port 5123" in cmd


def test_serve_command_omits_config_without_launch_dir():
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    cmd = mgr._build_serve_command(6000)
    assert "OPENCODE_CONFIG=" not in cmd
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in cmd  # OpenCode's own discovery then
    assert "opencode serve --port 6000" in cmd


def test_no_opencode_json_up_the_launch_dir_can_override_this_studio_s_config():
    # OpenCode merges every opencode.json from its cwd (the launch dir) up to / AFTER
    # OPENCODE_CONFIG — the old shared launch/opencode.json, its parent's relic: it must not look.
    cmd = OpenCodeServerManager(OpenCodeConfig(working_directory="/mnt/c/foo/launch"))._build_serve_command(1)
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in shlex.split(cmd)


def test_serve_command_translates_posix_launch_dir():
    cfg = OpenCodeConfig(working_directory="/mnt/c/foo/launch")
    cmd = OpenCodeServerManager(cfg)._build_serve_command(7000)
    assert f"OPENCODE_CONFIG=/mnt/c/foo/launch/opencode-{os.getpid()}.json" in cmd


def test_serve_command_shell_quotes_spaced_launch_dir():
    # A space in the home/launch path must be shell-quoted so it cannot break
    # the bash -ic command into two tokens (MAJOR fix).
    cfg = OpenCodeConfig(working_directory=r"C:\Users\John Doe\.workflow_editor\launch")
    cmd = OpenCodeServerManager(cfg)._build_serve_command(7100)
    # The config token is quoted as a single shell word; the raw space-bearing
    # path never appears unquoted.
    cfg_wsl = f"/mnt/c/Users/John Doe/.workflow_editor/launch/opencode-{os.getpid()}.json"
    assert f"OPENCODE_CONFIG='{cfg_wsl}'" in cmd
    # Round-trips through the shell lexer as ONE token (no split on the space).
    tokens = shlex.split(cmd)
    assert cfg_wsl in [t.split("=", 1)[1] for t in tokens if t.startswith("OPENCODE_CONFIG=")]


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
    mgr._orphans.write_pid_file(4242, 5005)
    pid_file = _pid_file(tmp_path)
    assert pid_file.exists()
    data = json.loads(pid_file.read_text())
    assert data["pid"] == 4242 and data["port"] == 5005
    assert mgr._orphans.read_pid_file()["pid"] == 4242


def test_remove_pid_file_is_safe_when_absent(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._orphans.remove_pid_file(4242)  # no file yet → must not raise
    assert not (_pid_file(tmp_path)).exists()


def test_pid_file_path_none_without_launch_dir():
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    assert mgr._orphans.pid_file_path() is None


def test_sweep_orphan_kills_our_opencode_pid(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._orphans.write_pid_file(9999, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        return r

    # Identity is OURS on the first check and STILL ours on the post-TERM
    # re-check → graceful TERM, then escalate to KILL -9.
    with patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.OURS), \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._orphans.sweep_orphan()

    flat = [" ".join(c) for c in calls]
    # A graceful TERM AND an escalated KILL were issued against OUR pid's tree.
    assert any("kill -TERM" in f and "t 9999" in f for f in flat)
    assert any("kill -KILL" in f and "t 9999" in f for f in flat)
    # The stale file is gone after a conclusive sweep.
    assert not (_pid_file(tmp_path)).exists()


def test_sweep_orphan_spares_non_opencode_pid(tmp_path):
    # The recorded pid was recycled to an unrelated process: cmdline check fails
    # → we must NOT kill it (only clear the stale file).
    mgr = _mgr(tmp_path)
    mgr._orphans.write_pid_file(1234, 5005)

    with patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.NOT_OURS), \
         patch("procedo.studio.llm.server_manager.subprocess.run") as run:
        mgr._orphans.sweep_orphan()
        run.assert_not_called()
    assert not (_pid_file(tmp_path)).exists()


def test_sweep_orphan_noop_without_pid_file(tmp_path):
    mgr = _mgr(tmp_path)
    with patch("procedo.studio.llm.server_manager.subprocess.run") as run:
        mgr._orphans.sweep_orphan()  # no pid file at all
        run.assert_not_called()


def test_pid_is_our_opencode_ours_on_environ_config_match(tmp_path):
    # cmdline is an opencode serve AND environ carries OUR launch-dir
    # opencode.json as an EXACT OPENCODE_CONFIG=<our_cfg> token → OURS.
    mgr = _mgr(tmp_path)
    our_cfg = mgr._orphans.our_config_wsl_path()
    assert our_cfg is not None
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005", "--hostname", "127.0.0.1"],
        environ=[f"OPENCODE_CONFIG={our_cfg}", "PATH=/usr/bin"],
    )
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.OURS


def test_pid_is_our_opencode_ours_on_argv_config_match(tmp_path):
    # Config asserted via argv `--config <our_cfg>` (two exact tokens) → OURS.
    mgr = _mgr(tmp_path)
    our_cfg = mgr._orphans.our_config_wsl_path()
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["/usr/bin/opencode", "serve", "--config", our_cfg, "--port", "5005"],
        environ=["PATH=/usr/bin"],
    )
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.OURS


def test_pid_is_our_opencode_not_ours_when_pid_dead(tmp_path):
    # /proc/<pid> absent → DEAD marker → conclusively NOT_OURS (safe to forget).
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(alive=False)
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_not_ours_for_other_process(tmp_path):
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["/usr/bin/python3", "some_other_server.py"],
        environ=["PATH=/usr/bin"],
    )
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


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
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_not_ours_for_backup_config_substring(tmp_path):
    # The pid IS an opencode serve and its OPENCODE_CONFIG is `<our_cfg>.backup`
    # — a SUPERSTRING of ours. The old `our_cfg in out` substring check matched
    # and would have killed a non-owned server; the EXACT-token match rejects it
    # (MAJOR-2).
    mgr = _mgr(tmp_path)
    our_cfg = mgr._orphans.our_config_wsl_path()
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005"],
        environ=[f"OPENCODE_CONFIG={our_cfg}.backup"],
    )
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.NOT_OURS


def test_pid_is_our_opencode_unknown_on_read_timeout(tmp_path):
    # A /proc read timeout / WSL hiccup is INCONCLUSIVE → UNKNOWN, never
    # NOT_OURS (MAJOR-1). The TimeoutExpired path.
    mgr = _mgr(tmp_path)
    import subprocess as _sp
    with patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=_sp.TimeoutExpired(cmd="wsl", timeout=3)):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.UNKNOWN


def test_pid_is_our_opencode_unknown_when_no_marker(tmp_path):
    # The shell ran but emitted NEITHER alive nor dead marker (a WSL-level
    # failure, e.g. bash not found in the relay) → UNKNOWN, not NOT_OURS.
    mgr = _mgr(tmp_path)
    result = MagicMock()
    result.returncode = 0
    result.stdout = "some unrelated noise without any marker"
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.UNKNOWN


def test_pid_is_our_opencode_falls_back_to_serve_without_launch_dir():
    # No launch dir → no config identity to assert → an `opencode serve` is
    # OURS on the serve-token check alone (the strongest marker available).
    mgr = OpenCodeServerManager(OpenCodeConfig(working_directory=None))
    assert mgr._orphans.our_config_wsl_path() is None
    result = MagicMock()
    result.returncode = 0
    result.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "5005", "--hostname", "127.0.0.1"],
        environ=["PATH=/usr/bin"],
    )
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.OURS


def test_sweep_orphan_keeps_pid_file_on_unknown(tmp_path):
    # Identity is INCONCLUSIVE (UNKNOWN): no kill, AND the pid file is KEPT so
    # the next launch retries — never permanently lose a still-running orphan
    # (MAJOR-1).
    mgr = _mgr(tmp_path)
    mgr._orphans.write_pid_file(4321, 5005)
    with patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.UNKNOWN), \
         patch("procedo.studio.llm.server_manager.subprocess.run") as run:
        mgr._orphans.sweep_orphan()
        run.assert_not_called()
    # Pid file SURVIVES the inconclusive sweep.
    assert (_pid_file(tmp_path)).exists()


def test_sweep_orphan_skips_kill9_when_recheck_not_ours(tmp_path):
    # OURS on the first check → graceful TERM issued; but the post-TERM re-check
    # is NOT_OURS (pid gone / recycled in the gap) → KILL -9 must be SKIPPED
    # (PID-reuse TOCTOU, MAJOR-3). Exactly one subprocess.run (the TERM).
    mgr = _mgr(tmp_path)
    mgr._orphans.write_pid_file(7777, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(" ".join(cmd))
        r = MagicMock()
        r.returncode = 0
        return r

    with patch.object(mgr._orphans, "pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.NOT_OURS]), \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._orphans.sweep_orphan()

    # The graceful TERM ran; the -9 did NOT.
    assert any("kill -TERM" in c and "t 7777" in c for c in calls)
    assert not any("kill -KILL" in c for c in calls)
    # A conclusive OURS-then-gone sweep clears the (now stale) pid file.
    assert not (_pid_file(tmp_path)).exists()


def test_sweep_orphan_keeps_pid_file_when_recheck_unknown(tmp_path):
    # OURS on the first check → graceful TERM; but the post-TERM re-check is
    # UNKNOWN (a /proc read hiccup). The orphan may still be alive, so KILL -9 is
    # SKIPPED *and* the pid file is KEPT for a next-launch retry — never delete on
    # an inconclusive post-TERM check (gpt-5.5 edge).
    mgr = _mgr(tmp_path)
    mgr._orphans.write_pid_file(8888, 5005)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(" ".join(cmd))
        r = MagicMock()
        r.returncode = 0
        return r

    with patch.object(mgr._orphans, "pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.UNKNOWN]), \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._orphans.sweep_orphan()

    # TERM ran; -9 did NOT (inconclusive re-check).
    assert any("kill -TERM" in c and "t 8888" in c for c in calls)
    assert not any("kill -KILL" in c for c in calls)
    # Pid file SURVIVES — a possibly-running orphan is never lost.
    assert (_pid_file(tmp_path)).exists()


# --------------------------------------------------------------------------- #
# is_available() is install-only (no server probe)                            #
# --------------------------------------------------------------------------- #

def test_is_available_is_install_check_only(tmp_path):
    mgr = _mgr(tmp_path)
    healthy = MagicMock()
    healthy.ok = True
    with patch.object(mgr, "_diagnose_installation", return_value=healthy) as diag, \
         patch("procedo.studio.llm.server_manager.requests.get") as get:
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
    with patch("procedo.studio.llm.server_manager.requests.get", return_value=ok):
        assert mgr.is_alive is True
    with patch("procedo.studio.llm.server_manager.requests.get", return_value=bad):
        assert mgr.is_alive is False


def test_is_alive_false_when_process_dead_without_http(tmp_path):
    # A dead process short-circuits is_alive — no HTTP probe is even attempted.
    mgr = _mgr(tmp_path)
    mgr._running = True
    mgr._server_process = _dead_proc()
    with patch("procedo.studio.llm.server_manager.requests.get") as get:
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
         patch.object(mgr._orphans, "sweep_orphan"), \
         patch("procedo.studio.llm.server_manager.find_free_port", return_value=5005), \
         patch("procedo.studio.llm.server_manager.subprocess.Popen", return_value=fresh) as popen, \
         patch.object(mgr, "_start_stderr_drain"), \
         patch.object(mgr._orphans, "write_pid_file"), \
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
    with patch("procedo.studio.llm.server_manager.subprocess.Popen") as popen, \
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
    with patch("procedo.studio.llm.server_manager.requests.get", return_value=ok), \
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
    with patch("procedo.studio.llm.server_manager.subprocess.Popen") as popen:
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
    with patch("procedo.studio.llm.server_manager.requests.get", return_value=bad), \
         patch.object(mgr, "_diagnose_installation", return_value=healthy), \
         patch.object(mgr._orphans, "sweep_orphan"), \
         patch.object(mgr, "_stop_process") as stop_process, \
         patch("procedo.studio.llm.server_manager.find_free_port", return_value=5005), \
         patch("procedo.studio.llm.server_manager.subprocess.Popen", return_value=fresh) as popen, \
         patch.object(mgr, "_start_stderr_drain"), \
         patch.object(mgr._orphans, "write_pid_file"), \
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
         patch("procedo.studio.llm.server_manager.subprocess.Popen") as popen:
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
    with patch("procedo.studio.llm.server_manager.requests.get",
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
# stop() kills only the process tree WE started — by pid, never by name        #
# --------------------------------------------------------------------------- #

def _fake_spawn(stderr_lines, *, alive=True):
    """A fake ``wsl.exe`` Popen handle (Windows pid 4242) whose stderr carries
    ``stderr_lines`` — the serve command's Linux-pid marker among wsl noise."""
    proc = _live_proc() if alive else _dead_proc()
    proc.stderr = iter(stderr_lines)
    return proc


def _start_with(mgr, proc, runs):
    """start() against a fake launcher; every subprocess.run is recorded."""
    healthy = MagicMock(); healthy.ok = True

    def fake_run(cmd, *a, **k):
        runs.append(list(cmd))
        r = MagicMock(); r.returncode = 0; r.stdout = ""
        return r

    with patch.object(mgr, "_diagnose_installation", return_value=healthy), \
         patch("procedo.studio.llm.server_manager.find_free_port", return_value=5005), \
         patch("procedo.studio.llm.server_manager.subprocess.Popen", return_value=proc), \
         patch("procedo.studio.llm.server_manager.subprocess.run", side_effect=fake_run), \
         patch.object(mgr, "_wait_for_server", return_value=True):
        assert mgr.start() is True


def _kill_runs(runs):
    return [" ".join(c) for c in runs if "kill" in " ".join(c)]


def test_stop_kills_the_linux_pid_it_started_never_by_name(tmp_path):
    mgr = _mgr(tmp_path)
    runs = []
    proc = _fake_spawn(["wsl: Failed to translate 'L:\\libcom'\n", "__OC_PID__31337\n",
                        "opencode booting\n"])
    _start_with(mgr, proc, runs)
    # The pid file records the LINUX pid the serve command printed, not wsl.exe's.
    deadline = time.monotonic() + 5
    while mgr._orphans.read_pid_file() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mgr._orphans.read_pid_file()["pid"] == 31337

    with patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=lambda cmd, *a, **k: runs.append(list(cmd)) or MagicMock()):
        mgr.stop()

    flat = [" ".join(c) for c in runs]
    assert not any("pkill" in f or "killall" in f or " -f " in f for f in flat)
    kills = _kill_runs(runs)
    assert len(kills) == 1 and "kill -TERM" in kills[0]
    assert "31337" in kills[0] and "4242" not in kills[0]
    # The bare pid is checked first: still an `opencode serve` on OUR port, else no kill.
    assert "/proc/31337/cmdline" in kills[0]
    assert "'opencode serve --port 5005 ' || exit 0" in kills[0]
    proc.terminate.assert_called_once()  # wsl.exe itself: through OUR handle
    assert mgr._orphans.read_pid_file() is None
    # The marker never reaches the failure-reason tail.
    assert not any("__OC_PID__" in line for line in mgr._stderr_tail)


def test_stop_skips_linux_kill_once_our_wsl_exe_exited(tmp_path):
    # Our wsl.exe already exited, so the Linux process is gone and its pid may
    # belong to someone else now: no Linux-side kill at all.
    mgr = _mgr(tmp_path)
    runs = []
    mgr._server_process = _fake_spawn(["__OC_PID__31337\n"], alive=False)
    mgr._start_stderr_drain(5005)
    with patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=lambda cmd, *a, **k: runs.append(list(cmd)) or MagicMock()):
        mgr._stop_process()
    assert _kill_runs(runs) == []


def test_stop_without_a_linux_pid_issues_no_name_kill(tmp_path, monkeypatch):
    # The marker never arrived (bash still in .bashrc): stop through the wsl.exe
    # handle only; never fall back to a name-based kill.
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(OpenCodeServerManager, "_LINUX_PID_WAIT", 0.01)
    runs = []
    proc = _fake_spawn([])
    mgr._server_process = proc
    mgr._start_stderr_drain(5005)
    with patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=lambda cmd, *a, **k: runs.append(list(cmd)) or MagicMock()):
        mgr._stop_process()
    assert _kill_runs(runs) == []
    proc.terminate.assert_called_once()


def _another_studios_record(tmp_path, owner=777777, pid=5150, port=6060):
    """The pid file a SECOND Studio process (Windows pid *owner*) wrote in the shared dir."""
    from procedo.studio.llm import launch_files
    path = launch_files.launch_file_path(tmp_path, launch_files.PID_FILE, owner)
    path.write_text(json.dumps({"pid": pid, "port": port, "started_at": 0}), encoding="utf-8")
    return path


def test_sweep_never_touches_a_live_studio_s_server(tmp_path):
    # Bench opens a second Studio: its start() must leave the first one's LIVE
    # server and record alone, even though the record names OUR config.
    mgr = _mgr(tmp_path)
    other = _another_studios_record(tmp_path)
    with patch("procedo.studio.llm.opencode_orphan_sweep.pid_alive", return_value=True), \
         patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.OURS) as ident, \
         patch("procedo.studio.llm.server_manager.subprocess.run") as run:
        mgr._orphans.sweep_orphan()
    run.assert_not_called()
    ident.assert_not_called()
    assert other.exists()


def test_sweep_kills_the_orphan_of_a_dead_studio(tmp_path):
    mgr = _mgr(tmp_path)
    other = _another_studios_record(tmp_path)
    calls = []
    with patch("procedo.studio.llm.opencode_orphan_sweep.pid_alive", return_value=False), \
         patch.object(mgr._orphans, "pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.NOT_OURS]), \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=lambda cmd, *a, **k: calls.append(" ".join(cmd)) or MagicMock()):
        mgr._orphans.sweep_orphan()
    assert any("kill -TERM" in c and "t 5150" in c and "--port 6060 " in c for c in calls)
    assert not other.exists()


def test_the_identity_check_matches_the_config_token_the_server_runs_with(tmp_path):
    # The environ token a launched server carries is the serve command's OPENCODE_CONFIG
    # after the shell unquotes it (a spaced path included): the sweep must call it OURS.
    launch = tmp_path / "Studio data" / "launch"
    mgr = _mgr(launch)
    tokens = shlex.split(mgr._build_serve_command(5005))
    environ = [t for t in tokens if t.startswith("OPENCODE_CONFIG=")]
    assert environ == [f"OPENCODE_CONFIG={mgr._orphans.our_config_wsl_path()}"]
    result = MagicMock()
    result.stdout = _proc_probe_stdout(cmdline=["opencode", "serve", "--port", "5005"],
                                       environ=environ)
    with patch("procedo.studio.llm.server_manager.subprocess.run", return_value=result):
        assert mgr._orphans.pid_is_our_opencode(4242) is PidIdentity.OURS
        # Another Studio's server runs with ITS config: not ours unless we sweep its record.
        assert mgr._orphans.pid_is_our_opencode(4242, 777777) is PidIdentity.NOT_OURS


def test_the_sweep_identifies_a_dead_studio_s_server_by_that_studio_s_config(tmp_path):
    from procedo.studio.llm import launch_files
    mgr = _mgr(tmp_path)
    other = _another_studios_record(tmp_path)
    probe = MagicMock()
    probe.stdout = _proc_probe_stdout(
        cmdline=["opencode", "serve", "--port", "6060"],
        environ=[f"OPENCODE_CONFIG={launch_files.config_wsl_path(tmp_path, 777777)}"])
    gone = MagicMock()
    gone.stdout = _proc_probe_stdout(alive=False)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(" ".join(cmd))
        if "/proc/5150/environ" in cmd[-1]:
            return probe if len([c for c in calls if "environ" in c]) == 1 else gone
        return MagicMock()

    with patch("procedo.studio.llm.opencode_orphan_sweep.pid_alive", return_value=False), \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run", side_effect=fake_run):
        mgr._orphans.sweep_orphan()
    assert any("kill -TERM" in c and "t 5150" in c for c in calls)
    assert not other.exists()


def _launch_files(tmp_path, owner):
    from procedo.studio.llm import launch_files
    paths = [launch_files.launch_file_path(tmp_path, kind, owner) for kind in
             (launch_files.PID_FILE, launch_files.CONFIG_FILE, launch_files.UNIVERSE_FILE)]
    for path in paths:
        path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    return paths


def test_the_sweep_removes_a_dead_studio_s_launch_files_never_the_shared_legacy_ones(tmp_path):
    from procedo.studio.llm import launch_files
    mgr = _mgr(tmp_path)
    dead, live = 777777, 888888
    dead_files = _launch_files(tmp_path, dead)
    live_files = _launch_files(tmp_path, live)
    own_config = launch_files.launch_file_path(tmp_path, launch_files.CONFIG_FILE)
    own_config.write_text("{}", encoding="utf-8")
    legacy = [tmp_path / name for name in ("opencode.pid", "opencode.json", "run_skill_universe.json")]
    for path in legacy:
        path.write_text("{}", encoding="utf-8")
    with patch("procedo.studio.llm.opencode_orphan_sweep.pid_alive", side_effect=lambda pid: pid == live), \
         patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.NOT_OURS):
        mgr._orphans.sweep_orphan()
    assert [p.exists() for p in dead_files] == [False, False, False]
    assert all(p.exists() for p in live_files)
    assert own_config.exists()  # built for the server about to start
    # An old-format Studio still running reads them (its server record, its relaunch config).
    assert all(p.exists() for p in legacy)


def test_a_dead_studio_s_config_stays_while_its_server_record_does(tmp_path):
    # An inconclusive identity keeps the record for the next sweep: its files stay with it.
    mgr = _mgr(tmp_path)
    files = _launch_files(tmp_path, 777777)
    with patch("procedo.studio.llm.opencode_orphan_sweep.pid_alive", return_value=False), \
         patch.object(mgr._orphans, "pid_is_our_opencode", return_value=PidIdentity.UNKNOWN):
        mgr._orphans.sweep_orphan()
    assert all(p.exists() for p in files)


def test_removing_a_studio_s_launch_files_keeps_its_server_record_and_other_studios_files(tmp_path):
    from procedo.studio.llm import launch_files
    own = _launch_files(tmp_path, os.getpid())
    other = _launch_files(tmp_path, 777777)
    launch_files.remove_launch_files(tmp_path)
    assert [p.exists() for p in own] == [True, False, False]
    assert all(p.exists() for p in other)


def test_the_studio_exit_stops_the_server_and_removes_its_launch_files(tmp_path):
    from types import SimpleNamespace

    from procedo.studio.controllers.llm_server_controller import LLMServerController
    own = _launch_files(tmp_path, os.getpid())
    sm = MagicMock()
    sm.config = OpenCodeConfig(working_directory=str(tmp_path))
    LLMServerController._stop_current_server(SimpleNamespace(server_manager=sm))
    sm.stop.assert_called_once()
    assert [p.exists() for p in own] == [True, False, False]


def test_the_self_spawned_server_is_pointed_at_this_studio_s_config(tmp_path):
    from procedo.studio.llm.lifecycle_adapters import make_wsl_lifecycle
    cfg = OpenCodeConfig(working_directory=str(tmp_path / "launch test"))
    command = shlex.split(make_wsl_lifecycle(cfg)._command()[-1])
    assert f"OPENCODE_CONFIG={_mgr(tmp_path / 'launch test')._orphans.our_config_wsl_path()}" in command
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in command


def test_an_old_manager_s_stop_keeps_the_new_manager_s_record(tmp_path):
    # A manager swap in one Studio: the retired manager's (async) stop must not
    # delete the record its successor already wrote in the same file.
    old, new = _mgr(tmp_path), _mgr(tmp_path)
    old._orphans.write_pid_file(111, 5005)
    new._orphans.write_pid_file(222, 5006)
    old._orphans.remove_pid_file(111)
    assert new._orphans.read_pid_file()["pid"] == 222


def test_stop_keeps_the_record_when_wsl_exe_already_exited(tmp_path):
    # No Linux kill was possible (the pid may be reused): keep the record so the
    # next start's identity-checked sweep can still find a surviving server.
    mgr = _mgr(tmp_path)
    mgr._server_process = _fake_spawn(["__OC_PID__31337\n"], alive=False)
    mgr._start_stderr_drain(5005)
    deadline = time.monotonic() + 5
    while mgr._orphans.read_pid_file() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    with patch("procedo.studio.llm.server_manager.subprocess.run") as run:
        mgr._stop_process()
    run.assert_not_called()
    assert mgr._orphans.read_pid_file()["pid"] == 31337


def test_a_spawn_without_stderr_forgets_the_previous_spawn_s_pid(tmp_path):
    mgr = _mgr(tmp_path)
    mgr._server_process = _fake_spawn(["__OC_PID__31337\n"])
    mgr._start_stderr_drain(5005)
    mgr._server_process = _live_proc()
    mgr._server_process.stderr = None
    mgr._start_stderr_drain(5006)
    assert mgr._spawned is None


def test_serve_command_prints_its_linux_pid_then_execs_opencode(tmp_path):
    cmd = _mgr(tmp_path)._build_serve_command(5123)
    assert cmd.startswith("echo __OC_PID__$$ >&2; ")
    assert "exec opencode serve --port 5123" in cmd


# --------------------------------------------------------------------------- #
# Auto-recovery backoff — give up after N consecutive failures; re-arm on a    #
# live server or a manual action (no crash-loop relaunch-every-tick).          #
# --------------------------------------------------------------------------- #

class _FakeSM:
    """A minimal server-manager stand-in for the LLMServerController recovery seam.

    The controller's tick + recovery finally now read the strong predicate
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
    LLMServerController methods can be driven unbound (no Qt)."""
    import types
    from procedo.studio.controllers.llm_server_controller import LLMServerController
    w = types.SimpleNamespace()
    w.server_manager = sm
    w._server_recovering = False
    w._server_recovery_attempts = 0
    # Off-thread health-probe state the new tick path reads/writes.
    w._last_server_alive = None
    w._health_probe_inflight = False
    # The cap is a LLMServerController class attribute the unbound method reads off self.
    w._MAX_SERVER_RECOVERY_ATTEMPTS = LLMServerController._MAX_SERVER_RECOVERY_ATTEMPTS
    # The window's footer.show_message / refresh_indicator are side-effect-only.
    w._mw = types.SimpleNamespace(footer=MagicMock())
    w.refresh_indicator = MagicMock()
    return w


def _run_tick_sync(w):
    """Drive one _on_server_health_tick to completion SYNCHRONOUSLY.

    The tick is fully off-thread: it spawns a daemon that probes ``is_alive``
    and answers the verdict to the UI thread through a ``procedo.core.off_gui``
    relay (``_health_verdict``; NOT QTimer.singleShot, which is lost when
    emitted from a non-Qt daemon thread). To observe the whole
    probe -> result -> recover -> done chain in one call we (a) run every
    spawned thread body inline on ``.start()`` and (b) deliver every answer
    inline. The bound methods resolve off ``w`` (a SimpleNamespace), so we
    install the inline answer channels and UI-thread continuations on it."""
    from procedo.studio.controllers.llm_server_controller import LLMServerController

    def _inline_thread(target, args=(), daemon=False):
        t = MagicMock()
        t.start.side_effect = lambda: target(*args)  # run the probe/recover body on .start()
        return t

    # The UI-thread continuations, bound on the SimpleNamespace, and the answer
    # channels delivering to them at once (no event loop).
    w._on_health_probe_result = lambda sm, alive: LLMServerController._on_health_probe_result(w, sm, alive)
    w._on_recover_done = lambda sm, ok: LLMServerController._on_recover_done(w, sm, ok)
    w._health_verdict = lambda verdict: LLMServerController._on_health_verdict(w, verdict)
    w._recovered = lambda verdict: LLMServerController._on_recovered(w, verdict)

    with patch("procedo.studio.controllers.llm_server_controller.threading.Thread",
               side_effect=_inline_thread):
        LLMServerController._on_server_health_tick(w)


def test_auto_recovery_gives_up_after_three_failures():
    # A server that stays down: each tick relaunches and increments the failure
    # count; after MAX (3) consecutive failures we STOP spawning recoveries.
    from procedo.studio.controllers.llm_server_controller import LLMServerController
    sm = _FakeSM(alive=False)
    w = _fake_window(sm)

    for _ in range(3):
        _run_tick_sync(w)
    # Three relaunch attempts, three failures recorded.
    assert sm.ensure_calls == 3
    assert w._server_recovery_attempts == 3
    assert w._server_recovery_attempts >= LLMServerController._MAX_SERVER_RECOVERY_ATTEMPTS

    # A FOURTH tick must NOT relaunch — auto-recovery has given up.
    _run_tick_sync(w)
    assert sm.ensure_calls == 3  # unchanged: no new attempt


def test_auto_recovery_resets_when_server_back_alive():
    # After the give-up cap, the moment the server reads alive again the tick
    # resets the counter so a FUTURE crash is auto-recovered.
    from procedo.studio.controllers.llm_server_controller import LLMServerController
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
    # A manual action (Restart backend / Start server) calls reset_recovery
    # which clears the give-up state so the next tick auto-recovers again.
    from procedo.studio.controllers.llm_server_controller import LLMServerController
    sm = _FakeSM(alive=False)
    w = _fake_window(sm)
    for _ in range(3):
        _run_tick_sync(w)
    assert w._server_recovery_attempts == 3  # gave up

    LLMServerController.reset_recovery(w)
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
    from procedo.studio.controllers.llm_server_controller import LLMServerController

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
    # LLMServerController instance state, so a bare object suffices as `self`.
    LLMServerController._stop_manager_async(object(), mgr)

    # SYNCHRONOUS guarantee: _retired is already armed even though stop()'s
    # teardown is still blocked on the gate (deferred off this thread).
    assert mgr._retired is True
    stop_gate.set()  # let the daemon thread finish cleanly


def test_stale_health_probe_does_not_clear_live_inflight_flag():
    # Residual #1: a probe spawned for an OLD manager must not clear the CURRENT
    # manager's in-flight flag — otherwise the next tick stacks a 2nd concurrent
    # probe. The flag clear must happen AFTER the stale-manager guard returns.
    from procedo.studio.controllers.llm_server_controller import LLMServerController
    current_sm = _FakeSM(alive=True)
    w = _fake_window(current_sm)
    w._health_probe_inflight = True  # the CURRENT manager's probe is in flight

    old_sm = _FakeSM(alive=True)  # a different (swapped-out) manager
    # A late result from the OLD manager's probe marshals back.
    LLMServerController._on_health_probe_result(w, old_sm, True)

    # Dropped by the stale-manager guard WITHOUT clearing the live flag.
    assert w._health_probe_inflight is True
    # The stale verdict must not have driven the current indicator either.
    w.refresh_indicator.assert_not_called()


def test_current_health_probe_clears_inflight_flag():
    # The complement: the CURRENT manager's probe DOES clear its own in-flight
    # flag (so the next tick is unblocked).
    from procedo.studio.controllers.llm_server_controller import LLMServerController
    sm = _FakeSM(alive=True)
    w = _fake_window(sm)
    w._health_probe_inflight = True

    LLMServerController._on_health_probe_result(w, sm, True)

    assert w._health_probe_inflight is False
    w.refresh_indicator.assert_called_once_with(True)


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
    self._closed = False
    # The probe's answer channel: not reached on the spawn-failure paths under test.
    self._models_answer = MagicMock()
    return self


def _fake_probe(server_manager):
    """A SettingsBackendProbe driven UNBOUND on a namespace of MagicMock widgets."""
    import types
    self = types.SimpleNamespace()
    self._start_button = MagicMock()
    self._note = MagicMock()
    self._server_manager = server_manager
    self._populate_models = MagicMock()
    self._start_server_generation = 0
    self._start_probe_answer = MagicMock()
    self._owner = MagicMock()
    self._owner.parent.return_value = None
    return self


def _raising_thread(*args, **kwargs):
    t = MagicMock()
    t.start.side_effect = RuntimeError("can't start new thread")
    return t


def test_models_probe_spawn_failure_clears_inflight_flag():
    # Residual #3: a Thread.start() failure must clear _models_probe_in_flight
    # (and surface a note) — otherwise the flag wedges True and the note is stuck
    # at "Querying server…" forever, blocking every future Refresh.
    from procedo.studio.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    with patch("threading.Thread", side_effect=_raising_thread):
        SettingsDialog._populate_opencode_models(self)
    assert self._models_probe_in_flight is False
    # The last note is the failure message, not the stuck "Querying server…".
    assert "Querying server" not in self.opencode_models_note.setText.call_args[0][0]


def test_start_probe_spawn_failure_re_enables_button():
    # Residual #4: ensure_running Thread.start() failure must re-enable the Start
    # button and replace the "Starting…" note — not wedge it disabled forever.
    from procedo.studio.dialogs.settings_backend_probe import SettingsBackendProbe
    self = _fake_probe(_FakeSM(alive=False))
    # The spawn fails BEFORE the poll timer is armed, so QTimer is never reached;
    # patch it anyway for safety (a SimpleNamespace is not a valid QObject parent).
    with patch("threading.Thread", side_effect=_raising_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsBackendProbe.start_server(self)
    # Button re-enabled (last setEnabled call is True) after the spawn failure.
    assert self._start_button.setEnabled.call_args[0][0] is True
    assert "Starting" not in self._note.setText.call_args[0][0]


def test_stale_start_probe_result_is_dropped_by_generation():
    # Residual #2: a probe result from an EARLIER Start-server attempt (older
    # generation token) must be dropped — it must not stop a newer poll's timer,
    # re-enable the button, or list models for the wrong state.
    from procedo.studio.dialogs.settings_backend_probe import SettingsBackendProbe
    self = _fake_probe(_FakeSM(alive=False))
    # First click installs _on_start_probe_result bound to generation 1.
    captured = {}

    def _capture_thread(target=None, daemon=False):
        t = MagicMock()
        t.start.side_effect = lambda: captured.setdefault("ran", True)
        return t

    with patch("threading.Thread", side_effect=_capture_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsBackendProbe.start_server(self)
    handler_gen1 = self._on_start_probe_result
    assert self._start_server_generation == 1

    # A SECOND click bumps the generation to 2 (a newer poll owns the state).
    with patch("threading.Thread", side_effect=_capture_thread), \
            patch("PySide6.QtCore.QTimer", MagicMock()):
        SettingsBackendProbe.start_server(self)
    assert self._start_server_generation == 2

    # Reset observable widgets, then deliver the STALE gen-1 result (alive=True).
    self._start_button.reset_mock()
    handler_gen1(True, 1)

    # Dropped: no button re-enable, no model re-query for the stale generation.
    self._start_button.setEnabled.assert_not_called()
    self._populate_models.assert_not_called()


def test_apply_models_no_ops_after_dialog_closed():
    # Residual #5: a model-fetch worker may post _apply_opencode_models() AFTER
    # the dialog closed; the C++ widgets are deleted. The _closed guard must drop
    # the late callback before it touches any widget.
    from procedo.studio.dialogs.settings_dialog import SettingsDialog
    self = _fake_dialog()
    self._models_probe_in_flight = True
    self._closed = True  # done() ran
    SettingsDialog._apply_opencode_models(self, ["a/b"], "")
    # Returned early: no widget mutation, flag left as-is (not cleared on a dead
    # dialog — nothing reads it anymore).
    self.opencode_model.clear.assert_not_called()
    self.opencode_models_note.setText.assert_not_called()


def test_relaunch_restarts_without_retiring_the_manager(tmp_path, monkeypatch):
    """The skill chat's Restart relaunches the SHARED server in place: the
    manager must stay usable (stop() would retire it and start() then refuses,
    leaving every tab without a server)."""
    mgr = _mgr(tmp_path)
    calls = []
    monkeypatch.setattr(mgr, "_stop_process", lambda: calls.append("stop_process"))
    monkeypatch.setattr(mgr, "start", lambda: calls.append("start") or True)
    assert mgr.relaunch() is True
    assert calls == ["stop_process", "start"]
    assert mgr._retired is False


def test_relaunch_of_a_retired_manager_launches_nothing(tmp_path, monkeypatch):
    """A manager retired by a swap (the skill chat may still hold it) stays
    retired: relaunch() launches no server."""
    from procedo.studio.llm import server_manager

    mgr = _mgr(tmp_path)
    mgr.stop()
    spawned = []
    monkeypatch.setattr(server_manager.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert mgr.relaunch() is False and mgr._retired is True
    assert spawned == []


def test_skill_chat_restart_relaunches_instead_of_stop_start():
    """The skill chat's Restart button relaunches the shared server in place —
    never stop() (which retires it) then start()."""
    import os
    import time
    from types import SimpleNamespace

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from procedo.studio.dock.skill_chat_widget import SkillChatWidget

    QApplication.instance() or QApplication([])
    calls = []
    sm = SimpleNamespace(relaunch=lambda: calls.append("relaunch"),
                         stop=lambda: calls.append("stop"), start=lambda: calls.append("start"))
    widget = SkillChatWidget([], SimpleNamespace(server_manager=sm))
    widget._on_restart_backend()
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["relaunch"]


def test_the_launch_test_sweep_kills_a_dead_studio_s_test_server_and_spares_a_live_one(tmp_path):
    # Studio exited mid-Test-connection: its throwaway server and files wait in launch_test/.
    from procedo.studio.llm import opencode_orphan_sweep as sweep
    dead, live = 777777, 888888
    dead_files, live_files = _launch_files(tmp_path, dead), _launch_files(tmp_path, live)
    _another_studios_record(tmp_path, owner=dead, pid=5150, port=6060)
    calls = []
    with patch.object(sweep, "get_opencode_launch_test_dir", return_value=tmp_path), \
         patch.object(sweep, "pid_alive", side_effect=lambda pid: pid == live), \
         patch.object(sweep.OrphanSweeper, "pid_is_our_opencode",
                      side_effect=[PidIdentity.OURS, PidIdentity.NOT_OURS]) as ident, \
         patch("procedo.studio.llm.server_manager.time.sleep"), \
         patch("procedo.studio.llm.server_manager.subprocess.run",
               side_effect=lambda cmd, *a, **k: calls.append(" ".join(cmd)) or MagicMock()):
        sweep.sweep_launch_test_dir(OpenCodeConfig(working_directory=r"C:\live\launch"))
    assert any("kill -TERM" in c and "t 5150" in c for c in calls)
    assert [c for c in ident.call_args_list if c.args[0] != 5150] == []
    assert not any(p.exists() for p in dead_files)
    assert all(p.exists() for p in live_files)


def test_prewarm_sweeps_the_launch_test_dir_once_before_the_server_starts(monkeypatch):
    import types
    from procedo.studio.controllers import llm_server_controller as ctl
    order = []
    sm = MagicMock()
    sm.config.run_skill_secret = "s"
    sm.start.side_effect = lambda: order.append("start")
    monkeypatch.setattr(ctl, "sweep_launch_test_dir", lambda config: order.append("sweep"))

    class _Sync:
        def __init__(self, target=None, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(ctl.threading, "Thread", _Sync)
    self = types.SimpleNamespace(server_manager=sm, _server_cleanup_registered=True,
                                 _mw=types.SimpleNamespace(project_manager=types.SimpleNamespace(project_root=None)))
    with patch("procedo.studio.llm.launch_config.build_launch_config", return_value="x"), \
         patch("procedo.studio.llm.launch_config.ensure_master_config"), \
         patch("procedo.studio.authoring.tool_folders.build_skill_tools_universe", return_value={}):
        ctl.LLMServerController.prewarm(self)
        ctl.LLMServerController.prewarm(self)
    assert order == ["sweep", "start", "start"]
