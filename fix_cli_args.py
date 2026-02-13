from pathlib import Path

# Read the file
file_path = Path(r"workflow_editor\main_window.py")
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the line with "self.dock.setEnabled(False)"
insert_after_idx = None
for i, line in enumerate(lines):
    if 'self.dock.setEnabled(False)' in line:
        insert_after_idx = i
        break

if insert_after_idx is None:
    print("ERROR: Could not find insertion point")
    exit(1)

# Create the new methods
new_code = '''
    def showEvent(self, event):
        """Handle window show event - process CLI arguments after UI is ready."""
        super().showEvent(event)
        
        # Process CLI arguments on first show only
        if hasattr(self, '_cli_args_processed'):
            return
        self._cli_args_processed = True
        
        # Process CLI arguments to load project/test
        self._process_cli_arguments()
    
    def _process_cli_arguments(self):
        """Process command-line arguments to load project and/or test."""
        try:
            # Step 1: Set project root (if provided)
            if self._cli_project_root:
                log.info(f"Loading project from CLI arg: {self._cli_project_root}")
                if not self.project_manager.set_project_root(self._cli_project_root):
                    log.error(f"Failed to set project root: {self._cli_project_root}")
                    return
                
                # Update workspace widget
                self.workspace_widget._load_test_list()
                self.workspace_widget.new_test_btn.setEnabled(True)
                
                # Detect rules
                self.project_manager.detect_rules_root()
                self._update_project_rules_indicators()
            
            # Step 2: Determine which test to open
            test_dir_to_open = None
            
            if self._cli_test_dir:
                # Direct test path (highest priority)
                test_dir_to_open = self._cli_test_dir
                log.info(f"Opening test from --test-dir: {test_dir_to_open}")
            elif self._cli_project_root and self._cli_test_name:
                # Test name under project root
                test_dir_to_open = self._cli_project_root / "tests" / self._cli_test_name
                log.info(f"Opening test from --test-name: {test_dir_to_open}")
            
            # Step 3: Open the test if determined
            if test_dir_to_open and test_dir_to_open.exists():
                # Ensure project is set if not already
                if not self.project_manager.project_root and self._cli_project_root:
                    self.project_manager.set_project_root(self._cli_project_root)
                
                # Open the test
                self.workspace_widget.test_opened.emit(test_dir_to_open)
                log.info(f"Test opened from CLI args: {test_dir_to_open}")
            elif test_dir_to_open:
                log.warning(f"Test directory does not exist: {test_dir_to_open}")
        
        except Exception as e:
            log.error(f"Error processing CLI arguments: {e}", exc_info=True)

'''

# Insert after the line
new_lines = lines[:insert_after_idx+1] + [new_code] + lines[insert_after_idx+1:]

# Write back
with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("SUCCESS: Added showEvent and _process_cli_arguments to MainWindow")
