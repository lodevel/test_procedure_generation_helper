"""Subprocess CLI runner for rules_packager_base wheel operations (one op, then exit).

Invoked by FILE PATH (NOT ``-m``)::

    <machine-local venv>/<bin>/python <abs>/_pack_parsers_subprocess.py

That venv is machine-local and keyed by ``project_id``, never inside the
project; this script does not work out where it is, and does not work out where
the bundle is either — the parent hands both in (``_bundle_dir`` in the spec).

Running via ``-m`` would trigger ``procedo/studio/__init__.py`` (which imports
PySide6, absent from the project venv); invoking by path keeps
``__name__ == "__main__"`` and skips the package init.  Reads a JSON op spec
from stdin, runs it through the op table the persistent daemon also serves
(``llm/_engine_ops.py``, loaded by path), writes the JSON result to stdout.
The spec and result shapes are documented there.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "llm"))
import _engine_ops  # noqa: E402


def main() -> None:
    spec = json.loads(sys.stdin.read())
    sys.stdout.write(json.dumps(_engine_ops.run(spec)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
