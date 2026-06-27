"""Phase-1 DCDC wizard: deterministic, zero-false-positive existence checks for
one IC's authoring parameters against the board.

Pure core: :func:`validate_params` runs three BASIC existence checks against a
tiny injected :class:`BoardData` interface, so it is unit-testable with a fake
board (no ODB archive, no subprocess). A thin :class:`OdbBoardData` adapter
(clearly separated, bottom of file) wraps the real ``odb_inspect.load_board``
loader from the integration map.

DEFERRED (intentionally NOT checked here): that the test point sits on the
correct rail net, the enable path, and the voltage source. Phase 1 confirms only
that the IC refdes exists, its part number matches the board's component
property, and the named rail test point resolves to a placed reference — a
``TP*`` pad, any other component (a board may name a test pad by its rail, e.g.
``+AUX0_16V``), or a component pin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable


# ---- result type ----------------------------------------------------------

@dataclass
class Check:
    """One deterministic existence-check outcome."""

    name: str
    passed: bool
    detail: str


# ---- injected board interface (the pure core depends only on this) --------

@runtime_checkable
class BoardData(Protocol):
    """Minimal board projection the validator needs. Implemented by the real
    :class:`OdbBoardData` adapter and by test fakes alike — so the validator is
    pure and never touches an ODB archive or a subprocess itself."""

    def component_part(self, refdes: str) -> Optional[str]:
        """A searchable part/identity string for component ``refdes`` (built from
        its board properties), or ``None`` when no such component exists. An
        existing component with no usable property returns ``""`` (it exists, but
        the part is unverified) — distinguishing 'missing' from 'present-but-
        unknown'."""
        ...

    def node_exists(self, name: str) -> bool:
        """True when ``name`` is a placed PROBE-POINT REFERENCE on the board: a
        ``TP*`` test-point refdes, ANY other placed component refdes (a board may
        designate a test pad by the rail name, e.g. ``+AUX0_16V``), or a component
        PIN of one (``U11.1.24``). A bare NET NAME is NOT a probe point — the
        reference must resolve to a placed component."""
        ...


# ---- stable check names (callers / tests look checks up by these) ---------

CHECK_IC_REFDES = "ic_refdes_exists"
CHECK_IC_PART = "ic_part_matches"
CHECK_RAIL_TP = "rail_test_point_exists"


def _norm(s: str) -> str:
    """Whitespace-collapse + case-fold for tolerant, deterministic comparison."""
    return " ".join((s or "").split()).casefold()


def _part_matches(expected: str, actual: str) -> bool:
    """Zero-false-positive part match: ``expected`` must literally occur in the
    component's property string (exact or substring, case-insensitively). Returns
    True only on positive evidence, so it can never pass a wrong part."""
    e, a = _norm(expected), _norm(actual)
    if not e or not a:
        return False
    return e == a or e in a


def validate_params(params: Mapping[str, object], board: BoardData) -> list[Check]:
    """Run the three Phase-1 existence checks for one IC's authoring params.

    ``params`` carries ``ic_refdes``, ``ic_part`` and ``rail_test_point``. Every
    check is deterministic and conservative: it passes only on positive board
    evidence (never a false positive) and reports a human-readable ``detail``.
    Always returns exactly three checks, in the fixed order
    refdes / part / test-point.
    """
    ic_refdes = str((params or {}).get("ic_refdes") or "").strip()
    ic_part = str((params or {}).get("ic_part") or "").strip()
    rail_tp = str((params or {}).get("rail_test_point") or "").strip()

    checks: list[Check] = []

    # 1) IC refdes exists in the board components.
    part = board.component_part(ic_refdes) if ic_refdes else None
    if not ic_refdes:
        checks.append(Check(CHECK_IC_REFDES, False, "No ic_refdes provided."))
    elif part is None:
        checks.append(Check(CHECK_IC_REFDES, False,
                            f"Component {ic_refdes!r} not found on the board."))
    else:
        checks.append(Check(CHECK_IC_REFDES, True,
                            f"Component {ic_refdes!r} is present."))

    # 2) IC part matches the component's board property.
    if not ic_part:
        checks.append(Check(CHECK_IC_PART, False, "No ic_part provided."))
    elif part is None:
        checks.append(Check(CHECK_IC_PART, False,
                            f"Cannot verify part {ic_part!r}: component "
                            f"{ic_refdes!r} not found."))
    elif _part_matches(ic_part, part):
        checks.append(Check(CHECK_IC_PART, True,
                            f"Part {ic_part!r} matches {ic_refdes!r} properties."))
    else:
        checks.append(Check(CHECK_IC_PART, False,
                            f"Part {ic_part!r} not found in {ic_refdes!r} "
                            f"properties ({part!r})."))

    # 3) Rail test-point node exists in the netlist.
    if not rail_tp:
        checks.append(Check(CHECK_RAIL_TP, False, "No rail_test_point provided."))
    elif board.node_exists(rail_tp):
        checks.append(Check(CHECK_RAIL_TP, True,
                            f"Test point {rail_tp!r} exists in the netlist."))
    else:
        checks.append(Check(CHECK_RAIL_TP, False,
                            f"Test point {rail_tp!r} not found in the netlist."))

    return checks


# ---------------------------------------------------------------------------
# Real-board adapter — clearly separated from the pure core above. Wraps the
# integration map's odb_inspect.load_board loader behind the BoardData interface.
# ---------------------------------------------------------------------------

class OdbBoardData:
    """:class:`BoardData` over an ``odb_inspect`` board dict
    ``{components, nets, error}`` (see ``workflow_editor/core/odb_inspect.py``).

    Construct from an already-loaded board dict, or via :meth:`from_project`,
    which calls the real loader headlessly (a synchronous subprocess — run it off
    the UI thread). The board-specific part-number KEY is unknown, so
    ``component_part`` returns ALL of a component's non-empty property values
    joined into one searchable string; ``_part_matches`` then tests it for the
    expected part with zero false positives.
    """

    def __init__(self, board: Mapping[str, object]):
        self._board = dict(board or {})
        self.error = str(self._board.get("error") or "")
        # refdes -> joined non-empty property values ("" when present, no props)
        self._parts: dict[str, str] = {}
        for comp in self._board.get("components") or ():
            if not isinstance(comp, Mapping):
                continue
            refdes = str(comp.get("refdes") or "")
            if not refdes:
                continue
            props = comp.get("properties") or {}
            vals = (
                [str(v) for v in props.values() if str(v).strip()]
                if isinstance(props, Mapping) else []
            )
            self._parts[refdes] = " ".join(vals)
        # probe-point set: every placed component refdes (a TP* pad OR any other
        # component — a board may designate a test pad by the rail name) + every
        # net-node refdes. A bare NET NAME is deliberately NOT here: the rail test
        # point must resolve to a placed REFERENCE, not just a net.
        self._nodes: set[str] = set(self._parts)
        for net in self._board.get("nets") or ():
            if not isinstance(net, Mapping):
                continue
            for node in net.get("nodes") or ():
                if isinstance(node, Mapping):
                    r = str(node.get("refdes") or "")
                    if r:
                        self._nodes.add(r)

    def component_part(self, refdes: str) -> Optional[str]:
        return self._parts.get(str(refdes or ""))

    def node_exists(self, name: str) -> bool:
        name = str(name or "")
        if not name:
            return False
        if name in self._nodes:                 # TP* pad / any component / net-node refdes
            return True
        # "go all the way": a component PIN ref ('U11.1.24') is a valid probe point
        # too — accept when stripping the trailing .pin yields a placed refdes.
        if "." in name:
            head = name.rsplit(".", 1)[0]
            if head != name and head in self._nodes:
                return True
        return False

    @classmethod
    def from_project(cls, project_root) -> "OdbBoardData":
        """Load the project's board via the real loader and wrap it. The import
        is lazy so the pure core above stays importable without the editor's core
        package on the path."""
        from workflow_editor.core import odb_inspect  # noqa: PLC0415
        return cls(odb_inspect.load_board(project_root))
