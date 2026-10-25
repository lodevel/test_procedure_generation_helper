"""What Studio's three LLM views share, pinned once for all of them: how an assistant reply maps
onto the output contract's fields; what each deterministic text transform (Renumber steps, Sync
Equipment, Sync Meta) does with its answer and says; how the ⚡ pack buttons are gated; how a
regenerated test.py keeps the operator's pinned bench constants (Quick Code, the assistant's test.py
proposal, Parse + Generate)."""

import json
from types import SimpleNamespace

import pytest

import procedo.studio.tabs.text_actions as text_actions
from procedo.engine_client import engine
from procedo.studio.core import ArtifactType
from procedo.studio.pack_ops import bundle_capabilities, pack_parsers

from .test_engine_async import _Tools, _pump

from .test_proposal_cards import opened as opened  # noqa: F401
from .test_main_window_characterization import (  # noqa: F401
    make_window as make_window,
    mbox as mbox,
    qapp as qapp,
)


# --- an assistant reply → the output contract's dict ----------------------------------------------


def _reply(**artifacts):
    """An LLMResponse with each named artifact proposed (mode "replace") and the rest absent."""
    proposed = {
        name: SimpleNamespace(mode="replace", content=content)
        for name, content in artifacts.items()
    }
    return SimpleNamespace(
        assistant_message="Done.",
        session_delta={"open_questions": ["Which PSU?"]},
        procedure_text=proposed.get("procedure_text"),
        procedure_json=proposed.get("procedure_json", SimpleNamespace(mode="", content="ignored")),
        test_code=proposed.get("test_code"),
    )


def test_each_view_maps_only_its_own_artifacts_onto_the_contract(opened):
    w = opened
    reply = _reply(procedure_text="T", procedure_json="J", test_code="C")
    head = {"assistant_message": "Done.", "open_questions": ["Which PSU?"], "propose_update": True}
    assert w.text_only_tab._parse_response_to_dict(reply) == {**head, "text_procedure": "T"}
    assert w.text_json_tab._parse_response_to_dict(reply) == {
        **head,
        "text_procedure": "T",
        "procedure_json": "J",
    }
    assert w.json_code_tab._parse_response_to_dict(reply) == {
        **head,
        "procedure_json": "J",
        "test_code": "C",
    }
    assert w.text_only_tab._get_expected_artifact_fields() == ["text_procedure"]
    assert w.text_json_tab._get_expected_artifact_fields() == ["text_procedure", "procedure_json"]
    assert w.json_code_tab._get_expected_artifact_fields() == ["procedure_json", "test_code"]


def test_a_reply_proposing_nothing_proposes_no_update(opened):
    w = opened
    reply = _reply(test_code="C")
    reply.session_delta = {}
    for tab in (w.text_only_tab, w.text_json_tab):
        assert tab._parse_response_to_dict(reply) == {
            "assistant_message": "Done.",
            "open_questions": [],
            "propose_update": False,
        }
    assert w.json_code_tab._parse_response_to_dict(_reply(procedure_text="T")) == {
        "assistant_message": "Done.",
        "open_questions": ["Which PSU?"],
        "propose_update": False,
    }


# --- the text transforms: applied, already so, or refused -----------------------------------------

_WARNED = "\n\n\u26a0 Warnings:\n\u2022 w1"
_BLOCKED = "[SYNCEQP_BLOCKED] line 3"


@pytest.mark.parametrize(
    "handler, changes, warnings, status, chat",
    [
        (
            "_on_renumber_steps",
            True,
            ["w1"],
            "Steps renumbered",
            "\U0001f522 Renumber Steps applied \u2014 every step in ## Steps is now sequential 1..N.",
        ),
        (
            "_on_renumber_steps",
            False,
            [],
            "Steps already sequential",
            "\U0001f522 Renumber Steps: already sequential \u2014 no changes.",
        ),
        (
            "_on_sync_equipment",
            True,
            ["w1"],
            "Equipment synced from steps",
            "\U0001f527 Sync Equipment applied \u2014 ## Equipment refreshed from steps. Declared types "
            "were kept (type conflicts are warned) and ratings merged as max(declared, observed)."
            + _WARNED,
        ),
        (
            "_on_sync_equipment",
            False,
            [],
            "Sync Equipment: no changes (everything already declared)",
            "\U0001f527 Sync Equipment: no changes (## Equipment already matches every device referenced "
            "in steps).",
        ),
        (
            "_on_sync_meta",
            True,
            ["w1"],
            "Meta synced from bundle",
            "\U0001f3f7\ufe0f Sync Meta applied \u2014 ## Meta regenerated from the active bundle (pack "
            "versions re-pinned; board / format_version preserved)." + _WARNED,
        ),
        (
            "_on_sync_meta",
            False,
            [],
            "Sync Meta: no changes (## Meta already matches the bundle)",
            "\U0001f3f7\ufe0f Sync Meta: no changes (## Meta already matches the active bundle).",
        ),
        # Only Sync Equipment refuses on a blocked line; Sync Meta applies whatever it is told.
        (
            "_on_sync_meta",
            True,
            [_BLOCKED],
            "Meta synced from bundle",
            "\U0001f3f7\ufe0f Sync Meta applied \u2014 ## Meta regenerated from the active bundle (pack "
            "versions re-pinned; board / format_version preserved).\n\n\u26a0 Warnings:\n\u2022 "
            + _BLOCKED,
        ),
    ],
)
def test_a_text_transform_applies_its_answer_or_says_why_not(
    opened, monkeypatch, handler, changes, warnings, status, chat
):
    w = opened
    view = w.text_json_tab
    w.view_stack.setCurrentWidget(view)
    before = view.text_card.editor.toPlainText()
    after = before + "changed\n" if changes else before
    monkeypatch.setattr(
        engine,
        "tools_op",
        _Tools(reply=lambda spec: {"ok": True, "text": after, "warnings": warnings}),
    )
    said, chats = [], []
    view.status_message.connect(said.append)
    monkeypatch.setattr(text_actions, "post_to_chat", lambda mw, content: chats.append(content))

    getattr(view, handler)()
    _pump(lambda: said)

    assert (said, chats) == ([status], [chat])
    assert view.text_card.editor.toPlainText() == after


@pytest.mark.parametrize("refusal", [_BLOCKED, "line 0 [EQP_NO_EMITTER] Sync aborted"])
def test_sync_equipment_refuses_a_blocked_answer_and_leaves_the_text(opened, monkeypatch, refusal):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    before = view.text_card.editor.toPlainText()
    monkeypatch.setattr(
        engine,
        "tools_op",
        _Tools(reply=lambda spec: {"ok": True, "text": "# gone\n", "warnings": [refusal]}),
    )
    said, chats = [], []
    view.status_message.connect(said.append)
    monkeypatch.setattr(text_actions, "post_to_chat", lambda mw, content: chats.append(content))

    view._on_sync_equipment()
    _pump(lambda: said)

    assert said == ["Sync Equipment: REFUSED \u2014 unparseable ## Equipment lines (see chat)"]
    assert chats == [
        "\U0001f527 Sync Equipment REFUSED \u2014 the existing ## Equipment block has lines that "
        "don't parse (wrong syntax, or their pack isn't in the active bundle); syncing would "
        "delete them. Nothing was changed.\n\n\u26a0 Warnings:\n\u2022 " + refusal
    ]
    assert view.text_card.editor.toPlainText() == before


@pytest.mark.parametrize("handler", ["_on_renumber_steps", "_on_sync_equipment", "_on_sync_meta"])
def test_a_text_transform_on_an_empty_text_asks_nothing(opened, monkeypatch, handler):
    view = opened.text_json_tab
    view.text_card.editor.setPlainText("")
    tools = _Tools()
    monkeypatch.setattr(engine, "tools_op", tools)
    getattr(view, handler)()
    assert tools.ops == []


# --- the ⚡ pack buttons: shown when the wheel imports, enabled when every type has a pack ----------

_TEXT = "# t1\n\n## Equipment\nPSU1 : psu\nDMM1 : dmm\n"
_JSON = '{"id": "t1", "equipment": [{"id": "PSU1", "type": "psu"}, {"id": "X"}], "steps": []}'


@pytest.fixture
def gated(opened, monkeypatch):
    """The window with the wheel importable; ``asked`` records each (capability, types) gated,
    and ``refuse`` names the capabilities no pack provides (for psu, by labscpi)."""
    asked, refuse = [], set()

    def can_for_types(cap, types, root=None):
        asked.append((cap, list(types)))
        if cap == "boom":
            raise RuntimeError("gating broke")
        return (False, [("psu", "labscpi")]) if cap in refuse else (True, [])

    monkeypatch.setattr(pack_parsers, "is_available", lambda root=None: (True, ""))
    monkeypatch.setattr(bundle_capabilities, "can_for_types", can_for_types)
    return opened, asked, refuse


def _state(button):
    return not button.isHidden(), button.isEnabled(), button.toolTip()


def test_the_text_views_gate_each_button_on_the_texts_equipment(gated):
    w, asked, refuse = gated
    tj, to = w.text_json_tab, w.text_only_tab
    refuse.add("emit")
    tj.text_editor.setPlainText(_TEXT)
    asked.clear()
    tj.refresh_parser_button()
    to.refresh_parser_button()

    assert _state(tj.quick_parse_btn) == (True, True, "Deterministic Text \u2192 JSON")
    assert _state(tj.quick_render_btn) == (
        True,
        False,
        "No deterministic support for psu (labscpi). Use the LLM workflow.",
    )
    assert _state(tj.parse_and_generate_btn) == (True, True, "Parse + generate code")
    assert _state(to.parse_and_generate_btn) == (True, True, "Parse + generate code")
    assert not tj.sync_meta_btn.isHidden() and not to.sync_meta_btn.isHidden()
    assert ("emit", ["psu", "dmm"]) in asked and ("codegen", ["psu", "dmm"]) in asked


def test_the_json_code_view_gates_quick_code_on_the_jsons_equipment(gated):
    w, asked, refuse = gated
    jc = w.json_code_tab
    jc.json_editor.setPlainText(_JSON)
    asked.clear()
    jc.refresh_parser_button()
    assert _state(jc.quick_code_btn) == (True, True, "Deterministic JSON \u2192 Code")
    assert asked == [("codegen", ["psu"])]

    refuse.add("codegen")
    jc.json_editor.setPlainText(_JSON + " ")  # an edit re-gates
    assert _state(jc.quick_code_btn) == (
        True,
        False,
        "No code generator for psu (labscpi). Use the LLM workflow.",
    )


def test_a_gating_failure_leaves_the_buttons_enabled(gated, monkeypatch):
    w, _asked, refuse = gated
    jc, tj = w.json_code_tab, w.text_json_tab
    refuse.update({"parse", "codegen"})
    jc.json_editor.setPlainText("{not json")
    jc.refresh_parser_button()
    assert jc.quick_code_btn.isEnabled()

    monkeypatch.setattr(
        type(tj), "_pack_gates", lambda self: [(tj.quick_parse_btn, "tip", ("boom",))]
    )
    tj.quick_parse_btn.setEnabled(False)
    tj.refresh_parser_button()
    assert tj.quick_parse_btn.isEnabled()


def test_hidden_buttons_are_not_gated_on_an_edit(opened, monkeypatch):
    w = opened
    monkeypatch.setattr(pack_parsers, "is_available", lambda root=None: (False, "no wheel"))
    for tab in (w.text_json_tab, w.text_only_tab):
        tab.refresh_parser_button()
    w.json_code_tab.refresh_parser_button()
    asked = []
    monkeypatch.setattr(
        bundle_capabilities, "can_for_types", lambda *a, **k: asked.append(a) or (True, [])
    )
    w.text_json_tab.text_editor.setPlainText(_TEXT)
    w.text_only_tab.text_editor.setPlainText(_TEXT)
    w.json_code_tab.json_editor.setPlainText(_JSON)
    assert asked == []
    assert w.json_code_tab.quick_code_btn.isHidden() and w.text_only_tab.sync_meta_btn.isHidden()


# --- a regenerated test.py keeps the operator's pinned bench constants ------------------------------

_PINNED = 'PSU1_VISA = "ASRL3::INSTR"\n\n\ndef main():\n    return 0\n'
_GENERATED = 'PSU1_VISA = "ASRL1::INSTR"\n\n\ndef main():\n    return 1\n'
_PROCEDURE = {"id": "t1", "equipment": [{"id": "PSU1", "type": "psu"}], "steps": []}


def _codegen(procedure=_PROCEDURE):
    def reply(spec):
        if spec["op"] == "parse_text":
            return {"ok": True, "json": procedure, "warnings": []}
        return {"ok": True, "code": _GENERATED, "warnings": []}

    return _Tools(reply=reply)


def _proposed(w, monkeypatch, start):
    """The test.py each way proposes: Quick Code / the assistant's proposal (the code card's
    proposal), Parse + Generate (its card's test.py)."""
    got = []
    monkeypatch.setattr(w.json_code_tab, "propose_edit", lambda card, text, **kw: got.append(text))
    monkeypatch.setattr(
        w.dock.chat_panel,
        "offer_proposal",
        lambda ctx, proposal: got.extend(
            change.proposed for change in proposal.changes if change.file_name == "test.py"
        ),
    )
    start()
    _pump(lambda: got)
    return got


def _quick_code(w, procedure=_PROCEDURE):
    jc = w.json_code_tab
    w.view_stack.setCurrentWidget(jc)
    jc.json_editor.setPlainText(json.dumps(procedure))
    return jc._on_quick_code


def _assistant(w, procedure=_PROCEDURE):
    jc = w.json_code_tab
    w.view_stack.setCurrentWidget(jc)
    w.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, json.dumps(procedure))
    return lambda: jc._handle_code_proposal(SimpleNamespace(mode="replace", content=_GENERATED))


def _parse_generate(w, procedure=None):
    to = w.text_only_tab
    w.view_stack.setCurrentWidget(to)
    return to._on_parse_and_generate


@pytest.mark.parametrize("way", [_quick_code, _assistant, _parse_generate])
def test_a_regenerated_test_py_keeps_the_pinned_bench_constants(opened, monkeypatch, way):
    w = opened
    monkeypatch.setattr(engine, "tools_op", _codegen())
    start = way(w)
    w.json_code_tab.code_editor.setPlainText(_PINNED)
    w.artifact_manager.set_content(ArtifactType.TEST_CODE, _PINNED)

    (proposed,) = _proposed(w, monkeypatch, start)

    assert "ASRL3::INSTR" in proposed and "ASRL1" not in proposed and "return 1" in proposed


@pytest.mark.parametrize("equipment", [[{"type": "psu"}], [{"id": 5, "type": "psu"}]])
@pytest.mark.parametrize("way", [_quick_code, _assistant])
def test_without_equipment_ids_the_generated_test_py_is_proposed_as_is(
    opened, monkeypatch, way, equipment
):
    w = opened
    monkeypatch.setattr(engine, "tools_op", _codegen())
    start = way(w, {"id": "t1", "equipment": equipment, "steps": []})
    w.json_code_tab.code_editor.setPlainText(_PINNED)

    assert _proposed(w, monkeypatch, start) == [_GENERATED]


def test_the_assistants_test_py_is_proposed_as_is_when_the_json_does_not_parse(opened, monkeypatch):
    w = opened
    jc = w.json_code_tab
    w.view_stack.setCurrentWidget(jc)
    w.artifact_manager.set_content(ArtifactType.PROCEDURE_JSON, "{not json")
    jc.code_editor.setPlainText(_PINNED)

    assert _proposed(
        w,
        monkeypatch,
        lambda: jc._handle_code_proposal(SimpleNamespace(mode="replace", content=_GENERATED)),
    ) == [_GENERATED]
