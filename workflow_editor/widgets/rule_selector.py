"""
Rule Selector Widget - UI for selecting which rules to include per tab.

Provides checkboxes for each rule file, allowing users to manually
select which rules are included in the LLM context for each tab.
"""

from typing import TYPE_CHECKING
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox,
    QPushButton, QLabel, QScrollArea, QFrame, QGroupBox,
    QDialog, QDialogButtonBox
)
from PySide6.QtCore import Signal, Qt

if TYPE_CHECKING:
    from ..core import ProjectManager


class RuleSelectorWidget(QGroupBox):
    """
    Widget for selecting rules to include in tab context.
    
    Features:
    - Checkbox for each rule file
    - Select All / Clear All buttons
    - Persists selections to config via ProjectManager
    """
    
    # Signal emitted when rule selection changes
    rules_changed = Signal(list)  # List of selected rule filenames
    
    def __init__(self, tab_id: str, project_manager: "ProjectManager", parent=None):
        """
        Initialize rule selector.
        
        Args:
            tab_id: Tab identifier ("text_json", "json_code")
            project_manager: ProjectManager for loading/saving config
            parent: Parent widget
        """
        super().__init__("Rule Selection", parent)
        self.tab_id = tab_id
        self.project_manager = project_manager
        self.checkboxes: dict[str, QCheckBox] = {}
        
        self._setup_ui()
        self._load_rules()
    
    def _setup_ui(self):
        """Setup the UI layout."""
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        
        # Instructions
        info_label = QLabel("Select which rules to include in LLM context:")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(info_label)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._on_select_all)
        button_layout.addWidget(self.select_all_btn)
        
        self.clear_all_btn = QPushButton("Clear All")
        self.clear_all_btn.clicked.connect(self._on_clear_all)
        button_layout.addWidget(self.clear_all_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        # Scroll area for checkboxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(200)  # Limit height
        
        # Container for checkboxes
        self.checkbox_container = QWidget()
        self.checkbox_layout = QVBoxLayout(self.checkbox_container)
        self.checkbox_layout.setContentsMargins(5, 5, 5, 5)
        self.checkbox_layout.setSpacing(3)
        
        scroll.setWidget(self.checkbox_container)
        layout.addWidget(scroll)
        
        # Rule count label
        self.count_label = QLabel("0 rules selected")
        self.count_label.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(self.count_label)
    
    def _load_rules(self):
        """Load available rules and restore selections from config."""
        # Clear existing checkboxes
        for checkbox in self.checkboxes.values():
            checkbox.deleteLater()
        self.checkboxes.clear()
        
        # Get available rule files
        rule_files = self.project_manager.get_rules_files()
        
        if not rule_files:
            no_rules_label = QLabel("No rules available")
            no_rules_label.setStyleSheet("color: gray; font-style: italic;")
            self.checkbox_layout.addWidget(no_rules_label)
            self._update_count()
            return
        
        # Load current selections from config
        config = self.project_manager.load_tab_contexts_config()
        tab_config = config.get(self.tab_id, {"selected_rules": "all"})
        selected_rules = self.project_manager.get_expanded_selected_rules(tab_config)
        
        # Create checkbox for each rule
        for rule_file in rule_files:
            filename = rule_file.name
            checkbox = QCheckBox(filename)
            checkbox.setChecked(filename in selected_rules)
            checkbox.stateChanged.connect(self._on_selection_changed)
            
            self.checkbox_layout.addWidget(checkbox)
            self.checkboxes[filename] = checkbox
        
        self.checkbox_layout.addStretch()
        self._update_count()
    
    def _on_selection_changed(self):
        """Handle checkbox state change."""
        selected = self.get_selected_rules()
        self._update_count()
        self._save_selections(selected)
        self.rules_changed.emit(selected)
    
    def _on_select_all(self):
        """Select all rules."""
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(True)
    
    def _on_clear_all(self):
        """Clear all selections."""
        for checkbox in self.checkboxes.values():
            checkbox.setChecked(False)
    
    def get_selected_rules(self) -> list[str]:
        """
        Get list of selected rule filenames.
        
        Returns:
            List of filenames (e.g., ['rule1.md', 'rule2.md'])
        """
        return [
            filename
            for filename, checkbox in self.checkboxes.items()
            if checkbox.isChecked()
        ]
    
    def _save_selections(self, selected_rules: list[str]):
        """
        Save current selections to config.
        
        Args:
            selected_rules: List of selected rule filenames
        """
        # Load config
        config = self.project_manager.load_tab_contexts_config()
        
        # Update this tab's selections
        config[self.tab_id]["selected_rules"] = selected_rules
        
        # Save config
        self.project_manager.save_tab_contexts_config(config)
    
    def _update_count(self):
        """Update the rule count label."""
        selected_count = len(self.get_selected_rules())
        total_count = len(self.checkboxes)
        self.count_label.setText(f"{selected_count}/{total_count} rules selected")
    
    def reload_rules(self):
        """Reload rules from project manager (call after project changes)."""
        self._load_rules()


class RuleSelectorDialog(QDialog):
    """
    Dialog for selecting rules to include in tab context.
    
    Contains RuleSelectorWidget with OK/Cancel buttons.
    Returns selected rules when accepted.
    """
    
    def __init__(self, tab_id: str, project_manager: "ProjectManager", parent=None):
        """
        Initialize rule selector dialog.
        
        Args:
            tab_id: Tab identifier ("text_json", "json_code")
            project_manager: ProjectManager for loading/saving config
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle(f"Select Rules - {tab_id}")
        self.resize(500, 400)
        
        self.tab_id = tab_id
        self.project_manager = project_manager
        
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Info label
        info = QLabel(f"Select which rules to include in the LLM context for the {self.tab_id} tab:")
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # Rule selector widget
        self.rule_selector = RuleSelectorWidget(self.tab_id, self.project_manager, self)
        layout.addWidget(self.rule_selector)
        
        # OK/Cancel buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def get_selected_rules(self) -> list[str]:
        """
        Get the currently selected rules.
        
        Returns:
            List of selected rule filenames
        """
        return self.rule_selector.get_selected_rules()
