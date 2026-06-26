"""Materialize an authored procedure into a real, on-disk test.

The DC-DC batch wizard's terminal leaf: given a target project (or a bare
``tests/`` directory), a human-readable test *name*, and the authored
``procedure_text`` (the ``## Equipment`` / ``## Steps`` / ``## Expected`` block
the ``dcdc_authoring`` skill emits via ``generate_dcdc_test``), this:

1. SANITIZES the name into a legal test-folder name (same illegal-char set as
   ``project_services.project_model.ProjectModel`` — ``+ - _`` and spaces are
   all legal, so ``"PSU - +MAIN_5V0"`` survives unchanged; only characters like
   ``/ : ? *`` are scrubbed).
2. DISAMBIGUATES collisions (``create_test_folder`` returns ``None`` on an
   existing name) by appending ``" (2)"``, ``" (3)"`` … until a free slot.
3. WRITES ``procedure_text.md`` — the body verbatim, completed (best-effort)
   into a parseable document by prepending a grammar-valid ``# <test_id>``
   title and a synthesized ``## Meta`` section when the input lacks them.
4. Best-effort WRITES ``procedure.json`` by running the deterministic
   text→json parser. The parser is pack-dispatched and needs the project's
   venv + bundle (or, in-process, a wheel whose pack registry knows the
   equipment types); when it can't run it raises and we keep the test
   text-only. We **never** write an empty/placeholder ``procedure.json`` — an
   empty json makes the main GUI show the test "visible-but-empty", whereas a
   text-only test is editor-visible and clean.

Pure leaf: no Qt, no UI. The wizard dialog (assembled separately) calls
:func:`materialize_test` with ``main_window.project_manager`` (a
``ProjectManager``) as the first argument.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)

# Illegal characters in a test-folder name. Kept identical to
# ``ProjectModel._INVALID_NAME_CHARS`` (control chars + the Windows-reserved
# ``<>:"/\|?*``). We resolve the live class attribute when project_services is
# importable, else fall back to this copy so the leaf stays usable headlessly.
_FALLBACK_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# A valid procedure test_id is ``[A-Za-z][A-Za-z0-9_-]*`` (<= 64 chars). Spaces,
# '+', '.', etc. are NOT allowed in the header line even though they are legal
# in a *folder* name — hence a separate id derivation.
_TESTID_INVALID = re.compile(r"[^A-Za-z0-9_-]")
_TESTID_MULTI_UNDERSCORE = re.compile(r"_{2,}")
_HAS_META = re.compile(r"(?m)^##\s+Meta\b")

_MAX_NAME_LEN = 200          # ProjectModel.validate_test_name cap
_MAX_TESTID_LEN = 64         # grammar cap on the header test_id
_MAX_DISAMBIG = 1000         # collision-bump safety stop


@dataclass
class MaterializeResult:
    """Outcome of :func:`materialize_test`.

    ``path`` is the created test folder (``None`` on failure); ``created`` is
    whether the folder was made; ``json_written`` is whether a real
    ``procedure.json`` was produced; ``message`` is a one-line human summary.
    """

    path: Optional[Path]
    created: bool
    json_written: bool
    message: str


# ---------------------------------------------------------------------------
# Name / id sanitization
# ---------------------------------------------------------------------------


def _invalid_name_chars() -> "re.Pattern[str]":
    """The live ``ProjectModel._INVALID_NAME_CHARS`` if importable, else a copy."""
    try:
        from project_services.project_model import ProjectModel  # type: ignore

        return ProjectModel._INVALID_NAME_CHARS
    except Exception:  # noqa: BLE001 — project_services optional in leaf/test context
        return _FALLBACK_INVALID_NAME_CHARS


def sanitize_test_name(name: str) -> str:
    """Scrub *name* into a legal test-folder name.

    Illegal characters become ``_``; surrounding whitespace is stripped; empty
    / ``.`` / ``..`` degrade to ``"test"``; length is capped at 200.
    """
    cleaned = _invalid_name_chars().sub("_", (name or "").strip()).strip()
    if cleaned in ("", ".", ".."):
        cleaned = "test"
    if len(cleaned) > _MAX_NAME_LEN:
        cleaned = cleaned[:_MAX_NAME_LEN].rstrip()
    return cleaned


def _make_test_id(name: str) -> str:
    """Derive a grammar-valid ``test_id`` (``[A-Za-z][A-Za-z0-9_-]*``, <=64)."""
    tid = _TESTID_INVALID.sub("_", name or "")
    tid = _TESTID_MULTI_UNDERSCORE.sub("_", tid).strip("_-")
    # Must start with a letter.
    while tid and not tid[0].isalpha():
        tid = tid[1:]
    tid = tid[:_MAX_TESTID_LEN].rstrip("_-")
    return tid or "Test"


# ---------------------------------------------------------------------------
# Target resolution (ProjectManager handle OR a bare tests/ dir)
# ---------------------------------------------------------------------------


def _resolve_target(
    project_or_tests_dir: object,
) -> Tuple[Optional[Path], Optional[Path], Callable[[str], Optional[Path]]]:
    """Return ``(tests_dir, project_root, create_folder)``.

    Accepts either a ``ProjectManager``-like handle (duck-typed:
    ``create_test_folder`` + ``get_tests_dir`` [+ ``project_root``]) or a
    ``Path``/``str`` pointing straight at a ``tests/`` directory.
    """
    obj = project_or_tests_dir
    if hasattr(obj, "create_test_folder") and hasattr(obj, "get_tests_dir"):
        tests_dir = obj.get_tests_dir()  # type: ignore[attr-defined]
        project_root = getattr(obj, "project_root", None)
        return tests_dir, project_root, obj.create_test_folder  # type: ignore[attr-defined]

    tests_dir = Path(obj)  # type: ignore[arg-type]
    project_root = tests_dir.parent if tests_dir.name == "tests" else None

    def _create(folder_name: str) -> Optional[Path]:
        folder = tests_dir / folder_name
        if folder.exists():
            return None  # collision — mirror ProjectManager.create_test_folder
        try:
            folder.mkdir(parents=True)
            return folder
        except OSError:
            return None

    return tests_dir, project_root, _create


# ---------------------------------------------------------------------------
# Document completion (body verbatim; header best-effort)
# ---------------------------------------------------------------------------


def _ensure_parseable_document(
    text: str, test_id: str, project_root: Optional[Path]
) -> str:
    """Complete an authored body into a parseable procedure document.

    Preserves the body verbatim. When the text already carries a ``## Meta``
    section it is treated as complete and returned untouched. Otherwise a
    ``# <test_id>`` title is prepended (when absent) and a ``## Meta`` block is
    synthesized via the deterministic ``sync_meta_text`` bridge. Never raises —
    if the meta synthesizer is unavailable the titled (meta-less) text is
    returned, which the parser will reject and the caller will keep text-only.
    """
    body = text or ""
    if _HAS_META.search(body):
        return body  # already a complete document — do not mutate

    first_nonblank = next((ln for ln in body.splitlines() if ln.strip()), "")
    has_title = first_nonblank.startswith("# ")
    titled = body if has_title else f"# {test_id}\n\n{body}"

    try:
        from workflow_editor.llm import pack_parsers

        if pack_parsers.supports_sync_meta(project_root):
            synced, _warnings = pack_parsers.sync_meta_text(
                titled, project_root=project_root
            )
            return synced
    except Exception as exc:  # noqa: BLE001 — best-effort; degrade to titled text
        log.debug("sync_meta during materialize skipped: %s", exc)
    return titled


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def materialize_test(
    project_or_tests_dir: object,
    name: str,
    procedure_text: str,
) -> MaterializeResult:
    """Create a named test folder, write its procedure_text.md, best-effort json.

    See the module docstring for the full contract. Never raises on a parser
    failure — falls back to a clean text-only test.
    """
    tests_dir, project_root, create_folder = _resolve_target(project_or_tests_dir)
    if tests_dir is None:
        return MaterializeResult(
            None, False, False, "No tests/ directory available for this project."
        )

    base = sanitize_test_name(name)
    test_id = _make_test_id(base)

    # --- create folder, disambiguating collisions -------------------------
    folder: Optional[Path] = None
    candidate = base
    n = 2
    while True:
        folder = create_folder(candidate)
        if folder is not None:
            break
        # None == collision (existing) OR a create error. Distinguish by
        # existence so a real failure isn't retried 1000 times.
        if (tests_dir / candidate).exists():
            if n > _MAX_DISAMBIG:
                return MaterializeResult(
                    None, False, False,
                    f"Could not find a free name for '{base}' "
                    f"(tried up to {_MAX_DISAMBIG} suffixes).",
                )
            candidate = f"{base} ({n})"
            n += 1
            continue
        return MaterializeResult(
            None, False, False, f"Could not create test folder '{candidate}'."
        )

    # --- write procedure_text.md (completed document; body verbatim) ------
    final_text = _ensure_parseable_document(procedure_text or "", test_id, project_root)

    from workflow_editor.core.artifact_manager import ArtifactManager, ArtifactType

    am = ArtifactManager()
    am.set_test_dir(folder)
    am.set_content(ArtifactType.PROCEDURE_TEXT, final_text)
    am.save_artifact(ArtifactType.PROCEDURE_TEXT)

    # --- best-effort procedure.json ---------------------------------------
    json_written = False
    message = f"Created test '{candidate}' (procedure_text.md only)."
    try:
        from workflow_editor.llm import pack_parsers

        procedure_json, _warnings = pack_parsers.parse_text(final_text, project_root)
        am.set_json_from_dict(procedure_json)
        am.save_artifact(ArtifactType.PROCEDURE_JSON)
        json_written = True
        message = f"Created test '{candidate}' with procedure_text.md + procedure.json."
    except Exception as exc:  # noqa: BLE001 — ParserUnavailable/ParseFailure/etc.
        # Keep the test text-only; do NOT write an empty/placeholder json.
        log.debug("procedure.json skipped during materialize: %s", exc)

    return MaterializeResult(folder, True, json_written, message)
