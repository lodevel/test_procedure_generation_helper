"""Document/test lifecycle controller carved out of the Studio workspace.

Owns the test-document path: tab-change syncing, the test-opened /
test-deleted lifecycle, the save choke-point (the task-#41 id=folder
enforcement rides ArtifactManager through these save paths), sync-hash
state + indicator + coherence prompts, the unsaved-changes prompt and
the window-close flow. Method bodies were moved verbatim from
``main_window.StudioWorkspace`` (``self`` -> ``mw``); the workspace's menus,
shell and signals connect straight to them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from procedo.core.ui_kit import set_pill

from ..core import ArtifactManager, ArtifactType, SessionState
from ..core.proposal import discard_pending
from ..shell import VIEWS

if TYPE_CHECKING:
    from pathlib import Path

    from ..main_window import StudioWorkspace

log = logging.getLogger(__name__)


class DocumentController:
    """Test-document lifecycle + save/sync logic for the Studio workspace."""

    def __init__(self, window: StudioWorkspace) -> None:
        self._mw = window

    def update_sync_indicator(self):
        """Paint the header's JSON\u2194Code Sync pill (a click acknowledges)."""
        mw = self._mw
        if not mw.session_state:
            set_pill(mw.sync_pill, "neutral", "\u21c4 Sync")
            mw.sync_pill.setToolTip("No test loaded")
        elif mw.session_state.artifacts_in_sync:
            set_pill(mw.sync_pill, "success", "\u21c4 JSON \u2194 Code in sync")
            mw.sync_pill.setToolTip(
                "procedure.json and test.py are in sync.\n"
                "Click to view status."
            )
        else:
            set_pill(mw.sync_pill, "warning", "\u21c4 Sync unconfirmed \u00b7 acknowledge")
            mw.sync_pill.setToolTip(
                "procedure.json and test.py may be out of sync!\n"
                "One was modified without the other.\n"
                "Click to acknowledge that they are coherent."
            )

    def on_sync_indicator_clicked(self):
        """Handle click on the sync indicator."""
        mw = self._mw
        if not mw.session_state:
            return

        if mw.session_state.artifacts_in_sync:
            QMessageBox.information(
                mw,
                "Artifacts In Sync",
                "procedure.json and test.py are currently marked as in sync."
            )
            return

        result = QMessageBox.question(
            mw,
            "Acknowledge Sync",
            "procedure.json and test.py are currently marked as OUT OF SYNC.\n\n"
            "One was modified without the other during this session.\n\n"
            "Are you sure both artifacts are now coherent?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if result == QMessageBox.Yes:
            self.mark_in_sync()
            self.update_sync_indicator()
            mw.workspace_widget.refresh_opened_test()
            mw.footer.show_message("Artifacts marked as in sync", 2000)

    def mark_in_sync(self):
        """Record procedure.json and test.py as coherent: their current hashes
        become the baseline later saves are compared against."""
        mw = self._mw
        if not mw.artifact_manager or not mw.session_state:
            return
        mw.session_state.artifacts_in_sync = True
        mw.session_state.artifact_hashes = mw.artifact_manager.compute_hashes()
        mw.session_state.save()

    def check_artifact_coherence(self) -> bool:
        """Check if JSON and Code artifacts are in sync and warn if not.

        Returns:
            True if the user cancelled (caller should abort), False otherwise.
        """
        mw = self._mw
        if not mw.session_state or mw.session_state.artifacts_in_sync:
            return False

        msg = QMessageBox(mw)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("\u26a0\ufe0f Artifacts Out of Sync")
        msg.setText(
            "procedure.json and test.py may be out of sync!\n\n"
            "One was modified without the other. The test code may not\n"
            "match the procedure definition."
        )

        mark_sync_btn = msg.addButton("They are in sync \u2714", QMessageBox.AcceptRole)
        continue_btn = msg.addButton("Continue anyway", QMessageBox.DestructiveRole)
        go_back_btn = msg.addButton("Go back and review", QMessageBox.RejectRole)
        # Qt under-sizes stylesheet-styled QMessageBox buttons for long
        # labels (the text rect collapses to ~text width with no slack),
        # clipping the last glyphs. Widen each from its own font metrics.
        for _b in (mark_sync_btn, continue_btn, go_back_btn):
            _b.setMinimumWidth(_b.fontMetrics().horizontalAdvance(_b.text()) + 56)
        msg.setDefaultButton(go_back_btn)

        msg.exec()
        clicked = msg.clickedButton()

        if clicked == mark_sync_btn:
            self.mark_in_sync()
            self.update_sync_indicator()
            mw.workspace_widget.refresh_opened_test()
            return False
        elif clicked == go_back_btn:
            return True  # User wants to go back and review
        else:
            # Continue anyway \u2014 leave out-of-sync flag
            return False

    def on_tab_changed(self, index: int):
        """Handle tab change."""
        mw = self._mw
        # Only sync the PREVIOUSLY active tab (the one being deactivated).
        # We must not sync all non-current tabs, as they may hold stale
        # content for shared artifacts (e.g. procedure.json).
        if hasattr(mw, '_previous_tab_index'):
            prev_tab = mw.view_stack.widget(mw._previous_tab_index)
            if prev_tab is not None and prev_tab != mw.view_stack.widget(index):
                if hasattr(prev_tab, 'sync_editors_to_artifacts'):
                    prev_tab.sync_editors_to_artifacts()
                if hasattr(prev_tab, 'on_deactivated'):
                    prev_tab.on_deactivated()

        mw._previous_tab_index = index

        tab = mw.view_stack.widget(index)
        if hasattr(tab, 'on_activated') and mw.artifact_manager is not None:
            tab.on_activated()

        # Switch chat context to current tab's TabContext
        if hasattr(mw, 'dock') and hasattr(tab, 'tab_context'):
            mw.dock.chat_panel.switch_context(tab.tab_context)
            mw.dock.session_viewer.switch_context(tab.tab_context)
            mw.dock.raw_viewer.switch_context(tab.tab_context)
            # Findings are per-test (session_state), no context switch needed

            # If this tab has a running LLM worker, restore in-flight UI
            worker = getattr(tab, '_worker', None)
            if worker and worker.isRunning():
                mw.dock.chat_panel.add_thinking_message()
                mw.dock.chat_panel.set_llm_active(True)
                # Restore all accumulated streaming text so far
                if worker.accumulated_thinking:
                    mw.dock.chat_panel.append_thinking_text(
                        worker.accumulated_thinking
                    )
                if worker.accumulated_response:
                    mw.dock.chat_panel.append_response_text(
                        worker.accumulated_response
                    )
                # Disconnect any stale connections before reconnecting
                # to avoid duplicate text from multiple connections
                try:
                    worker.thinking_chunk.disconnect(
                        mw.dock.chat_panel.append_thinking_text
                    )
                except (RuntimeError, TypeError):
                    # best-effort: disconnect raises when the signal was
                    # never connected (RuntimeError) or the slot owner is
                    # gone (TypeError)
                    pass
                try:
                    worker.text_chunk.disconnect(
                        mw.dock.chat_panel.append_response_text
                    )
                except (RuntimeError, TypeError):
                    # best-effort: same as above -- stale connection may
                    # not exist
                    pass
                # Reconnect streaming signals to the restored thinking widget
                worker.thinking_chunk.connect(
                    mw.dock.chat_panel.append_thinking_text
                )
                worker.text_chunk.connect(
                    mw.dock.chat_panel.append_response_text
                )

        # Update save action label to be context-aware
        mw.save_action.setText(f"&Save {VIEWS[index][0]}")

        # Update menu state based on current artifacts
        mw._update_menu_state()

    def on_test_opened(self, path: Path):
        """Handle test folder being opened."""
        mw = self._mw
        log.info(f"Opening test: {path}")

        # Check for unsaved changes before loading a new test
        if mw.artifact_manager and self.check_unsaved_changes():
            return  # User cancelled

        # Check artifact coherence before switching away
        if mw.artifact_manager and self.check_artifact_coherence():
            return  # User wants to review

        self.discard_proposals()

        # Save current session state (includes validation_issues) before switching
        if hasattr(mw, 'session_state') and mw.session_state and mw.session_state._file_path:
            try:
                mw.session_state.save()
            except Exception as e:
                log.warning(f"Failed to save session state before switching tests: {e}")

        # Initialize managers for this test
        am = mw.artifact_manager = ArtifactManager()
        am.on_media_refreshed = lambda saved: self._on_media_refreshed(am, saved)
        am.media_cue = mw.loading
        mw.artifact_manager.set_test_dir(path)
        mw.artifact_manager.load_all()  # Load existing files from disk
        mw.artifact_manager.set_exclusion_patterns(
            mw.project_manager.get_equipment_patterns()
        )

        # Initialize session state (empty, not with path)
        mw.session_state = SessionState()
        mw.session_state.set_file_path(path)
        mw.session_state.load()  # Load existing session data from disk if it exists

        # Check for external edits since last session
        self.check_for_external_changes()

        # ChatHistoryManager removed: chat history is now per-tab only

        # Update tab contexts with real managers (fixes None reference issue)
        if hasattr(mw.text_only_tab, 'tab_context'):
            mw.text_only_tab.tab_context.update_managers(mw.artifact_manager, mw.session_state)
        if hasattr(mw.text_json_tab, 'tab_context'):
            mw.text_json_tab.tab_context.update_managers(mw.artifact_manager, mw.session_state)
        if hasattr(mw.json_code_tab, 'tab_context'):
            mw.json_code_tab.tab_context.update_managers(mw.artifact_manager, mw.session_state)

        # Point findings panel at the new session state (per-test findings)
        mw.dock.findings_panel.set_session(mw.session_state)

        log.debug(f"Artifacts exist - JSON: {mw.artifact_manager.procedure_json.exists_on_disk}, "
                  f"Code: {mw.artifact_manager.test_code.exists_on_disk}, "
                  f"Text: {mw.artifact_manager.procedure_text.exists_on_disk}")

        # Highlight the opened test in workspace
        mw.workspace_widget.set_opened_test(path)

        # Detect rules (result stored in project_manager.rules_root)
        mw.project_manager.detect_rules_root()

        # Enable the views and the Assistant now that a test is loaded
        mw.view_column.setEnabled(True)
        mw.dock.setEnabled(True)

        # Refresh tabs
        mw.text_only_tab.load_content()
        mw.text_json_tab.load_content()
        mw.json_code_tab.load_content()
        mw.traceability_tab.refresh()

        # Parser availability may have changed since last refresh (e.g. the
        # user edited config.json externally, or a parser variant file was
        # added/removed). Re-evaluate visibility here so the Quick Parse
        # button state is consistent per-test.
        mw.text_json_tab.refresh_parser_button()
        mw.text_only_tab.refresh_parser_button()
        mw.json_code_tab.refresh_code_parser_button()

        # Refresh dock panels with session data
        mw.dock.refresh_session()

        # Update status indicators
        mw._update_status_indicators()
        mw._update_menu_state()

        # Refresh session viewer
        mw.dock.refresh_session()

        # Switch to appropriate tab only on first test load.
        # When switching between tests, preserve the user's current tab.
        previous_index = mw.view_stack.currentIndex()
        if not hasattr(mw, '_has_opened_test'):
            mw._has_opened_test = True
            # Default to the Text tab (text-only authoring view).
            mw.view_stack.setCurrentWidget(mw.text_only_tab)

        # Selecting the already-visible tab emits no currentChanged signal.
        # Bind the dock to the loaded test even on that default startup path.
        if mw.view_stack.currentIndex() == previous_index:
            self.on_tab_changed(previous_index)

    def on_test_deleted(self, path: Path):
        """Handle a test folder being deleted."""
        mw = self._mw
        log.info(f"Test deleted: {path}")

        # If the deleted test was the currently opened one, clear everything
        if mw.artifact_manager and mw.artifact_manager.test_dir == path:
            self.discard_proposals()
            mw.artifact_manager = None
            mw.session_state = None

            # Clear editors
            mw.text_only_tab.text_editor.clear()
            mw.text_json_tab.text_editor.clear()
            mw.text_json_tab.json_editor.clear()
            mw.json_code_tab.json_editor.clear()
            mw.json_code_tab.code_editor.clear()

            # Disable the views and the Assistant
            mw.view_column.setEnabled(False)
            mw.dock.setEnabled(False)

            # Reset the header
            mw._update_status_indicators()

            # Clear opened test highlight
            mw.workspace_widget.set_opened_test(None)

        mw.footer.show_message(f"Deleted test: {path.name}", 3000)

    def discard_proposals(self):
        """The open test is closing: the proposal cards made for it, still waiting in any
        view's chat, can no longer be applied (each card then says so)."""
        mw = self._mw
        count = sum(discard_pending(tab.tab_context.messages) for tab in mw._get_llm_tabs())
        if count:
            log.info("Discarded %d pending proposal(s): their test was closed", count)
            mw.dock.chat_panel.refresh_proposals()

    def on_save(self):
        """Save artifacts managed by the current tab.

        Delegates to the tab's save_all_artifacts() which properly syncs
        editor content, writes to disk, resets dirty flags, and updates
        status labels.
        """
        mw = self._mw
        if not mw.artifact_manager:
            return

        saved = mw.view_stack.currentWidget().save_all_artifacts()  # a failed card said why

        # Check if JSON/Code pair coherence is broken
        self.check_sync_hashes()

        mw._update_status_indicators()
        # Refresh workspace test list to update artifact indicators
        mw.workspace_widget.refresh_opened_test()
        if saved:
            mw.footer.show_message("Saved", 2000)

    def _on_media_refreshed(self, am: ArtifactManager, saved: str) -> bool:
        """A save's media refresh landed. For the open test, which holds its own sync baseline:
        the refreshed JSON is derived data, so when the JSON as saved was the acknowledged one the
        baseline follows it, and the procedure.json editors show what is on disk. False for a
        test closed since: its session file is the baseline (the artifact manager's)."""
        from ..widgets.editor_card import refresh_editor_after_save
        mw = self._mw
        if am is not mw.artifact_manager:
            return False
        content = am.get_content(ArtifactType.PROCEDURE_JSON)
        state, name = mw.session_state, ArtifactManager.PROCEDURE_JSON_NAME
        if (state is not None and state.artifacts_in_sync
                and state.artifact_hashes.get(name) == am.sync_hash(name, saved)):
            state.artifact_hashes = {**state.artifact_hashes, name: am.sync_hash(name, content)}
            state.save()
        for tab in mw._get_llm_tabs():
            for card in tab.cards:
                if card.kind is ArtifactType.PROCEDURE_JSON:
                    refresh_editor_after_save(card.editor, content)
        return True

    def check_sync_hashes(self):
        """Compare current artifact content hashes against the last-acknowledged baseline.

        If any canonical artifact (procedure.json, test.py) changed since the
        user last acknowledged sync, mark artifacts as out-of-sync.  Never
        auto-restores in-sync — only user acknowledgment does that.
        """
        mw = self._mw
        if not mw.artifact_manager or not mw.session_state:
            return
        stored = mw.session_state.artifact_hashes
        if not stored:
            # First save or legacy session — seed baseline, assume in-sync
            mw.session_state.artifact_hashes = mw.artifact_manager.compute_hashes()
            mw.session_state.save()
            return
        current = mw.artifact_manager.compute_hashes()
        if current != stored:
            if mw.session_state.artifacts_in_sync:
                mw.session_state.artifacts_in_sync = False
                mw.session_state.save()
                log.info("Artifacts marked out of sync: content hashes differ from acknowledged baseline")

    def check_for_external_changes(self):
        """Detect files edited outside the workflow editor since the last acknowledgment.

        Compares stored hashes (from .llm_session.json) against current disk
        content.  If any canonical artifact changed, marks artifacts out of sync.
        """
        mw = self._mw
        if not mw.artifact_manager or not mw.session_state:
            return
        stored = mw.session_state.artifact_hashes
        if not stored:
            # First open or legacy session — seed hashes without warning
            mw.session_state.artifact_hashes = mw.artifact_manager.compute_hashes()
            mw.session_state.save()
            return
        changed = mw.artifact_manager.check_external_changes(stored)
        if changed:
            names = ", ".join(changed)
            log.info(f"External changes detected in: {names}")
            mw.session_state.artifacts_in_sync = False
            mw.session_state.save()

    def on_tab_artifact_saved(self):
        """Handle artifact_saved signal from any tab's per-button save.

        Ensures the sync state and UI indicators are updated regardless of
        whether the user used Ctrl+S or a per-tab save button.
        """
        mw = self._mw
        self.check_sync_hashes()
        mw._update_status_indicators()
        mw.workspace_widget.refresh_opened_test()

    def on_save_all(self) -> bool:
        """Save all dirty artifacts across all tabs; False when a write failed (the error is
        shown, what was not written stays modified).

        Syncs only the current tab's editors (to avoid stale content from
        inactive tabs overwriting shared artifacts), then saves all dirty
        artifacts via the ArtifactManager, and reloads all tabs so their
        editors and dirty flags reflect the saved state.
        """
        mw = self._mw
        if not mw.artifact_manager:
            return True

        # Sync only the current tab — inactive tabs may hold stale content
        # for shared artifacts like procedure.json
        current_tab = mw.view_stack.currentWidget()
        if hasattr(current_tab, 'sync_editors_to_artifacts'):
            current_tab.sync_editors_to_artifacts()

        try:
            # Save all dirty artifacts via artifact manager (single source of truth)
            mw.artifact_manager.save_all()
            if mw.session_state:
                mw.session_state.save()
            # Check if artifacts changed from the acknowledged baseline
            self.check_sync_hashes()
        except Exception as exc:  # noqa: BLE001 — told to the user (a share gone, a full disk)
            QMessageBox.critical(mw, "Save Failed", str(exc))
            return False
        finally:
            # Reload all views so editors + Saved pills reflect what was saved
            for i in range(mw.view_stack.count()):
                mw.view_stack.widget(i).load_content()
            mw._update_status_indicators()
            # Refresh workspace test list to update artifact indicators
            mw.workspace_widget.refresh_opened_test()
        mw.footer.show_message("All saved", 2000)
        return True

    def check_unsaved_changes(self) -> bool:
        """Check for unsaved changes and prompt user.

        Syncs only the current tab's editors, then checks artifact dirty state.
        We must NOT sync inactive tabs because their editors may hold stale
        content for shared artifacts (e.g. procedure.json) and would overwrite
        the artifact manager's correct state.

        Returns:
            True if the user cancelled (caller should abort), False otherwise
        """
        mw = self._mw
        if not mw.artifact_manager:
            return False

        # Sync only the CURRENT tab to catch un-saved editor changes.
        # Inactive tabs may have stale content for shared artifacts.
        current_tab = mw.view_stack.currentWidget()
        if hasattr(current_tab, 'sync_editors_to_artifacts'):
            current_tab.sync_editors_to_artifacts()

        dirty = []
        if mw.artifact_manager.is_dirty(ArtifactType.PROCEDURE_JSON):
            dirty.append("procedure.json")
        if mw.artifact_manager.is_dirty(ArtifactType.TEST_CODE):
            dirty.append("test.py")
        if mw.artifact_manager.is_dirty(ArtifactType.PROCEDURE_TEXT):
            dirty.append("procedure_text.md")

        if not dirty:
            return False

        result = QMessageBox.question(
            mw,
            "Unsaved Changes",
            "You have unsaved changes in:\n  \u2022 " + "\n  \u2022 ".join(dirty) +
            "\n\nSave before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save
        )

        if result == QMessageBox.Save:
            return not self.on_save_all()  # a failed save cancels: the edits stay in the window
        elif result == QMessageBox.Cancel:
            return True  # User cancelled
        else:
            return False  # Discard

    def on_close_event(self, event):
        """Handle window close."""
        mw = self._mw
        # Save session state (validation_issues are already in session_state)
        if hasattr(mw, 'session_state') and mw.session_state:
            try:
                mw.session_state.save()
            except Exception as e:
                log.warning(f"Failed to save session state on close: {e}")

        # Check for unsaved changes (syncs editors + prompts)
        if self.check_unsaved_changes():
            event.ignore()
            return

        # Check artifact coherence before closing
        if self.check_artifact_coherence():
            event.ignore()
            return

        # Cancel any running LLM workers
        mw._assistant_controller.cancel_all_workers()

        # Stop all tab backends
        for tab in mw._get_llm_tabs():
            if hasattr(tab, 'tab_context') and tab.tab_context._backend:
                log.debug(f"Stopping backend for {tab.__class__.__name__}")
                tab.tab_context._backend.stop()

        # Stop server manager if exists
        if mw._llm_server.server_manager:
            log.info("Stopping OpenCode server manager...")
            mw._llm_server.server_manager.stop()

        event.accept()
