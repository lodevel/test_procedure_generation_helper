"""What both Text views (Text, Text–JSON) do to procedure_text.md, built once: the FILE row's
Renumber steps / Sync Equipment / Sync Meta, the ⚡ pack buttons' gating, Review Text, applying a
text proposal, and ⚡ Parse + Generate.

The view keeps procedure_text.md in ``self.text_card`` (an ``EditorCard``) and lists its ⚡ pack
buttons with what each needs in ``_pack_gates()``.
"""

from __future__ import annotations

import json
import logging

from PySide6.QtWidgets import QPushButton

from ..llm import LLMTask
from ..llm.reconstruction import reconstructed_or_error

log = logging.getLogger(__name__)


def post_to_chat(main_window, content: str) -> None:
    """Post a system-styled message to the chat panel. Silent no-op
    when the dock or chat panel isn't available (e.g. headless tests)."""
    dock = getattr(main_window, "dock", None)
    chat = getattr(dock, "chat_panel", None) if dock else None
    if chat is None or not hasattr(chat, "add_system_message"):
        return
    try:
        chat.add_system_message(content)
    except Exception:
        log.exception("chat notification failed; ignoring")


def format_chat_warnings(warnings: list[str]) -> str:
    """Append a warning block to a chat message, or empty string if none."""
    if not warnings:
        return ""
    return "\n\n⚠ Warnings:\n" + "\n".join(f"• {w}" for w in warnings)


class TextActionsMixin:
    """The Text views' shared actions on ``self.text_card``."""

    # ---- the FILE row ---------------------------------------------------------------------

    def _text_file_buttons(self) -> list[QPushButton]:
        """Renumber steps, Sync Equipment and Sync Meta (hidden until the installed wheel
        provides it: ``refresh_parser_button``)."""
        self.renumber_steps_btn = self.create_button(
            "Renumber steps", self._on_renumber_steps,
            tooltip="Rewrite the leading N. on every step in ## Steps to be sequential 1..N — useful after inserting a step in the middle",
        )
        self.sync_equipment_btn = self.create_button(
            "Sync Equipment", self._on_sync_equipment,
            tooltip=(
                "Rescan ## Steps and REGENERATE ## Equipment from device "
                "references. The existing block is replaced — operator "
                "customizations (controller subtype, extra headroom on "
                "max_current, etc.) are overwritten. Edit manually after "
                "Sync if you want bench headroom."
            ),
        )
        self.sync_meta_btn = self.create_button(
            "Sync Meta", self._on_sync_meta,
            tooltip=(
                "Regenerate ## Meta (format_version / board / rules_pack / "
                "labscpi_pack / fncore_pack) from the active bundle — no LLM. "
                "board and format_version are preserved; pack versions are "
                "re-pinned to the bundle."
            ),
        )
        self.sync_meta_btn.setVisible(False)
        return [self.renumber_steps_btn, self.sync_equipment_btn, self.sync_meta_btn]

    def _pack_button(self, text: str, callback, tooltip: str) -> QPushButton:
        """A ⚡ pack button: hidden until the rules_packager_base wheel imports."""
        button = self.create_button(text, callback, tooltip=tooltip)
        button.setVisible(False)
        return button

    # ---- ⚡ gating ------------------------------------------------------------------------

    def _pack_gates(self) -> list[tuple[QPushButton, str, tuple[str, ...]]]:
        """Each ⚡ button, its tooltip when usable, the pack capabilities it needs."""
        raise NotImplementedError

    def refresh_parser_button(self):
        """Gate the deterministic ⚡ buttons in two tiers:

        - install-tier (visibility): hidden unless the rules_packager_base
          wheel imports cleanly — then the LLM workflow is the only path.
        - capability-tier (enable + tooltip): when visible, each button is
          enabled only if every equipment type the current procedure uses has
          a pack providing that button's capability (parse / emit / codegen).
          Otherwise it is disabled with a tooltip naming the missing pack, so
          an unsupported-equipment procedure cannot dump a cryptic
          EQP_TYPE_UNKNOWN / ValueError — the operator is told to use the LLM.
        """
        from ..pack_ops import pack_parsers
        project_root = getattr(self.project_manager, "project_root", None)
        available, _ = pack_parsers.is_available(project_root)
        for button, _tip, _caps in self._pack_gates():
            button.setVisible(available)
        # Sync Meta gates on the function itself, not just wheel-imports: an old
        # wheel imports (available=True) yet lacks sync_meta_text.
        self.sync_meta_btn.setVisible(pack_parsers.supports_sync_meta(project_root))
        if available:
            self._apply_capability_gating(project_root)

    def _apply_capability_gating(self, project_root):
        """Enable/disable the ⚡ buttons per the current procedure's equipment
        capabilities. Best-effort: any failure leaves buttons enabled (gating
        is UX, never a correctness gate)."""
        from ..pack_ops import pack_parsers
        text = self.text_card.editor.toPlainText()
        for button, tip, caps in self._pack_gates():
            try:
                missing = []
                for cap in caps:
                    ok, miss = pack_parsers.can(cap, text, project_root)
                    if not ok:
                        missing.extend(miss)
                if missing:
                    names = ", ".join(f"{e} ({p})" for e, p in dict(missing).items())
                    button.setEnabled(False)
                    button.setToolTip(
                        f"No deterministic support for {names}. Use the LLM workflow."
                    )
                else:
                    button.setEnabled(True)
                    button.setToolTip(tip)
            except Exception:
                button.setEnabled(True)  # never break the editor over gating

    def _on_text_edited(self) -> None:
        """Re-gate the ⚡ buttons: the equipment set (hence the pack
        capabilities needed) can change as the operator edits the text."""
        gates = self._pack_gates()
        if gates and not gates[0][0].isHidden():
            self._apply_capability_gating(getattr(self.project_manager, "project_root", None))

    # ---- deterministic text transforms ----------------------------------------------------

    def _on_sync_equipment(self) -> None:
        """Rescan ``## Steps`` and refresh ``## Equipment``. Declared
        types anchor (conflicts are warned, never flipped) and ratings
        merge as max(declared, observed) — bench headroom survives.
        Refuses (text untouched) when declared lines don't parse.
        Outcome + warnings posted to the chat panel."""
        from ..pack_ops import pack_parsers
        original = self.text_card.editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            new_text, warnings = pack_parsers.sync_equipment_from_steps(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Sync Equipment failed", str(exc))
            return
        if any("[SYNCEQP_BLOCKED]" in w for w in warnings):
            self.status_message.emit(
                "Sync Equipment: REFUSED — unparseable ## Equipment lines "
                "(see chat)"
            )
            post_to_chat(
                self.main_window,
                "🔧 Sync Equipment REFUSED — the existing ## Equipment block "
                "has lines that don't parse (wrong syntax, or their pack "
                "isn't in the active bundle); syncing would delete them. "
                "Nothing was changed."
                + format_chat_warnings(warnings),
            )
            return
        if new_text == original:
            self.status_message.emit(
                "Sync Equipment: no changes (everything already declared)"
            )
            post_to_chat(
                self.main_window,
                "🔧 Sync Equipment: no changes (## Equipment already matches "
                "every device referenced in steps)."
                + format_chat_warnings(warnings),
            )
            return
        self.text_card.set_text(new_text)
        self.status_message.emit("Equipment synced from steps")
        post_to_chat(
            self.main_window,
            "🔧 Sync Equipment applied — ## Equipment refreshed from steps. "
            "Declared types were kept (type conflicts are warned) and "
            "ratings merged as max(declared, observed)."
            + format_chat_warnings(warnings),
        )

    def _on_sync_meta(self) -> None:
        """Regenerate the ``## Meta`` block (format_version / board + the
        rules_pack / labscpi_pack / fncore_pack version pins) from the active
        bundle, deterministically (no LLM). board / format_version and extra
        keys are preserved; pack versions are re-pinned to the bundle."""
        from ..pack_ops import pack_parsers
        original = self.text_card.editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            new_text, warnings = pack_parsers.sync_meta_text(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Sync Meta failed", str(exc))
            return
        if new_text == original:
            self.status_message.emit(
                "Sync Meta: no changes (## Meta already matches the bundle)"
            )
            post_to_chat(
                self.main_window,
                "🏷️ Sync Meta: no changes (## Meta already matches the active bundle)."
                + format_chat_warnings(warnings),
            )
            return
        self.text_card.set_text(new_text)
        self.status_message.emit("Meta synced from bundle")
        post_to_chat(
            self.main_window,
            "🏷️ Sync Meta applied — ## Meta regenerated from the active bundle "
            "(pack versions re-pinned; board / format_version preserved)."
            + format_chat_warnings(warnings),
        )

    def _on_renumber_steps(self) -> None:
        """Rewrite the leading ``N.`` on every step in the ``## Steps``
        block to be sequential 1..N. Pure text transform routed
        through the bundle's parser so the convention stays
        bundle-defined. Skips the rewrite when nothing changed so
        the dirty flag doesn't flip needlessly. Confirmation in chat."""
        from ..pack_ops import pack_parsers
        original = self.text_card.editor.toPlainText()
        if not original:
            return
        try:
            project_root = getattr(self.project_manager, "project_root", None)
            renumbered = pack_parsers.renumber_steps_text(
                original, project_root=project_root,
            )
        except pack_parsers.ParserUnavailable as exc:
            self.show_error("Renumber failed", str(exc))
            return
        if renumbered == original:
            self.status_message.emit("Steps already sequential")
            post_to_chat(
                self.main_window,
                "🔢 Renumber Steps: already sequential — no changes.",
            )
            return
        self.text_card.set_text(renumbered)
        self.status_message.emit("Steps renumbered")
        post_to_chat(
            self.main_window,
            "🔢 Renumber Steps applied — every step in ## Steps is now "
            "sequential 1..N.",
        )

    def _on_parse_and_generate(self):
        """Strict one-click: Text → JSON → test.py, offered as a proposal card.
        Delegates to the shared LLMTabMixin helper."""
        self._run_deterministic_parse_and_generate()

    # ---- the assistant on the text --------------------------------------------------------

    def _on_review_text(self):
        if not self.text_card.editor.toPlainText():
            self.show_warning("No Text", "Text editor is empty. Write text first.")
            return
        self._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)

    def _handle_text_proposal(self, proposal, task=None):
        """Offer a procedure_text proposal as a proposal card.

        Accepts "create" alongside "replace" — semantically identical (full
        content for the artifact): the LLM emits "create" for fresh authoring
        and "replace" for overwriting. "patch" is intentionally NOT included —
        partial-edit semantics need a different handler. ``task`` carries the
        per-task section-ownership override into the reconstruction.
        """
        if proposal.mode not in ("replace", "create"):
            return

        if isinstance(proposal.content, dict):
            content_str = json.dumps(proposal.content, indent=2)
        else:
            content_str = str(proposal.content)

        current_content = self.text_card.editor.toPlainText()
        # Reconstruct operator-owned sections (test id, description, Meta)
        # from the prior before showing the diff — the LLM authors only the
        # body sections; identity is parser-owned.
        project_root = getattr(self.project_manager, "project_root", None)
        override = self._task_section_override(task)
        reconstructed, best_effort, err = reconstructed_or_error(
            content_str, current_content,
            task_override=override, project_root=project_root,
        )
        banner = ""
        if err:
            if best_effort is None:
                # Nothing to show (e.g. parser/wheel unavailable) — warn only.
                self.main_window.dock.chat_panel.add_system_message(
                    f"⚠ Could not reconstruct procedure_text.md: {err}", tone="warning"
                )
                return
            # Invalid proposal, but a best-effort document exists: still offer it,
            # its card warning and the diff window showing the findings, so the
            # operator can fix / apply / reject it instead of it being silently dropped.
            banner = (
                "Proposal failed validation — review and fix before "
                "applying:\n" + err
            )
            content_str = best_effort
        else:
            content_str = reconstructed
        self.propose_edit(self.text_card, content_str, banner=banner,
                          test=self._ensure_run_state().test)
