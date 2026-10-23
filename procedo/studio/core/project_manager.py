"""
Project Manager - Handles project root and rules discovery.

Implements Section 5 of the spec:
- Project root detection
- Rules root detection
- Test folder enumeration
- Tab context configuration persistence
"""

import json
import logging
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import NamedTuple, Optional
from enum import Enum, auto

from procedo.core.config_manager import PROJECT_RULES, project_rule_doc_paths

from .fileio import atomic_create

log = logging.getLogger(__name__)

#: A project's own rule documents, sent on top of the bundle rules (which files are rule
#: documents there: ``config_manager.project_rule_doc_paths``, the one definition).
PROJECT_RULES_DIR = Path("config", PROJECT_RULES)
#: The key of a project document: this prefix + its file name (a bundle rule's is its name).
PROJECT_RULE_PREFIX = "project/"
#: Above this a text document is added with a warning; above the max it is refused.
RULE_DOC_WARN_BYTES = 16 * 1024
RULE_DOC_MAX_BYTES = 64 * 1024
#: Above this a PDF is added with a warning (never refused: whether the model reads it is
#: the user's business).
PDF_DOC_WARN_BYTES = 2 * 1024 * 1024
#: Heads the project documents wherever they are sent after the bundle rules (owner
#: decision 2026-10-19: they win).
PROJECT_RULES_WIN = ("The documents below are this project's own rules: where they conflict "
                     "with the bundle rules, the project rules win.")
#: Stands in the rules text for a document sent as a file (see ``attached_as_file``).
ATTACHED_NOTE = "(attached to this message as a file)"


def attached_as_file(path: Path) -> bool:
    """True for a document sent as it is, as a file part (a PDF), not in the rules text."""
    return path.suffix.lower() == ".pdf"


def warn_above(path: Path) -> int:
    """The size (bytes) above which adding the document *path* is worth a warning."""
    return PDF_DOC_WARN_BYTES if attached_as_file(path) else RULE_DOC_WARN_BYTES


class RuleDoc(NamedTuple):
    """One rule document a view can send: *key* is what ``selected_rules`` stores."""
    key: str
    path: Path
    origin: str  # "bundle" (the bundle's rules, or the folder picked instead) | "project"


class RuleDocError(Exception):
    """A document that cannot be added; the message says why, for the user."""


def _bundle_rule_files(folder: Optional[Path]) -> list[Path]:
    """The bundle rule documents in *folder*: its ``*.md`` files (any case), sorted."""
    if folder is None or not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".md")


class RulesState(Enum):
    """State of rules loading."""
    NOT_CHECKED = auto()
    LOADED = auto()
    NONE = auto()  # The open project has no rules


def kilobytes(size: int) -> str:
    """*size* bytes as whole KB, rounded up ("17 KB")."""
    return f"{max(1, -(-size // 1024))} KB"


def rule_doc_detail(doc: RuleDoc, size: int) -> str:
    """How a list describes *doc* of *size* bytes: "3 KB", "project · 3 KB" for a project's."""
    return f"project · {kilobytes(size)}" if doc.origin == "project" else kilobytes(size)


@dataclass
class TestFolderInfo:
    """Information about a test folder."""
    path: Path
    name: str
    has_json: bool = False
    has_code: bool = False
    has_text: bool = False

    @classmethod
    def of(cls, folder: Path) -> "TestFolderInfo":
        """The test folder ``folder`` as it is on disk now."""
        return cls(
            path=folder,
            name=folder.name,
            has_json=(folder / "procedure.json").exists(),
            has_code=(folder / "test.py").exists(),
            has_text=(folder / "procedure_text.md").exists(),
        )

    @property
    def artifact_state(self) -> str:
        """Get a description of artifact state."""
        parts = []
        if self.has_text:
            parts.append("Text")
        if self.has_json:
            parts.append("JSON")
        if self.has_code:
            parts.append("Code")
        return " · ".join(parts) if parts else "Empty"


@dataclass
class ProjectManager:
    """
    Manages project root and rules discovery.
    
    A valid project root contains either:
    - tests/ folder
    - config/ folder
    - or both
    """
    project_root: Optional[Path] = None
    rules_root: Optional[Path] = None
    rules_state: RulesState = RulesState.NOT_CHECKED

    # The rules folder the user picked for the open project (the footer's
    # Rules link); forgotten when another project opens.
    _picked_rules_root: Optional[Path] = field(default=None, repr=False)

    _equipment_patterns_cache: Optional[list[re.Pattern[str]]] = field(default=None, repr=False)

    def set_project_root(self, path: Path) -> bool:
        """
        Set project root if valid.

        Returns True if path is a valid project root.
        """
        if self.is_valid_project_root(path):
            if self.project_root != path:
                self._picked_rules_root = None  # a picked rules folder is that project's
            if self.project_root is not None and self.project_root != path:
                from procedo.studio.pack_ops import engine

                engine.stop(project_root=self.project_root)  # the project switched away
            self.project_root = path
            self._equipment_patterns_cache = None
            return True
        return False
    
    def is_valid_project_root(self, path: Path) -> bool:
        """Check if path is a valid project root."""
        if not path.exists() or not path.is_dir():
            return False
        
        # Valid if contains tests/ or config/
        tests_dir = path / "tests"
        config_dir = path / "config"
        
        return tests_dir.exists() or config_dir.exists()
    
    def detect_project_from_test_folder(self, test_folder: Path) -> Optional[Path]:
        """
        Try to detect project root from a test folder.
        
        If user selected a test folder directly, attempt to find project root.
        """
        # Check if this looks like a test folder
        has_json = (test_folder / "procedure.json").exists()
        has_code = (test_folder / "test.py").exists()
        
        if not (has_json or has_code):
            return None
        
        # Try parent as project root
        parent = test_folder.parent
        if parent.name == "tests":
            grandparent = parent.parent
            if self.is_valid_project_root(grandparent):
                return grandparent
        
        return None
    
    def get_tests_dir(self) -> Optional[Path]:
        """Get the tests directory."""
        if self.project_root is None:
            return None
        tests_dir = self.project_root / "tests"
        return tests_dir if tests_dir.exists() else None
    
    def get_config_dir(self) -> Optional[Path]:
        """Get the config directory."""
        if self.project_root is None:
            return None
        config_dir = self.project_root / "config"
        return config_dir if config_dir.exists() else None

    # Phase 5.1 (2026-05-11): the per-project parser/renderer loader
    # chain (get_text_parser, get_code_parser, get_text_renderer,
    # _load_parser, _last_load_errors, get_parser_load_error) was
    # deleted. Validators and Quick Parse / Quick Code now import from
    # the rules_packager_base wheel directly via
    # procedo.studio.pack_ops.pack_parsers. Project-local
    # config/parsers/<kind>/*.py wrappers are no longer consulted; the
    # folder is left in place for legacy projects but inert.

    def load_equipment_patterns(self) -> list[re.Pattern[str]]:
        """Compiled patterns matching operator-editable bench-config constants
        (``<EID>_VISA = ...``, ``<EID>_BAUD = ...``), used by
        ``sync_utils.normalize_for_hash`` so editing a bench value doesn't trip
        the procedure.json/test.py out-of-sync check.

        SINGLE SOURCE = the pack-declared ``bench_fields`` (read from the bundle
        via ``bundle_capabilities.bench_fields``): one pattern per declared field name,
        so a new pack/field is covered with zero project config. The project
        ``config/config.json`` ``patterns`` / ``profiles.controllers[].override_suffix``
        are still honoured as an optional per-project OVERRIDE (back-compat for
        pre-bench_fields bundles or operator-added custom fields).
        """
        compiled: list[re.Pattern[str]] = []

        # 1. Pack-declared bench fields (single source) → one constant-matching
        #    pattern per declared field name (VISA, BAUD, MANUAL_OVERRIDE, ...).
        try:
            from procedo.studio.pack_ops.bundle_capabilities import bench_fields as _bench_fields
            declared = _bench_fields(self.project_root)
        except Exception:
            declared = {}
        field_names = sorted({
            f["name"].upper()
            for fields in declared.values()
            for f in fields
            if isinstance(f, dict) and isinstance(f.get("name"), str)
        })
        for fname in field_names:
            compiled.append(
                re.compile(rf"^[_A-Za-z][_A-Za-z0-9]*_{re.escape(fname)}\s*=.*", re.MULTILINE)
            )

        # 2. Project config.json patterns / override_suffix — optional override.
        config_dir = self.get_config_dir()
        if config_dir is None:
            return compiled
        config_file = config_dir / "config.json"
        if not config_file.exists():
            return compiled
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Failed to read project config for equipment patterns")
            return compiled

        # Patterns section (visa, remote, baud, etc.)
        patterns = data.get("patterns", {})
        for key, entry in patterns.items():
            regex = entry.get("regex")
            if not regex:
                continue
            flags = 0
            for flag_name in entry.get("flags", []):
                flags |= getattr(re, flag_name, 0)
            try:
                compiled.append(re.compile(regex, flags))
            except re.error:
                log.warning(f"Invalid regex in patterns.{key}: {regex}")

        # Controller override_suffix → build a regex for VARNAME_MANUAL_OVERRIDE = ...
        for ctrl in data.get("profiles", {}).get("controllers", []):
            suffix = ctrl.get("override_suffix")
            if suffix:
                # Match lines like: SOMETHING_MANUAL_OVERRIDE = True/False
                pat = rf"^[_A-Za-z][_A-Za-z0-9]*{re.escape(suffix)}\s*=\s.*"
                try:
                    compiled.append(re.compile(pat, re.MULTILINE))
                except re.error:
                    log.warning(f"Invalid override_suffix pattern: {suffix}")

        return compiled

    def get_equipment_patterns(self) -> list[re.Pattern[str]]:
        """Return cached equipment patterns, loading on first access."""
        if self._equipment_patterns_cache is None:
            self._equipment_patterns_cache = self.load_equipment_patterns()
        return self._equipment_patterns_cache

    def enumerate_test_folders(self) -> list[TestFolderInfo]:
        """
        Enumerate all test folders in the project.
        
        Returns list of TestFolderInfo with artifact status.
        """
        tests_dir = self.get_tests_dir()
        if tests_dir is None:
            return []
        
        return [TestFolderInfo.of(item) for item in sorted(tests_dir.iterdir()) if item.is_dir()]
    
    def detect_rules_root(self) -> bool:
        """Load the open project's rules: the folder the user picked for it, else its
        active bundle's rules (``bundle_registry.active_rules_dir``, as Bench resolves
        them), else none, never what the previous project had. True when rules loaded."""
        from procedo.core import bundle_registry

        location = self._picked_rules_root
        if location is None and self.project_root is not None:
            location = bundle_registry.active_rules_dir(self.project_root)
        if location is not None and not self._is_valid_rules_root(location):
            location = None
        self._use_rules(location)
        log.info("Rules: %s", location or "none")
        return location is not None

    def set_rules_root(self, path: Path) -> bool:
        """Use the rules folder the user picked, for the open project. False (nothing
        changes) when it holds no rule docs."""
        if not self._is_valid_rules_root(path):
            return False
        self._picked_rules_root = path
        self._use_rules(path)
        return True

    def _use_rules(self, path: Optional[Path]) -> None:
        """Use the rule docs of *path*; None: no rules."""
        self.rules_root = path
        self.rules_state = RulesState.LOADED if path is not None else RulesState.NONE
    
    def _is_valid_rules_root(self, path: Path) -> bool:
        """Check if path contains any markdown files."""
        return bool(_bundle_rule_files(path))
    
    def project_rules_dir(self) -> Optional[Path]:
        """``<project>/config/project_rules`` (it may not exist); None without a project."""
        return self.project_root / PROJECT_RULES_DIR if self.project_root is not None else None

    def get_rule_docs(self) -> list[RuleDoc]:
        """Every rule document a view can send, scanned now: the bundle's rules (or the folder
        picked instead), keyed by file name, then the project's own documents, keyed
        ``project/<name>`` so a bundle rule of the same name never clashes."""
        return ([RuleDoc(p.name, p, "bundle") for p in _bundle_rule_files(self.rules_root)]
                + [RuleDoc(PROJECT_RULE_PREFIX + p.name, p, "project")
                   for p in project_rule_doc_paths(self.project_rules_dir())])

    def add_project_rule_doc(self, source: Path) -> RuleDoc:
        """Copy *source* (verbatim) into the project's rules folder, creating it; never over a
        document of the same name. RuleDocError (its message for the user) when it cannot."""
        folder = self.project_rules_dir()
        if folder is None:
            raise RuleDocError("Open a project first.")
        if source not in project_rule_doc_paths(source.parent):  # not a kind the project lists
            raise RuleDocError(f"{source.name}: a project rule document is .md or .txt text, "
                               "or a PDF.")
        if not attached_as_file(source):
            self._check_text_doc(source)
        data = source.read_bytes()
        target = folder / source.name
        try:
            atomic_create(target, data)
        except FileExistsError:
            raise RuleDocError(
                f"The project already has a document named {source.name}: remove it first "
                "(Open folder).") from None
        return RuleDoc(PROJECT_RULE_PREFIX + target.name, target, "project")

    @staticmethod
    def _check_text_doc(source: Path) -> None:
        """RuleDocError unless *source* is UTF-8 text of at most RULE_DOC_MAX_BYTES."""
        size = source.stat().st_size
        if size > RULE_DOC_MAX_BYTES:
            raise RuleDocError(
                f"{source.name} is {kilobytes(size)}: a project rule document can be at "
                f"most {kilobytes(RULE_DOC_MAX_BYTES)}, it is sent with every request.")
        try:
            source.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise RuleDocError(f"{source.name} is not UTF-8 text.") from None

    def get_rules_display(self) -> str:
        """Get rules state for display in window header."""
        if self.rules_state == RulesState.LOADED and self.rules_root:
            return f"Rules: {self.rules_root}"
        elif self.rules_state == RulesState.NONE:
            return "Rules: none"
        else:
            return "Rules: not loaded"
    
    def create_test_folder(self, name: str) -> Optional[Path]:
        """
        Create a new test folder.
        
        Returns the path to the created folder, or None on failure.
        """
        tests_dir = self.get_tests_dir()
        if tests_dir is None:
            return None
        
        new_folder = tests_dir / name
        if new_folder.exists():
            return None  # Already exists
        
        try:
            new_folder.mkdir(parents=True)
            # Seed the canonical artifacts so the new test starts with
            # id == sanitized folder name (folder is the source of truth;
            # save-time enforcement keeps it that way afterwards).
            from .test_id import sanitize_test_id
            test_id = sanitize_test_id(new_folder.name)
            (new_folder / "procedure_text.md").write_text(
                f"# {test_id}\n", encoding="utf-8"
            )
            starter = {
                "id": test_id,
                "description": "",
                "equipment": [],
                "steps": [],
                "expected": [],
            }
            (new_folder / "procedure.json").write_text(
                json.dumps(starter, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return new_folder
        except OSError:
            return None
    
