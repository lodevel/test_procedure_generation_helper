"""Preserve operator-pinned bench-identification constants when test.py
is regenerated.

Per the v2.0.x design (2026-04-28 operator directive): bench
identification (visa, port, baud, timeout, remote, manual_override)
lives in test.py module constants only. When codegen regenerates
test.py, the operator-pinned values must survive — otherwise every
regen would clobber the bench's real VISA/COM addresses with codegen
defaults.

Both extraction and merge use AST. The merge replaces the assignment
node's value-span surgically (preserving comments, whitespace, and
file structure around the constant). This handles:

  - Plain ``NAME = LITERAL`` assignments.
  - Type-annotated ``NAME: TYPE = LITERAL`` (PEP 526 ``ast.AnnAssign``).
  - Multi-line value spans (``NAME = (\\n  "ASRL3::INSTR"\\n)``).
"""

from __future__ import annotations

import ast
import logging
from typing import Any

log = logging.getLogger(__name__)


# Suffixes that identify a bench-identification module constant.
# Each becomes ``<EQUIPMENT_ID><SUFFIX>`` in test.py:
#   PSU1_VISA = "ASRL3::INSTR"
#   PSU1_CHANNEL = 1
#   PSU1_TIMEOUT_MS = 5000
#   FNCORE_PORT = "COM22"
#   FNCORE_BAUD = 115200
#   FNCORE_MANUAL_OVERRIDE = False
_PRESERVED_SUFFIXES: tuple[str, ...] = (
    "_VISA",
    "_PORT",
    "_BAUD",
    "_TIMEOUT_MS",
    "_TIMEOUT_S",
    "_REMOTE",
    "_MANUAL_OVERRIDE",
    "_CHANNEL",
)


def _build_preserved_names(equipment_ids: list[str]) -> frozenset[str]:
    """All ``<EQUIPMENT_ID><SUFFIX>`` combinations the merge cares about."""
    return frozenset(
        eq_id + suffix
        for eq_id in equipment_ids
        for suffix in _PRESERVED_SUFFIXES
    )


def _assign_target_name(node: ast.AST) -> str | None:
    """Return the target identifier for a top-level assignment, or None.

    Handles both ``ast.Assign`` (plain ``NAME = ...``) and ``ast.AnnAssign``
    (``NAME: TYPE = ...``). Anything more complex (tuple targets, attribute
    targets, etc.) returns ``None`` — operator-pinned bench constants are
    always simple module-level names.
    """
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1:
            return None
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name):
            return tgt.id
        return None
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            return node.target.id
        return None
    return None


def extract_pinned_constants(
    code: str,
    equipment_ids: list[str],
) -> dict[str, Any]:
    """Parse ``code`` for module-level ``<ID>_<SUFFIX>`` assignments and
    return them as a dict ``{name: literal_value}``.

    Handles plain assignments, type-annotated assignments, and any value
    expression that evaluates as a Python literal via ``ast.literal_eval``
    (including parenthesized multi-line strings). Non-literal values
    (env-var lookups, function calls, etc.) are silently skipped — the
    operator chose to compute the value, the merge respects that choice.
    """
    if not code or not equipment_ids:
        return {}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        log.warning("extract_pinned_constants: failed to parse: %s", exc)
        return {}

    preserved = _build_preserved_names(equipment_ids)
    pinned: dict[str, Any] = {}
    for node in tree.body:
        name = _assign_target_name(node)
        if name is None or name not in preserved:
            continue
        # AnnAssign may have value=None (type-only declaration); skip those.
        value_node = getattr(node, "value", None)
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, SyntaxError):
            continue
        pinned[name] = value
    return pinned


def merge_pinned_constants(
    new_code: str,
    pinned: dict[str, Any],
) -> tuple[str, list[str]]:
    """Replace top-level ``<NAME> = ...`` assignments in ``new_code`` with
    the operator's pinned literal value from ``pinned``.

    Returns ``(merged_code, replaced_names)``. AST-based span replacement,
    so type-annotated assignments and multi-line values are handled
    correctly without corrupting surrounding code. The replacement value
    is always a single-line literal; multi-line value spans collapse to
    one line on the new code (the rest of the file is unchanged).
    """
    if not pinned or not new_code:
        return new_code, []

    try:
        tree = ast.parse(new_code)
    except SyntaxError as exc:
        log.warning("merge_pinned_constants: failed to parse new_code: %s", exc)
        return new_code, []

    # Collect (start_lineno, end_lineno, indent_cols, new_line) for each
    # assignment whose target name is in `pinned`. We replace whole
    # statement spans by line-numbered slicing of the source.
    lines = new_code.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []  # (start_idx, end_idx, replacement_text)
    replaced: list[str] = []
    for node in tree.body:
        name = _assign_target_name(node)
        if name is None or name not in pinned:
            continue
        start_line = node.lineno - 1  # ast is 1-based
        end_line = getattr(node, "end_lineno", node.lineno) - 1
        # Preserve the original leading indentation of the statement.
        first = lines[start_line] if start_line < len(lines) else ""
        indent = first[: len(first) - len(first.lstrip())]
        # Build the replacement line. For AnnAssign, drop the annotation
        # — the operator's prior file may have had one; the new file may
        # not. Codegen emits plain assignments, which is the canonical form.
        new_line = f"{indent}{name} = {_format_literal(pinned[name])}\n"
        replacements.append((start_line, end_line, new_line))
        replaced.append(name)

    if not replacements:
        return new_code, []

    # Apply replacements from the bottom up so earlier spans' line indices
    # stay valid as we mutate the list.
    out_lines = list(lines)
    for start_idx, end_idx, repl in sorted(replacements, key=lambda r: -r[0]):
        out_lines[start_idx : end_idx + 1] = [repl]

    return "".join(out_lines), replaced


def _format_literal(value: Any) -> str:
    """Render a Python literal value as source. ``repr`` is correct for
    the literal types ``ast.literal_eval`` returns (str/int/float/bool/None,
    plus tuples/lists/dicts/sets of those — none of which appear in bench
    constants, but harmless if they do).

    Non-finite floats (``inf``, ``nan``) round-trip through ``repr`` to
    text Python can't parse back; bench constants never contain these,
    but guard anyway by falling back to a stringified form rather than
    silently producing invalid code.
    """
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        # NaN or infinity — invalid literal in source; fall back to a
        # safe parseable form via ``float("nan")``/``float("inf")``.
        return f'float({value!r})'
    return repr(value)


def preserve_bench_constants(
    new_code: str,
    existing_code: str,
    equipment_ids: list[str],
) -> tuple[str, list[str]]:
    """One-shot helper: extract operator-pinned bench constants from
    ``existing_code`` and merge them into ``new_code``.

    Returns ``(merged_code, replaced_names)``. Empty ``existing_code``
    or no equipment ids returns ``new_code`` unchanged.
    """
    if not existing_code or not existing_code.strip():
        return new_code, []
    pinned = extract_pinned_constants(existing_code, equipment_ids)
    if not pinned:
        return new_code, []
    return merge_pinned_constants(new_code, pinned)


def equipment_ids_from_procedure(procedure: dict[str, Any]) -> list[str]:
    """Extract equipment ids from a procedure dict. Tolerant of missing
    or malformed equipment lists. Used by callers that need to feed
    :func:`preserve_bench_constants` after parsing a procedure.json.
    """
    equipment = procedure.get("equipment")
    if not isinstance(equipment, list):
        return []
    out: list[str] = []
    for entry in equipment:
        if isinstance(entry, dict):
            eq_id = entry.get("id")
            if isinstance(eq_id, str) and eq_id:
                out.append(eq_id)
    return out
