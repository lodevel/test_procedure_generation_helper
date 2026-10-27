"""What the guided run's Values panel shows, as the ``build_manual_run`` op gives
it: each step's value as shown (``value_text``), why a Bind has none
(``value_note``), its loop turns' ``${…}`` names, and the procedure's own names —
read from the wheel's walk, over the JSON boundary as the daemon answers."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.engine_client import _engine_ops as engine_ops  # noqa: E402
from procedo.engine_client import _engine_ops_manual as engine_ops_manual  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402
from procedo.engine_client import engine_replies  # noqa: E402

from tests import _env  # noqa: E402

_wheel = pytest.mark.skipif(not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON)
pytestmark = pytest.mark.usefixtures("engine_in_process")
# The engine's source beside this editor (the host tree's external/): its guard never skips.
_ENGINE_SRC = Path(__file__).resolve().parents[2] / "rules_packager" / "src"

_HEAD = (
    "# T\nDesc.\n\n## Meta\nformat_version: 2.0.1\nboard: B\n"
    "rules_pack: rules_packager_base@3.2.0\n\n## Equipment\nDMM_GENERIC : dmm\n\n## Steps\n"
)
_FOR = (
    "@LET BASE = 10\n@FOR j IN 0..1\n"
    "1. Operator: read DC voltage on P1 pin 1 as {BASE + j*3}.\n"
    "2. Operator: read DC voltage on P1 pin 2 as {BASE + j*3 + 1}.\n"
    "3. {BASE + j*3 + 2} = {BASE + j*3} - {BASE + j*3 + 1} V.\n@ENDFOR\n"
)
_TABLE = (
    '@TABLE PINS\n@ROW PINS loc="P1 pin 1" net=VCC\n@ROW PINS loc="P1 pin 2" net=GND\n@ENDTABLE\n'
    "@ALLOC RD START=20 COUNT=2\n@FOR i, row IN PINS\n"
    "1. Operator: read DC voltage on ${row.loc} as {RD + i}.\n@ENDFOR\n"
    "@DO k\n2. Operator: read DC voltage on P2 pin 1 as {30}.\n@WHILE {30} < 1 V\n@ENDDO\n"
)


def _plan(
    steps: str, expected: str, measurements: dict | None = None
) -> engine_replies.ManualRunResult:
    doc, _ = pp.parse_text(_HEAD + steps + "\n## Expected\n" + expected)
    out = engine_ops.run(
        json.loads(
            json.dumps(
                {
                    "op": "build_manual_run",
                    "procedure_json": doc,
                    "measurements": measurements or {},
                }
            )
        )
    )
    assert out["ok"], out
    return engine_replies.ManualRunResult.from_dict(json.loads(json.dumps(out)))


def _by_ref(plan) -> dict:
    return {s.ref: s for s in plan.steps if s.ref is not None}


@_wheel
def test_a_reading_shows_in_its_criterions_unit_and_a_bind_as_its_row_says():
    first = _plan(_FOR, "{10} < 6 V\n{11} < 6000 mV\n")
    paths = {s.ref: s.node_path for s in first.steps if s.ref is not None}
    steps = _by_ref(_plan(_FOR, "{10} < 6 V\n{11} < 6000 mV\n", {paths[10]: 5.0, paths[11]: 4.5}))
    assert (steps[10].value_text, steps[10].verdict) == ("5 V", "PASS")
    assert steps[11].value_text == "4500 mV"  # stored in SI, shown in the criterion's unit
    assert (steps[12].value_text, steps[12].value_note) == ("0.5 V", "")
    assert steps[12].text.endswith("→ 0.5 V")  # the very words of its row


@_wheel
def test_a_bind_waiting_says_what_for_and_a_reading_not_taken_has_no_value():
    steps = _by_ref(_plan(_FOR, "{10} < 6 V\n"))
    assert (steps[10].value_text, steps[10].value_note) == ("", "")
    assert (steps[12].value_text, steps[12].value_note) == ("", "waiting for {10}")


@_wheel
def test_each_row_carries_its_loop_turns_names():
    steps = _by_ref(_plan(_FOR, "{10} < 6 V\n"))
    assert steps[10].names == (
        {"name": "${j}", "var": "j", "value": "0", "origin": "@FOR turn 1 of 2"},
    )
    assert steps[13].names == (
        {"name": "${j}", "var": "j", "value": "1", "origin": "@FOR turn 2 of 2"},
    )
    table = _by_ref(_plan(_TABLE, "{20} < 6 V\n"))
    assert table[21].names == (
        {"name": "${i}", "var": "i", "value": "1", "origin": "@FOR turn 2 of 2"},
        {"name": "${row.loc}", "var": "row.loc", "value": "P1 pin 2", "origin": "@FOR turn 2 of 2"},
        {"name": "${row.net}", "var": "row.net", "value": "GND", "origin": "@FOR turn 2 of 2"},
    )
    assert table[30].names == ({"name": "${k}", "var": "k", "value": "0", "origin": "@DO turn 1"},)


@_wheel
def test_the_procedures_own_names_come_with_the_plan():
    assert _plan(_FOR, "{10} < 6 V\n").constants == (
        {"name": "BASE", "value": "10", "origin": "@LET"},
    )
    assert _plan(_TABLE, "{20} < 6 V\n").constants == (
        {"name": "RD", "value": "20", "origin": "@ALLOC"},
        {"name": "PINS", "value": "2 rows", "origin": "@TABLE"},
    )


@_wheel
def test_the_display_data_changes_nothing_of_the_wheels_plan():
    """The op's plan is the wheel's build_manual_run, field for field."""
    doc, _ = pp.parse_text(_HEAD + _FOR + "\n## Expected\n{10} < 6 V\n")
    first = engine_ops.run({"op": "build_manual_run", "procedure_json": doc})
    readings = {first["steps"][0]["node_path"]: 5.0}
    out = engine_ops.run(
        {"op": "build_manual_run", "procedure_json": doc, "measurements": readings}
    )
    wheel = engine_ops._import_wheel().build_manual_run(doc, readings, {}, {})
    shown = ("value_text", "value_note", "names")
    assert [{k: v for k, v in s.items() if k not in shown} for s in out["steps"]] == [
        engine_ops_manual._step_payload(s) for s in wheel.steps
    ]


# What _engine_ops_manual's ShownSession relies on in the wheel's StepperSession: the
# private walk hooks it overrides or calls (parameters, in order, with defaults).
_HOOKS = {
    "_expand_for": "(self, block, block_path, bindings, ctx, skip)",
    "_runtime_do": "(self, block, do_path, bindings, ctx, skip)",
    "_concrete": "(self, raw, node_path, bindings, status='pending', texts=None)",
    "_compute": "(self, row, raw, bindings, ctx)",
    "_build_rules": "(self, measurements, notes)",
}
_DRIFT = "wheel drift: move display data into the wheel (manual_run.py) instead of _engine_ops_manual's ShownSession"


def _manual_run():
    try:
        from rules_packager_base.rules.v2_0_2.parser import manual_run
    except ImportError:
        if not (_ENGINE_SRC / "rules_packager_base").is_dir():
            pytest.skip(_env.WHEEL_SKIP_REASON)
        sys.path.insert(0, str(_ENGINE_SRC))
        from rules_packager_base.rules.v2_0_2.parser import manual_run
    return manual_run


def test_the_wheel_still_walks_as_the_values_panel_reads_it():
    """Guard: the hooks' signatures, a Bind row's `` → `` wording and the loop
    turns' node_path tags (``#iter=`` / ``#run=``) are what ShownSession reads."""
    manual_run = _manual_run()
    for name, want in _HOOKS.items():
        params = inspect.signature(getattr(manual_run.StepperSession, name)).parameters.values()
        got = (
            "("
            + ", ".join(
                p.name + ("" if p.default is p.empty else f"={p.default!r}") for p in params
            )
            + ")"
        )
        assert got == want, f"{_DRIFT}: StepperSession.{name}{got}, ShownSession expects {want}"
    assert "measurements" in manual_run._RunCtx.__slots__, f"{_DRIFT}: _RunCtx.measurements is gone"
    doc, _ = pp.parse_text(
        _HEAD + _FOR + "@DO k\n4. Operator: read DC voltage on P2 pin 1 as {30}.\n"
        "@WHILE {30} < 1 V\n@ENDDO\n\n## Expected\n{10} < 6 V\n"
    )
    first = manual_run.build_manual_run(doc)
    readings = {s.node_path: 5.0 for s in first.steps if s.ref in (10, 11)}
    steps = {
        s.ref: s for s in manual_run.build_manual_run(doc, readings).steps if s.ref is not None
    }
    assert steps[12].text.endswith(" → 0 V") and " → waiting for {13}" in steps[15].text, (
        f"{_DRIFT}: a Bind's row no longer ends ' → <value or why>': {steps[12].text!r}, {steps[15].text!r}"
    )
    assert all(engine_ops_manual._TURN_RE.search(steps[r].node_path) for r in (10, 13, 30)), (
        f"{_DRIFT}: loop turns are no longer tagged #iter=/#run=: {[steps[r].node_path for r in (10, 13, 30)]}"
    )
