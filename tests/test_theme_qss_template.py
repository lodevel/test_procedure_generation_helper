"""Invariants for the deduped Fluent QSS in ``workflow_editor/theme.py``.

The light and dark app stylesheets are one shared template
(``_FLUENT_APP_QSS_TEMPLATE``) instantiated with two token dicts.  These tests
lock the seam: both dicts drive the same key set, every token is used, and the
substitution composes cleanly (no stray ``$`` in the template or the output).

Pure ``pathlib`` + ``ast`` + ``string.Template`` — imports no Qt, so it runs
headless with no bundle / pack available (same style as test_size_budgets).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from string import Template

import pytest

_THEME = Path(__file__).resolve().parent.parent / "workflow_editor" / "theme.py"
_IDENT = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


@pytest.fixture(scope="module")
def qss_parts() -> dict[str, object]:
    """Extract token dicts + template from theme.py without importing Qt."""
    tree = ast.parse(_THEME.read_text(encoding="utf-8"))
    wanted = {
        "_FLUENT_QSS_TOKENS_LIGHT",
        "_FLUENT_QSS_TOKENS_DARK",
        "_FLUENT_APP_QSS_TEMPLATE",
    }
    found: dict[str, object] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target in wanted:
            found[target] = ast.literal_eval(node.value)
    missing = wanted - found.keys()
    assert not missing, f"theme.py no longer defines {sorted(missing)} as literals"
    return found


def test_light_and_dark_token_dicts_share_one_key_set(qss_parts) -> None:
    light = qss_parts["_FLUENT_QSS_TOKENS_LIGHT"]
    dark = qss_parts["_FLUENT_QSS_TOKENS_DARK"]
    assert set(light) == set(dark), (
        "light/dark token dicts diverged: "
        f"only-light={sorted(set(light) - set(dark))} "
        f"only-dark={sorted(set(dark) - set(light))}"
    )


def test_template_references_exactly_the_declared_tokens(qss_parts) -> None:
    template = qss_parts["_FLUENT_APP_QSS_TEMPLATE"]
    declared = set(qss_parts["_FLUENT_QSS_TOKENS_LIGHT"])
    referenced = set(_IDENT.findall(template))
    assert referenced == declared, (
        f"undeclared-in-dicts={sorted(referenced - declared)} "
        f"dead-tokens={sorted(declared - referenced)}"
    )


def test_substitution_composes_clean_distinct_stylesheets(qss_parts) -> None:
    template = Template(qss_parts["_FLUENT_APP_QSS_TEMPLATE"])
    # .substitute is strict: raises on a missing key or a malformed/stray `$`.
    light = template.substitute(qss_parts["_FLUENT_QSS_TOKENS_LIGHT"])
    dark = template.substitute(qss_parts["_FLUENT_QSS_TOKENS_DARK"])
    assert "$" not in light and "$" not in dark
    assert light != dark, "light and dark skins composed identically"
    # Both must still be real stylesheets, not accidentally emptied.
    for qss in (light, dark):
        assert "QPushButton" in qss and qss.count("{") == qss.count("}")
