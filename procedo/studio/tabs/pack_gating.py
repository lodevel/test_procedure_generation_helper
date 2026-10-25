"""The ⚡ pack buttons' gating, one implementation for every view that has them (Text, Text–JSON,
JSON–Code), in two tiers:

- install tier (visibility): a ⚡ button is shown only when the project's engine imports the
  rules_packager_base wheel (``pack_parsers.is_available``) — else the LLM workflow is the only path.
- capability tier (enabled + tooltip): a shown button is enabled only when every equipment type the
  view's procedure uses has a pack providing each capability the button needs (parse / emit /
  codegen); else it is disabled with a tooltip naming the missing pack, so an unsupported-equipment
  procedure cannot dump a cryptic EQP_TYPE_UNKNOWN / ValueError.

Gating is UX, never a correctness gate: any failure leaves a button enabled, and a button whose call
is running stays disabled until it answers. A view lists its buttons in ``_pack_gates()``, says where
its equipment types come from in ``_gated_equipment_types()``, and connects its procedure editor's
``changed`` to :meth:`PackGatingMixin._regate_pack_buttons`.
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from ..pack_ops import engine_async


class PackGatingMixin:
    """A view's ⚡ pack buttons, gated on the wheel and the procedure's equipment."""

    #: A disabled button's tooltip; ``{names}`` lists each type lacking a pack, with its pack (or why).
    _no_pack_tip = "No deterministic support for {names}. Use the LLM workflow."

    def _pack_button(self, text: str, callback, tooltip: str) -> QPushButton:
        """A ⚡ pack button: hidden until the rules_packager_base wheel imports."""
        button = self.create_button(text, callback, tooltip=tooltip)
        button.setVisible(False)
        return button

    def _pack_gates(self) -> list[tuple[QPushButton, str, tuple[str, ...]]]:
        """Each ⚡ button, its tooltip when usable, the pack capabilities it needs."""
        raise NotImplementedError

    def _gated_equipment_types(self, project_root) -> list[str]:
        """The equipment types the view's procedure uses (it may raise while being edited)."""
        raise NotImplementedError

    def refresh_parser_button(self) -> bool:
        """Show the ⚡ buttons iff the wheel imports, then gate them; returns whether it does."""
        from ..pack_ops import pack_parsers

        project_root = getattr(self.project_manager, "project_root", None)
        available, _ = pack_parsers.is_available(project_root)
        for button, _tip, _caps in self._pack_gates():
            button.setVisible(available)
        if available:
            self._apply_capability_gating(project_root)
        return available

    def _apply_capability_gating(self, project_root) -> None:
        """Enable each ⚡ button iff every equipment type has its capabilities (one still running
        is left as it is)."""
        from ..pack_ops import bundle_capabilities

        for button, tip, caps in self._pack_gates():
            if engine_async.busy(button):
                continue
            try:
                types = self._gated_equipment_types(project_root)
                missing = []
                for cap in caps:
                    missing.extend(bundle_capabilities.can_for_types(cap, types, project_root)[1])
                if missing:
                    names = ", ".join(f"{e} ({p})" for e, p in dict(missing).items())
                    button.setEnabled(False)
                    button.setToolTip(self._no_pack_tip.format(names=names))
                else:
                    button.setEnabled(True)
                    button.setToolTip(tip)
            except Exception:
                button.setEnabled(True)  # never break the editor over gating

    def _regate_pack_buttons(self) -> None:
        """Re-gate the shown ⚡ buttons: the equipment set (hence the pack capabilities needed) can
        change as the operator edits the procedure."""
        gates = self._pack_gates()
        if gates and not gates[0][0].isHidden():
            self._apply_capability_gating(getattr(self.project_manager, "project_root", None))
