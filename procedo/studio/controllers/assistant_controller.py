"""The Assistant's session actions of the Studio workspace, carved out of
``main_window.StudioWorkspace`` (bodies moved verbatim: ``self`` is this
controller, the window's state is read through ``self._mw``).

The chat panel's send / reset / cancel / compact, the backend restart, the
token readout's limit, cancelling every tab's running request and the
notification sound when a reply lands.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from procedo.core.off_gui import answer_to

from ..llm import LLMTask
from .llm_server_controller import RESTARTING

if TYPE_CHECKING:
    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)


class AssistantController(QObject):
    """The workspace's assistant-session actions (a QObject: the compact daemon answers on
    its slot)."""

    def __init__(self, window: StudioWorkspace) -> None:
        super().__init__(window)
        self._mw = window
        # The compact daemon answers through this relay and never holds this
        # controller: it may be deleted while it runs, and then the answer is
        # dropped (procedo.core.off_gui).
        self._compacted = answer_to(self._on_compacted)

    def cancel_all_workers(self):
        """Cancel any running LLM workers across all tabs."""
        for tab in self._mw._get_llm_tabs():
            if hasattr(tab, '_worker') and tab._worker:
                tab._worker.cancel()
                log.debug(f"Cancelled LLM worker for {tab.__class__.__name__}")

    def update_context_limit(self):
        """Update chat panel's 'Tokens: X/Y' indicator from common_llm.context_window.

        This is purely a UI display value (the model's total context window)
        so the operator can see how much budget has been consumed. It is
        not sent to the LLM. Falls back to the legacy ``max_tokens`` key
        for projects/users on older settings files.
        """
        if not hasattr(self._mw, 'dock') or not hasattr(self._mw.dock, 'chat_panel'):
            return

        common_llm = self._mw._settings.get("common_llm", {}) if hasattr(self._mw, '_settings') else {}
        context_limit = common_llm.get(
            "context_window", common_llm.get("max_tokens", 16384)
        )
        self._mw.dock.chat_panel.set_context_limit(context_limit)

    def on_reset_session(self):
        """Reset all LLM sessions (clears conversation history and rules cache)."""
        # Cancel any in-flight work
        self.cancel_all_workers()
        
        # Reset all tab contexts (clear messages, reset _first_interaction flag, reset backend)
        for tab in self._mw._get_llm_tabs():
            if hasattr(tab, 'tab_context'):
                tab.tab_context.reset_conversation()
                tab.tab_context.reset_backend()
                log.info(f"{tab.__class__.__name__} tab context reset")
        
        # Clear chat panel UI and re-switch to current tab's context
        if hasattr(self._mw, 'dock') and hasattr(self._mw.dock, 'chat_panel'):
            # Get current tab's context before clearing
            current_tab = self._mw.view_stack.currentWidget()
            current_tab_context = None
            if hasattr(current_tab, 'tab_context'):
                current_tab_context = current_tab.tab_context
            
            # Re-switch to current tab's context (which is now empty after reset)
            self._mw.dock.chat_panel.switch_context(current_tab_context)
            log.info("Chat panel UI cleared and switched to current tab context")
            
            # Add system message to chat indicating reset
            self._mw.dock.chat_panel.add_system_message(
                "🔄 Session reset - starting fresh. Rules will be sent on next interaction."
            )
        
        self._mw.llm_server.refresh_indicator()

    def on_cancel_llm(self):
        """Cancel the current tab's running request, else the send waiting for the AI server's
        start, whichever tab holds it (the chat locks for it on every view): not sent."""
        # Check current tab for a running per-tab worker
        current_tab = self._mw.view_stack.currentWidget()
        worker = getattr(current_tab, '_worker', None)
        if worker and worker.isRunning():
            log.info("User requested LLM cancellation (per-tab worker)")
            worker.cancel()

            # Immediate UI feedback
            self._mw.dock.chat_panel.set_llm_active(False)
            self._mw.dock.chat_panel.remove_thinking_message()
            self._mw.dock.chat_panel.add_system_message("Request cancelled by user")
            self._mw.footer.show_message("LLM request cancelled", 3000)
        else:
            self._mw.llm_server.drop_waiting_sends("cancelled.", "neutral")

    def on_restart_backend(self):
        """Restart the shared OpenCode server to recover a hung / unresponsive
        backend without relaunching the editor. Cancels any in-flight request,
        then retires the old manager and stands up a FRESH one (the retired
        manager is single-use); the next send re-uses the restarted server."""
        sm = self._mw.llm_server.server_manager
        if sm is None:
            self._mw.dock.chat_panel.add_system_message(
                "No restartable backend server is configured.", tone="warning")
            return
        # A send waiting for the old server's start is not sent; then cancel any in-flight
        # (possibly hung) request (best-effort).
        self._mw.llm_server.drop_waiting_sends(RESTARTING)
        try:
            self.on_cancel_llm()
        except Exception:
            log.exception("cancel before backend restart failed")
        self._mw.dock.chat_panel.add_message("system", "Restarting backend…")
        self._mw.footer.show_message("Restarting backend…", 3000)

        # A manager is single-use after stop() (its _retired flag is
        # permanent), so we can NOT stop+start the SAME object. Stand up a
        # FRESH manager instead: restart_for_project retires the old
        # one off the UI thread (no freeze, no orphan) and pre-warms the new
        # one on a daemon thread, so this returns immediately.
        self._mw.llm_server.restart_for_project()

    def on_compact_session(self):
        """Manually compact the active tab's LLM session so a long
        conversation keeps fitting in the context window. Runs the compact
        request (a network POST to the OpenCode server) off the UI thread;
        the readout reflects the freed context on the next turn (the server
        reports the reduced total then), and we refresh it now best-effort."""
        current_tab = self._mw.view_stack.currentWidget()
        tab_context = getattr(current_tab, "tab_context", None)
        if tab_context is None:
            self._mw.dock.chat_panel.add_system_message(
                "No active session to compact.")
            return
        backend = tab_context.backend
        self._mw.dock.chat_panel.add_message("system", "Compacting session…")
        self._mw.loading.start("Compacting session…")

        compacted = self._compacted

        def _compact():
            ok = False
            try:
                ok = backend is not None and bool(backend.compact())  # None: never started
            except Exception:
                log.exception("session compact failed")
            compacted(ok)  # the UI feedback runs on the UI thread (this is a daemon)

        threading.Thread(target=_compact, daemon=True).start()

    def _on_compacted(self, ok):
        """UI-thread end of one compaction."""
        self._mw.loading.done()
        if ok:
            self._mw.dock.chat_panel.add_system_message(
                "🗜️ Session compacted — context will shrink on the next reply.", tone="success")
        else:
            self._mw.dock.chat_panel.add_system_message(
                "Compaction is unavailable for this backend (or the request failed).", tone="warning")
        # Re-render the readout from the latest known total (best-effort;
        # the real reduction lands with the next turn's reported total).
        try:
            self._mw.dock.chat_panel._update_context_label()
        except Exception:
            log.exception("context-label refresh after compact failed")

    def on_chat_message(self, message: str):
        """Handle chat message from user."""
        # Set intent based on current tab
        if self._mw.session_state:
            current_tab = self._mw.view_stack.currentWidget()
            if current_tab == self._mw.text_only_tab:
                self._mw.session_state.intent = "Help write a clear, complete procedure text"
            elif current_tab == self._mw.text_json_tab:
                self._mw.session_state.intent = "Help write correct procedure text to generate valid JSON"
            elif current_tab == self._mw.json_code_tab:
                self._mw.session_state.intent = "Help generate correct test code from JSON procedure"
            elif hasattr(self._mw, 'traceability_tab') and current_tab == self._mw.traceability_tab:
                self._mw.session_state.intent = "Help verify traceability between procedure and code"
            else:
                self._mw.session_state.intent = "General assistance with test procedure development"
        
        # Get current active tab
        current_tab = self._mw.view_stack.currentWidget()
        
        # Check if chat is enabled for this tab
        tab_id = getattr(current_tab, 'tab_id', None)
        if tab_id and hasattr(self._mw, 'task_config_manager') and self._mw.task_config_manager:
            chat_config = self._mw.task_config_manager.get_chat_config(tab_id)
            if not chat_config.enabled:
                self._mw.dock.chat_panel.add_message(
                    "system", "Chat is disabled for this tab. Enable it in Settings → Chat."
                )
                return
        
        try:
            current_tab._run_task_async(LLMTask.AD_HOC_CHAT, user_message=message)
        except Exception as e:
            log.error(f"Error executing chat task: {e}", exc_info=True)
            QMessageBox.critical(self._mw, "Task Error", f"Failed to execute task: {str(e)}")

    def play_notification_sound(self) -> None:
        """Play a system notification sound when the LLM finishes."""
        log.debug("Playing notification sound...")
        try:
            import winsound
        except ImportError:
            log.warning("winsound not available (non-Windows platform)")
            return
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            log.debug("Notification sound played (MessageBeep)")
        except Exception:
            try:
                winsound.Beep(1000, 200)
                log.debug("Notification sound played (Beep fallback)")
            except Exception as e:
                log.warning(f"Sound playback failed: {e}")
