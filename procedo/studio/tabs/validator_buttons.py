"""A view's validator buttons: one per registered validator its tab configures, and running
one (the outcome goes to the dock's findings panel and the chat). Mixed into ``BaseTab``."""

import logging
from typing import Optional

from PySide6.QtWidgets import QLayout, QPushButton

log = logging.getLogger(__name__)


class ValidatorButtonsMixin:
    """The validator buttons of a ``BaseTab`` (registry-driven, Phase 2/3)."""

    def _build_validator_buttons(self, parent_layout: QLayout) -> int:
        # Remember the layout so ``rebuild_validator_buttons`` can refresh
        # it when the active project (and therefore the validator config)
        # changes.
        self._validator_button_layout = parent_layout
        return self._populate_validator_buttons(parent_layout)

    def rebuild_validator_buttons(self) -> None:
        """Rebuild this tab's validator buttons against the current
        TaskConfigManager state. Called by
        ``MainWindow.refresh_all_button_labels`` after
        ``task_config_manager.reload(project_root)`` so that opening a
        project surfaces its configured validators."""
        layout = getattr(self, "_validator_button_layout", None)
        if layout is None:
            return
        # Remove existing validator buttons (tagged with the validator_id
        # property). Leave any non-validator widgets the tab put in the
        # same row (e.g. Format JSON) in place.
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QPushButton) and widget.property("validator_id"):
                layout.removeWidget(widget)
                widget.deleteLater()
        self._populate_validator_buttons(layout)

    def _populate_validator_buttons(self, parent_layout: QLayout) -> int:
        """Populate *parent_layout* with one button per registered validator
        listed in ``workflows.<tab>.validators``.

        Returns the count of buttons actually added. Skips disabled
        specs and validators whose id has no matching registry entry
        (with a warning log so the operator can see why a config-
        declared validator failed to appear).

        Modal feedback is dropped universally (plan decision 2026-05-10);
        outcomes route to the dock panel only. The tab's
        ``status_message`` signal carries a one-line summary for the
        status bar.
        """
        from ..core.validators_registry import (
            ensure_builtins_registered,
            is_registered,
        )

        manager = self.task_config_manager
        tab_id = getattr(self, "tab_id", None)
        if manager is None or tab_id is None:
            return 0
        if not hasattr(manager, "get_validator_specs_for_tab"):
            return 0  # older TaskConfigManager — Phase 1 install

        # Probe validator availability so unusable buttons don't render.
        # Two levels (Phase 4.6):
        #   - Master toggle (validator_loop.enabled=false): hide ALL
        #     validator buttons. Operator explicitly opted out.
        #   - validate_procedure-specific availability: hide ONLY the
        #     "Validate Procedure" button when its deps (text_renderer +
        #     pack-side bijective handler) aren't present. Other
        #     validators (python_syntax) are stateless and
        #     keep showing — they only "skip" if the relevant artifact
        #     is empty at click time.
        project_root = getattr(self.project_manager, "project_root", None)
        hide_procedure_button = False
        try:
            if project_root is not None:
                from ..llm.validator_loop_settings import is_enabled
                if not is_enabled(project_root):
                    return 0
                from ..llm.validator_dispatch import is_loop_available
                loop_avail, _ = is_loop_available(project_root)
                hide_procedure_button = not loop_avail
        except Exception:
            log.debug("validator availability probe failed; rendering buttons",
                      exc_info=True)

        ensure_builtins_registered()
        specs = manager.get_validator_specs_for_tab(tab_id)

        added = 0
        for spec in specs:
            vid = spec.get("id")
            if not vid or not spec.get("enabled", True):
                continue
            if not is_registered(vid):
                log.warning(
                    "Tab %s: validator id %r is not in the registry; skipping button.",
                    tab_id, vid,
                )
                continue
            # Hide the Validate Procedure button when its deps aren't
            # available — config-issue case (Phase 4.6 fix).
            if hide_procedure_button and vid.endswith(".validate_procedure"):
                continue
            label = spec.get("label") or self._derive_validator_label(vid)
            tooltip = spec.get("tooltip") or f"Run validator: {vid}"
            btn = self.create_button(
                f"✓ {label}",
                # default-arg capture so the loop variable isn't late-bound.
                callback=lambda _checked=False, _vid=vid: self._run_validator(_vid),
                tooltip=tooltip,
            )
            btn.setProperty("validator_id", vid)
            parent_layout.addWidget(btn)
            added += 1
        return added

    def _derive_validator_label(self, validator_id: str) -> str:
        """Fallback label for a validator without an explicit ``label`` —
        e.g. ``rules_packager_base.validate_procedure`` → ``Validate Procedure``."""
        short = validator_id.rsplit(".", 1)[-1]
        return short.replace("_", " ").title()

    def _get_artifact_for_validation(self, name: str) -> Optional[str]:
        """Return the *current* (live editor) content for a validator: the
        view's editor for that artifact when it has one, else the artifact
        manager's copy.
        """
        card = self.card_for(name)
        if card is not None:
            return card.editor.toPlainText() or None
        am = getattr(self.main_window, "artifact_manager", None)
        if am is None:
            return None
        artifact = None
        if name == "procedure_text":
            artifact = getattr(am, "procedure_text", None)
        elif name == "procedure_json":
            artifact = getattr(am, "procedure_json", None)
        elif name == "test_code":
            artifact = getattr(am, "test_code", None)
        return artifact.content if artifact else None

    def _run_validator(self, validator_id: str) -> None:
        """Look up *validator_id*, build a ``ValidatorContext`` from the
        tab's live state, dispatch, and route the outcome to the dock."""
        from ..core.validators_registry import (
            ValidatorContext,
            get as get_validator,
        )
        from ..core.validation_outcome import render_validation_outcome_summary

        # Push editor → artifact_manager so live content is visible to
        # validators reading via the default ``_get_artifact_for_validation``.
        try:
            self.sync_editors_to_artifacts()
        except Exception:
            log.debug("sync_editors_to_artifacts raised; continuing", exc_info=True)

        try:
            validator = get_validator(validator_id)
        except KeyError:
            log.error("Validator %r not registered at click time", validator_id)
            self.status_message.emit(
                f"Validator {validator_id!r} is not registered (see logs)."
            )
            return

        ctx = ValidatorContext(
            artifact_text=self._get_artifact_for_validation("procedure_text"),
            artifact_json=self._get_artifact_for_validation("procedure_json"),
            artifact_code=self._get_artifact_for_validation("test_code"),
            project_root=self.project_manager.project_root,
            tab_id=getattr(self, "tab_id", ""),
        )

        try:
            outcome = validator(ctx)
        except Exception as exc:
            log.exception("Validator %s raised: %s", validator_id, exc)
            self.status_message.emit(f"Validator {validator_id} crashed; see logs.")
            return

        dock = getattr(self.main_window, "dock", None)
        chat_panel = getattr(dock, "chat_panel", None) if dock else None
        label = self._derive_validator_label(validator_id)

        if outcome.skipped:
            # Surface the skip reason in the dock's findings panel so
            # "Validate Procedure" doesn't appear to be a silent no-op
            # (Phase 4.6 fix). A single info-severity row with the
            # validator id + skip reason replaces the stale findings.
            reason_text = outcome.reason or f"{label}: skipped."
            if dock is not None:
                dock.show_validation_result_from_list([{
                    "severity": "info",
                    "code": "VALIDATOR_SKIPPED",
                    "message": reason_text,
                    "location": validator_id,
                }])
            self.status_message.emit(reason_text)
            # Post a system message in the chat so the operator sees
            # that the click did something — the findings panel can be
            # hidden / off-screen.
            if chat_panel is not None:
                chat_panel.add_system_message(f"⚙️ {label}: skipped — {reason_text}")
            return

        if dock is not None:
            dock.show_validation_result_from_list(
                [issue.to_dock_dict() for issue in outcome.issues]
            )
        summary = render_validation_outcome_summary(outcome)
        self.status_message.emit(summary)
        # Chat-panel echo: makes it obvious the button fired and
        # whether the run passed / found issues. Without this, a clean
        # pass clears the findings panel silently and users assume the
        # button didn't work.
        if chat_panel is not None:
            chat_panel.add_system_message(f"⚙️ {label}: {summary}")
