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
