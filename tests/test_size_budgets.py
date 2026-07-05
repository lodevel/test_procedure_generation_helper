"""AST size-budget guard for the workflow editor (``workflow_editor/``).

Sibling of the host ``tests/test_size_budgets.py`` — same ratchet, applied to
the editor's own source package.  Every ``.py`` under ``workflow_editor/`` must
stay within a hard structural budget:

* file length     <= ``FILE_CAP``   lines
* methods/class   <= ``METHOD_CAP``
* function length <= ``FUNC_CAP``   lines

EXCEPT units that already exceed a cap today; those are grandfathered at their
current measurement (a ceiling they may only shrink under, never grow past).
A new 1500-line non-allowlisted module fails immediately.

The god files (``main_window.py``, ``theme.py``, ...) are intentionally NOT
carved here; this only freezes their size so the debt cannot compound.

Pure ``pathlib`` + ``ast`` — imports nothing, so it runs headless with no
bundle / pack / Qt available.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FILE_CAP = 1000
METHOD_CAP = 40
FUNC_CAP = 150

# tests/ -> editor root -> workflow_editor package
_PKG = Path(__file__).resolve().parent.parent / "workflow_editor"
_SKIP_DIRS = {"__pycache__", "build", "dist", ".venv"}

# --- Grandfather ledger (measured on the current tree) -----------------------
# Ceilings for units that ALREADY breach a cap today; assertions use ``<=`` so a
# grandfathered unit may shrink but never grow.  Do NOT relax to make room —
# carve the file, then lower or delete its entry.

FILE_BUDGET: dict[str, int] = {
    "core/task_config.py": 1289,
    "dialogs/settings_dialog.py": 1298,  # +4: bare-except cleanup log lines
    "llm/_execute_op_subprocess.py": 1005,
    "llm/opencode_backend.py": 1251,  # +9: bare-except cleanup log lines
    "llm/pack_parsers.py": 1682,
    "llm/tab_context.py": 1057,
    "llm/validator_dispatch.py": 1163,
    "main_window.py": 2726,  # +5: bare-except cleanup log lines
    "theme.py": 1981,  # light/dark Fluent QSS deduped into one template + token dicts
}

CLASS_METHOD_BUDGET: dict[tuple[str, str], int] = {
    ("dock/skill_chat_widget.py", "SkillChatWidget"): 42,
    ("main_window.py", "MainWindow"): 94,
    ("tabs/json_code_tab.py", "JsonCodeTab"): 42,
    ("tabs/text_json_tab.py", "TextJsonTab"): 43,
}

FUNC_LEN_BUDGET: dict[tuple[str, str], int] = {
    ("dialogs/settings_dialog.py", "_create_llm_tab"): 187,
    ("dock/skill_chat_widget.py", "_setup_ui"): 156,
    ("main_window.py", "_setup_menu"): 167,
    ("tabs/llm_tab_mixin.py", "_run_deterministic_parse_and_generate"): 164,
}


def _py_files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*.py")) if not (set(p.parts) & _SKIP_DIRS)]


def _rel(p: Path) -> str:
    return str(p.relative_to(_PKG)).replace("\\", "/")


_FILES = _py_files(_PKG)
_IDS = [_rel(p) for p in _FILES]


@pytest.mark.parametrize("path", _FILES, ids=_IDS)
def test_file_within_line_budget(path: Path) -> None:
    rel = _rel(path)
    lines = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
    cap = FILE_BUDGET.get(rel, FILE_CAP)
    assert lines <= cap, (
        f"{rel} is {lines} lines (budget {cap}). Non-allowlisted files must be "
        f"<= {FILE_CAP}; allowlisted god files may only shrink. Carve it, do not "
        f"raise the ceiling."
    )


@pytest.mark.parametrize("path", _FILES, ids=_IDS)
def test_class_method_and_function_budgets(path: Path) -> None:
    rel = _rel(path)
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        pytest.skip(f"{rel} is not parseable Python (fragment/snippet)")

    over_methods: list[str] = []
    over_funcs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = sum(
                isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) for x in node.body
            )
            cap = CLASS_METHOD_BUDGET.get((rel, node.name), METHOD_CAP)
            if methods > cap:
                over_methods.append(f"class {node.name}: {methods} methods (budget {cap})")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
            flen = node.end_lineno - node.lineno + 1
            cap = FUNC_LEN_BUDGET.get((rel, node.name), FUNC_CAP)
            if flen > cap:
                over_funcs.append(f"def {node.name}: {flen} lines (budget {cap})")

    assert not (over_methods or over_funcs), (
        f"{rel} breaches a structural budget:\n  "
        + "\n  ".join(over_methods + over_funcs)
        + f"\nNon-allowlisted classes must be <= {METHOD_CAP} methods and "
        f"functions <= {FUNC_CAP} lines; allowlisted units may only shrink."
    )
