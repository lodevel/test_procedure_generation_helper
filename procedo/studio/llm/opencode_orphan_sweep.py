"""
OpenCode orphan sweep - the pid file and the leftover-server sweep.

On a clean exit ``OpenCodeServerManager.stop()`` tears the process down. But a
crash / hard kill of the editor can leave the Linux-side ``opencode serve``
alive. Each Studio process records ITS server's Linux pid+port in its pid file
(``launch_file_path``, one per Studio process) as soon as the spawn prints it,
and at the START of the next ``start()`` the records of DEAD Studio processes
(and our own) are read back and that process killed - NEVER a live Studio's
server, and ONLY after confirming the live pid is an ``opencode serve``
launched with that process's ``OPENCODE_CONFIG`` (``config_wsl_path``), checked
against /proc cmdline+environ. We NEVER pkill-by-name blindly: the recorded pid
may have been recycled to an unrelated process, and the user may run their own
opencode pointed at a DIFFERENT config.
"""

import dataclasses
import json
import logging
import os
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from procedo.core.process_alive import pid_alive

from .launch_files import (
    PID_FILE,
    config_wsl_path,
    launch_file_owners,
    launch_file_path,
    remove_launch_files,
    unlink_quietly,
)
from .launch_config import get_opencode_launch_test_dir
from .opencode_backend import OpenCodeConfig, safe_wsl_cwd

log = logging.getLogger(__name__)

# When this Studio process started (near enough): a record with our pid written before it
# belongs to an earlier process that had the same pid.
_PROCESS_STARTED = time.time()


class PidIdentity(Enum):
    """Tri-state verdict for a recorded pid's identity in the orphan sweep.

    The third state is the load-bearing one: a /proc read that times out or
    hits a WSL hiccup is INCONCLUSIVE, not "not ours". Treating inconclusive as
    "not ours" would delete the pid file and permanently lose a still-running
    orphan. On UNKNOWN the sweep keeps the pid file and retries next launch.
    """

    OURS = "ours"          # live, an opencode serve, carries OUR config → kill
    NOT_OURS = "not_ours"  # definitely dead/recycled/unrelated → safe to forget
    UNKNOWN = "unknown"    # read error / timeout / WSL hiccup → keep, retry later


def signal_linux_tree(wsl_path: str, pid: int, sig: str, port: int) -> None:
    """Send ``sig`` to the Linux process ``pid`` and all its descendants —
    selected by pid/parent pid only, never by name — but only while ``pid`` is
    still an ``opencode serve`` on OUR ``port`` (else nothing: the pid was
    reused). The whole tree is listed before any signal is sent. ``wsl -e``
    runs bash directly, so no login shell re-expands the ``$`` in the script.
    Best-effort."""
    script = (
        f"tr '\\0' ' ' < /proc/{int(pid)}/cmdline 2>/dev/null "
        f"| grep -qF -- 'opencode serve --port {int(port)} ' || exit 0; "
        't() { for c in $(pgrep -P "$1"); do t "$c"; done; echo "$1"; }; '
        f"kill -{sig} $(t {int(pid)}) 2>/dev/null"
    )
    try:
        subprocess.run(
            [wsl_path, "-e", "bash", "-c", script],
            capture_output=True, timeout=5, cwd=safe_wsl_cwd(),
        )
    except Exception as e:  # noqa: BLE001 — best-effort cleanup
        log.debug(f"Linux-side kill -{sig} of pid {pid} failed (best-effort): {e}")


def sweep_launch_test_dir(config: OpenCodeConfig) -> None:
    """Sweep the Test-connection probe's launch dir (``launch_test/``) with the live dir's
    rules: a Studio that exited mid-test leaves its throwaway server and files there. This
    Studio's own record, written since it started, is left alone: it is a Test connection
    started meanwhile (which sweeps a leftover of its own before it spawns)."""
    OrphanSweeper(dataclasses.replace(
        config, working_directory=str(get_opencode_launch_test_dir()))).sweep_orphan(include_own=False)


class OrphanSweeper:
    """The pid file of one manager's server and the sweep of leftovers.

    Held by ``OpenCodeServerManager`` (which shares its ``OpenCodeConfig``
    object, so the launch dir and ``wsl_path`` are read live)."""

    def __init__(self, config: OpenCodeConfig):
        self._config = config

    def pid_file_path(self) -> Optional[Path]:
        """Path to OUR pid file (``launch_file_path``) or ``None`` when no launch dir
        is configured (the file lives beside the derived config)."""
        launch_dir = self._config.working_directory
        if not launch_dir:
            return None
        return launch_file_path(launch_dir, PID_FILE)

    def write_pid_file(self, pid: int, port: int) -> None:
        """Record OUR spawned server as JSON ``{pid, port, started_at}`` so a
        crashed next session can sweep it. Best-effort — a write failure must
        not abort an otherwise-healthy launch."""
        path = self.pid_file_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"pid": pid, "port": port, "started_at": time.time()}),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 — best-effort bookkeeping
            log.debug(f"Could not write pid file {path}: {e}")

    def remove_pid_file(self, pid: int, path: Optional[Path] = None) -> None:
        """Delete the pid file (*path*, default OUR own) unless it now records a
        pid other than *pid*: a manager swap's retired manager must not delete
        the record its successor already wrote (best-effort)."""
        path = path or self.pid_file_path()
        if path is None:
            return
        data = self.read_pid_file(path)
        if data is not None and data["pid"] != pid:
            return
        unlink_quietly(path)

    def read_pid_file(self, path: Optional[Path] = None) -> Optional[dict]:
        """Read a pid file (*path*, default OUR own), or ``None`` when it is absent /
        unreadable / not a JSON object carrying an int ``pid``."""
        path = path or self.pid_file_path()
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            return None
        return data

    def our_config_wsl_path(self, owner: Optional[int] = None) -> Optional[str]:
        """The WSL ``OPENCODE_CONFIG`` path the Studio process *owner* (default THIS
        one) launches with in our launch dir (``config_wsl_path``), or ``None`` when
        no launch dir is set. This is a server's launch identity: the orphan sweep
        requires the recorded pid's cmdline/environ to carry exactly the path of the
        record's owner before it kills."""
        launch_dir = self._config.working_directory
        if not launch_dir:
            return None
        return config_wsl_path(launch_dir, owner)

    # Sentinels the identity probe emits so we can tell a DEAD pid apart from a
    # WSL-level read failure: a clean run prints ALIVE/DEAD; if neither shows up
    # the shell itself failed and the verdict is UNKNOWN (never NOT_OURS).
    _ID_ALIVE = "__OC_ALIVE__"
    _ID_DEAD = "__OC_DEAD__"
    _ID_SEP = "__OC_ENV__"

    def pid_is_our_opencode(self, pid: int, owner: Optional[int] = None) -> "PidIdentity":
        """Tri-state identity of the recorded ``pid`` (a Linux pid) in WSL.

        - ``OURS``: the LIVE process is an ``opencode serve`` AND was launched
          with the ``OPENCODE_CONFIG`` of the record's *owner* (default us),
          matched as an EXACT token (not a substring) against its cmdline and
          environ — so a sibling config like ``<our_cfg>.backup`` never matches
          and the USER's own opencode at a DIFFERENT config is spared.
        - ``NOT_OURS``: the pid is definitely gone/recycled, OR it is alive but
          not an opencode serve / not carrying our config — safe to forget.
        - ``UNKNOWN``: the /proc read timed out or WSL hiccuped — INCONCLUSIVE.
          The caller must keep the pid file and retry; treating this as
          NOT_OURS would permanently lose a still-running orphan.

        When this manager has no launch dir (no config identity to assert), we
        fall back to a bare ``opencode serve`` token check — the strongest
        marker available, and the pid file is per-launch-dir anyway."""
        our_cfg = self.our_config_wsl_path(owner)
        # Probe pid liveness explicitly (ALIVE/DEAD marker) so a dead pid is a
        # DEFINITE verdict, and emit cmdline + environ NUL-delimited so we can
        # split into exact tokens. One wsl.exe round-trip.
        script = (
            f"if [ -e /proc/{int(pid)} ]; then echo {self._ID_ALIVE}; "
            f"else echo {self._ID_DEAD}; fi; "
            f"cat /proc/{int(pid)}/cmdline 2>/dev/null; echo; "
            f"echo {self._ID_SEP}; "
            f"cat /proc/{int(pid)}/environ 2>/dev/null"
        )
        try:
            result = subprocess.run(
                [self._config.wsl_path, "bash", "-ic", script],
                capture_output=True, text=True, timeout=3, cwd=safe_wsl_cwd(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return PidIdentity.UNKNOWN
        out = result.stdout or ""
        # The shell ran but produced neither marker → WSL-level failure, not a
        # statement about the pid. Inconclusive.
        if self._ID_ALIVE not in out and self._ID_DEAD not in out:
            return PidIdentity.UNKNOWN
        if self._ID_DEAD in out:
            return PidIdentity.NOT_OURS  # /proc/<pid> absent → definitely gone

        # Split the NUL-delimited cmdline + environ into exact tokens.
        cmdline_blob, _, environ_blob = out.partition(self._ID_SEP)
        # Drop the ALIVE marker line from the cmdline blob.
        cmdline_blob = cmdline_blob.split("\n", 1)[-1]
        # Split on NUL, then strip the surrounding-newline noise the `echo`
        # separators inject (argv/environ tokens carry no real newlines) so the
        # EXACT-token compare below sees clean tokens.
        cmd_tokens = [t.strip("\r\n") for t in cmdline_blob.split("\0")]
        cmd_tokens = [t for t in cmd_tokens if t]
        env_tokens = [t.strip("\r\n") for t in environ_blob.split("\0")]
        env_tokens = [t for t in env_tokens if t]

        # Must be an `opencode serve` — adjacent exact argv tokens, not a
        # substring of some unrelated command line.
        if not self._is_opencode_serve(cmd_tokens):
            return PidIdentity.NOT_OURS
        if our_cfg is None:
            # No config identity to assert — the serve check is the marker.
            return PidIdentity.OURS
        # EXACT-token config match (MAJOR-2): the environ token must equal
        # `OPENCODE_CONFIG=<our_cfg>` exactly (so `<our_cfg>.backup` is NOT a
        # match), or argv must carry `--config <our_cfg>` / `--config=<our_cfg>`
        # as exact tokens. No substring containment.
        if f"OPENCODE_CONFIG={our_cfg}" in env_tokens:
            return PidIdentity.OURS
        if self._argv_has_config(cmd_tokens, our_cfg):
            return PidIdentity.OURS
        return PidIdentity.NOT_OURS

    @staticmethod
    def _is_opencode_serve(cmd_tokens: list) -> bool:
        """True iff argv contains the adjacent exact tokens ``opencode serve``
        (the basename may be a full path like ``/usr/bin/opencode``)."""
        for i in range(len(cmd_tokens) - 1):
            tok = cmd_tokens[i]
            base = tok.rsplit("/", 1)[-1]
            if base == "opencode" and cmd_tokens[i + 1] == "serve":
                return True
        return False

    @staticmethod
    def _argv_has_config(cmd_tokens: list, our_cfg: str) -> bool:
        """True iff argv carries OUR config as an exact token: either
        ``--config <our_cfg>`` (two tokens) or ``--config=<our_cfg>`` (one)."""
        joined = f"--config={our_cfg}"
        for i, tok in enumerate(cmd_tokens):
            if tok == joined:
                return True
            if tok == "--config" and i + 1 < len(cmd_tokens) \
                    and cmd_tokens[i + 1] == our_cfg:
                return True
        return False

    def sweep_orphan(self, include_own: bool = True) -> None:
        """Sweep the records of Studio processes that are gone (and our own unless
        *include_own* is False: then only a record of an earlier process with our pid),
        then the other launch files of a gone process whose record is gone.

        Launch files whose owner (the Windows pid in their name) is alive and not
        us belong to ANOTHER live Studio: they and its server are left alone. A
        reused owner pid reads as alive, so those are skipped too (fail safe: a
        missed orphan, never a killed live server)."""
        launch_dir = self._config.working_directory
        if not launch_dir:
            return
        for owner in launch_file_owners(launch_dir):
            if owner != os.getpid() and pid_alive(owner):
                log.debug(f"Studio process {owner} is alive; leaving its server alone")
                continue
            path = launch_file_path(launch_dir, PID_FILE, owner)
            data = self.read_pid_file(path)
            if (data is not None and owner == os.getpid() and not include_own
                    and data.get("started_at", 0) >= _PROCESS_STARTED):
                continue
            if data is not None:
                self._sweep_record(path, data, owner)
            if owner != os.getpid() and not path.exists():
                remove_launch_files(launch_dir, owner)

    def _sweep_record(self, path: Path, data: dict, owner: int) -> None:
        """Kill the leftover ``opencode serve`` one swept record names.

        Tri-state gated (MAJOR-1): we only ever kill on a conclusive ``OURS``,
        and we only delete the pid file on a conclusive verdict (OURS after the
        kill, or NOT_OURS). On ``UNKNOWN`` (a /proc read timeout / WSL hiccup)
        we KEEP the pid file and skip — deleting it would permanently lose a
        still-running orphan; the next launch retries the sweep.

        The kill is a graceful TERM, a brief wait, an identity RE-CHECK, and a
        KILL -9 ONLY if the re-check is still conclusively ``OURS`` (MAJOR-3).
        If the pid went away or was recycled in the gap the re-check is no
        longer OURS and we do NOT -9, closing the PID-reuse TOCTOU. NEVER a
        blind pkill-by-name."""
        pid = data["pid"]
        port = data.get("port") if isinstance(data.get("port"), int) else 0

        identity = self.pid_is_our_opencode(pid, owner)
        if identity is PidIdentity.UNKNOWN:
            # Inconclusive — could not read /proc (timeout / WSL hiccup). Keep
            # the pid file and retry on the next launch; never lose an orphan.
            log.warning(
                f"Orphan-sweep identity check for pid {pid} was inconclusive; "
                f"keeping pid file and retrying next launch"
            )
            return
        if identity is PidIdentity.NOT_OURS:
            # Definitely gone / recycled / unrelated — forget the stale file.
            log.debug(f"PID-file pid {pid} is not OUR live opencode serve; not killing")
            self.remove_pid_file(pid, path)
            return

        # identity is OURS — graceful TERM first.
        log.info(f"Sweeping orphan opencode serve (pid {pid}) from a prior session")
        signal_linux_tree(self._config.wsl_path, pid, "TERM", port)

        # Brief grace for the process to exit on its own.
        time.sleep(1.0)

        # RE-CHECK identity before escalating to -9 (MAJOR-3): if the pid is
        # gone or was recycled to a different process in the TERM→wait gap, this
        # is no longer OURS and we MUST NOT -9 (PID-reuse TOCTOU). Only escalate
        # on a fresh, conclusive OURS.
        post = self.pid_is_our_opencode(pid, owner)
        if post is PidIdentity.OURS:
            signal_linux_tree(self._config.wsl_path, pid, "KILL", port)
            # We forcibly -9'd OUR process — the pid file has served its purpose.
            self.remove_pid_file(pid, path)
        elif post is PidIdentity.NOT_OURS:
            # Gone or recycled to a non-ours process in the TERM→wait gap: no -9
            # (closes the PID-reuse TOCTOU), and the stale file can go.
            log.debug(f"pid {pid} no longer OURS after TERM; skipping KILL -9")
            self.remove_pid_file(pid, path)
        else:  # UNKNOWN
            # Could NOT confirm the TERM took effect (a /proc read timeout / WSL
            # hiccup) — the orphan may still be alive. KEEP the pid file so the
            # next launch retries the sweep; never delete on an inconclusive
            # post-TERM check (gpt-5.5 edge).
            log.warning(
                f"pid {pid} identity inconclusive after TERM; keeping pid file "
                f"for next-launch retry"
            )
