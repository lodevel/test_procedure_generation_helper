"""What the validator says when it cannot run: one text for "no project", and a
plain sentence (not a crash dump) when the parser is not available."""

from __future__ import annotations

from procedo.engine_client.engine_session import ParserUnavailable
from procedo.studio.llm import validator_dispatch
from procedo.studio.pack_ops import pack_parsers


def _message(outcome) -> str:
    return outcome.reason


def test_no_project_is_one_text_in_the_validator_and_the_availability_probe():
    outcome = validator_dispatch.validate_response(None, {}, None)  # type: ignore[arg-type]
    assert outcome.skipped and pack_parsers.NO_PROJECT in _message(outcome)
    assert validator_dispatch.is_loop_available(None) == (False, pack_parsers.NO_PROJECT)
    assert pack_parsers.NO_PROJECT == "No project is open."


def test_parser_unavailable_is_said_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_parsers, "is_available", lambda root: (True, ""))

    def boom(*args, **kwargs):
        raise ParserUnavailable("the engine was stopped")

    monkeypatch.setattr(validator_dispatch, "_ARTIFACT_HANDLERS", (boom,))
    monkeypatch.setattr(validator_dispatch, "_auto_restore_operator_only_fields", lambda *a: [])
    outcome = validator_dispatch.validate_response(None, {}, tmp_path)  # type: ignore[arg-type]
    assert outcome.skipped
    assert "Validator could not run: parser not available (the engine was stopped)" in _message(
        outcome
    )
    assert "crashed" not in _message(outcome)
