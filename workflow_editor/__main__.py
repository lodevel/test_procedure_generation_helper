"""
CLI entry point for LLM Workflow Editor.

Usage:
    python -m workflow_editor [options]
    workflow_editor.exe [options]

Options:
    --project-root <path>       Path to project root (contains tests/ and/or config/)
    --rules-root <path>         Path to rules folder (optional)
    --test-name <name>          Name of test folder to open
    --test-dir <path>           Direct path to test folder (overrides --test-name)
    --llm-backend <backend>     LLM backend: opencode|external|none
    --llm-profile <name>        LLM profile name (optional)
"""

import sys
import argparse
import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from .main_window import MainWindow


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="workflow_editor",
        description="LLM Workflow Editor for structured test procedures"
    )
    
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Path to project root (contains tests/ and/or config/)"
    )
    parser.add_argument(
        "--rules-root",
        type=Path,
        help="Path to rules folder containing *.md files"
    )
    parser.add_argument(
        "--test-name",
        type=str,
        help="Name of test folder to open under tests/"
    )
    parser.add_argument(
        "--test-dir",
        type=Path,
        help="Direct path to test folder (overrides --test-name)"
    )
    parser.add_argument(
        "--llm-backend",
        choices=["opencode", "external", "none"],
        default=None,
        help="LLM backend to use"
    )
    parser.add_argument(
        "--llm-profile",
        type=str,
        help="LLM profile name"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Path to log file"
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging FIRST
    from .logging_config import setup_logging
    setup_logging(debug=args.debug, log_file=args.log_file)
    
    log = logging.getLogger("workflow_editor.__main__")
    log.info("=" * 50)
    log.info("Workflow Editor starting")
    log.debug(f"CLI args: {args}")
    
    # Validate arguments
    if args.test_dir and args.test_name:
        log.warning("--test-dir overrides --test-name")
        print("Warning: --test-dir overrides --test-name", file=sys.stderr)
    
    # Create application
    log.debug("Creating QApplication...")
    app = QApplication(sys.argv)
    app.setApplicationName("LLM Workflow Editor")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("TestProcedureHelper")
    log.debug("QApplication created")
    
    # Enable high DPI scaling
    app.setAttribute(Qt.AA_UseHighDpiPixmaps)
    
    # Create and show main window
    log.info("Creating MainWindow...")
    window = MainWindow(
        project_root=args.project_root,
        rules_root=args.rules_root,
        test_name=args.test_name,
        test_dir=args.test_dir,
        llm_backend=args.llm_backend,
        llm_profile=args.llm_profile
    )
    log.info("MainWindow created successfully")
    
    log.info("Showing main window")
    window.show()
    
    # Run event loop
    log.info("Starting Qt event loop")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
