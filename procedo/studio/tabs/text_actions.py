"""What both Text views (Text, Text–JSON) do to procedure_text.md, built once: the FILE row's
Renumber steps / Sync Equipment / Sync Meta, the ⚡ pack buttons' gating on the text's equipment,
Review Text, applying a text proposal, and ⚡ Parse + Generate.

The view keeps procedure_text.md in ``self.text_card`` (an ``EditorCard``) and lists its ⚡ pack
buttons with what each needs in ``_pack_gates()`` (:mod:`.pack_gating`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from PySide6.QtWidgets import QPushButton

from ..core.proposal import FileChange, FileText, PartlyApplied, Proposal, current_test
from ..llm import LLMTask
from ..llm.reconstruction import reconstructed_or_error
from ..pack_ops import engine_async
from .pack_gating import PackGatingMixin
from .pinned_constants import keep_pinned_constants

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


#: The warning marking a Sync Equipment answer that would delete unparseable lines.
_SYNCEQP_BLOCKED = "[SYNCEQP_BLOCKED]"


@dataclass(frozen=True)
class _TextTransform:
    """A deterministic rewrite of procedure_text.md: the ``pack_parsers`` function (text →
    ``(text, warnings)``, or the text alone without *warns*), the button it runs from, and the
    (status, chat) it says when the text is *applied*, already the *same*, or *refused* (a
    blocked answer, when given)."""

    op: str
    button: str
    cue_label: str
    error_title: str
    applied: tuple[str, str]
    same: tuple[str, str]
    refused: Optional[tuple[str, str]] = None
    warns: bool = True


_SYNC_EQUIPMENT = _TextTransform(
    "sync_equipment_from_steps", "sync_equipment_btn", "Syncing equipment\u2026",
    "Sync Equipment failed",
    applied=("Equipment synced from steps",
             "🔧 Sync Equipment applied — ## Equipment refreshed from steps. "
             "Declared types were kept (type conflicts are warned) and "
             "ratings merged as max(declared, observed)."),
    same=("Sync Equipment: no changes (everything already declared)",
          "🔧 Sync Equipment: no changes (## Equipment already matches "
          "every device referenced in steps)."),
    refused=("Sync Equipment: REFUSED — unparseable ## Equipment lines (see chat)",
             "🔧 Sync Equipment REFUSED — the existing ## Equipment block "
             "has lines that don't parse (wrong syntax, or their pack "
             "isn't in the active bundle); syncing would delete them. "
             "Nothing was changed."))

_SYNC_META = _TextTransform(
    "sync_meta_text", "sync_meta_btn", "Syncing meta\u2026", "Sync Meta failed",
    applied=("Meta synced from bundle",
             "🏷️ Sync Meta applied — ## Meta regenerated from the active bundle "
             "(pack versions re-pinned; board / format_version preserved)."),
    same=("Sync Meta: no changes (## Meta already matches the bundle)",
          "🏷️ Sync Meta: no changes (## Meta already matches the active bundle)."))

_RENUMBER_STEPS = _TextTransform(
    "renumber_steps_text", "renumber_steps_btn", "Renumbering steps\u2026", "Renumber failed",
    applied=("Steps renumbered",
             "🔢 Renumber Steps applied — every step in ## Steps is now sequential 1..N."),
    same=("Steps already sequential", "🔢 Renumber Steps: already sequential — no changes."),
    warns=False)


class TextActionsMixin(PackGatingMixin):
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

    # ---- ⚡ gating ------------------------------------------------------------------------

    def _gated_equipment_types(self, project_root) -> list[str]:
        from ..pack_ops import bundle_capabilities
        return bundle_capabilities.equipment_types_in(self.text_card.editor.toPlainText(), project_root)

    def refresh_parser_button(self) -> bool:
        """Gate the ⚡ buttons (:class:`PackGatingMixin`); Sync Meta shows with them."""
        available = super().refresh_parser_button()
        self.sync_meta_btn.setVisible(available)
        return available

    # ---- deterministic text transforms ----------------------------------------------------

    def _on_sync_equipment(self) -> None:
        """Rescan ``## Steps`` and refresh ``## Equipment``. Declared
        types anchor (conflicts are warned, never flipped) and ratings
        merge as max(declared, observed) — bench headroom survives.
        Refuses (text untouched) when declared lines don't parse."""
        self._transform_text(_SYNC_EQUIPMENT)

    def _on_sync_meta(self) -> None:
        """Regenerate the ``## Meta`` block (format_version / board + the
        rules_pack / labscpi_pack / fncore_pack version pins) from the active
        bundle, deterministically (no LLM). board / format_version and extra
        keys are preserved; pack versions are re-pinned to the bundle."""
        self._transform_text(_SYNC_META)

    def _on_renumber_steps(self) -> None:
        """Rewrite the leading ``N.`` on every step in the ``## Steps``
        block to be sequential 1..N. Pure text transform routed
        through the bundle's parser so the convention stays
        bundle-defined."""
        self._transform_text(_RENUMBER_STEPS)

    def _transform_text(self, how: _TextTransform) -> None:
        """Run *how* on the text off the GUI thread; :meth:`_text_transformed` takes its answer."""
        from ..pack_ops import pack_parsers
        original = self.text_card.editor.toPlainText()
        if not original:
            return
        project_root = getattr(self.project_manager, "project_root", None)
        transform = getattr(pack_parsers, how.op)
        engine_async.call(
            project_root, lambda: transform(original, project_root=project_root),
            lambda answer: self._text_transformed(how, original, answer),
            button=getattr(self, how.button), cue=self.text_card.loading, cue_label=how.cue_label)

    def _text_transformed(self, how: _TextTransform, original: str, answer) -> None:
        """Apply the transformed text, unless refused or unchanged (the text left as it is, so
        the dirty flag doesn't flip needlessly); the outcome goes to the status bar and the chat,
        the warnings with it."""
        from procedo.engine_client import engine_session
        try:
            new_text, warnings = answer() if how.warns else (answer(), [])
        except engine_session.ParserUnavailable as exc:
            self.show_error(how.error_title, str(exc))
            return
        if not engine_async.unchanged(self.text_card.editor, original, self.status_message.emit):
            return
        if how.refused and any(_SYNCEQP_BLOCKED in w for w in warnings):
            status, chat = how.refused
        elif new_text == original:
            status, chat = how.same
        else:
            self.text_card.set_text(new_text)
            status, chat = how.applied
        self.status_message.emit(status)
        post_to_chat(self.main_window, chat + format_chat_warnings(warnings))

    def _on_parse_and_generate(self):
        """Strict one-click: Text → JSON → test.py, offered as one proposal card.

        Reads the live text editor, parses to JSON then generates code deterministically
        (no LLM), each off the GUI thread, aborts on any parser/codegen warning, then — when
        procedure.json or test.py already has content that would change — waits for the
        card's Accept before :meth:`_write_parse_and_generate` writes ``procedure_text`` (if
        dirty), ``procedure.json`` and ``test.py``.

        The card's bases come from the ArtifactManager. The text and JSON editors mirror
        their content there on every keystroke, so those bases are fresh. The ``test_code``
        base is whatever was last loaded/saved (no Text tab has a code editor to mirror
        from) — the diff preview for test.py may therefore lag an out-of-band edit, but the
        write itself is always correct.
        """
        from ..pack_ops import pack_parsers
        sent = self.text_card.editor.toPlainText()
        text = sent.strip()
        if not text:
            self.show_warning("No Content", "Procedure text is empty. Add text first.")
            return
        project_root = getattr(self.project_manager, "project_root", None)
        engine_async.call(
            project_root, lambda: pack_parsers.parse_text(text, project_root=project_root),
            lambda answer: self._parse_and_generate_parsed(sent, answer),
            button=self.parse_and_generate_btn, cue=self.text_card.loading, cue_label="Parsing\u2026")

    def _parse_and_generate_abort(self, reason: str) -> None:
        self.main_window.dock.chat_panel.add_system_message(reason, tone="danger")

    def _parse_and_generate_parsed(self, sent: str, answer) -> None:
        """Text → JSON (strict) answered: on to JSON → code."""
        from procedo.engine_client import engine_session

        from ..pack_ops import pack_parsers
        try:
            procedure, parse_warnings = answer()
        except engine_session.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Parse + Generate: parse_text raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e, title="Parse + Generate — parser findings",
                intro="The deterministic Text→JSON parser rejected the input.",
                parent=self,
            )
            return

        if parse_warnings:
            warn_lines = "\n".join(f"  • {w}" for w in parse_warnings)
            self._parse_and_generate_abort(
                f"✗ Parse + Generate aborted — parser produced {len(parse_warnings)} warning(s).")
            self.show_warning(
                "Parse + Generate — strict",
                f"Parser produced {len(parse_warnings)} warning(s); aborting "
                f"(strict mode):\n\n{warn_lines}",
            )
            return
        project_root = getattr(self.project_manager, "project_root", None)
        engine_async.call(
            project_root, lambda: pack_parsers.generate_code(procedure, project_root=project_root),
            lambda answer: self._parse_and_generate_generated(sent, procedure, answer),
            button=self.parse_and_generate_btn, cue=self.text_card.loading, cue_label="Generating code\u2026")

    def _parse_and_generate_generated(self, sent: str, procedure: dict, answer) -> None:
        """JSON → code (strict) answered: offer both files as one card."""
        from ..core import ArtifactType
        from procedo.engine_client import engine_session

        # ensure_ascii=False matches the LLM JSON-proposal write path so the
        # diff base (artifact_manager content) doesn't show spurious \uXXXX vs
        # literal-unit-symbol noise (µ, ±, Ω are common in this domain).
        procedure_str = json.dumps(procedure, indent=2, ensure_ascii=False)
        try:
            code, gen_warnings = answer()
        except engine_session.ParserUnavailable as e:
            self.show_warning("Parser Unavailable", str(e))
            return
        except Exception as e:
            log.exception("Parse + Generate: generate_code raised")
            from ..dialogs.validator_error_dialog import ValidatorErrorDialog
            ValidatorErrorDialog.show_from_exception(
                e, title="Parse + Generate — codegen findings",
                intro="The deterministic JSON→Code generator failed.",
                parent=self,
            )
            return

        if gen_warnings:
            warn_lines = "\n".join(f"  • {w}" for w in gen_warnings)
            self._parse_and_generate_abort(
                f"✗ Parse + Generate aborted — codegen produced {len(gen_warnings)} warning(s).")
            self.show_warning(
                "Parse + Generate — strict",
                f"Codegen produced {len(gen_warnings)} warning(s); aborting "
                f"before writing files (strict mode):\n\n{warn_lines}",
            )
            return
        if not engine_async.unchanged(self.text_card.editor, sent, self.status_message.emit):
            return

        code = keep_pinned_constants(
            code, (self.artifact_manager.get_content(ArtifactType.TEST_CODE) or "").strip(),
            procedure, "Parse + Generate")

        def current(kind):
            return lambda: self.artifact_manager.get_content(kind) or ""

        def rejected() -> None:
            kept = proposal.written()  # an Accept that failed part-way wrote these
            self._parse_and_generate_abort(
                "✗ Parse + Generate — changes rejected."
                + (f" {' + '.join(kept)} stays as written." if kept else ""))

        proposal = Proposal(
            [FileChange("procedure.json", current(ArtifactType.PROCEDURE_JSON), procedure_str),
             FileChange("test.py", current(ArtifactType.TEST_CODE), code)],
            lambda: self._write_parse_and_generate(procedure_str, code), rejected,
            inputs=[FileText("procedure_text.md", self.text_card.editor.toPlainText)],
            test=current_test(self.artifact_manager))
        if any(change.base.strip() for change in proposal.changes):
            self.main_window.dock.chat_panel.offer_proposal(self.tab_context, proposal)
        else:  # nothing there yet to overwrite: written at once
            proposal.accept()

    def _write_parse_and_generate(self, procedure_str: str, code: str) -> None:
        """Parse + Generate accepted: save the text (if modified), write procedure.json then
        test.py, and record them in sync. Raises when a file cannot be written —
        :class:`PartlyApplied` once procedure.json was. Every view's editor of either file shows
        the artifact afterwards: what was written, or what it held when its write failed."""
        from ..core import ArtifactType

        if self.text_card.dirty and not self.text_card.save():
            raise OSError(f"{self.text_card.file_name} could not be saved")

        written: list[str] = []
        files = ((ArtifactType.PROCEDURE_JSON, "procedure.json", procedure_str),
                 (ArtifactType.TEST_CODE, "test.py", code))
        try:
            for kind, name, content in files:
                self._write_artifact(kind, content)
                written.append(name)
        except Exception as exc:
            if written:
                raise PartlyApplied(f"{' + '.join(written)} saved, {name} not: {exc}") from exc
            raise
        finally:
            for tab in self.main_window._get_llm_tabs():
                for card in tab.cards:
                    if card.kind in (ArtifactType.PROCEDURE_JSON, ArtifactType.TEST_CODE):
                        card.load()

        self.main_window.dock.chat_panel.add_system_message(
            "⚡ Parse + Generate complete — procedure.json and test.py saved.", tone="success"
        )
        self.status_message.emit("⚡ Parse + Generate complete")
        # Both files were just generated from the same text: they are in sync.
        self.main_window._document_controller.mark_in_sync()
        self.artifact_saved.emit()

    def _write_artifact(self, kind, content: str) -> None:
        """Save *content* as the *kind* artifact; when the write raises, the artifact keeps what
        it held."""
        held = self.artifact_manager.get_content(kind)
        self.artifact_manager.set_content(kind, content)
        try:
            self.artifact_manager.save_artifact(kind)
        except Exception:
            self.artifact_manager.set_content(kind, held)
            raise


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
        test = self._ensure_run_state().test
        engine_async.call(
            project_root,
            lambda: reconstructed_or_error(content_str, current_content,
                                           task_override=override, project_root=project_root),
            lambda answer: self._offer_reconstructed(current_content, test, answer),
            cue=self.text_card.loading, cue_label="Preparing the proposal\u2026")

    def _offer_reconstructed(self, current_content: str, test, answer) -> None:
        """The reconstruction answered: offer it as a card for the text it was made from."""
        reconstructed, best_effort, err = answer()
        if not engine_async.unchanged(self.text_card.editor, current_content,
                                      self.status_message.emit):
            return
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
        self.propose_edit(self.text_card, content_str, banner=banner, test=test)
