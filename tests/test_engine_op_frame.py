"""The live daemon answers an engine ``op`` frame (engine_process plan, slice 1).

One op table (``engine_client/_engine_ops.py``) serves every engine process, so the daemon's
answer is the op table's answer over the JSON boundary. The op judges
the document IN the frame: it never reads the path, never changes the daemon's
latched instrument document, and a bundle whose packs will not load is never silent:
an op that needs no pack answers, a per-key op says it in each key's ``errors``, any
other op fails with the reason.
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

from procedo.engine_client import _engine_ops as engine_ops  # noqa: E402
from procedo.studio.pack_ops import pack_parsers as pp  # noqa: E402
from procedo.engine_client import engine_session  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _env.rules_packager_available(), reason=_env.WHEEL_SKIP_REASON
)

_DAEMON = Path(__file__).resolve().parent.parent / "procedo" / "engine_client" / "_execute_op_subprocess.py"

_spec = importlib.util.spec_from_file_location("_exec_op_runner_opframe", _DAEMON)
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)

_HEAD = ("# T\nDesc.\n\n## Meta\nformat_version: 2.0.1\nboard: B\n"
         "rules_pack: rules_packager_base@2.12.0\n\n## Equipment\nDMM_GENERIC : dmm\n\n## Steps\n")

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
    sess = engine_session.RemoteSession(Path("/nonexistent"))

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


def test_the_result_op_makes_each_bench_op_never_redone_an_outcome_detail():
    doc = _document("if_else")
    wait = [s for s in engine_ops.run({"op": "build_manual_run", "procedure_json": doc})["steps"]
            if s["op"] == "wait"][0]
    fields = {"procedure_json": doc, **_inputs(doc),
              "failures": {wait["node_path"]: {"error": "PSU1 at COM3: OSError: no reply", "then": "continued"}}}
    result = _in_process({"op": "build_manual_result", **fields})["result"]
    assert result["outcome_details"] == [{"code": "BENCH_OP_FAILED", "description": "PSU1 at COM3: OSError: no reply",
                                          "step": 4, "then": "continued"}]
    assert result["overall"] == "ERROR"


def test_the_run_op_says_a_refused_value_ended_the_run():
    doc, _ = pp.parse_text(_HEAD + "@LET T = MAX\n1. Wait ${T} s.\n")
    assert engine_ops.run({"op": "build_manual_run", "procedure_json": doc})["error"] == \
        "ValueError: not a number: 'MAX'"
    assert engine_ops.run({"op": "build_manual_run", "procedure_json": _document("linear")})["error"] == ""


def test_the_result_op_makes_an_operator_stop_aborted():
    doc = _document("if_else")
    result = _in_process({"op": "build_manual_result", "procedure_json": doc, **_inputs(doc),
                          "operator_stop": True})["result"]
    assert (result["overall"], result["aborted"]) == ("ABORTED", True)


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


def test_a_bundle_that_will_not_load_fails_the_op_with_its_reason_and_refuses_instruments(monkeypatch):
    """Never a plan without the bundle's packs: its pack steps would show blank (no emitter)."""
    tried = []

    def _broken(session, req):
        tried.append(req["cmd"])
        raise RuntimeError("bundle broken")

    monkeypatch.setattr(er, "_load_bundle", _broken)
    session = er._Session()
    op, _ = er._dispatch(session, {"cmd": "op", "op": "build_manual_run",
                                   "procedure_json": _document("linear")})
    assert op == {"ok": False, "kind": "ParserUnavailable",
                  "error": "The bundle's packs could not be loaded: RuntimeError: bundle broken"}
    assert tried == ["op"]   # the op asked for the daemon's once-per-process pack load
    exec_ops, _ = er._dispatch(session, {"cmd": "exec_ops", "ops": []})
    assert exec_ops["ok"] is False and "bundle broken" in exec_ops["error"]


def _no_packs(spec):
    raise RuntimeError("bundle broken")


_NO_PACKS = "The bundle's packs could not be loaded: RuntimeError: bundle broken"


def test_a_bundle_that_will_not_load_still_names_a_broken_pack_and_says_why_per_key(tmp_path):
    pytest.importorskip("rules_packager_base.pack_contract")
    contract = engine_ops.run({"op": "pack_contract", "modules": ["no_such_pack_parser"]}, _no_packs)
    assert contract["ok"] and contract["packs"]["no_such_pack_parser"]["error"].startswith("ModuleNotFoundError")
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "defaults.json").write_text(json.dumps({"extractors": {
        "meta": {"module": "rules_packager_base.rules.v2_0_2.extractors", "function": "extract_meta"}}}),
        encoding="utf-8")
    out = engine_ops.run({"op": "extract", "project": str(tmp_path), "keys": ["meta", "equipment"],
                          "procedures": [{"id": "T1", "title": "Title"}]}, _no_packs)
    (result,) = out["results"]
    assert out["ok"] and result["data"]["meta"]["display_name"] == "Title (T1)"
    assert result["errors"] == {"meta": _NO_PACKS, "equipment": "bundle declares no 'equipment' extractor"}


def test_a_venv_without_the_wheel_keeps_the_ops_own_message(monkeypatch):
    def _no_wheel():
        raise ImportError("no wheel")

    monkeypatch.setattr(engine_ops, "_import_wheel", _no_wheel)
    assert engine_ops.run({"op": "parse_text", "text": "x"}, _no_packs) == {
        "ok": False, "kind": "ParserUnavailable", "error": "no wheel"}


def test_a_bundle_without_packs_or_a_frame_without_a_bundle_answers(monkeypatch, tmp_path):
    from rules_packager_base.rules.v2_0_2.parser import _default_registry

    # Loading a bundle without packs clears the process-wide registry: this worker's later tests need it.
    monkeypatch.setattr(_default_registry, "clear_registry", lambda: None)
    (tmp_path / "defaults.json").write_text("{}", encoding="utf-8")  # declares no pack_dispatch
    frame = {"cmd": "op", "op": "build_manual_run", "procedure_json": _document("linear")}
    for req in (frame, {**frame, "_bundle_dir": str(tmp_path)}):
        op, _ = er._dispatch(er._Session(), req)
        assert op["ok"] and op["steps"]


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
    sess = engine_session.RemoteSession(tmp_path)
    seen = {}
    monkeypatch.setattr(sess, "_request", lambda frame, timeout, on_progress=None:
                        seen.update(frame=frame, timeout=timeout) or {"ok": True})
    doc = {"steps": []}
    sess.engine_op("build_manual_result", procedure_json=doc, measurements={"a": 1})
    assert seen["frame"] == {"cmd": "op", "op": "build_manual_result",
                             "procedure_json": doc, "measurements": {"a": 1}}
    assert seen["timeout"] == engine_session._timeout()


def test_the_write_not_started_op_leaves_the_tests_error_result(tmp_path):
    # Owner, 2026-10-23: a run the bench refused (or whose instrument would not open) left no
    # results.json, so it showed nowhere and the test kept its previous verdict.
    module = tmp_path / "test.py"
    module.write_text('TEST_NAME = "T"\nRULES = {}\n', encoding="utf-8")
    answer = engine_ops.run({"op": "write_not_started", "module_path": str(module), "results_dir": str(tmp_path),
                             "code": "RUN_ACTIVE", "description": "another test is already running on this bench"})
    assert answer == {"ok": True}
    written = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert (written["test_name"], written["overall"]) == ("T", "ERROR")
    assert written["outcome_details"] == [{"code": "RUN_ACTIVE", "step": None, "then": "stopped",
                                           "description": "another test is already running on this bench"}]
