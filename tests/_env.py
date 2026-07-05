"""Session-once environment probes for precondition-based test skips.

Each probe detects ONE capability of the running environment, cheaply and at
most once per process (memoized). Tests skip via ``pytest.mark.skipif`` on
these probes instead of hardcoding failure lists, so on a properly provisioned
machine (project bundle / matched grammar packs installed, native Windows
console) the same tests RUN.
"""
from __future__ import annotations

import functools
import subprocess
import sys
import threading

# --- probe 1: in-process labscpi 'psu' reconstruction ------------------------

# Minimal grammar-valid labscpi fixtures (mirrors tests/test_reconstruction.py).
_PSU_FRAGMENT = """## Equipment
PSU1 : psu channels=[{1, max_voltage=24.0 V, max_current=2.0 A}]

## Steps
1. Set PSU1 CH1 voltage = 5.0 V.

## Expected
"""

_PSU_PRIOR = """# PROBE_TEST
Probe description.

## Meta
format_version: 2.0.1
board: BOARD_A
rules_pack: old@1.0.0
labscpi_pack: old@1.0.0
"""

LABSCPI_SKIP_REASON = (
    "in-process pack dispatch cannot reconstruct labscpi 'psu' grammar: the "
    "editor venv has no bundle-matched rules_packager_base wheel + labscpi "
    "grammar pack registered (probe reconstruct fails with EQP_TYPE_UNKNOWN "
    "'psu'). Runs on a bench machine with the project bundle installed."
)


@functools.lru_cache(maxsize=1)
def labscpi_psu_reconstruct_available() -> bool:
    """True iff the in-process (project_root=None) pack-dispatch path can
    successfully reconstruct a minimal labscpi ``psu`` fragment.

    This is the shared precondition of every reconstruct/validate-dispatch
    test that feeds ``psu`` grammar through ``workflow_editor.llm``:
    without a matched wheel + registered labscpi pack the engine reports
    EQP_TYPE_UNKNOWN/GRAM_UNKNOWN_METHOD and reconstruction cannot succeed.
    """
    try:
        from tests._qt_stub import ensure_workflow_editor_importable

        ensure_workflow_editor_importable()
        from workflow_editor.llm import pack_parsers

        report = pack_parsers.reconstruct_text(
            _PSU_FRAGMENT, _PSU_PRIOR, project_root=None
        )
        return report.success is True
    except Exception:
        return False


# --- probe 2: nested interpreter spawn with an inherited stdin pipe ----------

# Replicates the project-tools MCP server's structure: a child process whose
# stdin is a never-written pipe blocks its main thread on stdin while a worker
# thread spawns a grandchild interpreter (the server's ``_run_cli`` shell-out).
# In some environments (observed: Windows venv python driven from WSL) the
# grandchild spawn deadlocks until killed; on a native console it is instant.
_NESTED_SPAWN_CHILD = r"""
import subprocess, sys, threading

def worker():
    try:
        p = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=8,
        )
        print("OK" if p.returncode == 0 else "RC%s" % p.returncode, flush=True)
    except Exception:
        print("ERR", flush=True)

threading.Thread(target=worker, daemon=True).start()
sys.stdin.readline()
"""

NESTED_SPAWN_SKIP_REASON = (
    "nested interpreter spawn deadlocks in this environment: a grandchild "
    "python launched from a worker thread while the parent's inherited stdin "
    "is an open pipe (exactly the MCP server's tools/call -> cli.py shell-out) "
    "hangs until killed (observed under WSL-driven Windows venv python). "
    "Runs on a native console/bench machine."
)


@functools.lru_cache(maxsize=1)
def nested_interpreter_spawn_works() -> bool:
    """True iff a stdio-piped child process can itself spawn a grandchild
    interpreter from a worker thread (the MCP server's ``_run_cli`` pattern)."""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _NESTED_SPAWN_CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return False
    result: dict = {}

    def _reader() -> None:
        result["line"] = proc.stdout.readline()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(15)
    try:
        proc.kill()
    except Exception:
        pass
    return result.get("line", "").strip() == "OK"


# --- probe 3: the rules_packager_base wheel itself ----------------------------

WHEEL_SKIP_REASON = (
    "the rules_packager_base grammar wheel is not installed in this venv; the "
    "in-process pack_parsers bridge cannot run. Installed on bench/host venvs."
)


@functools.lru_cache(maxsize=1)
def rules_packager_available() -> bool:
    """True iff the rules_packager_base wheel is importable in-process."""
    import importlib.util

    return importlib.util.find_spec("rules_packager_base") is not None
