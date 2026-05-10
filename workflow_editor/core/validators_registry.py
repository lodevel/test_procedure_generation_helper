"""Validators registry — namespaced ID lookup for Phase 2/3.

Phase 2 introduces a tab-agnostic, registry-driven validator dispatch:

  - A validator is a function ``Callable[[ValidatorContext], ValidationOutcome]``.
  - Validators register under **namespaced** ids of the form
    ``<pack_id>.<validator_id>`` (e.g. ``rules_packager_base.validate_procedure``).
  - The registry resolver accepts both the namespaced form and a bare
    shorthand (``validate_procedure``) — shorthand is back-compat for
    hand-written configs and resolves to the first match.
  - The registry is the *single source of truth* for both the button
    path (operator-clicked "Validate …" buttons) and the LLM-with-
    feedback loop. Phase 5 lifts the pack-side validators into their
    rules pack physically; the contract here stays unchanged.

Validators receive **only** the ``ValidatorContext`` (artifacts + project
root + tab id). No ``main_window``, ``tab``, or ``dock`` references — the
caller routes the returned ``ValidationOutcome`` to the dock panel.

The module is import-side-effect free. Built-in validator registrations
live in ``workflow_editor/llm/validators_builtin.py`` and run on demand
via :func:`ensure_builtins_registered`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..llm.validator_dispatch import ValidationOutcome  # canonical result type

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validator contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatorContext:
    """Input to a registered validator.

    Validators must be pure with respect to this context — no peeking at
    GUI state, no hidden globals. ``project_root`` lets a validator load
    auxiliary data (e.g. the project's text_renderer variant) without
    going through the GUI.
    """

    artifact_text: Optional[str]
    artifact_json: Optional[str]   # raw string; the validator parses if it cares
    artifact_code: Optional[str]
    project_root: Optional[Path]
    tab_id: str


# Callable shape registered validators must satisfy.
Validator = Callable[[ValidatorContext], ValidationOutcome]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_lock = threading.RLock()
_registry: Dict[str, Validator] = {}
_builtins_registered: bool = False


def register(validator_id: str, validator: Validator) -> None:
    """Register a validator under a (preferably namespaced) id.

    Re-registering the same id overwrites silently — this lets the test
    suite swap implementations and lets Phase 5's code-move reuse an id
    after the pack-internal validator replaces the editor-side stub.
    Pack ids should be of the form ``<pack_id>.<validator_id>``; the
    ``core.`` pseudo-pack is reserved for grammar-agnostic validators
    that don't belong to any rules pack (e.g. ``core.check_python_syntax``).
    """
    if not validator_id:
        raise ValueError("validator_id must be non-empty")
    with _lock:
        if validator_id in _registry:
            log.debug("Re-registering validator %r (existing entry overwritten)", validator_id)
        _registry[validator_id] = validator


def get(validator_id: str) -> Validator:
    """Resolve a validator id.

    The resolver accepts:

    * **Namespaced** ids (``rules_packager_base.validate_procedure``) —
      exact match.
    * **Shorthand** ids (``validate_procedure``) — returns the first
      registered match (alphabetical pack order). Shorthand exists for
      back-compat with hand-written configs that pre-date namespacing;
      callers should prefer namespaced ids.

    Raises:
        KeyError: if no validator matches.
    """
    with _lock:
        if validator_id in _registry:
            return _registry[validator_id]
        # Shorthand resolution: look for ``<pack>.<shorthand>`` ending in
        # ``.<shorthand>``. Alphabetical order makes the choice
        # deterministic when multiple packs register the same name.
        candidates = sorted(
            full_id for full_id in _registry if full_id.endswith("." + validator_id)
        )
        if candidates:
            chosen = candidates[0]
            if len(candidates) > 1:
                log.info(
                    "Shorthand %r matched %d validators; picking %s",
                    validator_id, len(candidates), chosen,
                )
            return _registry[chosen]
        raise KeyError(f"No validator registered for id {validator_id!r}")


def list_ids() -> List[str]:
    """Return the namespaced ids of every registered validator. Sorted."""
    with _lock:
        return sorted(_registry)


def is_registered(validator_id: str) -> bool:
    """Cheap existence check using the same resolver semantics as :func:`get`."""
    try:
        get(validator_id)
        return True
    except KeyError:
        return False


def unregister_all() -> None:
    """Test-only helper: clear the registry. Production code should never
    call this — re-registration on the same id is idempotent."""
    global _builtins_registered
    with _lock:
        _registry.clear()
        _builtins_registered = False


# ---------------------------------------------------------------------------
# Lazy built-in registration
# ---------------------------------------------------------------------------


def ensure_builtins_registered() -> None:
    """Register Phase 2's built-in validators on first call.

    Idempotent and lock-protected so concurrent first-touch (e.g. two
    tabs constructing in parallel) is safe. Built-ins physically live
    in ``workflow_editor/llm/validators_builtin.py``; Phase 5 lifts
    pack-owned ones into their rules pack.
    """
    global _builtins_registered
    with _lock:
        if _builtins_registered:
            return
        # Imported lazily to avoid a circular dependency: validators_builtin
        # uses ``register`` from this module.
        from ..llm import validators_builtin  # noqa: F401 — import-side-effect
        validators_builtin.register_builtins()
        _builtins_registered = True
        log.debug("Built-in validators registered: %s", list_ids())
