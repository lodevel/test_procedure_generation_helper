"""
Settings Dialog - Application configuration.

Implements Section 12.2 of the spec with unified task management.
"""

import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QWidget, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox,
    QPushButton, QLabel, QGroupBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QSplitter
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFont

from .. import theme
from ..core.task_config import TaskConfig, TaskConfigManager, ChatConfig
from ..llm.backend_base import LLMTask
from ..llm.server_manager import fetch_opencode_models
from ..theme import muted_text
from ..core.odb_inspect import load_hide_prefixes, save_hide_prefixes

log = logging.getLogger(__name__)


def get_settings_path() -> Path:
    """Get the settings file path."""
    # Use user's home directory
    home = Path.home()
    settings_dir = home / ".workflow_editor"
    settings_dir.mkdir(exist_ok=True)
    return settings_dir / "settings.json"


def load_settings() -> dict:
    """Load settings from file."""
    path = get_settings_path()
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(settings: dict):
    """Save settings to file."""
    path = get_settings_path()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


def get_opencode_config_dir() -> Path:
    """The editor-OWNED OpenCode config directory (sibling of settings.json).

    Holds the project-agnostic ``master.json`` blueprint and the per-launch
    ``launch/opencode.json`` derived config. One config tree, tied to the editor,
    not per project.
    """
    d = get_settings_path().parent / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_opencode_master_path() -> Path:
    """The editor's project-agnostic MASTER blueprint (``master.json``).

    Holds ONLY providers + general config — NO ``model``/``small_model`` (so
    OpenCode auto-picks a supported model) and NO ``mcp`` (the MCP blocks are
    built FRESH at each launch by :func:`build_launch_config`, computing every
    install/project path at runtime). Fully portable; generated once; user-editable.
    """
    return get_opencode_config_dir() / "master.json"


def get_opencode_launch_dir() -> Path:
    """The stable directory the per-launch derived ``opencode.json`` is written
    to (and that ``opencode serve`` is launched with / pointed at via
    ``OPENCODE_CONFIG``)."""
    d = get_opencode_config_dir() / "launch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_opencode_launch_test_dir() -> Path:
    """A SEPARATE, throwaway launch dir used ONLY by Test Connection.

    Test Connection spins up its own ``opencode serve`` to validate the config.
    That probe must NOT share the live ``launch/`` dir: a test manager's
    ``start()`` would overwrite ``launch/opencode.pid`` and its ``stop()`` would
    DELETE it, wiping the LIVE server's orphan-sweep record (the exact
    orphan-left bug class). Isolating the probe in ``launch_test/`` keeps its pid
    file + derived ``opencode.json`` from ever colliding with the live ones."""
    d = get_opencode_config_dir() / "launch_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_master_config(seed_from: Optional[Path] = None) -> Path:
    """Return the editor's MASTER blueprint path, GENERATING it ONCE (if absent)
    from the project's opencode.json (``seed_from``): keep its PROVIDERS + general
    config but strip ``model``/``small_model`` (so OpenCode auto-picks a supported
    model — the reliable Default) AND ``mcp`` (the MCP blocks are project/install
    specific and are always built fresh at launch, never baked into the portable
    master).

    Generated only once so the user can edit it; delete it to regenerate from the
    current project. The project's own opencode.json is a relic, never used directly.
    """
    master = get_opencode_master_path()
    if not master.exists() and seed_from is not None:
        try:
            src = Path(seed_from)
            if src.is_file():
                data = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.pop("model", None)
                    data.pop("small_model", None)
                    data.pop("mcp", None)
                    master.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    log.info("Generated editor OpenCode master blueprint from %s", src)
        except Exception:
            log.exception("Failed to generate editor OpenCode master blueprint")
    return master


def _python_win() -> str:
    """The editor venv's Windows python.exe (pythonw has no console; prefer
    python.exe for reliable MCP stdio pipes). Computed at runtime via
    ``sys.executable`` so it self-heals on reinstall/move."""
    return sys.executable.replace("pythonw.exe", "python.exe")


def _mcp_script_win(name: str) -> str:
    """Windows path to an ``authoring/_*_mcp.py`` MCP script, computed at runtime
    relative to this file so it self-heals on move/reinstall."""
    return str(Path(__file__).resolve().parents[1] / "authoring" / name)


def _build_pdf_tools_block(project_root: Optional[Path]) -> dict:
    """Build the ``pdf_tools`` MCP block FRESH for ``project_root``.

    Computes the venv python, the script path, the project's documents dir and
    the bundle's rules dir all at RUNTIME (no baked paths). ``project_root`` may
    be ``None`` (no project open) — documents/rules then point at a portable
    placeholder under the config dir so the server still launches cleanly.
    """
    from ..llm.mcp_config import build_pdf_tools_mcp_block

    if project_root is not None:
        documents_dir = project_root / "documents"
        rules_dir = project_root / "bundle" / "rules"
    else:
        documents_dir = get_opencode_config_dir() / "documents"
        rules_dir = get_opencode_config_dir() / "rules"
    try:
        documents_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create documents dir %s", documents_dir)
    return build_pdf_tools_mcp_block(
        venv_python_win=_python_win(),
        mcp_script_win=_mcp_script_win("_pdf_tool_mcp.py"),
        documents_dir_win=str(documents_dir),
        rules_dir_win=str(rules_dir),
    )


def _build_project_tools_block(project_root: Optional[Path]) -> Optional[dict]:
    """Build the ``project_tools`` MCP block FRESH for ``project_root``, or
    ``None`` when there's no ODB++ archive to wire.

    The server is launched with the project's ODB++ archive (the first ``*.tgz``
    in the project root, like odb_inspect's auto-detect) via ``--odb-tgz``. No
    project or no archive -> return ``None`` (the block is dropped from the
    derived config)."""
    from ..llm.mcp_config import build_project_tools_mcp_block

    if project_root is None:
        return None
    try:
        tgz = sorted(project_root.glob("*.tgz"))
    except OSError:
        tgz = []
    if not tgz:
        return None  # no board archive in the project — nothing to wire
    return build_project_tools_mcp_block(
        venv_python_win=_python_win(),
        mcp_script_win=_mcp_script_win("_project_tools_mcp.py"),
        odb_tgz_win=str(tgz[0]),
    )


def _build_skill_tools_blocks(project_root: Optional[Path]) -> dict:
    """Build an MCP block for EVERY discovered tool folder (skill-owned + common),
    auto-registered — no per-tool host code. One generic ``_skill_tools_mcp.py``
    backs each folder, pointed at it via ``--tools-dir``. Discovery (BUILTIN/BUNDLED
    tiers only; reserved infra names skipped) lives in ``authoring.tool_folders``."""
    from ..llm.mcp_config import build_skill_tools_mcp_block
    from ..authoring.tool_folders import discover_tool_folders
    from ..authoring.locations import skill_roots, tool_roots

    blocks: dict = {}
    for tf in discover_tool_folders(skill_roots(project_root) + tool_roots(project_root)):
        blocks.update(build_skill_tools_mcp_block(
            server_name=tf.server,
            venv_python_win=_python_win(),
            mcp_script_win=_mcp_script_win("_skill_tools_mcp.py"),
            tools_dir_win=str(tf.path),
        ))
    return blocks


def build_launch_config(
    project_root: Optional[Path] = None,
    launch_dir: Optional[Path] = None,
) -> Path:
    """Derive the per-launch ``opencode.json`` from the MASTER blueprint and the
    CURRENT project, returning the LAUNCH DIR (what ``opencode serve`` is pointed
    at via ``OPENCODE_CONFIG`` / launched from).

    Reads ``master.json`` (providers + general, no model/mcp), deep-copies it, and
    BUILDS the 3 MCP blocks FRESH — ``pdf_tools`` (documents/rules) and
    ``dcdc_tools`` always present, ``project_tools`` only when the project has a
    ``*.tgz`` board archive. Every path (venv python via ``sys.executable``,
    scripts via ``__file__``, documents/rules/tgz under ``project_root``) is
    computed at RUNTIME, so the derived config self-heals on reinstall/move and
    follows the active project. The result is written ATOMICALLY (temp file in the
    SAME launch dir + ``os.replace``) so a half-written config can never be loaded.

    ``launch_dir`` overrides the live ``launch/`` dir — Test Connection passes a
    throwaway ``launch_test/`` so its derived config + pid file never collide
    with the live server's (see :func:`get_opencode_launch_test_dir`).
    """
    master = get_opencode_master_path()
    data: dict = {}
    if master.exists():
        try:
            loaded = json.loads(master.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = copy.deepcopy(loaded)
        except Exception:
            log.exception("could not parse master.json; deriving from empty config")

    # Master must NOT carry model/mcp — strip BOTH defensively in case the user
    # edited them back in, so the blueprint stays project-agnostic (OpenCode then
    # auto-picks a supported model; MCP is always built fresh below).
    data.pop("model", None)
    data.pop("small_model", None)
    data.pop("mcp", None)

    mcp: dict = {}
    try:
        mcp.update(_build_pdf_tools_block(project_root))
    except Exception:
        log.exception("Failed to build pdf_tools MCP block")
    proj_block = None
    try:
        proj_block = _build_project_tools_block(project_root)
    except Exception:
        log.exception("Failed to build project_tools MCP block")
    if proj_block:
        mcp.update(proj_block)
    try:
        # Auto-discover + register every skill-owned / common tool folder. Infra
        # blocks (pdf_tools/project_tools) are added FIRST and their names are
        # reserved in tool_folders, so a tool folder can't shadow them.
        mcp.update(_build_skill_tools_blocks(project_root))
    except Exception:
        log.exception("Failed to build skill-tools MCP blocks")
    data["mcp"] = mcp

    if launch_dir is None:
        launch_dir = get_opencode_launch_dir()
    else:
        launch_dir.mkdir(parents=True, exist_ok=True)
    target = launch_dir / "opencode.json"
    payload = json.dumps(data, indent=2)
    # Atomic write: temp in the SAME dir as target so os.replace stays on one
    # filesystem (no EXDEV from /tmp), then rename over the target.
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(launch_dir), prefix=".opencode-", suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    log.info("Built launch config (project=%s, mcp=%s)",
             project_root, sorted(mcp.keys()))
    return launch_dir



class SettingsDialog(QDialog):
    """
    Settings dialog for application configuration.
    
    Settings are stored in settings.json in user's home directory.
    Task configurations are managed through TaskConfigManager.
    """

    # Thread-safe UI marshal: a worker daemon (the model-list fetch, the
    # Start-server liveness probe) emits a callable here and Qt queues the slot
    # onto THIS dialog's (UI) thread. QTimer.singleShot(0, fn) from a non-Qt
    # daemon thread creates the timer in the CALLING thread, so the callback can
    # be lost (the probe-in-flight flag would stay set forever and the note
    # would stick at "Querying server…"); a signal/slot is the canonical
    # cross-thread marshal (AutoConnection -> QueuedConnection).
    _ui_call = Signal(object)

    def __init__(
        self,
        task_config_manager: TaskConfigManager,
        parent=None,
        project_root=None,
        server_manager=None,
    ):
        super().__init__(parent)
        # Cross-thread UI marshal: queue daemon-posted callables onto the UI
        # thread (AutoConnection -> QueuedConnection from a worker thread).
        self._ui_call.connect(self._run_ui_call)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(700)
        self.setMinimumHeight(600)

        self._settings = load_settings()
        self._task_config_manager = task_config_manager
        # Project root is needed by the Validator tab to read/write
        # ``<project>/config/config.json``'s ``validator_loop`` section.
        # ``None`` means no project is open — the Validator tab's controls
        # then disable themselves with an explanatory tooltip.
        self._project_root = project_root
        # The live OpenCode server manager (when one exists): the model picker
        # queries its ACTUAL running port (the OS may have OS-assigned a
        # different port than the saved spinbox value), and Test Connection can
        # confirm reachability against the running server. ``None`` -> fall back
        # to the spinbox host/port.
        self._server_manager = server_manager

        # Set true in done() (the accept/reject chokepoint). A model-fetch worker
        # may post _apply_opencode_models() back AFTER the dialog is closed; the
        # posted callback returns early on this flag instead of touching deleted
        # C++ widgets (use-after-close). The Start-server poll has its own
        # _start_poll_done latch; this guards the model-refresh path.
        self._closed = False
        # Generation token for the Start-server probe: bumped on each
        # _on_start_server_clicked so a stale probe (from an earlier start
        # attempt) is dropped instead of applying alive=True to a newer poll.
        self._start_server_generation = 0

        # Note: task / chat editing moved to the parent app's
        # ProjectConfigDialog -> Workflows tab (Phase 4). This dialog
        # now owns only LLM backend + validator-loop settings.
        self._setup_ui()
        self._load_values()

    @Slot(object)
    def _run_ui_call(self, fn):
        """UI-thread slot for the _ui_call signal: invoke the marshalled
        callable. Runs on this dialog's (the UI) thread because the signal was
        emitted from a worker thread (queued connection)."""
        fn()

    def _post_to_ui(self, fn):
        """Marshal a callable onto the UI thread via the _ui_call signal: the
        canonical thread-safe cross-thread dispatch. A worker daemon has NO Qt
        event loop, so QTimer.singleShot(0, fn) from it would create the timer
        in the daemon thread and the callback could be lost. Emitting a signal
        is thread-safe and Qt queues the slot onto the UI thread."""
        self._ui_call.emit(fn)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Tab widget
        tabs = QTabWidget()
        
        # LLM Backend tab (index 0)
        llm_tab = self._create_llm_tab()
        tabs.addTab(llm_tab, "LLM Backend")

        # Tasks + Chat editing moved to ProjectConfigDialog ->
        # Workflows tab (Phase 4). See the workflow editor's
        # "Configure Workflows…" menu item for the entry point.

        # Validator tab (index 1) - Per-project validator-loop options.
        # Currently hosts only the global retry cap; reserved for future
        # validator-related settings (per-artifact toggles, etc.) without
        # bloating the LLM Backend tab.
        validator_tab = self._create_validator_tab()
        tabs.addTab(validator_tab, "Validator")

        layout.addWidget(tabs)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        self.save_btn.setDefault(True)
        btn_layout.addWidget(self.save_btn)
        
        layout.addLayout(btn_layout)
    
    def _create_llm_tab(self) -> QWidget:
        """Create the LLM settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Backend selection
        backend_group = QGroupBox("LLM Backend")
        backend_layout = QFormLayout(backend_group)
        
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["opencode", "none"])
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        backend_layout.addRow("Backend:", self.backend_combo)
        
        layout.addWidget(backend_group)
        
        # Common LLM Parameters
        common_group = QGroupBox("Common Parameters")
        common_layout = QFormLayout(common_group)

        self.context_window = QSpinBox()
        self.context_window.setRange(1000, 2_000_000)
        self.context_window.setValue(16384)
        self.context_window.setToolTip(
            "Model's total context window (input + output token budget).\n"
            "Used only by the chat panel's 'Tokens: X/Y' indicator so you "
            "know when to reset the session.\n"
            "Not sent to the LLM. Examples: GPT-4 → 128000, Gemma 4 26B → 131072, Claude 4.7 → 1000000."
        )
        common_layout.addRow("Context Window:", self.context_window)
        
        self.request_timeout = QDoubleSpinBox()
        self.request_timeout.setRange(10.0, 10800.0)
        self.request_timeout.setSingleStep(10.0)
        self.request_timeout.setValue(120.0)
        self.request_timeout.setSuffix(" sec")
        self.request_timeout.setKeyboardTracking(True)
        self.request_timeout.setToolTip("Request timeout in seconds (10-10800). For long generations, use 300-600 seconds.")
        common_layout.addRow("Request Timeout:", self.request_timeout)
        
        layout.addWidget(common_group)
        
        # OpenCode settings
        self.opencode_group = QGroupBox("OpenCode Settings")
        opencode_layout = QFormLayout(self.opencode_group)
        
        self.opencode_port = QSpinBox()
        self.opencode_port.setRange(1024, 65535)
        self.opencode_port.setValue(4096)
        opencode_layout.addRow("Port:", self.opencode_port)
        
        self.opencode_host = QLineEdit()
        self.opencode_host.setPlaceholderText("127.0.0.1")
        opencode_layout.addRow("Host:", self.opencode_host)
        
        self.opencode_model = QComboBox()
        self.opencode_model.setEditable(True)
        self.opencode_model.setInsertPolicy(QComboBox.NoInsert)
        self.opencode_model.lineEdit().setPlaceholderText(
            "Default — leave empty for OpenCode to auto-pick")
        self.opencode_model.setToolTip(
            "Model OpenCode uses (providerID/modelID). Leave EMPTY for the "
            "reliable default — OpenCode auto-picks a model your account "
            "supports. The dropdown lists the server's configured models for "
            "convenience, but you can TYPE any valid id (e.g. openai/gpt-5.5); "
            "the list isn't exhaustive and some listed ids may be rejected by "
            "your account.")
        self.opencode_model_refresh = QPushButton("Refresh")
        self.opencode_model_refresh.setToolTip(
            "Re-query the running OpenCode server for the models configured in "
            "its opencode.json.")
        self.opencode_model_refresh.clicked.connect(
            lambda: self._populate_opencode_models())
        # One-click recovery affordance: shown only when the server reads as down
        # (no /config). It relaunches OUR server on a daemon thread, then
        # re-queries the model list — so a crashed mid-session server can be
        # brought back from the picker WITHOUT restarting the whole app.
        self.opencode_start_server = QPushButton("Start server")
        self.opencode_start_server.setToolTip(
            "The OpenCode server is not reachable. Click to (re)launch it, then "
            "the model list refreshes automatically.")
        self.opencode_start_server.clicked.connect(self._on_start_server_clicked)
        self.opencode_start_server.setVisible(False)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self.opencode_model, 1)
        model_row.addWidget(self.opencode_model_refresh)
        model_row.addWidget(self.opencode_start_server)
        opencode_layout.addRow("Model:", model_row)
        self.opencode_models_note = QLabel("")
        self.opencode_models_note.setStyleSheet(
            f"color: {theme.muted_color()}; font-size: 11px;")
        opencode_layout.addRow("", self.opencode_models_note)
        
        self.opencode_wsl_path = QLineEdit()
        self.opencode_wsl_path.setPlaceholderText("/mnt/c/...")
        self.opencode_wsl_path.setToolTip("WSL path to OpenCode directory")
        opencode_layout.addRow("WSL Path:", self.opencode_wsl_path)
        
        self.opencode_startup_timeout = QDoubleSpinBox()
        self.opencode_startup_timeout.setRange(10.0, 300.0)
        self.opencode_startup_timeout.setValue(60.0)
        self.opencode_startup_timeout.setToolTip("Timeout for backend startup")
        opencode_layout.addRow("Startup Timeout:", self.opencode_startup_timeout)

        self.wizard_max_sessions = QSpinBox()
        self.wizard_max_sessions.setRange(1, 16)
        self.wizard_max_sessions.setValue(5)
        self.wizard_max_sessions.setToolTip(
            "Max concurrent LLM sessions the DCDC wizard runs in parallel "
            "(per-IC rail-reads / builds); the wizard Parallel spin-box "
            "defaults to this.")
        opencode_layout.addRow(
            "Wizard max concurrent sessions:", self.wizard_max_sessions)

        # The editor's MASTER blueprint (master.json) — providers only, project
        # agnostic. The per-launch opencode.json is DERIVED from it (MCP built
        # fresh) at each launch. Generated once from the project; user-editable.
        cfg_row = QHBoxLayout()
        cfg_row.setContentsMargins(0, 0, 0, 0)
        self.opencode_config_path = QLabel(str(get_opencode_master_path()))
        self.opencode_config_path.setWordWrap(True)
        self.opencode_config_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        self.opencode_config_path.setStyleSheet(
            f"color: {theme.muted_color()}; font-size: 11px;")
        cfg_row.addWidget(self.opencode_config_path, 1)
        self.open_opencode_config_btn = QPushButton("Open…")
        self.open_opencode_config_btn.setToolTip(
            "Open the editor's OpenCode master blueprint (master.json) — the "
            "project-agnostic providers config. The per-launch opencode.json is "
            "derived from it (default model stripped so it auto-picks; MCP tools "
            "built fresh each launch). Edit it to change providers; delete it to "
            "regenerate from the project.")
        self.open_opencode_config_btn.clicked.connect(self._on_open_opencode_config)
        cfg_row.addWidget(self.open_opencode_config_btn)
        opencode_layout.addRow("Config:", cfg_row)

        # Skill-chat default toggles. These only seed the INITIAL state of the
        # skill chat's per-request toggles; the user can still flip either off/on
        # per conversation. Both default off (no web / no project-data access
        # unless explicitly opted in).
        self.web_default = QCheckBox("Web access on by default (skill chat)")
        self.web_default.setToolTip(
            "Seed the skill chat's web toggle ON for new conversations. The "
            "model can still be flipped off per chat. Off by default.")
        opencode_layout.addRow("", self.web_default)

        self.save_docs_default = QCheckBox(
            "Save downloaded datasheets by default (skill chat)")
        self.save_docs_default.setToolTip(
            "Default state of the skill chat's 💾 Save datasheets toggle: when "
            "on, downloaded datasheets are cached into the project documents "
            "folder for reuse.")
        opencode_layout.addRow("", self.save_docs_default)

        self.project_tools_default = QCheckBox(
            "Project data tools on by default (skill chat)")
        self.project_tools_default.setToolTip(
            "Seed the skill chat's project-data tools toggle ON for new "
            "conversations (netlist/BOM/component access via the project_tools "
            "MCP server). The model can still be flipped off per chat. Off by "
            "default.")
        opencode_layout.addRow("", self.project_tools_default)

        layout.addWidget(self.opencode_group)
        
        # Test Connection button
        test_btn_layout = QHBoxLayout()
        test_btn_layout.addStretch()
        self.test_connection_btn = QPushButton("Test Connection")
        self.test_connection_btn.clicked.connect(self._on_test_connection)
        test_btn_layout.addWidget(self.test_connection_btn)
        layout.addLayout(test_btn_layout)
        
        layout.addStretch()
        
        return tab
    
    def _on_backend_changed(self, backend: str):
        """Handle backend selection change."""
        self.opencode_group.setVisible(backend == "opencode")
        # Lazily fill the model dropdown the first time OpenCode is shown
        # (a /config round-trip; Refresh re-queries on demand).
        if backend == "opencode" and self.opencode_model.count() == 0:
            self._populate_opencode_models()

    def _on_open_opencode_config(self):
        """Open the editor's master.json blueprint (or its folder if it hasn't
        been generated yet) in the OS default application."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        master = get_opencode_master_path()
        target = master if master.exists() else master.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _opencode_model_value(self) -> str:
        """The model id the user typed/selected, or '' for Default (no override
        -> OpenCode auto-picks a supported model)."""
        return self.opencode_model.currentText().strip()

    def _populate_opencode_models(self):
        """Fill the editable model field's dropdown from the running server's
        /config — a convenience list to pick/copy from. The user may also TYPE a
        model id NOT in the list (e.g. openai/gpt-5.5, which OpenCode auto-picks
        but doesn't report in /config). Empty = Default (auto-pick). The current
        text is preserved across a refresh.

        ``fetch_opencode_models`` is a blocking 2s HTTP GET, so it runs on a
        daemon thread and the result is marshaled back to the UI thread — the
        dialog never freezes (matters on dialog-open, the Refresh button, and the
        Start-server poll's re-query). One in-flight probe at a time.
        """
        if self.opencode_model.count() == 0:
            preserve = self._settings.get("opencode", {}).get("model", "") or ""
        else:
            preserve = self.opencode_model.currentText().strip()
        # C4: query the LIVE running server's port when a manager is running —
        # the OS may have OS-assigned a port different from the saved spinbox
        # value. Fall back to the spinbox host/port when there's no live server.
        # is_running is a non-blocking process poll, so this read is cheap.
        if self._server_manager is not None and self._server_manager.is_running:
            url = self._server_manager.server_url
        else:
            url = (f"http://{self.opencode_host.text() or '127.0.0.1'}"
                   f":{self.opencode_port.value()}")
        # One in-flight probe at a time so rapid Refresh clicks don't stack
        # worker threads; a later probe just supersedes the note.
        if getattr(self, "_models_probe_in_flight", False):
            return
        self._models_probe_in_flight = True
        self.opencode_models_note.setText("Querying server…")

        import threading

        def _worker():
            try:
                models = fetch_opencode_models(url)
            except Exception:
                models = []
            # Marshal the widget mutation back to the UI thread via the
            # thread-safe signal (QTimer.singleShot from this daemon would be
            # lost — a lost callback leaves _models_probe_in_flight=True forever
            # and the note stuck at "Querying server…").
            self._post_to_ui(lambda: self._apply_opencode_models(models, preserve))

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            # Spawn failed after the flag was set — clear it so a later Refresh
            # can probe again (never wedge the flag / note permanently).
            self._models_probe_in_flight = False
            self.opencode_models_note.setText(
                "Could not query the server — try Refresh again.")

    def _apply_opencode_models(self, models, preserve):
        """Apply a fetched model list to the picker (UI thread only)."""
        # The fetch worker may marshal this back AFTER the dialog closed; the
        # C++ widgets are then deleted. Drop the late result (use-after-close).
        if getattr(self, "_closed", False):
            return
        self._models_probe_in_flight = False
        self.opencode_model.blockSignals(True)
        self.opencode_model.clear()
        self.opencode_model.addItems(models)        # convenience list
        self.opencode_model.setEditText(preserve)   # keep typed text (empty=Default)
        self.opencode_model.blockSignals(False)
        # When the server is reachable we have models; when it's down (no
        # /config) we surface the "Start server" affordance instead of a dead-end
        # grey line — the user can relaunch right here. The button only makes
        # sense when there IS a manager to (re)launch.
        reachable = bool(models)
        can_start = self._server_manager is not None
        self.opencode_start_server.setVisible(not reachable and can_start)
        if reachable:
            self.opencode_models_note.setText(
                "Pick/copy from the list, TYPE any id (e.g. openai/gpt-5.5), or "
                "leave empty for auto-pick.")
        elif can_start:
            self.opencode_models_note.setText(
                "OpenCode server is down — click “Start server” to "
                "relaunch it, then the list refreshes.")
        else:
            self.opencode_models_note.setText(
                "Server not reachable — type a model id or leave empty for "
                "auto-pick.")

    def _on_start_server_clicked(self):
        """(Re)launch OUR OpenCode server from the picker, off the UI thread, and
        re-query the model list once it answers.

        Auto-recovery seam for a crashed mid-session server: ``ensure_running``
        relaunches via the SAME ``start()`` path the app uses (orphan-sweep +
        respawn), so the user never has to restart the whole app. We poll the
        manager's liveness on a short QTimer (the daemon ``start()`` returns
        asynchronously) and refresh the list when it comes up, all without
        freezing the dialog.
        """
        import threading
        from PySide6.QtCore import QTimer
        sm = self._server_manager
        if sm is None:
            return
        # A manual Start-server is a user intervention: clear any auto-recovery
        # give-up state on the main window so the background poll resumes
        # auto-recovering this server after this attempt (best-effort — the
        # parent is the main window when launched from it).
        parent = self.parent()
        reset = getattr(parent, "_reset_server_recovery", None)
        if callable(reset):
            reset()
        # Bump the generation token: each Start-server attempt owns a fresh
        # token. A probe spawned by an EARLIER attempt carries the old token and
        # its result is dropped (it must not stop a newer poll's timer, re-enable
        # the button, or list models for the wrong state).
        self._start_server_generation += 1
        generation = self._start_server_generation
        self.opencode_start_server.setEnabled(False)
        self.opencode_models_note.setText("Starting OpenCode server…")
        # Launch off the UI thread; ensure_running short-circuits if it is
        # already alive and otherwise calls start().
        try:
            threading.Thread(target=sm.ensure_running, daemon=True).start()
        except Exception:
            # Spawn failed — re-enable the button and surface a note instead of
            # wedging it disabled with the "Starting…" note forever. No poll
            # timer is armed (nothing to poll), so just bail.
            self.opencode_start_server.setEnabled(True)
            self.opencode_models_note.setText(
                "Could not start the server — check the OpenCode install / "
                "logs, then try again.")
            return

        # Poll liveness for up to ~startup timeout; refresh + re-enable on the UI
        # thread when it answers (or give up with a clear note). is_alive is a
        # blocking 2s HTTP /health GET, so each tick runs it on a daemon thread
        # and the verdict is MARSHALLED back to the UI thread via _post_to_ui —
        # the probe never mutates dialog state cross-thread, and the modal dialog
        # never freezes. _start_poll_done latches the terminal state so a late
        # probe result can still flip us to success after a timeout note (no
        # cross-thread flag the UI reads stale, no ignored late success).
        self._start_poll_elapsed = 0.0
        self._start_probe_in_flight = False
        self._start_poll_done = False
        timer = QTimer(self)
        timer.setInterval(1000)
        # Stash the timer so done() (dialog close) can stop it — a fired-after-
        # close tick would otherwise run HTTP on the UI thread and touch deleted
        # C++ widgets (use-after-close).
        self._start_poll_timer = timer

        def _tick():
            if self._start_poll_done:
                return
            self._start_poll_elapsed += 1.0
            if self._start_poll_elapsed >= 60.0:
                # Timeout: stop ticking and show the give-up note. We do NOT set
                # _start_poll_done here — an is_alive probe may still be in
                # flight; if it comes back alive its UI-thread handler honours
                # the LATE success (flips the note + lists models) instead of
                # silently dropping it.
                timer.stop()
                self.opencode_start_server.setEnabled(True)
                self.opencode_models_note.setText(
                    "Server did not come up — check the OpenCode install / logs, "
                    "then try again.")
                return
            # Kick off the next is_alive probe off the UI thread (one at a time).
            if self._start_probe_in_flight:
                return
            self._start_probe_in_flight = True

            def _probe():
                # Off the UI thread (blocking ~2s /health). The verdict is
                # marshalled back via the thread-safe signal; the daemon mutates
                # NO dialog state directly (a lost QTimer.singleShot here would
                # wedge _start_probe_in_flight=True forever).
                try:
                    alive = sm.is_alive
                except Exception:
                    alive = False
                self._post_to_ui(
                    lambda: self._on_start_probe_result(alive, generation))

            try:
                threading.Thread(target=_probe, daemon=True).start()
            except Exception:
                # Spawn failed after the flag was set — clear it so the next
                # tick can probe again (never wedge _start_probe_in_flight=True).
                self._start_probe_in_flight = False

        def _on_start_probe_result(alive, gen=generation):
            # UI-thread continuation of one is_alive probe.
            # Drop a stale probe from an EARLIER Start-server attempt: applying
            # its verdict would stop a newer poll's timer / re-enable the button /
            # list models for the wrong state.
            if gen != self._start_server_generation:
                return
            self._start_probe_in_flight = False
            if self._start_poll_done:
                return  # already terminal (a prior probe won, or dialog logic done)
            if not alive:
                return  # keep polling (the tick re-arms / enforces timeout)
            # Gate success on is_alive (process up AND /health answers): start()
            # spawns the process EARLY then waits for /health, so is_running
            # flips true before the server can serve /config. This is the SINGLE
            # success seam — it also catches a late success after the timeout
            # note (the timer may already be stopped).
            self._start_poll_done = True
            timer.stop()
            self.opencode_start_server.setEnabled(True)
            self._populate_opencode_models()  # toggles the button off + lists models

        self._on_start_probe_result = _on_start_probe_result
        timer.timeout.connect(_tick)
        timer.start()

    def done(self, result):
        """Cancel the Start-server poll on ANY dialog close (OK or Cancel).

        ``done()`` is the single chokepoint for accept/reject, so stopping the
        timer here guarantees no tick fires after the dialog (and its C++
        widgets) are gone — avoiding HTTP on the UI thread + use-after-close.
        """
        timer = getattr(self, "_start_poll_timer", None)
        if timer is not None:
            timer.stop()
        # Latch the poll terminal so a probe still in flight, whose result
        # marshals back after this close, no-ops in its UI-thread handler
        # instead of touching deleted C++ widgets (use-after-close).
        self._start_poll_done = True
        # Disposed flag for any OTHER posted callback (the model-refresh worker's
        # _apply_opencode_models) — it returns early on this instead of mutating
        # deleted C++ widgets after close.
        self._closed = True
        super().done(result)

    def _on_test_connection(self):
        """Test the LLM backend configuration."""
        backend = self.backend_combo.currentText()
        
        if backend == "none":
            QMessageBox.information(self, "Test Connection", "No backend selected.")
            return
        
        try:
            if backend == "opencode":
                # Test OpenCode backend
                from ..llm.opencode_backend import OpenCodeBackend, OpenCodeConfig
                from ..llm.backend_base import LLMRequest, LLMTask
                
                # Build the per-launch derived config (MCP built fresh) from the
                # MASTER blueprint, matching how real chat launches it. Ensure the
                # master exists first (seeded once from the project relic).
                #
                # CRITICAL: build into a THROWAWAY launch_test/ dir, NOT the live
                # launch/ dir. The test server's start() writes a pid file and its
                # stop() DELETES it; sharing the live dir would clobber the LIVE
                # server's orphan-sweep record (opencode.pid) — the exact
                # orphan-left bug class. Isolating it means the probe's pid file +
                # derived opencode.json never collide with the live ones.
                ensure_master_config(
                    seed_from=(self._project_root / "opencode.json")
                    if self._project_root else None)
                test_launch_dir = get_opencode_launch_test_dir()
                launch_dir = str(build_launch_config(
                    self._project_root, launch_dir=test_launch_dir))
                config = OpenCodeConfig(
                    server_port=self.opencode_port.value(),
                    server_hostname=self.opencode_host.text() or "127.0.0.1",
                    model=self._opencode_model_value() or None,
                    working_directory=launch_dir,
                )

                # Classified availability check — a precise reason (e.g.
                # "OpenCode was not found in the WSL PATH. Install it
                # (npm i -g opencode-ai)...") instead of a vague "not available".
                from ..llm.server_manager import OpenCodeServerManager
                mgr = OpenCodeServerManager(config)
                if not mgr.is_available():
                    status = mgr.last_status
                    reason = status.message if status else "OpenCode is not available."
                    QMessageBox.warning(self, "Test Connection", f"✗ {reason}")
                    return

                # C6: install OK is not enough — actually START a server and
                # confirm /health is reachable, surfacing bind failures (the
                # manager classifies PORT_IN_USE/START_TIMEOUT/START_FAILED).
                # Then STOP it so the chat-test backend (which spawns its own
                # `opencode serve`) doesn't collide on the port. try/finally so
                # the probe server is ALWAYS torn down (and its launch_test pid
                # file cleaned), even on an early-return warning path below.
                if not mgr.start():
                    st = mgr.last_status
                    why = f"\n\n{st.message}" if st and not st.ok else ""
                    QMessageBox.warning(
                        self, "Test Connection",
                        f"✗ Could not start OpenCode\n\nHost: {config.server_hostname}\nPort: {config.server_port}{why}"
                    )
                    return
                try:
                    reachable = mgr.health_check()
                finally:
                    mgr.stop()
                if not reachable:
                    QMessageBox.warning(
                        self, "Test Connection",
                        f"✗ Server started but /health was not reachable\n\n"
                        f"Host: {config.server_hostname}\nPort: {config.server_port}"
                    )
                    return

                # Always run a real end-to-end chat test (Default = auto-pick
                # included), so the button truly validates the model, not just
                # that OpenCode is installed + reachable.
                model_label = config.model or "auto-pick (Default)"
                backend_obj = OpenCodeBackend(config=config)
                if not backend_obj.is_running:
                    if not backend_obj.start():
                        st = mgr.last_status
                        why = f"\n\n{st.message}" if st and not st.ok else ""
                        QMessageBox.warning(
                            self, "Test Connection",
                            f"✗ Could not start OpenCode\n\nHost: {config.server_hostname}\nPort: {config.server_port}{why}"
                        )
                        return
                try:
                    test_request = LLMRequest(
                        task=LLMTask.AD_HOC_CHAT,
                        strict_mode=False,
                        user_message="Respond with just 'OK'",
                    )
                    import threading
                    result = {"response": None, "error": None}

                    def test_send():
                        try:
                            result["response"] = backend_obj.send_request(test_request)
                        except Exception as e:
                            result["error"] = str(e)

                    thread = threading.Thread(target=test_send)
                    thread.start()
                    thread.join(timeout=30)
                    if thread.is_alive():
                        result["error"] = "Request timed out after 30 seconds"

                    if backend_obj.is_running:
                        backend_obj.stop()

                    hint = ("\n\nIf you picked a specific model, your account "
                            "may not support it — try leaving Model empty "
                            "(auto-pick).")
                    if result["error"]:
                        QMessageBox.warning(
                            self, "Test Connection",
                            f"✗ Chat test failed\n\nModel: {model_label}\n\nError: {result['error']}{hint}"
                        )
                    elif result["response"] and result["response"].error_message:
                        QMessageBox.warning(
                            self, "Test Connection",
                            f"✗ Chat test failed\n\nModel: {model_label}\n\nError: {result['response'].error_message}{hint}"
                        )
                    else:
                        QMessageBox.information(
                            self, "Test Connection",
                            f"✓ OpenCode is working\n\nHost: {config.server_hostname}\nPort: {config.server_port}\nModel: {model_label}"
                        )
                except Exception:
                    if backend_obj.is_running:
                        backend_obj.stop()
                    raise
            
        except Exception as e:
            QMessageBox.critical(
                self, "Test Connection Failed",
                f"Error testing connection:\n\n{str(e)}"
            )
    



    
    # ------------------------------------------------------------------ #
    # Validator tab (per-project: validator_loop.* settings)              #
    # ------------------------------------------------------------------ #

    def _create_validator_tab(self) -> QWidget:
        """Build the Validator tab.

        Single field today: ``Max validator-correction retries`` —
        persisted per-project under ``validator_loop.max_attempts``.
        Reserved for future fields (per-artifact toggles, etc.).
        Falls back to a disabled state with explanatory tooltip when no
        project is open.
        """
        from ..core.task_config import DEFAULT_MAX_VALIDATOR_ATTEMPTS

        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Top-level enable/disable: operator opt-out (Phase 4.6).
        # When unchecked, the deterministic validator is skipped, the
        # auto-correct checkbox is greyed (no loop to run), and the
        # "validator unavailable" chat warning is suppressed.
        self.validator_enabled = QCheckBox("Enable deterministic validator")
        self.validator_enabled.setChecked(True)
        self.validator_enabled.setToolTip(
            "Master switch for the per-project deterministic validator + "
            "its auto-correction loop. Uncheck to skip validation entirely "
            "for this project. When enabled, the validator additionally "
            "requires a text_renderer to be picked in "
            "Project Config → Parsers."
        )
        layout.addWidget(self.validator_enabled)

        retry_group = QGroupBox("Auto-correction loop")
        retry_form = QFormLayout(retry_group)

        self.validator_max_attempts = QSpinBox()
        self.validator_max_attempts.setRange(1, 10)
        self.validator_max_attempts.setValue(DEFAULT_MAX_VALIDATOR_ATTEMPTS)
        self.validator_max_attempts.setToolTip(
            "Total LLM attempts (1 original + N-1 retries) before the "
            "auto-correction loop gives up and falls through to operator "
            "review. Higher values cost more tokens; default 3 resolves "
            "~95%% of fixable failures empirically. Per-project."
        )
        retry_form.addRow("Max validator-correction attempts:", self.validator_max_attempts)

        # Disable the retry-cap row when the master toggle is off.
        self.validator_enabled.toggled.connect(retry_group.setEnabled)

        layout.addWidget(retry_group)

        # Board-netlist (Text-tab panel) hidden net-name prefixes — per project.
        netlist_group = QGroupBox("Board netlist")
        netlist_form = QFormLayout(netlist_group)
        self.net_hide_prefixes = QLineEdit()
        self.net_hide_prefixes.setPlaceholderText("Net")
        self.net_hide_prefixes.setToolTip(
            "Comma-separated net-name prefixes hidden by default in the Text "
            "tab's netlist panel (case-insensitive). Default 'Net' hides Altium "
            "autogen names like NetD16_A."
        )
        netlist_form.addRow("Hidden net prefixes:", self.net_hide_prefixes)
        layout.addWidget(netlist_group)

        # Helper note + project-scope explanation.
        note = QLabel(
            "Settings on this tab are stored per-project in "
            "<code>config/config.json</code> under the "
            "<code>validator_loop</code> section. They take effect on "
            "the next LLM task you run."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.muted_color()}; font-size: 11px;")
        layout.addWidget(note)

        # Disable when no project is bound — there's nowhere to persist.
        if self._project_root is None:
            self.validator_enabled.setEnabled(False)
            self.validator_max_attempts.setEnabled(False)
            self.net_hide_prefixes.setEnabled(False)
            disabled_note = QLabel(
                "<i>No project open — open a project to edit these settings.</i>"
            )
            disabled_note.setStyleSheet(f"color: {theme.muted_color()};")
            layout.addWidget(disabled_note)

        layout.addStretch()
        return tab

    def _load_validator_values(self) -> None:
        """Populate the Validator tab from the project's
        ``validator_loop`` config section."""
        if self._project_root is None:
            return  # spinbox disabled; default value already set
        try:
            from ..llm.validator_loop_settings import load_settings as _load_section
            section = _load_section(self._project_root)
        except Exception:
            log.exception("Failed to load validator_loop settings")
            return
        if "enabled" in section:
            self.validator_enabled.setChecked(bool(section["enabled"]))
        if "max_attempts" in section:
            try:
                self.validator_max_attempts.setValue(int(section["max_attempts"]))
            except (TypeError, ValueError):
                log.warning(
                    "validator_loop.max_attempts is not an int (%r); "
                    "leaving spinbox at default.", section["max_attempts"],
                )

    def _save_validator_values(self) -> None:
        """Persist the Validator tab's values into the project's
        ``validator_loop`` config section. No-op without a project."""
        if self._project_root is None:
            return
        try:
            from ..llm.validator_loop_settings import save_setting
            save_setting(
                self._project_root, "enabled",
                bool(self.validator_enabled.isChecked()),
            )
            save_setting(
                self._project_root,
                "max_attempts",
                int(self.validator_max_attempts.value()),
            )
            prefixes = [p.strip() for p in self.net_hide_prefixes.text().split(",")
                        if p.strip()]
            save_hide_prefixes(self._project_root, prefixes)
        except Exception:
            log.exception("Failed to persist validator_loop settings")

    def _load_values(self):
        """Load values from settings.

        Loads LLM backend configuration from ``settings.json`` and the
        per-project ``validator_loop`` section. Task / chat editing
        lives in the parent app's Project Configuration dialog as of
        Phase 4 — see ``_WorkflowsTab``.
        """
        # LLM Backend
        self.backend_combo.setCurrentText(
            self._settings.get("llm_backend", "opencode")
        )
        
        # Common LLM parameters
        common_llm = self._settings.get("common_llm", {})
        # Backwards-compat: old setting name was "max_tokens"; prefer new "context_window"
        self.context_window.setValue(
            common_llm.get("context_window", common_llm.get("max_tokens", 16384))
        )
        self.request_timeout.setValue(common_llm.get("request_timeout", 120.0))
        
        # OpenCode settings — host/port set BEFORE the final
        # _on_backend_changed (below) so the model dropdown queries the right
        # server; _populate_opencode_models preserves opencode["model"].
        opencode = self._settings.get("opencode", {})
        self.opencode_port.setValue(opencode.get("port", 4096))
        self.opencode_host.setText(opencode.get("host", "127.0.0.1"))
        self.opencode_wsl_path.setText(opencode.get("wsl_path", ""))
        self.opencode_startup_timeout.setValue(opencode.get("startup_timeout", 60.0))
        # Skill-chat default toggles (both default off).
        self.web_default.setChecked(opencode.get("web_default", False))
        self.save_docs_default.setChecked(opencode.get("save_docs_default", False))
        self.project_tools_default.setChecked(
            opencode.get("project_tools_default", False))

        # Wizard settings
        wizard = self._settings.get("wizard", {})
        self.wizard_max_sessions.setValue(
            int(wizard.get("max_concurrent_sessions", 5)))


        # Update visibility
        self._on_backend_changed(self.backend_combo.currentText())

        # Validator tab — populate from project's validator_loop section.
        self._load_validator_values()
        try:
            self.net_hide_prefixes.setText(
                ", ".join(load_hide_prefixes(self._project_root)))
        except Exception:
            log.exception("Failed to load net_explorer.hide_prefixes")

    def _on_save(self):
        """Save settings.

        Persists LLM backend configuration to ``settings.json`` and the
        per-project ``validator_loop`` section. Task / chat editing
        moved to ``_WorkflowsTab`` in the parent app's Project
        Configuration dialog (Phase 4).
        """
        # Validator tab — persist into project's validator_loop section.
        self._save_validator_values()

        # Save LLM backend settings to settings.json
        self._settings = {
            "llm_backend": self.backend_combo.currentText(),
            "common_llm": {
                "context_window": self.context_window.value(),
                "request_timeout": self.request_timeout.value(),
            },
            "opencode": {
                "port": self.opencode_port.value(),
                "host": self.opencode_host.text() or "127.0.0.1",
                "model": self._opencode_model_value(),
                "wsl_path": self.opencode_wsl_path.text(),
                "startup_timeout": self.opencode_startup_timeout.value(),
                "web_default": self.web_default.isChecked(),
                "save_docs_default": self.save_docs_default.isChecked(),
                "project_tools_default": self.project_tools_default.isChecked(),
            },
            "wizard": {
                "max_concurrent_sessions": self.wizard_max_sessions.value(),
            },
        }
        
        try:
            save_settings(self._settings)
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", str(e))

    
    def get_settings(self) -> dict:
        """Get the current settings."""
        return self._settings
