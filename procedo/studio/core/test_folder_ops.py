"""Rename and delete a test folder — the user's work, so each operation first
proves it acts on a real test folder of this project, right before acting.
Design: docs/plans/editor_test_folder_ops_design.md (host repo)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from procedo.core.paths import is_link, rmtree

from . import test_sync
from .fileio import atomic_write_text, retry_while_busy
from .session_state import read_sync_baseline
from .test_id import force_json_id, force_text_id_line


class TestFolderError(Exception):
    """Refused or failed; the message is for the user."""

    __test__ = False  # not a pytest test class


def _prove_child(tests_dir: Path, test_dir: Path) -> None:
    """``test_dir`` is a real folder directly inside the tests folder — not a
    link or junction (acting on one would reach its target), not elsewhere."""
    if is_link(test_dir):
        raise TestFolderError(f"'{test_dir.name}' is a link, not a test folder.")
    if test_dir.resolve().parent != tests_dir.resolve():
        raise TestFolderError(f"'{test_dir.name}' is not a test folder of this project.")
    if not test_dir.is_dir():
        raise TestFolderError(f"The test folder '{test_dir.name}' no longer exists.")


def _aside_name(tests_dir: Path, name: str) -> Path:
    """A free hidden name for a folder being deleted (an earlier failed delete
    may have left ``.<name>.deleting`` behind)."""
    aside, n = tests_dir / f".{name}.deleting", 1
    while aside.exists():
        n += 1
        aside = tests_dir / f".{name}.deleting{n}"
    return aside


def delete_test(tests_dir: Path, test_dir: Path) -> None:
    """Delete a test folder. It is first renamed aside, so a file locked by
    another program stops the delete before anything is removed."""
    _prove_child(tests_dir, test_dir)
    aside = _aside_name(tests_dir, test_dir.name)
    try:
        retry_while_busy(lambda: test_dir.rename(aside))
    except OSError as e:
        raise TestFolderError(
            f"Could not delete '{test_dir.name}' — nothing was removed. A file or folder inside "
            f"it is open in another program (Explorer, a terminal, an editor); close it and try "
            f"again.\n\n{e}") from e
    try:
        rmtree(aside)  # clears a read-only file first: the user confirmed deleting everything
    except OSError as e:
        raise TestFolderError(
            f"'{test_dir.name}' was only partly deleted; what is left is in '{aside.name}':\n{e}"
        ) from e


def rename_test(project_root: Path, tests_dir: Path, test_dir: Path, new_name: str,
                patterns: list[re.Pattern[str]]) -> tuple[Path, list[str]]:
    """Rename a test folder to ``new_name`` (already a valid id) and keep the id
    and the project's references in step. Returns the new folder and warnings
    for what could not follow (the ids re-sync on the next save)."""
    _prove_child(tests_dir, test_dir)
    new_dir = tests_dir / new_name
    # A case-only rename (Foo -> foo) on Windows names the same folder: allowed.
    if new_dir.exists() and not _same_folder(test_dir, new_dir):
        raise TestFolderError(f"A test named '{new_name}' already exists.")
    # Acknowledged AND unchanged since (the list's own ⚠ test), decided on the
    # old folder: a rename must never acknowledge an edit the ⚠ was showing.
    baseline = read_sync_baseline(test_dir)
    was_in_sync = (baseline is not None and bool(baseline[1])
                   and not test_sync.is_out_of_sync(test_dir, patterns))
    try:
        test_dir.rename(new_dir)
    except OSError as e:
        raise TestFolderError(f"Could not rename '{test_dir.name}' to '{new_name}':\n{e}") from e

    warnings: list[str] = []
    for fname, fixer in (("procedure_text.md", force_text_id_line), ("procedure.json", force_json_id)):
        fpath = new_dir / fname
        if fpath.exists():
            try:
                atomic_write_text(fpath, fixer(fpath.read_text(encoding="utf-8"), new_name))
            except (OSError, ValueError) as e:
                warnings.append(f"{fname}: {e}")
    # The sync hashes enforce the folder id: an acknowledged test stays acknowledged.
    if was_in_sync:
        try:
            test_sync.mark_in_sync(new_dir, patterns)
        except (OSError, ValueError) as e:
            warnings.append(f".llm_session.json: {e}")
    warnings += _swap_references(project_root, test_dir.name, new_name)
    return new_dir, warnings


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _swap_references(project_root: Path, old: str, new: str) -> list[str]:
    """Swap ``old`` for ``new`` in config.json ``test_order`` and every
    ``scenarios/*.json`` ``tests`` list. Returns warnings."""
    warnings: list[str] = []

    def swapped(seq: list) -> list:
        return [new if entry == old else entry for entry in seq]

    def rewrite(path: Path, label: str, get, put) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            seq = get(data)
            if isinstance(seq, list) and old in seq:
                put(data, swapped(seq))
                atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except (OSError, ValueError, AttributeError, TypeError) as e:
            warnings.append(f"{label}: {e}")

    config_file = project_root / "config" / "config.json"
    if config_file.exists():
        rewrite(config_file, "config.json test_order",
                lambda d: d.get("test_order", {}).get("order"),
                lambda d, v: d["test_order"].__setitem__("order", v))
    scenarios_dir = project_root / "scenarios"
    if scenarios_dir.is_dir():
        for sc_file in sorted(scenarios_dir.glob("*.json")):
            rewrite(sc_file, f"scenarios/{sc_file.name}",
                    lambda d: d.get("tests"), lambda d, v: d.__setitem__("tests", v))
    return warnings
