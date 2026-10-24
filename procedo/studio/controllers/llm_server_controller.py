"""The OpenCode server lifecycle of the Studio workspace, carved out of
``main_window.StudioWorkspace`` (bodies moved verbatim: ``self`` is this
controller, the window's state is read through ``self._mw``).

Owns the shared server manager and the backend factory every tab builds its
backend from: building both from the settings (retiring the previous manager
off the UI thread on a swap), the pre-warm, the exit stop, the restart on a
project switch, the per-request settings applied live, and the health poll
with its auto-recovery state machine and the header's OpenCode pill.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject

from procedo.core.off_gui import answer_to

from .. import shell
from ..llm import OpenCodeConfig
from ..llm.backend_factory import BACKEND_TYPE_OPENCODE, BackendConfig, BackendFactory
from ..llm.launch_files import remove_launch_files
from ..llm.opencode_orphan_sweep import sweep_launch_test_dir
from ..llm.server_manager import OpenCodeServerManager

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)


class LLMServerController(QObject):
    """The workspace's OpenCode server: manager, factory, health poll and recovery (a
    QObject: the probe and recover daemons answer on its slots)."""

    def __init__(self, window: StudioWorkspace) -> None:
        super().__init__(window)
        self._mw = window
        # Daemons (health probe / recover) answer through these relays and never
        # hold this controller: it may be deleted while they run, and then their
        # answers are dropped (procedo.core.off_gui).
        self._health_verdict = answer_to(self._on_health_verdict)
        self._recovered = answer_to(self._on_recovered)

    def _stop_manager_async(self, manager):
        """Retire + stop a RETIRED-bound server manager off the UI thread.

        We arm the manager's permanent ``_retired`` flag synchronously HERE,
        before the stop thread runs (so a still-pending prewarm daemon on it
        refuses to launch -> no orphan), then tear the process down. That
        teardown (stop(), which also re-sets the flag) can block up to the
        startup timeout if the manager is mid-boot, so it runs on a daemon
        thread and the UI never freezes. The manager is single-use after this;
        callers build a FRESH OpenCodeServerManager for the new server.
        """
        if manager is None:
            return

        # Arm the permanent retirement guard SYNCHRONOUSLY, here on the
        # calling (UI) thread, BEFORE spawning the stop thread. stop() also
        # sets it (idempotent), but the teardown it does runs on the daemon
        # thread and can lag. A stale prewarm thread that races into the old
        # manager start() must see _retired==True at the swap instant, or it
        # spawns the orphan we are swapping away from. Only the guard flag is
        # armed now; the teardown stays deferred off the UI thread.
        manager._retired = True

        def _stop():
            try:
                manager.stop()
            except Exception:
                log.debug('async server manager stop() failed', exc_info=True)

        threading.Thread(target=_stop, daemon=True).start()

    def init_backend(self):
        """Initialize LLM backend infrastructure.
        
        Creates server manager (for OpenCode) and backend factory.
        Each tab will create its own backend via the factory.
        """
        # C2 manager-swap: retire the OLD manager before replacing it so a
        # re-init (e.g. Settings Save) never orphans the pre-warmed server.
        # stop() runs on a DAEMON THREAD: it can block up to the startup
        # timeout if the old manager is mid-boot, and that must never freeze
        # the UI. stop() sets the manager's permanent _retired flag
        # synchronously here (before the thread even runs), so a stale prewarm
        # daemon on the old manager refuses to launch -> no orphan. The fresh
        # manager built below is a NEW object (a retired one never restarts).
        _old_server_manager = getattr(self, 'server_manager', None)
        if _old_server_manager is not None:
            log.info('Retiring previous OpenCode server manager (swap)...')
            self._stop_manager_async(_old_server_manager)
        # Initialize server manager (will be None if not using OpenCode)
        self.server_manager: Optional[OpenCodeServerManager] = None
        
        # Build backend configuration
        config = self._build_backend_config()
        
        # Create server manager for OpenCode (shared across tabs)
        if config.backend_type == BACKEND_TYPE_OPENCODE:
            log.info("Creating OpenCode server manager...")
            self.server_manager = OpenCodeServerManager(config.opencode)
            # Don't start yet - will start on first backend creation
        
        # Create factory for tabs to use
        self._backend_factory = BackendFactory(config, self.server_manager)

        # Re-arm auto-recovery on EVERY manager swap: a fresh manager must
        # never inherit a retired predecessor's give-up count (the Settings
        # Save path reaches here without reset_recovery), and this
        # also clears any _server_recovering left set by a now-stale recover
        # thread. The reset in restart_for_project is now redundant
        # but harmless.
        self.reset_recovery()
        
        # C2: pre-warm the NEW manager on a daemon thread (set its launch
        # config first so it never launches from safe_wsl_cwd with the
        # wrong config). Only on a swap; the initial __init__ call defers
        # to showEvent (no project root yet), gated on _cli_args_processed.
        if getattr(self._mw, '_cli_args_processed', False) \
                and self.server_manager is not None:
            self.prewarm()
        
        log.info(f"Backend infrastructure initialized: type={config.backend_type}")
        
        # Update the OpenCode pill
        self.refresh_indicator()
        
        # Update all tab contexts with new factory (if tabs are already initialized)
        self._update_all_tab_contexts()

    def prewarm(self):
        """Set the current manager's launch config + pre-warm it (C1/C2/C3).

        Rebuilds the derived launch dir for the CURRENT project, assigns it
        to ``working_directory`` BEFORE start() fires (C3 race), registers a
        single stable cleanup hook that always targets the LIVE
        ``self.server_manager`` (C2 -- never a stale bound reference), and
        starts the server on a daemon thread so the UI never blocks.
        """
        sm = getattr(self, 'server_manager', None)
        if sm is None:
            return
        try:
            from ..llm.launch_config import (
                build_launch_config, ensure_master_config)
            from ..authoring.tool_folders import build_skill_tools_universe
            _pr = getattr(self._mw.project_manager, 'project_root', None)
            _seed = (_pr / 'opencode.json') if _pr else None
            ensure_master_config(seed_from=_seed)
            # Compute the gate universe FIRST: if discovery throws, the except
            # fires before opencode.json is written -> never registered-but-ungated.
            _uni = build_skill_tools_universe(_pr)
            # Mint the run_skill HMAC secret ONCE per process (skip if already set
            # so a re-prewarm on the same config reuses it); pass it into the launch
            # config so the run_skill MCP block + the token-mint share one secret.
            if not getattr(sm.config, 'run_skill_secret', None):
                import secrets as _secrets
                sm.config.run_skill_secret = _secrets.token_hex()
            sm.config.working_directory = str(build_launch_config(
                _pr, run_skill_secret=sm.config.run_skill_secret))
            sm.config.skill_tools = _uni
        except Exception:
            log.warning('failed to build launch config for pre-warm',
                        exc_info=True)
        # Register the exit cleanup hook ONCE; it reads self.server_manager
        # live so it always stops whatever manager is current (C2).
        if not getattr(self, '_server_cleanup_registered', False):
            import atexit
            atexit.register(self._stop_current_server)
            self._server_cleanup_registered = True
        sweep_tests = not getattr(self, '_launch_test_swept', False)
        self._launch_test_swept = True  # once per process: a later prewarm must not kill a running test's server

        def _start():
            if sweep_tests:
                try:
                    sweep_launch_test_dir(sm.config)
                except Exception:
                    log.warning('sweep of the Test-connection launch dir failed', exc_info=True)
            sm.start()

        threading.Thread(target=_start, daemon=True).start()

    def _stop_current_server(self):
        """Stop whatever server manager is CURRENT (stable cleanup target) and
        delete this process's launch files (the app leaves no traces).

        Bound once via atexit in prewarm; reads self.server_manager
        at call time so a manager swap (Settings Save) can't leave a stale
        reference uncleaned (C2).
        """
        sm = getattr(self, 'server_manager', None)
        if sm is not None:
            try:
                sm.stop()
            except Exception:
                log.debug('current server manager stop() failed',
                          exc_info=True)
            if sm.config.working_directory:
                remove_launch_files(sm.config.working_directory)

    def restart_for_project(self):
        """On project change, retire the old server and stand up a FRESH one
        for the new project (Q2 auto-restart).

        A manager is single-use once retired (its ``_retired`` flag is
        permanent), so we can NOT stop+prewarm the same object — we delegate to
        ``init_backend``, which retires the old manager OFF the UI thread
        (no freeze, no orphan), builds a brand-new OpenCodeServerManager +
        factory, rewires the tab contexts, and pre-warms it (the prewarm
        rebuilds the derived launch config for the new project so its MCP
        blocks take effect). No-op when no manager / the UI hasn't been shown
        yet (showEvent does the initial pre-warm).
        """
        sm = getattr(self, 'server_manager', None)
        if sm is None or not getattr(self._mw, '_cli_args_processed', False):
            return
        # A send waiting for the old server's start never goes to the new one: not sent, now.
        for tab in self._mw._get_llm_tabs():
            tab.drop_queued_send("the AI server is restarting.")
        # A manual restart (Restart backend / project switch) clears any
        # auto-recovery give-up state so the fresh manager is auto-recoverable.
        self.reset_recovery()
        self.init_backend()

    def settings_require_restart(self, old: dict, new: dict) -> bool:
        """True when a Settings change needs the OpenCode server rebuilt.

        Only LAUNCH-IDENTITY keys force a respawn: the backend type and the
        server host/port/wsl-path (which define WHICH ``opencode serve`` we talk
        to), plus ``custom_output_format`` (baked into each backend's cached
        PromptBuilder, so it needs a fresh backend). Everything else the dialog
        can change — model, timeouts, fold-system — is per-request and is
        applied live by :meth:`apply_settings_live`, keeping the session.

        master.json providers and MCP wiring are also launch-affecting but are
        NOT settings.json keys (edited via the Config controls, or changed on a
        project switch), so they are handled outside ``_on_settings`` and are
        intentionally not compared here.
        """
        old = old or {}
        new = new or {}
        # No warm, reusable manager to keep -> must (re)build.
        sm = getattr(self, "server_manager", None)
        if sm is None or getattr(sm, "_retired", False):
            return True
        if old.get("llm_backend", "opencode") != new.get("llm_backend", "opencode"):
            return True
        if (old.get("custom_output_format", "") or "") != (
            new.get("custom_output_format", "") or ""
        ):
            return True
        o = old.get("opencode", {}) or {}
        n = new.get("opencode", {}) or {}
        for key in ("host", "port", "wsl_path"):
            if o.get(key) != n.get(key):
                return True
        return False

    def apply_settings_live(self) -> None:
        """Apply per-request LLM settings WITHOUT restarting the server.

        The manager, the factory and every per-tab backend share ONE
        ``OpenCodeConfig`` by reference, and the client reads ``model`` /
        ``fold_system_into_prompt`` / ``request_timeout`` at send time — so
        mutating those fields in place makes the NEXT request use them while the
        warm ``opencode serve`` and its session_id (conversation history +
        accumulated MCP tool results) stay intact. Named fields ONLY: never
        replace the object — ``start()`` / prewarm write ``server_port`` /
        ``working_directory`` / ``skill_tools`` / ``run_skill_secret`` onto it.
        """
        sm = getattr(self, "server_manager", None)
        if sm is None:  # defensive; the predicate already guards this
            self.init_backend()
            return
        oc = self._mw._settings.get("opencode", {}) or {}
        common = self._mw._settings.get("common_llm", {}) or {}
        live = sm.config  # the shared OpenCodeConfig (== factory + every backend)
        live.model = oc.get("model") or None
        live.fold_system_into_prompt = oc.get("fold_system_into_prompt", False)
        live.request_timeout = common.get("request_timeout", 120.0)
        live.startup_timeout = oc.get("startup_timeout", live.startup_timeout)
        # Drop each live backend's memoised context window so the token readout
        # re-resolves for the new model (a tab not started yet has none).
        for tab in self._mw._get_llm_tabs():
            ctx = getattr(tab, "tab_context", None)
            backend = getattr(ctx, "backend", None)
            if backend is not None:
                backend._context_window = None
        self.refresh_indicator()
        log.info(
            "Applied LLM settings live (model=%s) — server + session kept",
            live.model,
        )

    def _build_backend_config(self) -> BackendConfig:
        """Build backend config from settings.

        Returns:
            BackendConfig for the configured backend type
        """
        backend_type = self._mw._settings.get("llm_backend", "opencode")
        log.info(f"Building backend config: {backend_type}")
        
        # Load custom output format
        custom_output_format = self._mw._settings.get("custom_output_format", "")
        
        # Load common LLM parameters
        common_llm = self._mw._settings.get("common_llm", {})
        
        if backend_type == "opencode":
            config_dict = self._mw._settings.get("opencode", {})
            log.debug(f"OpenCode config: {config_dict}")
            opencode_config = OpenCodeConfig(
                server_port=config_dict.get("port", 4096),
                server_hostname=config_dict.get("host", "127.0.0.1"),
                model=config_dict.get("model") or None,
                wsl_path=config_dict.get("wsl_path") or "wsl",
                startup_timeout=config_dict.get("startup_timeout", 60.0),
                request_timeout=common_llm.get("request_timeout", 120.0),
                fold_system_into_prompt=config_dict.get("fold_system_into_prompt", False),
            )
            return BackendConfig(
                backend_type=BACKEND_TYPE_OPENCODE,
                opencode=opencode_config,
                custom_prompts={},  # Deprecated: now handled by TaskConfigManager
                custom_output_format=custom_output_format,
            )
        else:
            return BackendConfig.create_disabled()

    @property
    def backend_factory(self) -> BackendFactory:
        """Get the backend factory for creating per-tab backends.
        
        Returns:
            BackendFactory instance
        """
        return self._backend_factory

    def _update_all_tab_contexts(self):
        """Update all tab contexts with the current backend factory."""
        for tab in self._mw._get_llm_tabs():
            if hasattr(tab, 'tab_context'):
                tab.tab_context.update_backend_factory(self._backend_factory)
        
        log.info("Backend factory propagated to all tab contexts")

    def start_health_poll(self):
        """Poll OUR OpenCode server's liveness on a timer and auto-recover a
        crash.

        prewarm only fires on app-start / project-switch, so a server that
        CRASHED mid-session was never noticed or relaunched — the user just hit
        an 'unreachable' picker and had to restart the whole app. This timer
        closes that gap: every few seconds it checks real liveness and, when
        the server is down (and a manager exists, not retired), relaunches it on
        a daemon thread via ensure_running(). The indicator is refreshed each
        tick so the 'running / down' signal stays honest.
        """
        from PySide6.QtCore import QTimer
        if getattr(self, '_server_health_timer', None) is not None:
            return
        self._server_health_timer = QTimer(self)
        self._server_health_timer.setInterval(5000)
        self._server_health_timer.timeout.connect(self._on_server_health_tick)
        self._server_health_timer.start()

    # Stop auto-recovering after this many consecutive failed relaunches so a
    # crash-looping server doesn't relaunch every tick forever (log-spam + CPU).
    _MAX_SERVER_RECOVERY_ATTEMPTS = 3

    def reset_recovery(self):
        """Re-arm auto-recovery: clear the consecutive-failure counter (and any
        in-flight flag). Called on every MANUAL recovery action (Restart
        backend, project switch, settings Start server) so a user intervention
        always lifts a prior give-up state."""
        self._server_recovery_attempts = 0
        self._server_recovering = False
        # Off-thread health-probe state (read via getattr defaults elsewhere):
        # the last probe verdict for the cheap indicator refresh, and the
        # single-probe-in-flight gate so ticks never stack daemons.
        self._last_server_alive = None
        self._health_probe_inflight = False

    def _on_server_health_tick(self):
        """One liveness poll, fully OFF the UI thread.

        is_alive does a process poll AND a synchronous ~2s /health GET, so
        it must NEVER run on the UI thread (it would freeze the Qt event
        loop every 5s tick). Instead we spawn a daemon that probes is_alive
        and answers the verdict to the UI thread (_health_verdict). One
        probe at a time (gated by _health_probe_inflight) so ticks never
        stack daemons while a slow /health is still outstanding.
        """
        sm = getattr(self, 'server_manager', None)
        if sm is None:
            self.refresh_indicator()
            return
        # One probe in flight at a time; the next tick re-checks.
        if getattr(self, '_health_probe_inflight', False):
            return
        self._health_probe_inflight = True
        verdict = self._health_verdict

        def _probe():
            try:
                alive = sm.is_alive  # process poll + ~2s /health, OFF the UI thread
            except Exception:
                log.debug('server health probe failed', exc_info=True)
                alive = False
            verdict((sm, alive))

        try:
            threading.Thread(target=_probe, daemon=True).start()
        except Exception:
            # Spawn failed after the in-flight flag was set — clear it so the
            # next tick can probe again (never wedge the poll permanently).
            self._health_probe_inflight = False
            log.debug('server health probe thread spawn failed', exc_info=True)

    def _on_health_probe_result(self, sm, alive):
        """UI-thread continuation of one health probe.

        Runs on the UI thread (answered through _health_verdict), so every read/write
        of the recovery state (_server_recovering / _server_recovery_attempts /
        _last_server_alive) and the manager-identity guard happen on ONE thread.
        The manager swap (init_backend) is also UI-thread, so they
        serialise — no lock needed, no stale-thread clobber.
        """
        # A swap (Restart backend / project switch / Settings Save) may have
        # installed a NEW manager while this probe was in flight. Drop the stale
        # verdict — it must not drive the new manager's indicator or recovery.
        if sm is not getattr(self, 'server_manager', None):
            return
        # Clear AFTER the stale-manager guard: only the CURRENT manager's probe
        # owns the in-flight flag (a stale probe must not unblock the next tick).
        self._health_probe_inflight = False
        self._last_server_alive = alive
        self.refresh_indicator(alive)
        if alive or getattr(sm, '_retired', False):
            self._server_recovering = False
            # Server is back (or retired): a recovery succeeded — clear the
            # consecutive-failure count so auto-recovery is armed for the next
            # crash.
            self._server_recovery_attempts = 0
            return
        # Server is DOWN. After N consecutive failed auto-recoveries we GIVE
        # UP — a crash-looping server would otherwise relaunch every tick
        # forever (log-spam + CPU at the bench). The indicator then tells the
        # user to act; a manual Restart backend / Start server re-arms us.
        if getattr(self, '_server_recovery_attempts', 0) >= self._MAX_SERVER_RECOVERY_ATTEMPTS:
            return
        # One recovery in flight at a time (gated by _server_recovering).
        if getattr(self, '_server_recovering', False):
            return
        self._server_recovering = True
        log.warning('OpenCode server detected down; auto-recovering...')
        self._mw.footer.show_message('OpenCode server down — recovering…', 4000)
        try:
            threading.Thread(target=_recover, args=(sm, self._recovered),
                             daemon=True).start()
        except Exception:
            # Thread.start() raised after the in-flight flag was set —
            # clear it so auto-recovery is not permanently disabled.
            self._server_recovering = False
            log.debug('server auto-recovery thread spawn failed', exc_info=True)

    def _on_health_verdict(self, verdict):
        self._on_health_probe_result(*verdict)

    def _on_recovered(self, verdict):
        self._on_recover_done(*verdict)

    def _on_recover_done(self, sm, ok):
        """UI-thread continuation of one recovery attempt — the SINGLE place
        recovery state is mutated after a relaunch.

        A swap may have retired THIS sm and installed a new manager while we
        relaunched. Only the verdict for the CURRENT manager may touch the
        shared recovery flags, else a stale thread clobbers the new manager's
        state (bogus failure count -> premature give-up; or a spurious
        recovering=False that breaks the single-in-flight gate). The guard is
        re-validated HERE, on the UI thread, AFTER the blocking relaunch+probe.
        """
        if sm is not getattr(self, 'server_manager', None):
            return
        self._server_recovering = False
        # is_alive (process + /health) so a relaunch that leaves the server
        # still wedged counts as a failed attempt toward the give-up cap.
        if not ok:
            self._server_recovery_attempts = (
                getattr(self, '_server_recovery_attempts', 0) + 1)
        else:
            self._server_recovery_attempts = 0
        self._last_server_alive = ok
        self.refresh_indicator(ok)

    def refresh_indicator(self, alive=None):
        """Paint the header's OpenCode pill: the model while the server runs,
        recovering / down otherwise, "LLM off" without an OpenCode backend.

        ``alive`` may be passed by the poll (avoids a redundant process poll);
        otherwise the last off-thread probe's verdict is used.
        """
        if not hasattr(self._mw, 'server_pill'):
            return
        sm = getattr(self, 'server_manager', None)
        if sm is None:
            shell.show_server_state(self._mw, 'neutral', 'LLM off',
                                    'No LLM backend: choose one in LLM settings.')
            return
        if alive is None:
            # Use the cached verdict from the last off-thread probe — NEVER
            # call is_alive/health_check here (that is blocking HTTP and this
            # runs on the UI thread). None (no probe yet) is treated as down.
            alive = getattr(self, '_last_server_alive', None)
        if getattr(self, '_server_recovering', False) and not alive:
            shell.show_server_state(self._mw, 'warning', '● OpenCode · recovering…',
                                    'Relaunching the OpenCode server…')
        elif alive:
            shell.show_server_state(self._mw, 'success', f'● OpenCode · {sm.config.model or "default model"}',
                                    'OpenCode server is running.')
        elif getattr(self, '_server_recovery_attempts', 0) >= self._MAX_SERVER_RECOVERY_ATTEMPTS:
            shell.show_server_state(
                self._mw, 'danger', '● OpenCode · down',
                'Auto-recovery gave up after repeated failures. Use Restart server to try again.'
                + (f'\n\n{sm.last_status.message}'
                   if sm.last_status is not None and not sm.last_status.ok else ''))
        else:
            shell.show_server_state(self._mw, 'danger', '● OpenCode · down',
                                    'OpenCode server is down — auto-recovery will relaunch it.')


def _recover(sm, done):
    """Daemon body: relaunch the server, then probe the result — both
    OFF the UI thread. ensure_running() is a blocking relaunch and is_alive
    is a ~2s HTTP probe, so neither may touch the UI thread. The verdict is
    answered through ``done`` to _on_recover_done on the UI thread, which owns ALL
    recovery-state mutation and the manager-identity guard."""
    try:
        sm.ensure_running()
    except Exception:
        log.debug('server auto-recovery failed', exc_info=True)
    try:
        ok = sm.is_alive  # post-relaunch verdict, OFF the UI thread
    except Exception:
        log.debug('post-recovery health probe failed', exc_info=True)
        ok = False
    done((sm, ok))
