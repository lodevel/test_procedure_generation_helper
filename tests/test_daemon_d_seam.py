"""The engine daemon's ``globals()`` seam (``d``) holds every key its siblings read.

``_bench_session_runner`` / ``_guided_call`` read the daemon's globals as ``d``;
``_safe_off``'s ``_resolve_hold`` (and the ``_bench_session_runner`` functions it calls) read
``_safe_off``'s own globals, the ``d`` that ``_ensure_held`` hands them.  The keys are derived by AST from
the readers, so a future ``d["..."]`` read is covered without editing this test, and an
"unused" import dropped from either module fails here instead of as a KeyError at runtime.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

_FOLDER = Path(__file__).resolve().parent.parent / "procedo" / "engine_client"
_spec = importlib.util.spec_from_file_location("_exec_op_runner_dseam",
                                               _FOLDER / "_execute_op_subprocess.py")
er = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(er)


def _tree(name: str) -> ast.Module:
    return ast.parse((_FOLDER / f"{name}.py").read_text(encoding="utf-8"))


def _d_keys(node) -> set:
    """Keys read as ``d["k"]`` or ``d.get("k")`` anywhere under ``node``."""
    keys = set()
    for n in ast.walk(node):
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "d"
                and isinstance(n.slice, ast.Constant)):
            keys.add(n.slice.value)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "d" and n.args and isinstance(n.args[0], ast.Constant)):
            keys.add(n.args[0].value)
    return keys


def _bench_keys_from(entry: set) -> set:
    """d-keys read by the ``_bench_session_runner`` functions in ``entry`` and every
    module-level function of it they call with ``d``."""
    funcs = {f.name: f for f in _tree("_bench_session_runner").body if isinstance(f, ast.FunctionDef)}
    seen, todo, keys = set(), [n for n in entry if n in funcs], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        keys |= _d_keys(funcs[name])
        for n in ast.walk(funcs[name]):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in funcs
                    and any(isinstance(a, ast.Name) and a.id == "d" for a in n.args)):
                todo.append(n.func.id)
    return keys


def test_the_daemon_globals_hold_every_key_its_siblings_read():
    keys = set().union(*(_d_keys(_tree(m)) for m in
                         ("_bench_session_runner", "_guided_call")))
    assert "_safe_off_all_held" in keys and "_PROTO_OUT" in keys     # the derivation sees them
    assert sorted(k for k in keys if k not in vars(er)) == []


def test_the_safe_off_globals_hold_every_key_hold_resolution_reaches():
    hold = next(f for f in _tree("_safe_off").body if isinstance(f, ast.FunctionDef) and f.name == "_resolve_hold")
    called = {n.attr for n in ast.walk(hold)
              if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "b"}
    keys = _d_keys(hold) | _bench_keys_from(called)
    assert "_raw_send" in keys and "_ensure_held" in keys            # check_held's probe + rebuild
    so = sys.modules["_safe_off"]
    assert sorted(k for k in keys if k not in vars(so)) == []
    assert all(vars(so)[k] is vars(er)[k] for k in keys)            # the same objects as the daemon's
