"""The skill-chat session — the pure brain of the dedicated send-path.

A skill chat is a plain multi-turn conversation on a PERSISTENT OpenCode session
(like the dock/tab chat): the skill's ``SKILL.md`` rides as the message ``system``
(:attr:`SkillChatSession.system_prompt`) so it GOVERNS the model, and each turn
sends only the NEW user message (the delta). OpenCode keeps the conversation —
including MCP tool results (the netlist / BOM) — server-side across turns, so
nothing is replayed and nothing is thrown away. The pushed context (rules / docs /
artifacts) rides the FIRST message of the session, and again only when it CHANGES.

Replies are plain prose — a no-JSON response is the NORMAL case, not a failure
(the Qt controller sends with ``LLMRequest.raw_prompt`` to bypass the JSON output
contract, and checks ``response.success`` itself; ``interpret`` is display-only).

Pure: no Qt, no backend, no threading — the controller owns the persistent session
(``ensure_session`` before each send; ``new_session`` only on trash / skill-switch)
and calls :meth:`start_user_turn` / :meth:`record_assistant` / :meth:`interpret`.
That keeps the prompt-assembly and turn logic unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .skill import Skill


@dataclass(frozen=True)
class SkillTurn:
    """One line of the conversation, for display."""

    role: str  # "user" | "assistant"
    content: str


@dataclass
class SkillChatSession:
    """Conversation state for one run of one skill (persistent-session send-path)."""

    skill: Skill
    context_text: str = ""
    turns: list[SkillTurn] = field(default_factory=list)
    # Context already DELIVERED to the model (committed on a successful reply) and
    # context staged for the in-flight turn. Context rides the first message and
    # again only when it changes; both gate on success so a failed send re-sends it.
    _delivered_context: str = ""
    _pending_context: Optional[str] = None

    def set_context(self, context_text: str) -> None:
        """Set the pushed context. It rides the next send when it differs from what
        the model already holds — the first message, or whenever the user changes
        the checked context mid-conversation."""
        self.context_text = context_text

    @property
    def started(self) -> bool:
        return bool(self.turns)

    @property
    def system_prompt(self) -> str:
        """The skill's SKILL.md — sent as the message ``system`` so it GOVERNS the
        model. The controller passes this as ``LLMRequest.system_prompt`` on every
        send; the wire body carries only the (first/changed) context + the user
        delta."""
        return self.skill.system_prompt

    def _lead_context(self) -> str:
        """Stage the context for this send: the current context if it differs from
        what the model already holds, else nothing. Returns the lead string (``""``
        when there is nothing to (re)send) and records it as PENDING until the reply
        lands (:meth:`record_assistant` commits it, :meth:`drop_last_user_turn`
        un-stages it)."""
        ctx = self.context_text.strip()
        if ctx and ctx != self._delivered_context:
            self._pending_context = ctx
            return ctx
        self._pending_context = None
        return ""

    def kickoff(self) -> str:
        """The RUN body with no user message — the (first) pushed context only (the
        skill prompt rides as the message ``system``). Starts a self-contained skill
        without the user typing; the model responds to the system instructions
        directly. Records no turn (the reply is recorded via :meth:`record_assistant`).
        Falls back to a minimal nudge when no context is checked, so the body is
        never empty."""
        return self._lead_context() or "Begin."

    def start_user_turn(self, user_message: str) -> str:
        """Record the user message and return the WIRE body: just the message (the
        delta), with the pushed context prepended ONLY on the first send or when the
        checked context changed. OpenCode holds the prior turns server-side, so
        nothing else is replayed."""
        self.turns.append(SkillTurn("user", user_message))
        lead = self._lead_context()
        return f"{lead}\n\n{user_message}" if lead else user_message

    def record_assistant(self, text: str) -> None:
        """Append the model's reply and COMMIT any context staged for that turn (it
        has now reached the model)."""
        if self._pending_context is not None:
            self._delivered_context = self._pending_context
            self._pending_context = None
        self.turns.append(SkillTurn("assistant", text))

    def drop_last_user_turn(self) -> None:
        """Remove a trailing UNANSWERED user turn (after a failed/cancelled send)
        and un-stage any context that rode it, so the next send re-sends both."""
        self._pending_context = None
        if self.turns and self.turns[-1].role == "user":
            self.turns.pop()

    @staticmethod
    def interpret(response) -> str:
        """Pull the plain-text reply from an ``LLMResponse``.

        Skill chat is prose, so prefer the parser's ``assistant_message`` (set even
        when the response carries no JSON) and fall back to the raw text."""
        text = (getattr(response, "assistant_message", "") or "").strip()
        if text:
            return text
        return (getattr(response, "raw_response", "") or "").strip()
