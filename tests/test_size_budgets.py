"""AST size-budget guard for the workflow editor (``procedo/studio/``).

Sibling of the host ``tests/test_size_budgets.py`` — same ratchet, applied to
the editor's own source package.  Every ``.py`` under ``procedo/studio/`` must
stay within a hard structural budget:

* file length     <= ``FILE_CAP``   lines
* methods/class   <= ``METHOD_CAP``
* function length <= ``FUNC_CAP``   lines

EXCEPT units that already exceed a cap today; those are grandfathered at their
current measurement (a ceiling they may only shrink under, never grow past).
A new 1500-line non-allowlisted module fails immediately.

The god files (``main_window.py``, ...) are intentionally NOT
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

# tests/ -> editor root -> procedo.studio package
_PKG = Path(__file__).resolve().parent.parent / "procedo" / "studio"
_SKIP_DIRS = {"__pycache__", "build", "dist", ".venv"}

# --- Grandfather ledger (measured on the current tree) -----------------------
# Ceilings for units that ALREADY breach a cap today; assertions use ``<=`` so a
# grandfathered unit may shrink but never grow.  Do NOT relax to make room —
# carve the file, then lower or delete its entry.

FILE_BUDGET: dict[str, int] = {
    "core/task_config.py": 1288,
    "dialogs/settings_dialog.py": 1252,  # -53: its pages are Bench's paged dialog (Studio redesign slice 7b)
    "llm/_execute_op_subprocess.py": 1283,  # -15: the per-path procedure latch went (engine_process slice 3)
    # +50: bench-session S3 — frame dispatch,
    # reader-thread wiring, and the held-rekey seams (_ensure_held routing,
    # channel-union safe-off fallback, per-path procedure docs); ALL new logic
    # lives in the new sibling llm/_bench_session_runner.py.
    # +12: F-H6 boundary health check — the guided-op probe seam in
    # _ensure_held plus the typed HealthRefusal passthrough in _cmd_exec_ops
    # +8: R2 backstop — shutdown folds any leftover unconfirmed_off record
    # into unsafe (residual channel outside the declared sweep).
    # (the check itself lives in _bench_session_runner.check_held).
    # +51: F-H6 safety-never-refuses — probe_refuse seam on _ensure_held,
    # _cmd_safe_off proceed-after-rebuild + open-failures land in unsafe,
    # _cmd_raw typed refusal passthrough, exec_ops partial-batch results.
    # +136: R2 unconfirmed-off memory — _record_unconfirmed / _confirm_off /
    # _retry_unconfirmed, the _safe_off record/confirm chokepoint, retry
    # sweeps in _cmd_safe_off + _safe_off_all_held, and the module-docstring
    # contract (shutdown can no longer report false-clean after a failed off
    # whose handle dropped out of held).
    # +18: R3 structured unsafe — _attach_unsafe builds the parallel
    # "unsafe_targets" records from the unconfirmed-off memory on every
    # unsafe-carrying response (host recovery no longer parses strings).
    # +20 (cap 1280 -> 1300): per-run timeout retargeting — Seam B (guided
    # per_session ops call the sibling's _retarget_timeout) in _cmd_exec_ops
    # plus the applied_timeout invalidation bookkeeping in _close_held_device;
    # the helper itself lives in _bench_session_runner.py.
    # -2 (cap 1300 -> 1298): stale-name-hold fix (owner ruling 2026-08-27) —
    # _ensure_held's resolution CARVED into the new sibling
    # llm/_hold_resolution.py (fold-first rule, stale drop, ns rebind); the
    # daemon file only delegates and records the open.
    "llm/opencode_backend.py": 1251,  # +9: bare-except cleanup log lines
    "llm/validator_dispatch.py": 1032,  # -131: the dead inventory loader and _import_validator went
    "main_window.py": 1640,  # -44: Word export keywords and About left for dialogs/ (Studio redesign slice 7a)
}

CLASS_METHOD_BUDGET: dict[tuple[str, str], int] = {
    ("dock/skill_chat_widget.py", "SkillChatWidget"): 42,
    ("main_window.py", "StudioWorkspace"): 85,  # the former MainWindow, less the status bar, dock handlers (slice 2) and menus (slice 6)
}

FUNC_LEN_BUDGET: dict[tuple[str, str], int] = {
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
