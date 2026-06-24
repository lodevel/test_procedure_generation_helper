"""Tests for the skill-chat session (the pure bridge brain)."""
from pathlib import Path
from types import SimpleNamespace

from workflow_editor.authoring import Skill, SkillChatSession, SkillSource


def _skill(prompt="You author a draft."):
    return Skill(
        skill_id="dcdc",
        title="DCDC",
        system_prompt=prompt,
        path=Path("/x"),
        source=SkillSource.LOCAL,
    )


def test_first_turn_leads_with_system_prompt_and_context():
    s = SkillChatSession(_skill("SYSTEM"), context_text="CONTEXT")
    wire = s.start_user_turn("hello")
    assert wire == "SYSTEM\n\nCONTEXT\n\nUser: hello"
    assert s.started is True


def test_first_turn_without_context_omits_it():
    s = SkillChatSession(_skill("SYSTEM"))
    assert s.start_user_turn("hello") == "SYSTEM\n\nUser: hello"


def test_later_turn_carries_full_transcript():
    s = SkillChatSession(_skill("SYSTEM"), context_text="CONTEXT")
    s.start_user_turn("first")
    s.record_assistant("a reply")
    wire = s.start_user_turn("second")
    # full transcript every turn — correct on a stateless backend.
    assert wire == (
        "SYSTEM\n\nCONTEXT\n\nUser: first\n\nAssistant: a reply\n\nUser: second"
    )


def test_empty_system_and_context_collapse_to_message():
    s = SkillChatSession(_skill(""), context_text="   ")
    assert s.start_user_turn("just this") == "User: just this"


def test_turns_recorded_in_order():
    s = SkillChatSession(_skill())
    s.start_user_turn("q1")
    s.record_assistant("a1")
    s.start_user_turn("q2")
    assert [(t.role, t.content) for t in s.turns] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
    ]


def test_set_context_before_first_turn_takes_effect():
    s = SkillChatSession(_skill("SYS"))
    s.set_context("LATE CONTEXT")
    assert s.start_user_turn("hi") == "SYS\n\nLATE CONTEXT\n\nUser: hi"


def test_started_false_before_any_turn():
    assert SkillChatSession(_skill()).started is False


def test_interpret_prefers_assistant_message():
    r = SimpleNamespace(assistant_message="  the draft  ", raw_response="raw stuff")
    assert SkillChatSession.interpret(r) == "the draft"


def test_interpret_falls_back_to_raw():
    r = SimpleNamespace(assistant_message="", raw_response="  raw text  ")
    assert SkillChatSession.interpret(r) == "raw text"


def test_interpret_empty_when_nothing():
    r = SimpleNamespace(assistant_message=None, raw_response=None)
    assert SkillChatSession.interpret(r) == ""
