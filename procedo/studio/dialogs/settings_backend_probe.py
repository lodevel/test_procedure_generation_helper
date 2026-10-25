"""The Settings dialog's OpenCode server probes: Start server (relaunch it, poll until it
answers) and Test connection (start a throwaway server, one real chat request)."""

import atexit

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from procedo.core.off_gui import answer_to, run_off_gui

from ..llm.launch_config import (
    build_launch_config,
    ensure_master_config,
    get_opencode_launch_test_dir,
)
from ..llm.launch_files import remove_launch_files


class SettingsBackendProbe(QObject):
    """The probes behind the dialog's server row. A child of the dialog, so its UI-thread
    answers are dropped once the dialog is deleted (procedo.core.off_gui)."""

    def __init__(
        self,
        owner,
        *,
        server_manager,
        project_root,
        backend,
        host,
        port,
        model_value,
        start_button,
        note,
        test_button,
        test_cue,
        populate_models,
    ):
        super().__init__(owner)
        self._owner = owner
        self._server_manager = server_manager
        self._project_root = project_root
        self._backend = backend  # () -> the selected backend name
        self._host = host  # the host line edit
        self._port = port  # the port spin box
        self._model_value = model_value  # () -> the model id typed/selected, '' = Default
        self._start_button = start_button
        self._note = note
        self._test_button = test_button
        self._test_cue = test_cue  # the shared loading cue shown while a test runs
        self._test_running = False
        self._start_was_enabled = True  # Start's state when the running test began
        self._closed = False
        self._populate_models = populate_models  # re-query the model list
        # The Start-server liveness probe answers through this relay and never holds this object.
        self._start_probe_answer = answer_to(self._on_start_probe_answer)
        # Generation token for the Start-server probe: bumped on each start_server so a stale
        # probe (from an earlier start attempt) is dropped instead of applying alive=True to a
        # newer poll.
        self._start_server_generation = 0

    def start_server(self):
        """(Re)launch OUR OpenCode server from the picker, off the UI thread, and
        re-query the model list once it answers.

        Auto-recovery seam for a crashed mid-session server: ``ensure_running``
        relaunches via the SAME ``start()`` path the app uses (orphan-sweep +
        respawn), so the user never has to restart the whole app. We poll the
        manager's liveness on a short QTimer (the daemon ``start()`` returns
        asynchronously) and refresh the list when it comes up, all without
        freezing the dialog.
        """
        import threading
        from PySide6.QtCore import QTimer

        sm = self._server_manager
        if sm is None or self._test_running:
            return
        # A manual Start-server is a user intervention: clear any auto-recovery
        # give-up state on the main window so the background poll resumes
        # auto-recovering this server after this attempt (best-effort — the
        # parent is the main window when launched from it).
        parent = self._owner.parent()
        reset = getattr(getattr(parent, "llm_server", None), "reset_recovery", None)
        if callable(reset):
            reset()
        # Bump the generation token: each Start-server attempt owns a fresh
        # token. A probe spawned by an EARLIER attempt carries the old token and
        # its result is dropped (it must not stop a newer poll's timer, re-enable
        # the button, or list models for the wrong state).
        self._start_server_generation += 1
        generation = self._start_server_generation
        self._start_button.setEnabled(False)
        self._note.setText("Starting OpenCode server…")
        # Launch off the UI thread; ensure_running short-circuits if it is
        # already alive and otherwise calls start().
        try:
            threading.Thread(target=sm.ensure_running, daemon=True).start()
        except Exception:
            # Spawn failed — re-enable the button and surface a note instead of
            # wedging it disabled with the "Starting…" note forever. No poll
            # timer is armed (nothing to poll), so just bail.
            self._start_button.setEnabled(True)
            self._note.setText(
                "Could not start the server — check the OpenCode install / logs, then try again."
            )
            return

        # Poll liveness for up to ~startup timeout; refresh + re-enable on the UI
        # thread when it answers (or give up with a clear note). is_alive is a
        # blocking 2s HTTP /health GET, so each tick runs it on a daemon thread
        # and the verdict is answered to the UI thread (_start_probe_answer) —
        # the probe never mutates dialog state cross-thread, and the modal dialog
        # never freezes. _start_poll_done latches the terminal state so a late
        # probe result can still flip us to success after a timeout note (no
        # cross-thread flag the UI reads stale, no ignored late success).
        self._start_poll_elapsed = 0.0
        self._start_probe_in_flight = False
        self._start_poll_done = False
        timer = QTimer(self)
        timer.setInterval(1000)
        # Stash the timer so done() (dialog close) can stop it — a fired-after-
        # close tick would otherwise run HTTP on the UI thread and touch deleted
        # C++ widgets (use-after-close).
        self._start_poll_timer = timer

        def _tick():
            if self._start_poll_done:
                return
            self._start_poll_elapsed += 1.0
            if self._start_poll_elapsed >= 60.0:
                # Timeout: stop ticking and show the give-up note. We do NOT set
                # _start_poll_done here — an is_alive probe may still be in
                # flight; if it comes back alive its UI-thread handler honours
                # the LATE success (flips the note + lists models) instead of
                # silently dropping it.
                timer.stop()
                self._start_button.setEnabled(True)
                self._note.setText(
                    "Server did not come up — check the OpenCode install / logs, then try again."
                )
                return
            # Kick off the next is_alive probe off the UI thread (one at a time).
            if self._start_probe_in_flight:
                return
            self._start_probe_in_flight = True
            answer = self._start_probe_answer

            def _probe():
                # Off the UI thread (blocking ~2s /health). The verdict is
                # posted back to the UI thread; the daemon mutates
                # NO dialog state directly (a lost QTimer.singleShot here would
                # wedge _start_probe_in_flight=True forever).
                try:
                    alive = sm.is_alive
                except Exception:
                    alive = False
                answer((alive, generation))

            try:
                threading.Thread(target=_probe, daemon=True).start()
            except Exception:
                # Spawn failed after the flag was set — clear it so the next
                # tick can probe again (never wedge _start_probe_in_flight=True).
                self._start_probe_in_flight = False

        def _on_start_probe_result(alive, gen=generation):
            # UI-thread continuation of one is_alive probe.
            # Drop a stale probe from an EARLIER Start-server attempt: applying
            # its verdict would stop a newer poll's timer / re-enable the button /
            # list models for the wrong state.
            if gen != self._start_server_generation:
                return
            self._start_probe_in_flight = False
            if self._start_poll_done:
                return  # already terminal (a prior probe won, or dialog logic done)
            if not alive:
                return  # keep polling (the tick re-arms / enforces timeout)
            # Gate success on is_alive (process up AND /health answers): start()
            # spawns the process EARLY then waits for /health, so is_running
            # flips true before the server can serve /config. This is the SINGLE
            # success seam — it also catches a late success after the timeout
            # note (the timer may already be stopped).
            self._start_poll_done = True
            timer.stop()
            self._start_button.setEnabled(True)
            self._populate_models()  # toggles the button off + lists models

        self._on_start_probe_result = _on_start_probe_result
        timer.timeout.connect(_tick)
        timer.start()

    def _on_start_probe_answer(self, answer):
        """One Start-server probe's ``(alive, generation)``: the newest click's handler
        drops a stale generation."""
        self._on_start_probe_result(*answer)

    def shutdown(self):
        """Cancel the Start-server poll (the dialog closes).

        Stopping the timer guarantees no tick fires after the dialog (and its C++ widgets) are
        gone, avoiding HTTP on the UI thread + use-after-close; latching the poll terminal makes
        a probe still in flight, whose result marshals back after this close, no-op in its
        UI-thread handler.
        """
        timer = getattr(self, "_start_poll_timer", None)
        if timer is not None:
            timer.stop()
        self._start_poll_done = True
        self._closed = (
            True  # a Test connection still running shows nothing (its worker cleans up itself)
        )

    def test_connection(self):
        """Test the LLM backend configuration, off the UI thread; the result dialog follows."""
        backend = self._backend()

        if backend == "none":
            QMessageBox.information(self._owner, "Test Connection", "No backend selected.")
            return
        if self._test_running or self._start_poll_active():
            return
        self._test_running = True
        self._test_button.setEnabled(False)
        self._start_was_enabled = self._start_button.isEnabled()
        self._start_button.setEnabled(False)  # both would use the same host:port
        self._test_cue.start("Testing connection…", at_once=True)
        port, host, model = (
            self._port.value(),
            self._host.text() or "127.0.0.1",
            self._model_value() or None,
        )
        project_root = self._project_root
        try:
            run_off_gui(
                lambda: self._run_test(backend, project_root, host, port, model),
                self._on_test_answer,
                name="settings-test-connection",
            )
        except Exception:
            self._end_test()
            raise

    def _start_poll_active(self):
        """A Start-server attempt is waiting for its server to come up."""
        timer = getattr(self, "_start_poll_timer", None)
        return timer is not None and timer.isActive()

    def _end_test(self):
        self._test_running = False
        self._test_button.setEnabled(True)
        self._start_button.setEnabled(self._start_was_enabled)
        self._test_cue.done()

    def _on_test_answer(self, answer):
        """UI thread: the finished test's ``(QMessageBox method, title, text)``, or None."""
        if self._closed:  # the dialog was closed meanwhile: its widgets and the user are gone
            return
        self._end_test()
        if isinstance(answer, dict):  # the worker itself died
            answer = (
                "critical",
                "Test Connection Failed",
                f"Error testing connection:\n\n{answer.get('error')}",
            )
        if answer is None:  # a backend with nothing to test
            return
        method, title, text = answer
        getattr(QMessageBox, method)(self._owner, title, text)

    @staticmethod
    def _run_test(backend, project_root, host, port, model):
        """Worker thread: the whole test, to the dialog to show: ``(method, title, text)``, or None for none."""
        try:
            if backend == "opencode":
                # Test OpenCode backend
                from ..llm.opencode_backend import OpenCodeBackend, OpenCodeConfig
                from ..llm.lifecycle_adapters import ManagerLifecycle
                from ..llm.backend_base import LLMRequest, LLMTask

                # Build the per-launch derived config (MCP built fresh) from the
                # MASTER blueprint, matching how real chat launches it. Ensure the
                # master exists first (seeded once from the project relic).
                #
                # CRITICAL: build into a THROWAWAY launch_test/ dir, NOT the live
                # launch/ dir. The test server's start() writes a pid file and its
                # stop() DELETES it; sharing the live dir would clobber the LIVE
                # server's orphan-sweep record (its pid file) — the exact
                # orphan-left bug class. Isolating it means the probe's pid file +
                # derived opencode.json never collide with the live ones.
                ensure_master_config(
                    seed_from=(project_root / "opencode.json") if project_root else None
                )
                test_launch_dir = get_opencode_launch_test_dir()
                launch_dir = str(build_launch_config(project_root, launch_dir=test_launch_dir))
                config = OpenCodeConfig(
                    server_port=port,
                    server_hostname=host,
                    model=model,
                    working_directory=launch_dir,
                )

                # Classified availability check — a precise reason (e.g.
                # "OpenCode was not found in the WSL PATH. Install it
                # (npm i -g opencode-ai)...") instead of a vague "not available".
                from ..llm.server_manager import OpenCodeServerManager

                mgr = OpenCodeServerManager(config)
                if not mgr.is_available():
                    status = mgr.last_status
                    reason = status.message if status else "OpenCode is not available."
                    return ("warning", "Test Connection", f"✗ {reason}")

                # C6: install OK is not enough — actually START a server and
                # confirm /health is reachable, surfacing bind failures (the
                # manager classifies PORT_IN_USE/START_TIMEOUT/START_FAILED).
                # The chat test then runs on that same server; try/finally so it
                # is ALWAYS stopped by the manager that started it (it alone
                # knows the server's Linux pid) and its launch_test pid file
                # cleaned, even on an early-return warning path below. This
                # worker is a daemon thread: Studio exiting meanwhile stops it at
                # exit (stop retires a boot still in flight).
                atexit.register(mgr.stop)
                try:
                    if not mgr.start():
                        st = mgr.last_status
                        why = f"\n\n{st.message}" if st and not st.ok else ""
                        return (
                            "warning",
                            "Test Connection",
                            f"✗ Could not start OpenCode\n\nHost: {config.server_hostname}\nPort: {config.server_port}{why}",
                        )
                    if not mgr.health_check():
                        return (
                            "warning",
                            "Test Connection",
                            f"✗ Server started but /health was not reachable\n\n"
                            f"Host: {config.server_hostname}\nPort: {config.server_port}",
                        )

                    # Always run a real end-to-end chat test (Default = auto-pick
                    # included), so the button truly validates the model, not just
                    # that OpenCode is installed + reachable.
                    model_label = config.model or "auto-pick (Default)"
                    backend_obj = OpenCodeBackend(config=config, lifecycle=ManagerLifecycle(mgr))
                    if not backend_obj.start():
                        st = mgr.last_status
                        why = f"\n\n{st.message}" if st and not st.ok else ""
                        return (
                            "warning",
                            "Test Connection",
                            f"✗ Could not start OpenCode\n\nHost: {config.server_hostname}\nPort: {config.server_port}{why}",
                        )
                    test_request = LLMRequest(
                        task=LLMTask.AD_HOC_CHAT,
                        strict_mode=False,
                        user_message="Respond with just 'OK'",
                    )
                    import threading

                    result = {"response": None, "error": None}

                    def test_send():
                        try:
                            result["response"] = backend_obj.send_request(test_request)
                        except Exception as e:
                            result["error"] = str(e)

                    thread = threading.Thread(target=test_send)
                    thread.start()
                    thread.join(timeout=30)
                    if thread.is_alive():
                        result["error"] = "Request timed out after 30 seconds"

                    hint = (
                        "\n\nIf you picked a specific model, your account "
                        "may not support it — try leaving Model empty "
                        "(auto-pick)."
                    )
                    if result["error"]:
                        return (
                            "warning",
                            "Test Connection",
                            f"✗ Chat test failed\n\nModel: {model_label}\n\nError: {result['error']}{hint}",
                        )
                    if result["response"] and result["response"].error_message:
                        return (
                            "warning",
                            "Test Connection",
                            f"✗ Chat test failed\n\nModel: {model_label}\n\nError: {result['response'].error_message}{hint}",
                        )
                    return (
                        "information",
                        "Test Connection",
                        f"✓ OpenCode is working\n\nHost: {config.server_hostname}\nPort: {config.server_port}\nModel: {model_label}",
                    )
                finally:
                    mgr.stop()
                    atexit.unregister(mgr.stop)

        except Exception as e:
            return ("critical", "Test Connection Failed", f"Error testing connection:\n\n{str(e)}")
        finally:  # the probe's derived config goes with it (the app leaves no traces)
            remove_launch_files(get_opencode_launch_test_dir())
