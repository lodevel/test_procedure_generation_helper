"""The skill-chat session — the pure brain of the dedicated send-path.

A skill chat is a plain multi-turn conversation. The skill's ``SKILL.md`` rides
as the message ``system`` (``LLMRequest.system_prompt``, see
:attr:`SkillChatSession.system_prompt`) so it GOVERNS the model; the rendered
wire (``raw_prompt``) carries only the user's chosen context plus the whole
transcript so far. Replies are plain prose — a no-JSON response is the NORMAL
case, not a failure (the Qt controller sends with ``LLMRequest.raw_prompt`` to
bypass the JSON output contract).

**Each turn carries the full transcript**, so the bridge is correct on ANY
backend — the stateless external API *and* OpenCode — without depending on
hidden server-side chat memory. Controller contract: send each turn as an
INDEPENDENT request (for OpenCode, reset the session per send) so the server
doesn't also prepend its own history and double it; and check
``response.success`` itself (``interpret`` is display-only, not an error gate).

Pure: no Qt, no backend, no threading — the controller owns those and calls
:meth:`start_user_turn` / :meth:`record_assistant` / :meth:`interpret`. That
keeps the prompt-assembly and turn logic unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .skill import Skill


@dataclass(frozen=True)
class SkillTurn:
    """One line of the conversation, for display."""

    role: str  # "user" | "assistant"
    content: str


@dataclass
class SkillChatSession:
    """Conversation state for one run of one skill."""

    skill: Skill
    context_text: str = ""
    turns: list[SkillTurn] = field(default_factory=list)

    def set_context(self, context_text: str) -> None:
        """Set the pushed context. Only the FIRST turn carries it, so this is
        meaningful until the conversation starts."""
        self.context_text = context_text

    @property
    def started(self) -> bool:
        return bool(self.turns)

    @property
    def system_prompt(self) -> str:
        """The skill's SKILL.md — sent as the message ``system`` so it GOVERNS
        the model, instead of being buried in the user body under OpenCode's
        default agent prompt. The controller passes this as
        ``LLMRequest.system_prompt``; the rendered wire carries only the
        context + transcript."""
        return self.skill.system_prompt

    def start_user_turn(self, user_message: str) -> str:
        """Record the user message and return the FULL prompt to send.

        The skill system prompt + pushed context always lead, followed by the
        whole conversation so far. Rebuilding the complete transcript every turn
        is what makes the bridge backend-agnostic (see the module docstring)."""
        self.turns.append(SkillTurn("user", user_message))
        return self._render()

    def kickoff(self) -> str:
        """The RUN body with no user message — the pushed context only (the skill
        prompt now rides as the message ``system`` via :attr:`system_prompt`).
        Starts a self-contained skill without the user typing anything; the model
        responds to the system instructions directly. Records no turn (the
        assistant reply is recorded via :meth:`record_assistant`). Falls back to a
        minimal nudge when no context is checked, so the user body is never empty."""
        return self.context_text.strip() or "Begin."

    def _render(self) -> str:
        parts = [self.context_text]
        parts += [
            f"{'User' if t.role == 'user' else 'Assistant'}: {t.content}"
            for t in self.turns
        ]
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def record_assistant(self, text: str) -> None:
        """Append the model's reply to the visible history."""
        self.turns.append(SkillTurn("assistant", text))

    def drop_last_user_turn(self) -> None:
        """Remove a trailing UNANSWERED user turn (after a failed/cancelled
        send) so the next turn doesn't double-prompt with two user messages."""
        if self.turns and self.turns[-1].role == "user":
            self.turns.pop()

    @staticmethod
    def interpret(response) -> str:
        """Pull the plain-text reply from an ``LLMResponse``.

        Skill chat is prose, so prefer the parser's ``assistant_message`` (set
        even when the response carries no JSON) and fall back to the raw text."""
        text = (getattr(response, "assistant_message", "") or "").strip()
        if text:
            return text
        return (getattr(response, "raw_response", "") or "").strip()
