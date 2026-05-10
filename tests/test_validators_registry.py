"""Tests for the Phase-2 validators registry.

Covers:

* Register / get / list / shorthand resolution.
* Built-in registration is idempotent.
* The three built-in wrappers (rules_packager_base.validate_procedure,
  rules_packager_base.validate_json_schema, core.check_python_syntax)
  produce ValidationOutcome with the expected shape.
* Built-ins handle missing artifacts by returning ``skipped=True``
  instead of raising.
"""

from __future__ import annotations

import pytest

from workflow_editor.core.validators_registry import (
    ValidatorContext,
    ensure_builtins_registered,
    get,
    is_registered,
    list_ids,
    register,
    unregister_all,
)
from workflow_editor.llm.validator_dispatch import ValidationOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test runs against an empty registry. ``unregister_all`` also
    resets the ``_builtins_registered`` flag so ``ensure_builtins_registered``
    re-runs cleanly."""
    unregister_all()
    yield
    unregister_all()


def _noop_validator(_ctx: ValidatorContext) -> ValidationOutcome:
    return ValidationOutcome(ok=True, skipped=False, issues=[])


# ---------------------------------------------------------------------------
# Registry core: register / get / list
# ---------------------------------------------------------------------------


def test_register_and_get_namespaced_id():
    register("pack_a.validate_foo", _noop_validator)
    assert get("pack_a.validate_foo") is _noop_validator
    assert is_registered("pack_a.validate_foo")


def test_get_missing_id_raises_keyerror():
    with pytest.raises(KeyError, match="not_registered"):
        get("not_registered")


def test_register_empty_id_rejected():
    with pytest.raises(ValueError):
        register("", _noop_validator)


def test_list_ids_returns_sorted_namespaced_ids():
    register("pack_b.x", _noop_validator)
    register("pack_a.y", _noop_validator)
    register("pack_a.x", _noop_validator)
    assert list_ids() == ["pack_a.x", "pack_a.y", "pack_b.x"]


def test_register_idempotent_overwrites_previous():
    def v1(_ctx): return ValidationOutcome(ok=False, issues=[])
    def v2(_ctx): return ValidationOutcome(ok=True, issues=[])
    register("pack.v", v1)
    register("pack.v", v2)
    assert get("pack.v") is v2


# ---------------------------------------------------------------------------
# Shorthand resolution
# ---------------------------------------------------------------------------


def test_shorthand_resolves_when_one_match():
    register("rules_packager_base.validate_procedure", _noop_validator)
    assert get("validate_procedure") is _noop_validator


def test_shorthand_picks_alphabetical_first_when_multiple():
    def for_a(_ctx): return ValidationOutcome(ok=True, reason="a")
    def for_b(_ctx): return ValidationOutcome(ok=True, reason="b")
    register("pack_b.validate", for_b)
    register("pack_a.validate", for_a)
    # Alphabetical order — pack_a wins despite later registration.
    assert get("validate") is for_a


def test_shorthand_no_match_raises():
    register("pack_a.validate_foo", _noop_validator)
    with pytest.raises(KeyError):
        get("validate_bar")


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


def test_ensure_builtins_registered_is_idempotent():
    ensure_builtins_registered()
    ids_first = set(list_ids())
    ensure_builtins_registered()
    ids_second = set(list_ids())
    assert ids_first == ids_second


def test_ensure_builtins_registered_registers_expected_ids():
    ensure_builtins_registered()
    ids = set(list_ids())
    assert "rules_packager_base.validate_procedure" in ids
    assert "rules_packager_base.validate_json_schema" in ids
    assert "core.check_python_syntax" in ids


# ---------------------------------------------------------------------------
# Built-in wrappers — shape contracts
# ---------------------------------------------------------------------------


def _ctx(text=None, jsonstr=None, code=None, project_root=None, tab_id="text_json"):
    return ValidatorContext(
        artifact_text=text,
        artifact_json=jsonstr,
        artifact_code=code,
        project_root=project_root,
        tab_id=tab_id,
    )


def test_validate_json_schema_skipped_when_no_json():
    ensure_builtins_registered()
    outcome = get("rules_packager_base.validate_json_schema")(_ctx(jsonstr=None))
    assert outcome.skipped is True
    assert outcome.ok is True
    assert outcome.issues == []


def test_validate_json_schema_passes_minimal_valid_doc():
    ensure_builtins_registered()
    outcome = get("rules_packager_base.validate_json_schema")(
        _ctx(jsonstr='{"name": "x", "steps": []}')
    )
    assert outcome.skipped is False
    assert outcome.ok is True


def test_validate_json_schema_flags_invalid_json():
    ensure_builtins_registered()
    outcome = get("rules_packager_base.validate_json_schema")(_ctx(jsonstr='{ not json'))
    assert outcome.skipped is False
    assert outcome.ok is False
    assert outcome.has_errors
    assert any("JSON_PARSE_ERROR" == i.code for i in outcome.issues)


def test_check_python_syntax_skipped_when_no_code():
    ensure_builtins_registered()
    outcome = get("core.check_python_syntax")(_ctx(code=None))
    assert outcome.skipped is True


def test_check_python_syntax_passes_valid_code():
    ensure_builtins_registered()
    outcome = get("core.check_python_syntax")(_ctx(code="x = 1 + 2\n"))
    assert outcome.skipped is False
    assert outcome.ok is True


def test_check_python_syntax_rejects_invalid_code():
    ensure_builtins_registered()
    outcome = get("core.check_python_syntax")(_ctx(code="def x(:\n"))
    assert outcome.skipped is False
    assert outcome.ok is False
    assert outcome.has_errors


# ---------------------------------------------------------------------------
# validate_procedure: the LLM-loop pipeline lives behind ``project_root``
# resolution; without a real project we expect ``skipped=True``.
# ---------------------------------------------------------------------------


def test_validate_procedure_skipped_without_project_root():
    ensure_builtins_registered()
    outcome = get("rules_packager_base.validate_procedure")(_ctx())
    assert outcome.skipped is True
