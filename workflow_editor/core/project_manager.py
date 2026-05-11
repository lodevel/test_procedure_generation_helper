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
from typing import Optional
from enum import Enum, auto

log = logging.getLogger(__name__)


class RulesState(Enum):
    """State of rules loading."""
    NOT_CHECKED = auto()
    LOADED = auto()
    NONE = auto()  # User chose to continue without rules


@dataclass
class TestFolderInfo:
    """Information about a test folder."""
    path: Path
    name: str
    has_json: bool = False
    has_code: bool = False
    has_text: bool = False
    
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
        return "+".join(parts) if parts else "Empty"


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
    
    # Cached rules content
    _rules_content: Optional[str] = field(default=None, repr=False)
    _rules_files: list[Path] = field(default_factory=list, repr=False)
    _equipment_patterns_cache: Optional[list[re.Pattern[str]]] = field(default=None, repr=False)
    # Per-kind diagnostic for the last _load_parser failure. Populated by
    # _load_parser when import / instantiation crashes so callers (notably
    # is_loop_available) can surface the real error instead of the
    # generic "no variant configured" message.
    _last_load_errors: dict[str, str] = field(default_factory=dict, repr=False)
    
    def set_project_root(self, path: Path) -> bool:
        """
        Set project root if valid.
        
        Returns True if path is a valid project root.
        """
        if self.is_valid_project_root(path):
            self.project_root = path
            self._equipment_patterns_cache = None
            # Drop diagnostics from any previous project — a stale
            # "wheel out of date" message from a different project must
            # not leak into is_loop_available for the new one.
            self._last_load_errors.clear()
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
    
    def create_project_structure(
        self,
        project_path: Path,
        create_config: bool = False,
        create_readme: bool = False
    ) -> bool:
        """
        Create a new project folder structure.
        
        Args:
            project_path: Path where project should be created
            create_config: If True, creates an (empty) config/ folder. Workflow defaults are written lazily by TaskConfigManager on first save.
            create_readme: If True, creates a basic README.md file
            
        Returns:
            True if successful, False otherwise
        """
        import logging
        import json
        from .task_config import DEFAULT_TASK_CONFIGS, DEFAULT_CHAT_CONFIG
        
        log = logging.getLogger(__name__)
        
        try:
            # Create project root
            project_path.mkdir(parents=True, exist_ok=True)
            log.info(f"Created project folder: {project_path}")
            
            # Create tests/ folder (required)
            tests_dir = project_path / "tests"
            tests_dir.mkdir(exist_ok=True)
            log.info(f"Created tests/ folder: {tests_dir}")
            
            # Create rules/ folder (required)
            rules_dir = project_path / "rules"
            rules_dir.mkdir(exist_ok=True)
            log.info(f"Created rules/ folder: {rules_dir}")
            
            # Add a README to the rules folder to guide users
            rules_readme = rules_dir / "README.md"
            if not rules_readme.exists():
                rules_readme_content = """# Rules Folder

This folder contains markdown files with rules and guidance for the LLM.

## How to Use

1. Add `.md` files to this folder with your rules, guidelines, or context
2. The LLM will use these rules when generating test procedures
3. Rules are automatically loaded when you open the project

## Example Rules

You can include:
- Coding standards and conventions
- Test procedure templates
- Domain-specific guidelines
- Safety or compliance requirements
"""
                with open(rules_readme, 'w', encoding='utf-8') as f:
                    f.write(rules_readme_content)
                log.info(f"Created rules/README.md: {rules_readme}")
            
            # Create config/ folder if requested. Workflow defaults live
            # in the project's ``config/config.json:workflows`` section
            # and are written by ``TaskConfigManager.save_config()`` on
            # demand — no seed file needed at create time.
            if create_config:
                config_dir = project_path / "config"
                config_dir.mkdir(exist_ok=True)
                log.info(f"Created config/ folder: {config_dir}")
            
            # Create README.md if requested
            if create_readme:
                readme_path = project_path / "README.md"
                if not readme_path.exists():
                    readme_content = f"""# {project_path.name}

Test procedure project created with Workflow Editor.

## Project Structure

- `tests/` - Test procedure folders (each test has its own subfolder)
  - Each test folder contains:
    - `procedure_text.md` - Human-readable test description
    - `procedure.json` - Structured JSON test procedure
    - `test.py` - Python test implementation
- `rules/` - LLM rules and guidance (markdown files)
  - Add `.md` files here to guide test procedure generation
- `config/` - Configuration files (``config.json:workflows`` holds per-tab tasks and chat settings)

## Getting Started

1. Open this project in the Workflow Editor
2. Create a new test using the Workspace tab
3. Use the Text-JSON, JSON-Code, and Traceability tabs to generate test artifacts
4. Select rules for LLM guidance (File → Settings → Rules)

## Workflow

1. **Text-JSON Tab**: Generate JSON procedure from text description
2. **JSON-Code Tab**: Generate Python test code from JSON procedure
3. **Traceability Tab**: Verify alignment between artifacts
"""
                    with open(readme_path, 'w', encoding='utf-8') as f:
                        f.write(readme_content)
                    log.info(f"Created README.md: {readme_path}")
            
            # Set as current project root
            self.project_root = project_path
            log.info(f"Project created successfully: {project_path}")
            return True
            
        except Exception as e:
            log.error(f"Failed to create project structure: {e}")
            return False
    
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

    def get_text_parser(self):
        """Load the project's selected text-parser variant.

        Resolves ``config/config.json -> parsers.json_parser`` to
        ``config/parsers/json_parser/<value>.py``; falls back to the
        legacy ``config/text_parser.py`` for projects seeded before the
        ``parsers/`` layout existed. The loaded module must define
        ``class ProcedureTextParser`` with a
        ``parse(text: str) -> tuple[dict, list[str]]`` method.

        See :meth:`_load_parser` for details.
        """
        return self._load_parser(
            "json_parser", "ProcedureTextParser",
            legacy_filename="text_parser.py",
        )

    def get_code_parser(self):
        """Load the project's selected code-parser variant.

        Resolves ``config/config.json -> parsers.code_parser`` to
        ``config/parsers/code_parser/<value>.py``. The loaded module
        must define ``class ProcedureCodeParser`` with a
        ``parse(procedure: dict) -> tuple[str, list[str]]`` method that
        returns Python source for the test file plus non-fatal warning
        messages to surface in the UI.

        See :meth:`_load_parser` for details.
        """
        return self._load_parser("code_parser", "ProcedureCodeParser")

    def get_text_renderer(self):
        """Load the project's selected text-renderer variant.

        Resolves ``config/config.json -> parsers.text_renderer`` to
        ``config/parsers/text_renderer/<value>.py``. The loaded module
        must define ``class ProcedureTextRenderer`` exposing:

          - ``validate(text=None, json_obj=None, mode='all') -> Report``
            where ``Report`` has ``.ok: bool`` and ``.errors: list``;
            each error exposes ``.code``, ``.message``, ``.severity``,
            ``.location``, ``.fix_hint``, ``.fixable_by`` (the same shape
            ``llm/validator_dispatch.py`` reads from the legacy
            ``bijective_validator``).
          - ``render(json_obj) -> str`` (Phase 2 — may raise
            NotImplementedError until canonical-text emission lands in
            the wheel).

        Returns ``None`` when no variant is configured. The workflow
        editor's validator dispatch falls back to the legacy
        ``bijective_validator`` path in that case.
        """
        return self._load_parser("text_renderer", "ProcedureTextRenderer")

    def _load_parser(
        self,
        kind: str,
        class_name: str,
        *,
        legacy_filename: Optional[str] = None,
    ):
        """Generic loader for a project-supplied parser plugin.

        Resolution order:

        1. Read ``config/config.json`` and look up ``parsers.<kind>``.
           If set, load ``config/parsers/<kind>/<value>.py``.
        2. (Optional) Fall back to ``config/<legacy_filename>`` if
           provided and the primary path resolved nothing — preserves
           backward compatibility for kinds that existed before the
           ``parsers/`` layout.

        The loaded module must define a class named *class_name*
        exposing a ``parse`` method. No caching: each call re-reads the
        file so developers editing a parser variant see the effect on
        the next user action without restarting the editor.

        Returns an instantiated parser on success, or ``None`` when no
        parser is configured / the file is missing / loading fails (in
        which case the consuming UI hides its action button and a
        warning is logged).
        """
        import importlib.util
        import sys

        # Reset the diagnostic for this kind before re-probing.
        self._last_load_errors.pop(kind, None)

        config_dir = self.get_config_dir()
        if config_dir is None:
            return None

        parser_path: Optional[Path] = None

        # Preferred: customer-template selection in config.json.
        config_file = config_dir / "config.json"
        if config_file.exists():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"Failed to read project config.json for parser selection: {e}")
                cfg = {}
            selected = (cfg.get("parsers") or {}).get(kind)
            if selected:
                candidate = config_dir / "parsers" / kind / f"{selected}.py"
                if candidate.exists():
                    parser_path = candidate
                else:
                    msg = (
                        f"parsers.{kind}='{selected}' selected but file "
                        f"not found at {candidate}"
                    )
                    log.warning(msg + "; falling back to legacy.")
                    self._last_load_errors[kind] = msg

        # Legacy fallback for kinds that pre-date the parsers/ layout.
        if parser_path is None and legacy_filename:
            legacy = config_dir / legacy_filename
            if legacy.exists():
                parser_path = legacy

        if parser_path is None:
            return None

        # Namespace the dynamic module by absolute path so switching
        # projects in a single session does not reuse a stale module
        # cached under a shared name.
        module_name = f"_project_parser_{kind}_{abs(hash(str(parser_path.resolve())))}"
        sys.modules.pop(module_name, None)

        try:
            spec = importlib.util.spec_from_file_location(module_name, parser_path)
            if spec is None or spec.loader is None:
                msg = f"Could not build import spec for parser at {parser_path}"
                log.warning(msg)
                self._last_load_errors[kind] = msg
                return None
            module = importlib.util.module_from_spec(spec)
            # Register before exec_module so @dataclass / inspect / typing
            # machinery that does sys.modules.get(cls.__module__) finds the
            # module. Required since Python 3.13 (dataclass._is_type calls
            # sys.modules.get(...).__dict__). Cleaned up on next reload via
            # the sys.modules.pop above.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except SyntaxError as e:
            msg = f"Syntax error in {parser_path.name}: {e}"
            log.warning(f"{kind}: {msg}")
            self._last_load_errors[kind] = msg
            return None
        except ImportError as e:
            msg = f"Import error in {parser_path.name}: {e}"
            log.warning(f"{kind}: {msg}")
            self._last_load_errors[kind] = msg
            return None
        except Exception as e:
            msg = f"Failed to load {parser_path.name}: {e}"
            log.warning(f"{kind}: {msg}")
            self._last_load_errors[kind] = msg
            return None

        cls = getattr(module, class_name, None)
        if cls is None:
            msg = f"{parser_path.name} has no {class_name} class"
            log.warning(f"{kind}: {msg}")
            self._last_load_errors[kind] = msg
            return None
        try:
            return cls()
        except Exception as e:
            msg = f"{class_name}.__init__ failed in {parser_path.name}: {e}"
            log.warning(msg)
            self._last_load_errors[kind] = msg
            return None

    def get_parser_load_error(self, kind: str) -> Optional[str]:
        """Return the most recent load-failure reason for a parser kind
        (e.g. ``"text_renderer"``). ``None`` means either no failure was
        recorded yet, or the loader has not been called for this kind.
        Consumed by :func:`workflow_editor.llm.validator_dispatch.is_loop_available`
        to surface the real failure to the operator instead of a generic
        "no variant configured" message.
        """
        return self._last_load_errors.get(kind)

    def load_equipment_patterns(self) -> list[re.Pattern[str]]:
        """Load equipment-configuration patterns from the project config.

        Reads ``config/config.json`` → ``patterns`` section (visa, remote,
        baud regexes) and ``profiles.controllers[].override_suffix``.
        Returns compiled patterns suitable for ``sync_utils.normalize_for_hash``.
        """
        config_dir = self.get_config_dir()
        if config_dir is None:
            return []
        config_file = config_dir / "config.json"
        if not config_file.exists():
            return []
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Failed to read project config for equipment patterns")
            return []

        compiled: list[re.Pattern[str]] = []

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
        
        folders = []
        
        for item in sorted(tests_dir.iterdir()):
            if not item.is_dir():
                continue
            
            info = TestFolderInfo(
                path=item,
                name=item.name,
                has_json=(item / "procedure.json").exists(),
                has_code=(item / "test.py").exists(),
                has_text=(item / "procedure_text.md").exists(),
            )
            folders.append(info)
        
        return folders
    
    def detect_rules_root(self, cli_rules_root: Optional[Path] = None) -> bool:
        """
        Detect rules root following fallback order:
        
        1. CLI --rules-root if provided
        2. <project_root>/config/rules/ (spec default)
        3. <project_root>/rules/ (common alternative)
        4. <project_root>/../rules/ (sibling folder)
        5. Return False to indicate user prompt needed
        
        Returns True if rules were found, False if user needs to choose.
        """
        import logging
        log = logging.getLogger(__name__)
        
        # 1. CLI argument
        if cli_rules_root is not None:
            if self._is_valid_rules_root(cli_rules_root):
                self.rules_root = cli_rules_root
                self.rules_state = RulesState.LOADED
                self._load_rules()
                log.info(f"Rules loaded from CLI arg: {cli_rules_root}")
                return True
            else:
                log.warning(f"CLI rules root invalid: {cli_rules_root}")
        
        # 2-4. Fallback locations
        if self.project_root is not None:
            fallback_locations = [
                self.project_root / "config" / "rules",  # Spec default
                self.project_root / "rules",              # Common alternative
                self.project_root.parent / "rules",       # Sibling folder
            ]
            
            for location in fallback_locations:
                if self._is_valid_rules_root(location):
                    self.rules_root = location
                    self.rules_state = RulesState.LOADED
                    self._load_rules()
                    log.info(f"Rules auto-detected: {location}")
                    return True
        
        # 5. Not found - user prompt needed
        log.warning("Rules not found in any fallback location")
        return False
    
    def set_rules_root(self, path: Optional[Path]) -> bool:
        """
        Set rules root from user selection.
        
        Pass None to indicate "Continue without rules".
        """
        if path is None:
            self.rules_root = None
            self.rules_state = RulesState.NONE
            self._rules_content = None
            self._rules_files = []
            return True
        
        if self._is_valid_rules_root(path):
            self.rules_root = path
            self.rules_state = RulesState.LOADED
            self._load_rules()
            return True
        
        return False
    
    def _is_valid_rules_root(self, path: Path) -> bool:
        """Check if path contains any markdown files."""
        if not path.exists() or not path.is_dir():
            return False
        
        md_files = list(path.glob("*.md"))
        return len(md_files) > 0
    
    def _load_rules(self) -> None:
        """Load and concatenate all rule markdown files."""
        if self.rules_root is None:
            self._rules_content = None
            self._rules_files = []
            return
        
        self._rules_files = sorted(self.rules_root.glob("*.md"))
        
        contents = []
        for md_file in self._rules_files:
            header = f"\n{'='*60}\n# Rules from: {md_file.name}\n{'='*60}\n"
            content = md_file.read_text(encoding="utf-8")
            contents.append(header + content)
        
        self._rules_content = "\n".join(contents)
    
    def get_rules_content(self) -> Optional[str]:
        """Get concatenated rules content for LLM prompts."""
        return self._rules_content
    
    def get_rules_files(self) -> list[Path]:
        """Get list of rule files."""
        return self._rules_files.copy()
    
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
            return new_folder
        except OSError:
            return None
    
