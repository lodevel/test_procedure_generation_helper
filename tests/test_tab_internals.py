"""What Studio's three LLM views share, pinned once for all of them: how an assistant reply maps
onto the output contract's fields; what each deterministic text transform (Renumber steps, Sync
Equipment, Sync Meta) does with its answer and says."""

from types import SimpleNamespace

import pytest

import procedo.studio.tabs.text_actions as text_actions
from procedo.engine_client import engine

from .test_engine_async import _Tools, _pump

from .test_proposal_cards import opened as opened  # noqa: F401
from .test_main_window_characterization import (  # noqa: F401
    make_window as make_window, mbox as mbox, qapp as qapp,
)


# --- an assistant reply → the output contract's dict ----------------------------------------------

def _reply(**artifacts):
    """An LLMResponse with each named artifact proposed (mode "replace") and the rest absent."""
    proposed = {name: SimpleNamespace(mode="replace", content=content)
                for name, content in artifacts.items()}
    return SimpleNamespace(
        assistant_message="Done.", session_delta={"open_questions": ["Which PSU?"]},
        procedure_text=proposed.get("procedure_text"),
        procedure_json=proposed.get("procedure_json", SimpleNamespace(mode="", content="ignored")),
        test_code=proposed.get("test_code"))


def test_each_view_maps_only_its_own_artifacts_onto_the_contract(opened):
    w = opened
    reply = _reply(procedure_text="T", procedure_json="J", test_code="C")
    head = {"assistant_message": "Done.", "open_questions": ["Which PSU?"], "propose_update": True}
    assert w.text_only_tab._parse_response_to_dict(reply) == {**head, "text_procedure": "T"}
    assert w.text_json_tab._parse_response_to_dict(reply) == {
        **head, "text_procedure": "T", "procedure_json": "J"}
    assert w.json_code_tab._parse_response_to_dict(reply) == {
        **head, "procedure_json": "J", "test_code": "C"}
    assert w.text_only_tab._get_expected_artifact_fields() == ["text_procedure"]
    assert w.text_json_tab._get_expected_artifact_fields() == ["text_procedure", "procedure_json"]
    assert w.json_code_tab._get_expected_artifact_fields() == ["procedure_json", "test_code"]


def test_a_reply_proposing_nothing_proposes_no_update(opened):
    w = opened
    reply = _reply(test_code="C")
    reply.session_delta = {}
    for tab in (w.text_only_tab, w.text_json_tab):
        assert tab._parse_response_to_dict(reply) == {
            "assistant_message": "Done.", "open_questions": [], "propose_update": False}
    assert w.json_code_tab._parse_response_to_dict(_reply(procedure_text="T")) == {
        "assistant_message": "Done.", "open_questions": ["Which PSU?"], "propose_update": False}


# --- the text transforms: applied, already so, or refused -----------------------------------------

_WARNED = "\n\n\u26a0 Warnings:\n\u2022 w1"
_BLOCKED = "[SYNCEQP_BLOCKED] line 3"


@pytest.mark.parametrize("handler, changes, warnings, status, chat", [
    ("_on_renumber_steps", True, ["w1"], "Steps renumbered",
     "\U0001f522 Renumber Steps applied \u2014 every step in ## Steps is now sequential 1..N."),
    ("_on_renumber_steps", False, [], "Steps already sequential",
     "\U0001f522 Renumber Steps: already sequential \u2014 no changes."),
    ("_on_sync_equipment", True, ["w1"], "Equipment synced from steps",
     "\U0001f527 Sync Equipment applied \u2014 ## Equipment refreshed from steps. Declared types "
     "were kept (type conflicts are warned) and ratings merged as max(declared, observed)." + _WARNED),
    ("_on_sync_equipment", False, [], "Sync Equipment: no changes (everything already declared)",
     "\U0001f527 Sync Equipment: no changes (## Equipment already matches every device referenced "
     "in steps)."),
    ("_on_sync_meta", True, ["w1"], "Meta synced from bundle",
     "\U0001f3f7\ufe0f Sync Meta applied \u2014 ## Meta regenerated from the active bundle (pack "
     "versions re-pinned; board / format_version preserved)." + _WARNED),
    ("_on_sync_meta", False, [], "Sync Meta: no changes (## Meta already matches the bundle)",
     "\U0001f3f7\ufe0f Sync Meta: no changes (## Meta already matches the active bundle)."),
    # Only Sync Equipment refuses on a blocked line; Sync Meta applies whatever it is told.
    ("_on_sync_meta", True, [_BLOCKED], "Meta synced from bundle",
     "\U0001f3f7\ufe0f Sync Meta applied \u2014 ## Meta regenerated from the active bundle (pack "
     "versions re-pinned; board / format_version preserved).\n\n\u26a0 Warnings:\n\u2022 " + _BLOCKED),
])
def test_a_text_transform_applies_its_answer_or_says_why_not(
        opened, monkeypatch, handler, changes, warnings, status, chat):
    w = opened
    view = w.text_json_tab
    w.view_stack.setCurrentWidget(view)
    before = view.text_card.editor.toPlainText()
    after = before + "changed\n" if changes else before
    monkeypatch.setattr(engine, "tools_op", _Tools(
        reply=lambda spec: {"ok": True, "text": after, "warnings": warnings}))
    said, chats = [], []
    view.status_message.connect(said.append)
    monkeypatch.setattr(text_actions, "post_to_chat", lambda mw, content: chats.append(content))

    getattr(view, handler)()
    _pump(lambda: said)

    assert (said, chats) == ([status], [chat])
    assert view.text_card.editor.toPlainText() == after


def test_sync_equipment_refuses_a_blocked_answer_and_leaves_the_text(opened, monkeypatch):
    w = opened
    view = w.text_only_tab
    w.view_stack.setCurrentWidget(view)
    before = view.text_card.editor.toPlainText()
    monkeypatch.setattr(engine, "tools_op", _Tools(
        reply=lambda spec: {"ok": True, "text": "# gone\n", "warnings": [_BLOCKED]}))
    said, chats = [], []
    view.status_message.connect(said.append)
    monkeypatch.setattr(text_actions, "post_to_chat", lambda mw, content: chats.append(content))

    view._on_sync_equipment()
    _pump(lambda: said)

    assert said == ["Sync Equipment: REFUSED \u2014 unparseable ## Equipment lines (see chat)"]
    assert chats == [
        "\U0001f527 Sync Equipment REFUSED \u2014 the existing ## Equipment block has lines that "
        "don't parse (wrong syntax, or their pack isn't in the active bundle); syncing would "
        "delete them. Nothing was changed.\n\n\u26a0 Warnings:\n\u2022 " + _BLOCKED]
    assert view.text_card.editor.toPlainText() == before


@pytest.mark.parametrize("handler", ["_on_renumber_steps", "_on_sync_equipment", "_on_sync_meta"])
def test_a_text_transform_on_an_empty_text_asks_nothing(opened, monkeypatch, handler):
    view = opened.text_json_tab
    view.text_card.editor.setPlainText("")
    tools = _Tools()
    monkeypatch.setattr(engine, "tools_op", tools)
    getattr(view, handler)()
    assert tools.ops == []
