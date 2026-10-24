"""
CLI entry point for LLM Workflow Editor.

Usage:
    python -m procedo.studio [options]
    procedo-studio.exe [options]

Options:
    --project-root <path>       Path to project root (contains tests/ and/or config/)
    --test-name <name>          Name of test folder to open
    --test-dir <path>           Direct path to test folder (overrides --test-name)
    --llm-backend <backend>     LLM backend: opencode|none
    --llm-profile <name>        LLM profile name (optional)
"""

import sys
import argparse
import logging
import os
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

# Dev-convenience shim (deprecated): only for STANDALONE runs launched from
# inside a host checkout — walks up to the host's src/ so `procedo.core`
# resolves. Embedded runs get it from the host venv's editable installs. The host is
# required: Studio wears Bench's theme (procedo.ui.theme).
_BOOTSTRAPPED_SRC: str | None = None


def _bootstrap_procedo_core() -> None:
    global _BOOTSTRAPPED_SRC
    import importlib.util
    try:
        if importlib.util.find_spec("procedo.core") is not None:
            return
    except ModuleNotFoundError:  # no `procedo` at all (find_spec imports the parent)
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "procedo" / "core" / "__init__.py").is_file():
            _BOOTSTRAPPED_SRC = str(parent / "src")
            logging.getLogger(__name__).debug(
                "procedo.core dev-shim: sys.path += %s", _BOOTSTRAPPED_SRC)
            sys.path.insert(0, _BOOTSTRAPPED_SRC)
            return


_bootstrap_procedo_core()


from .main_window import MainWindow
from procedo.ui.theme import apply_app_theme


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="procedo.studio",
        description="Procedo Studio for structured test procedures"
    )
    
    parser.add_argument(
        "--project-root",
        type=Path,
        help="Path to project root (contains tests/ and/or config/)"
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
        choices=["opencode", "none"],
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
    
    log = logging.getLogger("procedo.studio.__main__")
    if _BOOTSTRAPPED_SRC:
        log.debug("procedo.core resolved via dev-shim walk-up: %s",
                  _BOOTSTRAPPED_SRC)
    log.info("=" * 50)
    log.info("Procedo Studio starting")
    log.debug(f"CLI args: {args}")

    from procedo.core import app_identity, temp_root
    
    # Validate arguments
    if args.test_dir and args.test_name:
        log.warning("--test-dir overrides --test-name")
        print("Warning: --test-dir overrides --test-name", file=sys.stderr)
    
    # Create application
    log.debug("Creating QApplication...")
    app_identity.claim_taskbar_identity(app_identity.STUDIO_APP_ID)
    app = QApplication(sys.argv)
    temp_root.start(app)
    app.setApplicationName("Procedo Studio")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName(app_identity.SETTINGS_ORGANIZATION)
    log.debug("QApplication created")
    
    # Enable high DPI scaling
    app.setAttribute(Qt.AA_UseHighDpiPixmaps)
    from procedo.core import studio_launch_env
    applied_theme = apply_app_theme(os.environ.get(studio_launch_env.APP_THEME), app)
    log.debug("Applied application theme: %s", applied_theme)
    # Every error and warning box gets a Copy button (title, text, details to the clipboard).
    from procedo.ui import error_copy
    error_copy.install_error_copy(app)

    # Create and show main window
    log.info("Creating MainWindow...")
    window = MainWindow(
        project_root=args.project_root,
        test_name=args.test_name,
        test_dir=args.test_dir,
        llm_backend=args.llm_backend,
        llm_profile=args.llm_profile
    )
    log.info("MainWindow created successfully")
    
    log.info("Showing main window")
    window.showMaximized()
    
    # Run event loop
    log.info("Starting Qt event loop")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
