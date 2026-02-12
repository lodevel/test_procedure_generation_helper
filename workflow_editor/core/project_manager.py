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
    
    def set_project_root(self, path: Path) -> bool:
        """
        Set project root if valid.
        
        Returns True if path is a valid project root.
        """
        if self.is_valid_project_root(path):
            self.project_root = path
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
            create_config: If True, creates config/ folder with default tab_contexts.json
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
            
            # Create config/ folder with default tab_contexts.json if requested
            if create_config:
                config_dir = project_path / "config"
                config_dir.mkdir(exist_ok=True)
                log.info(f"Created config/ folder: {config_dir}")
                
                # Create default tab_contexts.json
                tab_contexts_path = config_dir / "tab_contexts.json"
                if not tab_contexts_path.exists():
                    default_config = {}
                    for tab_id in ["text_json", "json_code"]:
                        tasks = DEFAULT_TASK_CONFIGS.get(tab_id, [])
                        chat_config = DEFAULT_CHAT_CONFIG.get(tab_id)
                        
                        default_config[tab_id] = {
                            "tasks": [
                                {
                                    "name": task.name,
                                    "button_label": task.button_label,
                                    "prompt_template": task.prompt_template,
                                    "enabled": task.enabled
                                }
                                for task in tasks
                            ],
                            "chat_config": {
                                "enabled": chat_config.enabled if chat_config else True,
                                "system_prompt": chat_config.system_prompt if chat_config else None
                            }
                        }
                    
                    with open(tab_contexts_path, 'w', encoding='utf-8') as f:
                        json.dump(default_config, f, indent=2)
                    log.info(f"Created tab_contexts.json: {tab_contexts_path}")
            
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
- `config/` - Configuration files
  - `tab_contexts.json` - Task and chat configurations per tab

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
    
    def get_tab_contexts_config_path(self) -> Optional[Path]:
        """Get the path to tab_contexts.json config file."""
        log.info(f"ProjectManager: get_tab_contexts_config_path() called")
        log.info(f"ProjectManager: project_root = {self.project_root}")
        if self.project_root is None:
            log.warning("ProjectManager: project_root is None! Cannot determine config path.")
            return None
        
        config_dir = self.project_root / "config"
        config_dir.mkdir(exist_ok=True)
        
        config_path = config_dir / "tab_contexts.json"
        log.info(f"ProjectManager: config_path = {config_path}")
        log.info(f"ProjectManager: config file exists = {config_path.exists()}")
        return config_path
    
    def load_tab_contexts_config(self) -> dict:
        """
        Load tab contexts configuration from config/tab_contexts.json.
        
        Returns:
            Dictionary with tab configurations. If file doesn't exist,
            returns default config with all rules selected.
        """
        log.info("ProjectManager: load_tab_contexts_config() called")
        config_path = self.get_tab_contexts_config_path()
        log.info(f"ProjectManager: config_path = {config_path}")
        
        # Default config - all rules selected for all tabs
        default_config = {
            "text_json": {
                "selected_rules": "all"  # Will be expanded to list of rule filenames
            },
            "json_code": {
                "selected_rules": "all"
            }
        }
        
        if config_path is None:
            log.warning("ProjectManager: config_path is None! Returning default config with 'all' rules.")
            return default_config
        
        if not config_path.exists():
            log.warning(f"ProjectManager: Config file does not exist: {config_path}")
            log.warning("ProjectManager: Returning default config with 'all' rules.")
            return default_config
        
        try:
            log.info(f"ProjectManager: Reading config file: {config_path}")
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                log.info(f"ProjectManager: Config file content:\n{content}")
                config = json.loads(content)
            
            log.info(f"ProjectManager: Parsed config = {config}")
            
            # Validate structure - ensure all tabs exist
            for tab_id in ["text_json", "json_code"]:
                if tab_id not in config:
                    log.warning(f"ProjectManager: tab_id '{tab_id}' missing from config, adding default")
                    config[tab_id] = default_config[tab_id].copy()
            
            log.info(f"ProjectManager: Final config after validation = {config}")
            return config
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"Failed to load tab_contexts.json: {e}, using defaults")
            return default_config
    
    def save_tab_contexts_config(self, config: dict) -> bool:
        """
        Save tab contexts configuration to config/tab_contexts.json.
        
        Args:
            config: Dictionary with tab configurations
            
        Returns:
            True if saved successfully, False otherwise
        """
        config_path = self.get_tab_contexts_config_path()
        if config_path is None:
            return False
        
        try:
            # Ensure config directory exists
            config_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            
            return True
        except OSError as e:
            log.error(f"Failed to save tab_contexts.json: {e}")
            return False
    
    def get_expanded_selected_rules(self, tab_config: dict) -> list[str]:
        """
        Expand 'all' to list of rule filenames.
        
        Args:
            tab_config: Tab configuration dict with 'selected_rules' field
            
        Returns:
            List of rule filenames (e.g., ['rule1.md', 'rule2.md'])
        """
        log.info(f"ProjectManager: get_expanded_selected_rules() called with tab_config = {tab_config}")
        selected = tab_config.get("selected_rules", "all")
        log.info(f"ProjectManager: selected_rules value = {repr(selected)}")
        
        if selected == "all":
            log.info("ProjectManager: 'all' detected - expanding to all rule files")
            all_rules = [f.name for f in self.get_rules_files()]
            log.info(f"ProjectManager: Expanded to {len(all_rules)} rules: {all_rules}")
            return all_rules
        
        # selected should be a list of filenames
        if isinstance(selected, list):
            log.info(f"ProjectManager: List detected - returning {len(selected)} rules: {selected}")
            return selected
        
        log.warning(f"ProjectManager: Unexpected selected_rules type: {type(selected)} - returning empty list")
        return []

