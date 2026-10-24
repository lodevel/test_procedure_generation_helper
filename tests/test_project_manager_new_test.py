"""Studio's new test folder (``ProjectManager.create_test_folder``): seeded by the host's one
starter writer (``procedo.core.starter_test``), byte for byte what Studio always wrote — a
``# <id>`` procedure_text.md and an empty procedure.json, id == the sanitized folder name, no
test.py (codegen writes that)."""

from __future__ import annotations

import os
from pathlib import Path

from procedo.studio.core.project_manager import ProjectManager


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _procedure(test_id: str) -> bytes:
    text = (f'{{\n  "id": "{test_id}",\n  "description": "",\n  "equipment": [],\n'
            f'  "steps": [],\n  "expected": []\n}}\n')
    return text.replace("\n", os.linesep).encode()


def test_a_new_test_folder_is_byte_for_byte_the_starter(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    pm = ProjectManager()
    assert pm.set_project_root(tmp_path)

    for name in ("Alpha", "my test", "3x"):
        assert pm.create_test_folder(name) == tmp_path / "tests" / name

    nl = os.linesep.encode()
    assert _tree(tmp_path) == {
        "tests/3x/procedure.json": _procedure("T3x"),
        "tests/3x/procedure_text.md": b"# T3x" + nl,
        "tests/Alpha/procedure.json": _procedure("Alpha"),
        "tests/Alpha/procedure_text.md": b"# Alpha" + nl,
        "tests/my test/procedure.json": _procedure("my_test"),
        "tests/my test/procedure_text.md": b"# my_test" + nl,
    }


def test_an_existing_folder_is_refused_and_left_alone(tmp_path: Path) -> None:
    folder = tmp_path / "tests" / "Alpha"
    folder.mkdir(parents=True)
    pm = ProjectManager()
    assert pm.set_project_root(tmp_path)

    assert pm.create_test_folder("Alpha") is None
    assert _tree(tmp_path) == {}
