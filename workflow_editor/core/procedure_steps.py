"""The numbered steps of a procedure.json, in source order."""

from __future__ import annotations


def numbered_steps(steps) -> list:
    """Flatten procedure steps[] to the real numbered steps in source order.

    Macro blocks are array entries but not numbered steps: ``block_for`` /
    ``block_if`` bodies hold the actual ``<n>.`` steps (which codegen emits),
    while ``block_alloc`` / ``block_table`` carry no ``n``. Recurse into block
    bodies and keep only nodes with an integer ``n`` so the count, numbering,
    and code cross-check match the generated test.py instead of the raw
    top-level array length.
    """
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        op = s.get("op")
        if op in ("block_for", "block_do"):
            out += numbered_steps(s.get("body"))
        elif op == "block_if":
            for br in s.get("branches", []):
                out += numbered_steps(br.get("body") if isinstance(br, dict) else None)
        elif isinstance(s.get("n"), int):
            out.append(s)
    return out
