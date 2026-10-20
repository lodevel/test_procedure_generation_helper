"""The live daemon answers an engine ``op`` frame (engine_process plan, slice 1).

One op table (``llm/_engine_ops.py``) serves every engine process, so the daemon's
answer is the op table's answer over the JSON boundary. The op judges
the document IN the frame: it never reads the path, never changes the daemon's
latched instrument document, and a bundle that will not load degrades it instead
of failing it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _env
from tests._qt_stub import ensure_workflow_editor_importable

ensure_workflow_editor_importable()

from procedo.studio.llm import _engine_ops as engine_ops  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON
)

_STUDIO = Path(__file__).resolve().parent.parent / "procedo" / "studio"
_DAEMON = _STUDIO / "llm" / "_execute_op_subprocess.py"

_spec = importlib.util.spec_from_file_location("_exec_op_runner_opframe", _DAEMON)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

_HEAD = ("# T\nDesc.\n\n## Meta\nformat_version: 2.0.1\nboard: B\n"
         "rules_pack: rules_packager_base@2.3.3\n\n## Equipment\nDMM_GENERIC : dmm\n\n## Steps\n")

# Procedures copied from the engine's own tests (rules/v2_0_2/tests): a linear
# read pair with an operator judgement, an @IF/@ELSE branch on a reading
# (test_manual_run._IF_PROC) and Binds computed inside a @FOR
# (test_bind_and_condition_engine).
PROCEDURES = {
    "linear": ("1. Operator: read voltage on TP X with DMM_GENERIC as {1}.\n"
               "2. Operator: read voltage on TP Y with DMM_GENERIC as {2}.\n"
               '3. Operator: judge the LED (target = "lit") as {3}.\n',
               '{1} < 5 V\n{2} > 3 V\n{3} operator decides "lit"\n'),
    "if_else": ("1. Operator: read voltage on TP X with DMM_GENERIC as {1}.\n"
                "@IF {1} > 1.0 V\n"
                "2. Operator: read voltage on TP Y with DMM_GENERIC as {2}.\n"
                "@ELSE\n"
                "3. Operator: read voltage on TP Z with DMM_GENERIC as {3}.\n"
                "@ENDIF\n"
                "4. Wait 1 s.\n",
                "{1} < 5 V\n{2} < 3 V\n{3} < 3 V\n"),
    "bind_in_for": ("@LET BASE = 10\n@FOR j IN 0..1\n"
                    "1. Operator: read DC voltage on P1 pin 1 as {BASE + j*3}.\n"
                    "2. Operator: read DC voltage on P1 pin 2 as {BASE + j*3 + 1}.\n"
                    "3. {BASE + j*3 + 2} = {BASE + j*3} - {BASE + j*3 + 1}.\n@ENDFOR\n",
                    "{12} < 1\n{15} < 1\n"),
}


def _document(name: str) -> dict:
    steps, expected = PROCEDURES[name]
    doc, _ = pp.parse_text(_HEAD + steps + "\n## Expected\n" + expected)
    return doc


def _inputs(doc: dict) -> dict:
    """Readings for every binding row of the static plan (2.0 each: the @IF takes
    its branch), plus the operator's verdict on {3}, keyed by an int ref as the
    dialog keeps it (it crosses JSON as a string)."""
    plan = engine_ops.run({"op": "build_manual_run", "procedure_json": doc})
    rows = {s["node_path"]: 2.0 for s in plan["steps"] if s["is_binding"]}
    return {"measurements": rows, "controls": {}, "operator_verdicts": {3: "PASS"}}


def _in_process(spec: dict) -> dict:
    """The op table run here, over the same JSON boundary the daemon's pipe is."""
    return json.loads(json.dumps(engine_ops.run(json.loads(json.dumps(spec)))))


@pytest.fixture(scope="module")
def live_session():
    """A RemoteSession over the REAL daemon script, on this interpreter (no project venv)."""
    sess = pp.RemoteSession(Path("/nonexistent"))

    def _start():
        sess._bundle_dir = None
        sess._proc = subprocess.Popen([sys.executable, str(_DAEMON), "--role", "run"],
                                      stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, bufsize=1)
        sess._dead = False

    sess._start = _start
    yield sess
    sess.shutdown()


@pytest.mark.parametrize("name", sorted(PROCEDURES))
def test_the_daemon_op_answers_what_the_op_table_answers(live_session, name):
    doc = _document(name)
    fields = {"procedure_json": doc, **_inputs(doc)}
    live = live_session.engine_op("build_manual_run", **fields)
    assert live["ok"] and live["steps"]
    assert live == _in_process({"op": "build_manual_run", **fields})
    assert any(s["verdict"] for s in live["steps"])   # the readings reached the verdicts
    if name == "linear":         # and the operator's verdict did (the observation 2.0 alone is no "lit")
        assert [s["verdict"] for s in live["steps"] if s["kind"] == "judge"] == ["PASS"]


def test_the_daemon_result_op_answers_what_the_op_table_answers(live_session):
    doc = _document("bind_in_for")
    fields = {"procedure_json": doc, **_inputs(doc)}
    live = live_session.engine_op("build_manual_result", **fields)
    assert live["ok"] and live["result"]["verdicts"]
    assert live == _in_process({"op": "build_manual_result", **fields})


def test_the_op_judges_its_frame_document_and_never_changes_the_sessions():
    session = er._Session()
    held = session.procedure_json = {"held": True}
    sent = _document("linear")
    req = {"cmd": "op", "op": "build_manual_run", "procedure_json": sent,
           "measurements": {}, "controls": {}}
    response, stop = er._dispatch(session, req)
    assert not stop
    assert response == engine_ops.run({"op": "build_manual_run", "procedure_json": sent})
    assert session.procedure_json is held


def test_a_bundle_that_will_not_load_degrades_the_op_but_refuses_instruments(monkeypatch):
    tried = []

    def _broken(session, req):
        tried.append(req["cmd"])
        raise RuntimeError("bundle broken")

    monkeypatch.setattr(er, "_load_bundle", _broken)
    session = er._Session()
    op, _ = er._dispatch(session, {"cmd": "op", "op": "build_manual_run",
                                   "procedure_json": _document("linear")})
    assert op["ok"] and op["steps"]
    assert tried == ["op"]   # the op asked for the daemon's once-per-process pack load
    exec_ops, _ = er._dispatch(session, {"cmd": "exec_ops", "ops": []})
    assert exec_ops["ok"] is False and "bundle broken" in exec_ops["error"]


def test_error_kinds_keep_the_exception_table(monkeypatch):
    unknown = engine_ops.run({"op": "nope"})
    assert (unknown["ok"], unknown["kind"]) == (False, "Other") and "Unknown op" in unknown["error"]

    def _no_wheel(spec):
        raise ImportError("no wheel")

    def _boom(spec):
        raise ValueError("boom")

    monkeypatch.setitem(engine_ops._OPS, "no_wheel", _no_wheel)
    monkeypatch.setitem(engine_ops._OPS, "boom", _boom)
    assert engine_ops.run({"op": "no_wheel"}) == {"ok": False, "kind": "ParserUnavailable",
                                                  "error": "no wheel"}
    boom = engine_ops.run({"op": "boom"})
    assert (boom["kind"], boom["error"]) == ("Other", "ValueError: boom") and "Traceback" in boom["traceback"]


def test_engine_op_sends_one_op_frame_with_the_document(monkeypatch, tmp_path):
    sess = pp.RemoteSession(tmp_path)
    seen = {}
    monkeypatch.setattr(sess, "_request", lambda frame, timeout, on_progress=None:
                        seen.update(frame=frame, timeout=timeout) or {"ok": True})
    doc = {"steps": []}
    sess.engine_op("build_manual_result", procedure_json=doc, measurements={"a": 1})
    assert seen["frame"] == {"cmd": "op", "op": "build_manual_result",
                             "procedure_json": doc, "measurements": {"a": 1}}
    assert seen["timeout"] == pp._timeout()
