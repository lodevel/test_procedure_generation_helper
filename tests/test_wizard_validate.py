"""Phase-1 DCDC wizard validator: deterministic existence checks.

Pure unit tests over a FAKE board (no ODB archive, no subprocess). The all-exist
case passes every check; a missing refdes / wrong part / missing test point each
fails exactly its own check and nothing false-passes. One adapter test proves
:class:`OdbBoardData` projects a real ``odb_inspect`` board dict correctly.
"""
from workflow_editor.authoring.wizard.validate import (
    CHECK_IC_PART,
    CHECK_IC_REFDES,
    CHECK_RAIL_TP,
    Check,
    OdbBoardData,
    validate_params,
)

GOOD_PARAMS = {"ic_refdes": "U86", "ic_part": "TPS62933", "rail_test_point": "TP12"}


class FakeBoard:
    """Minimal in-memory BoardData for unit tests.

    ``parts`` maps refdes -> property string; a key being present means the
    component exists. ``nodes`` is the set of node names known to the netlist.
    """

    def __init__(self, parts, nodes):
        self._parts = dict(parts)
        self._nodes = set(nodes)

    def component_part(self, refdes):
        return self._parts.get(refdes)

    def node_exists(self, name):
        return name in self._nodes


def _by_name(checks):
    by = {c.name: c for c in checks}
    assert len(by) == len(checks), "check names must be unique"
    return by


def _good_board():
    return FakeBoard(parts={"U86": "TPS62933DRLR 10uF"}, nodes={"U86", "TP12"})


def test_all_exist_all_pass():
    checks = validate_params(GOOD_PARAMS, _good_board())
    assert all(isinstance(c, Check) for c in checks)
    by = _by_name(checks)
    assert set(by) == {CHECK_IC_REFDES, CHECK_IC_PART, CHECK_RAIL_TP}
    assert all(c.passed for c in checks), \
        [(c.name, c.detail) for c in checks if not c.passed]


def test_missing_refdes_fails_refdes_check():
    board = FakeBoard(parts={}, nodes={"TP12"})  # U86 absent
    by = _by_name(validate_params(GOOD_PARAMS, board))
    assert by[CHECK_IC_REFDES].passed is False
    assert "U86" in by[CHECK_IC_REFDES].detail
    # part cannot be verified without the component, but the TP is unaffected.
    assert by[CHECK_IC_PART].passed is False
    assert by[CHECK_RAIL_TP].passed is True


def test_wrong_part_fails_part_check_only():
    board = FakeBoard(parts={"U86": "LM317"}, nodes={"U86", "TP12"})
    by = _by_name(validate_params(GOOD_PARAMS, board))
    assert by[CHECK_IC_REFDES].passed is True
    assert by[CHECK_IC_PART].passed is False
    assert "TPS62933" in by[CHECK_IC_PART].detail
    assert by[CHECK_RAIL_TP].passed is True


def test_missing_test_point_fails_tp_check_only():
    board = FakeBoard(parts={"U86": "TPS62933"}, nodes={"U86"})  # TP12 absent
    by = _by_name(validate_params(GOOD_PARAMS, board))
    assert by[CHECK_IC_REFDES].passed is True
    assert by[CHECK_IC_PART].passed is True
    assert by[CHECK_RAIL_TP].passed is False
    assert "TP12" in by[CHECK_RAIL_TP].detail


def test_part_matches_as_substring_of_properties():
    # Real boards store e.g. an ordering-part-number; the manufacturer part is a
    # substring. The match must still pass (zero false NEGATIVE here).
    board = FakeBoard(parts={"U86": "NAME=TPS62933DRLR VALUE=DC-DC"},
                      nodes={"U86", "TP12"})
    by = _by_name(validate_params(GOOD_PARAMS, board))
    assert by[CHECK_IC_PART].passed is True


def test_blank_params_fail_cleanly():
    by = _by_name(validate_params({}, FakeBoard({}, set())))
    assert set(by) == {CHECK_IC_REFDES, CHECK_IC_PART, CHECK_RAIL_TP}
    assert all(c.passed is False for c in by.values())


def test_odb_adapter_projects_board_dict():
    """The real-board adapter, fed a synthetic odb_inspect dict (no subprocess),
    projects parts + nodes correctly and drives validate end-to-end."""
    board = {
        "components": [
            {"refdes": "U86", "properties": {"NAME": "TPS62933", "VALUE": "DC-DC"}},
            {"refdes": "TP12", "properties": {}},      # placed, no props
            {"refdes": "R1", "properties": {"Value": "10k"}},
        ],
        "nets": [
            {"net": "3V3", "nodes": [{"refdes": "U86", "pin": "VOUT"},
                                     {"refdes": "TP12", "pin": "1"}]},
        ],
        "error": "",
    }
    bd = OdbBoardData(board)
    assert "TPS62933" in (bd.component_part("U86") or "")
    assert bd.component_part("TP12") == ""        # exists, but no usable property
    assert bd.component_part("U999") is None       # absent component
    assert bd.node_exists("TP12") is True
    assert bd.node_exists("U86") is True
    assert bd.node_exists("NOPE") is False

    by = _by_name(validate_params(
        {"ic_refdes": "U86", "ic_part": "TPS62933", "rail_test_point": "TP12"}, bd))
    assert all(c.passed for c in by.values())


def test_odb_adapter_surfaces_loader_error():
    bd = OdbBoardData({"components": [], "nets": [], "error": "No project open."})
    assert bd.error == "No project open."
    assert bd.component_part("U1") is None
    assert bd.node_exists("U1") is False


def test_named_pad_and_pin_are_valid_references_but_bare_net_is_not():
    """A board may designate a test point by the RAIL name — KC30 has a placed pad
    whose refdes IS ``+AUX0_16V`` (not ``TP*``). Any placed component reference,
    or a component PIN of one, is a valid probe point; a BARE net name (no placed
    reference) is NOT."""
    board = {
        "components": [
            {"refdes": "U11.1", "properties": {"NAME": "LMZM33604RLXR"}},
            {"refdes": "+AUX0_16V", "properties": {}},      # TP pad named by the rail
        ],
        "nets": [
            {"net": "+AUX0_16V", "nodes": [{"refdes": "U11.1", "pin": "24"}]},
            {"net": "+RAW_ONLY", "nodes": [{"refdes": "U11.1", "pin": "25"}]},
        ],
        "error": "",
    }
    bd = OdbBoardData(board)
    assert bd.node_exists("+AUX0_16V") is True     # the placed pad (a component)
    assert bd.node_exists("U11.1.24") is True       # a component PIN ref ("go all the way")
    assert bd.node_exists("+RAW_ONLY") is False     # a net with no placed pad is NOT a probe point
    by = _by_name(validate_params(
        {"ic_refdes": "U11.1", "ic_part": "LMZM33604RLXR",
         "rail_test_point": "+AUX0_16V"}, bd))
    assert by[CHECK_RAIL_TP].passed is True
