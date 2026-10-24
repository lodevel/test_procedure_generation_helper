"""What Studio's three LLM views share, pinned once for all of them: how an assistant reply maps
onto the output contract's fields."""

from types import SimpleNamespace

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
