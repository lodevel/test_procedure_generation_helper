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


def test_first_send_is_context_plus_message_system_separate():
    s = SkillChatSession(_skill("SYSTEM"), context_text="CONTEXT")
    wire = s.start_user_turn("hello")
    # context leads the FIRST message; the user delta is raw (OpenCode role-tags
    # it); the skill prompt rides as the message system, NOT in the wire.
    assert wire == "CONTEXT\n\nhello"
    assert s.system_prompt == "SYSTEM"
    assert s.started is True


def test_first_turn_without_context_omits_it():
    s = SkillChatSession(_skill("SYSTEM"))
    assert s.start_user_turn("hello") == "hello"
    assert s.system_prompt == "SYSTEM"


def test_later_turn_sends_only_the_delta():
    s = SkillChatSession(_skill("SYSTEM"), context_text="CONTEXT")
    s.start_user_turn("first")
    s.record_assistant("a reply")
    # native session: OpenCode holds the history, so only the new message is sent.
    assert s.start_user_turn("second") == "second"


def test_changed_context_is_resent_then_drops_back_to_delta():
    s = SkillChatSession(_skill("SYS"), context_text="CTX1")
    s.start_user_turn("q1")
    s.record_assistant("a1")
    s.set_context("CTX2")                       # user checks new context mid-chat
    assert s.start_user_turn("q2") == "CTX2\n\nq2"
    s.record_assistant("a2")
    assert s.start_user_turn("q3") == "q3"      # unchanged again -> delta only


def test_unchanged_context_is_not_resent():
    s = SkillChatSession(_skill("SYS"), context_text="CTX")
    s.start_user_turn("q1")
    s.record_assistant("a1")
    s.set_context("CTX")                        # same value re-set -> not re-sent
    assert s.start_user_turn("q2") == "q2"


def test_empty_system_and_context_collapse_to_message():
    s = SkillChatSession(_skill(""), context_text="   ")
    assert s.start_user_turn("just this") == "just this"


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
    assert s.start_user_turn("hi") == "LATE CONTEXT\n\nhi"


def test_started_false_before_any_turn():
    assert SkillChatSession(_skill()).started is False


def test_kickoff_is_context_only_system_separate():
    s = SkillChatSession(_skill("SYS"), context_text="CTX")
    assert s.kickoff() == "CTX"             # context only; no "User:" line
    assert s.system_prompt == "SYS"         # the skill rides as system
    assert s.started is False               # records no turn
    s.record_assistant("draft")             # the kickoff reply commits the context
    assert s.started is True
    # context already delivered on kickoff -> the follow-up is a delta only
    assert s.start_user_turn("more") == "more"


def test_kickoff_falls_back_when_no_context():
    s = SkillChatSession(_skill("SYS"))
    assert s.kickoff() == "Begin."          # never an empty body
    assert s.system_prompt == "SYS"


def test_failed_first_send_resends_context():
    s = SkillChatSession(_skill("SYS"), context_text="CTX")
    assert s.start_user_turn("q1") == "CTX\n\nq1"
    s.drop_last_user_turn()                 # send failed -> un-stage the user turn
    assert s.turns == []
    # the context rode the failed turn, so it is re-sent on the retry
    assert s.start_user_turn("q1 again") == "CTX\n\nq1 again"


def test_interpret_prefers_assistant_message():
    r = SimpleNamespace(assistant_message="  the draft  ", raw_response="raw stuff")
    assert SkillChatSession.interpret(r) == "the draft"


def test_interpret_falls_back_to_raw():
    r = SimpleNamespace(assistant_message="", raw_response="  raw text  ")
    assert SkillChatSession.interpret(r) == "raw text"


def test_interpret_empty_when_nothing():
    r = SimpleNamespace(assistant_message=None, raw_response=None)
    assert SkillChatSession.interpret(r) == ""


def test_drop_last_user_turn():
    s = SkillChatSession(_skill())
    s.start_user_turn("q1")
    s.drop_last_user_turn()                      # unanswered user turn removed
    assert s.turns == []
    s.start_user_turn("q2")
    s.record_assistant("a2")
    s.drop_last_user_turn()                      # last turn is assistant -> no-op
    assert [t.role for t in s.turns] == ["user", "assistant"]


def test_parser_plain_text_is_success_and_untruncated():
    from workflow_editor.llm.response_parser import ResponseParser
    long = "x" * 1200
    r = ResponseParser().parse(long, None, plain_text=True)
    assert r.success is True
    assert r.assistant_message == long          # full draft, not capped at 500
    # without plain_text, a non-JSON reply is still a failure (unchanged).
    assert ResponseParser().parse("just prose", None).success is False
