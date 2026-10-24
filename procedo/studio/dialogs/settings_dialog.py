"""
Settings Dialog - Procedo Studio's settings, as Procedo Bench's: a page list (LLM backend,
Skills, Validator) beside the selected page, a header and a footer band.
"""

import logging
from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QWidget, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton, QLabel, QMessageBox,
)
from PySide6.QtCore import Qt
from procedo.ui import ui_kit
from procedo.core.board_netlist import read_hide_prefixes, write_hide_prefixes
from procedo.core.off_gui import answer_to
from procedo.ui.page_nav import PagedArea, field_row, heading, page_form

from ..core.task_config import TaskConfigManager
from ..core.studio_settings import load_settings, read_settings, save_settings
from ..llm.launch_config import get_opencode_master_path
from ..llm.server_manager import fetch_opencode_models
from .settings_backend_probe import SettingsBackendProbe

log = logging.getLogger(__name__)


def _add_row(form: QFormLayout, label: str, field) -> None:
    """One row of a page's form, its label in the shared column (a page's forms line up)."""
    form.addRow(ui_kit.form_label(label, ui_kit.LABEL_WIDTH), field)


def _fixed(widget: QWidget, width: int) -> QWidget:
    """*widget* as wide as the board draws it (a number, a host name)."""
    widget.setFixedWidth(width)
    return widget


class SettingsDialog(QDialog):
    """
    Procedo Studio's settings: LLM backend and Skills in ``settings.json``
    (:func:`get_settings_path`), the Validator page in the open project's config.
    """

    def __init__(
        self,
        task_config_manager: TaskConfigManager,
        parent=None,
        project_root=None,
        server_manager=None,
    ):
        super().__init__(parent)
        # The model-list fetch worker answers through this relay and never holds this dialog:
        # it may be deleted while it runs, and then its answer is dropped (procedo.core.off_gui).
        self._models_answer = answer_to(self._on_models_answer)
        self.setWindowTitle("Settings")
        self.setMinimumSize(700, 600)  # every page scrolls when shorter than its fields
        self.resize(780, 760)

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
        # poll latch (SettingsBackendProbe); this guards the model-refresh path.
        self._closed = False

        # Note: task / chat editing moved to the parent app's
        # ProjectConfigDialog -> Workflows tab (Phase 4). This dialog
        # now owns only LLM backend + validator-loop settings.
        self._setup_ui()
        self._probe = SettingsBackendProbe(
            self, server_manager=server_manager, project_root=project_root,
            backend=self.backend_combo.currentText, host=self.opencode_host,
            port=self.opencode_port, model_value=self._opencode_model_value,
            start_button=self.opencode_start_server, note=self.opencode_models_note,
            populate_models=self._populate_opencode_models)
        self._load_values()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(ui_kit.HeaderBand("Settings", "Procedo Studio \u00b7 this PC", stacked=True))
        self._paged = PagedArea(self, list_width=160)
        layout.addWidget(self._paged, 1)
        self._llm_page(self._paged.add_scroll_page("LLM backend"))
        self._skills_page(self._paged.add_scroll_page("Skills"))
        self._validator_page(self._paged.add_scroll_page("Validator"))

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = ui_kit.primary(QPushButton("Save"))
        self.save_btn.clicked.connect(self._on_save)
        layout.addWidget(ui_kit.FooterBand([self.cancel_btn, self.save_btn]))

    def _llm_page(self, page: QVBoxLayout) -> None:
        """The backend and its common parameters; the OpenCode server below (opencode only)."""
        page.addWidget(heading("LLM backend"))
        form = page_form()
        self.backend_combo = _fixed(QComboBox(), 220)
        self.backend_combo.addItems(["opencode", "none"])
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        _add_row(form, "Backend", field_row(self.backend_combo, None))

        self.context_window = _fixed(QSpinBox(), 120)
        self.context_window.setRange(1000, 2_000_000)
        self.context_window.setValue(16384)
        self.context_window.setToolTip(
            "Model's total context window (input + output token budget).\n"
            "Used only by the chat panel's 'Tokens: X/Y' indicator so you "
            "know when to reset the session.\n"
            "Not sent to the LLM. Examples: GPT-4 → 128000, Gemma 4 26B → 131072, Claude 4.7 → 1000000."
        )
        _add_row(form, "Context window", field_row(self.context_window, ui_kit.note("tokens")))

        self.request_timeout = _fixed(QDoubleSpinBox(), 80)
        self.request_timeout.setRange(10.0, 10800.0)
        self.request_timeout.setSingleStep(10.0)
        self.request_timeout.setValue(120.0)
        self.request_timeout.setKeyboardTracking(True)
        self.request_timeout.setToolTip("Request timeout in seconds (10-10800). For long generations, use 300-600 seconds.")
        _add_row(form, "Request timeout", field_row(
            self.request_timeout,
            ui_kit.note("seconds (10\u201310800); 300\u2013600 for long generations")))
        page.addLayout(form)

        self.opencode_group = self._opencode_section()
        page.addWidget(self.opencode_group)
        page.addStretch()

    def _opencode_section(self) -> QWidget:
        """The OpenCode server: where it listens, the model, how it starts, its config."""
        section = QWidget()
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        column.addWidget(heading("OpenCode server"))
        form = page_form()

        self.opencode_host = _fixed(QLineEdit(), 150)
        self.opencode_host.setPlaceholderText("127.0.0.1")
        self.opencode_port = _fixed(QSpinBox(), 80)
        self.opencode_port.setRange(1024, 65535)
        self.opencode_port.setValue(4096)
        _add_row(form, "Host \u00b7 port", field_row(self.opencode_host, self.opencode_port, None))

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
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.addWidget(self.opencode_model, 1)
        model_row.addWidget(self.opencode_model_refresh)
        _add_row(form, "Model", model_row)
        self.opencode_models_note = ui_kit.note("")
        form.addRow("", self.opencode_models_note)

        self.opencode_wsl_path = QLineEdit()
        self.opencode_wsl_path.setPlaceholderText("/mnt/c/...")
        self.opencode_wsl_path.setToolTip("WSL path to OpenCode directory")
        _add_row(form, "WSL path", self.opencode_wsl_path)

        self.opencode_startup_timeout = _fixed(QDoubleSpinBox(), 80)
        self.opencode_startup_timeout.setRange(10.0, 300.0)
        self.opencode_startup_timeout.setValue(60.0)
        self.opencode_startup_timeout.setToolTip("Timeout for backend startup")
        _add_row(form, "Startup timeout",
                 field_row(self.opencode_startup_timeout, ui_kit.note("seconds")))

        self.wizard_max_sessions = _fixed(QSpinBox(), 80)
        self.wizard_max_sessions.setRange(1, 16)
        self.wizard_max_sessions.setValue(5)
        self.wizard_max_sessions.setToolTip(
            "Max concurrent LLM sessions a batch wizard runs in parallel "
            "(per-item reads / builds); a wizard's Parallel spin-box "
            "defaults to this.")
        _add_row(form, "Wizard sessions",
                 field_row(self.wizard_max_sessions, ui_kit.note("max concurrent")))

        # The editor's MASTER blueprint (master.json) — providers only, project
        # agnostic. The per-launch opencode.json is DERIVED from it (MCP built
        # fresh) at each launch. Generated once from the project; user-editable.
        self.opencode_config_path = ui_kit.monospace(ui_kit.note(str(get_opencode_master_path())))
        self.opencode_config_path.setTextInteractionFlags(Qt.TextSelectableByMouse)  # to copy it
        self.opencode_config_path.setToolTip(self.opencode_config_path.text())  # a path wraps only at its breaks
        self.open_opencode_config_btn = QPushButton("Open…")
        self.open_opencode_config_btn.setToolTip(
            "Open the editor's OpenCode master blueprint (master.json) — the "
            "project-agnostic providers config. The per-launch opencode.json is "
            "derived from it (default model stripped so it auto-picks; MCP tools "
            "built fresh each launch). Edit it to change providers; delete it to "
            "regenerate from the project.")
        self.open_opencode_config_btn.clicked.connect(self._on_open_opencode_config)
        cfg_row = QHBoxLayout()
        cfg_row.setContentsMargins(0, 0, 0, 0)
        cfg_row.addWidget(self.opencode_config_path, 1)
        cfg_row.addWidget(self.open_opencode_config_btn)
        _add_row(form, "Config", cfg_row)

        _add_row(form, "Server", self._server_row())
        column.addLayout(form)
        return section

    def _server_row(self) -> QHBoxLayout:
        """Start server (only while it is down), Test connection, and what the last model
        query found."""
        # One-click recovery affordance: shown only when the server reads as down
        # (no /config). It relaunches OUR server on a daemon thread, then
        # re-queries the model list — so a crashed mid-session server can be
        # brought back from the picker WITHOUT restarting the whole app.
        self.opencode_start_server = QPushButton("Start server")
        self.opencode_start_server.setToolTip(
            "The OpenCode server is not reachable. Click to (re)launch it, then "
            "the model list refreshes automatically.")
        self.opencode_start_server.clicked.connect(lambda: self._probe.start_server())
        self.opencode_start_server.setVisible(False)
        self.test_connection_btn = QPushButton("Test connection")
        self.test_connection_btn.clicked.connect(lambda: self._probe.test_connection())
        self.opencode_server_pill = QLabel()
        self.opencode_server_pill.setVisible(False)
        row = field_row(self.opencode_start_server, self.test_connection_btn,
                        self.opencode_server_pill, None)
        row.setAlignment(self.opencode_server_pill, Qt.AlignmentFlag.AlignVCenter)  # a pill, not a bar
        return row

    def _skills_page(self, page: QVBoxLayout) -> None:
        """The skill chat's starting toggles and the model's prompt shape.

        The toggles only seed the INITIAL state of the skill chat's per-request
        toggles; the user can still flip each off/on per conversation. All default
        off (no web / no project-data access unless explicitly opted in)."""
        page.addWidget(heading("Skill chat defaults"))
        self.web_default = QCheckBox("Web access on by default")
        self.web_default.setToolTip(
            "Seed the skill chat's web toggle ON for new conversations. The "
            "model can still be flipped off per chat. Off by default.")
        self.save_docs_default = QCheckBox("Save downloaded datasheets by default")
        self.save_docs_default.setToolTip(
            "Default state of the skill chat's 💾 Save datasheets toggle: when "
            "on, downloaded datasheets are cached into the project documents "
            "folder for reuse.")
        self.project_tools_default = QCheckBox("Project data tools on by default")
        self.project_tools_default.setToolTip(
            "Seed the skill chat's project-data tools toggle ON for new "
            "conversations (netlist/BOM/component access via the project_tools "
            "MCP server). The model can still be flipped off per chat. Off by "
            "default.")
        page.addLayout(self._checks(self.web_default, self.save_docs_default,
                                    self.project_tools_default))

        page.addWidget(heading("Models"))
        self.fold_system_into_prompt = QCheckBox(
            "Fold skill into prompt (models with no system role, e.g. Gemma)")
        self.fold_system_into_prompt.setToolTip(
            "Gemma and similar models have no system role in their chat "
            "template, so the skill (sent as the message system prompt) is "
            "silently dropped and the model never sees it. Enable this to fold "
            "the skill into the user message instead. Leave OFF for GPT/Claude.")
        page.addLayout(self._checks(self.fold_system_into_prompt))
        page.addStretch()

    @staticmethod
    def _checks(*boxes: QCheckBox) -> QVBoxLayout:
        """Check boxes under a heading, closer to each other than the page's sections."""
        column = QVBoxLayout()
        column.setSpacing(8)
        for box in boxes:
            column.addWidget(box)
        return column

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
        from procedo.core import os_open
        master = get_opencode_master_path()
        os_open.open_or_log(master if master.exists() else master.parent)

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

        answer = self._models_answer

        def _worker():
            try:
                models = fetch_opencode_models(url)
            except Exception:
                models = []
            # The widget mutation runs on the UI thread (QTimer.singleShot from this
            # daemon would be lost — _models_probe_in_flight would stay True forever
            # and the note stuck at "Querying server…").
            answer((models, preserve))

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            # Spawn failed after the flag was set — clear it so a later Refresh
            # can probe again (never wedge the flag / note permanently).
            self._models_probe_in_flight = False
            self.opencode_models_note.setText(
                "Could not query the server — try Refresh again.")

    def _on_models_answer(self, answer):
        self._apply_opencode_models(*answer)

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
        self.opencode_server_pill.setVisible(reachable)
        if reachable:
            count = len(models)
            ui_kit.set_pill(self.opencode_server_pill, "success",
                            f"\u2713 Connected \u00b7 {count} model{'' if count == 1 else 's'}")
        if reachable:
            self.opencode_models_note.setText(
                "Pick from the list, type any id (e.g. openai/gpt-5.5), or "
                "leave empty for auto-pick.")
        elif can_start:
            self.opencode_models_note.setText(
                "OpenCode server is down — click “Start server” to "
                "relaunch it, then the list refreshes.")
        else:
            self.opencode_models_note.setText(
                "Server not reachable — type a model id or leave empty for "
                "auto-pick.")

    def done(self, result):
        """Cancel the Start-server poll on ANY dialog close (OK or Cancel).

        ``done()`` is the single chokepoint for accept/reject, so stopping the
        timer here guarantees no tick fires after the dialog (and its C++
        widgets) are gone — avoiding HTTP on the UI thread + use-after-close.
        """
        self._probe.shutdown()
        # Disposed flag for any OTHER posted callback (the model-refresh worker's
        # _apply_opencode_models) — it returns early on this instead of mutating
        # deleted C++ widgets after close.
        self._closed = True
        super().done(result)

    # ------------------------------------------------------------------ #
    # Validator page (per-project: validator_loop.* settings)             #
    # ------------------------------------------------------------------ #

    def _validator_page(self, page: QVBoxLayout) -> None:
        """The project's validator: on/off, its auto-correction attempts, and the
        board netlist's hidden net prefixes — disabled while no project is open
        (there is nowhere to persist them)."""
        from ..core.task_config import DEFAULT_MAX_VALIDATOR_ATTEMPTS

        title = heading("Validator")
        title.layout().insertWidget(1, ui_kit.set_pill(QLabel(), "info", "this project"))
        page.addWidget(title)

        # Top-level enable/disable: operator opt-out (Phase 4.6).
        # When unchecked, the deterministic validator is skipped, the
        # attempts field is greyed (no loop to run), and the
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
        page.addWidget(self.validator_enabled)

        form = page_form()
        self.validator_max_attempts = _fixed(QSpinBox(), 80)
        self.validator_max_attempts.setRange(1, 10)
        self.validator_max_attempts.setValue(DEFAULT_MAX_VALIDATOR_ATTEMPTS)
        self.validator_max_attempts.setToolTip(
            "Total LLM attempts (1 original + N-1 retries) before the "
            "auto-correction loop gives up and falls through to operator "
            "review. Higher values cost more tokens; default 3 resolves "
            "~95%% of fixable failures empirically. Per-project."
        )
        _add_row(form, "Correction attempts", field_row(
            self.validator_max_attempts, ui_kit.note("max, auto-correction loop")))
        # Editable only with a project open (somewhere to save it) and a loop to run.
        self.validator_enabled.toggled.connect(
            lambda on: self.validator_max_attempts.setEnabled(on and self._project_root is not None))

        # Board-netlist (Text-tab panel) hidden net-name prefixes — per project.
        self.net_hide_prefixes = _fixed(QLineEdit(), 160)
        self.net_hide_prefixes.setPlaceholderText("Net")
        self.net_hide_prefixes.setToolTip(
            "Comma-separated net-name prefixes hidden by default in the Text "
            "tab's netlist panel (case-insensitive). Default 'Net' hides Altium "
            "autogen names like NetD16_A."
        )
        _add_row(form, "Hidden net prefixes", field_row(self.net_hide_prefixes, None))
        page.addLayout(form)

        page.addWidget(ui_kit.note(
            "Stored in config/config.json (validator_loop). Applies to the next LLM task."))
        if self._project_root is None:
            self.validator_enabled.setEnabled(False)
            self.validator_max_attempts.setEnabled(False)
            self.net_hide_prefixes.setEnabled(False)
            page.addWidget(ui_kit.note("No project open — open a project to edit these settings."))
        page.addStretch()

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
        ``validator_loop`` config section. No-op without a project; raises when the
        project's config cannot be read or written (the caller says so)."""
        if self._project_root is None:
            return
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
        write_hide_prefixes(Path(self._project_root) / "config", prefixes)

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
        self.fold_system_into_prompt.setChecked(
            opencode.get("fold_system_into_prompt", False))

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
                ", ".join(read_hide_prefixes(Path(self._project_root) / "config" if self._project_root else None)))
        except Exception:
            log.exception("Failed to load net_explorer.hide_prefixes")

    def _on_save(self):
        """Save settings.

        Persists LLM backend configuration to ``settings.json`` and the
        per-project ``validator_loop`` section. Task / chat editing
        moved to ``_WorkflowsTab`` in the parent app's Project
        Configuration dialog (Phase 4).
        """
        # Save LLM backend settings to settings.json: the file as stored NOW (keys another
        # writer owns, e.g. the wizard's, or written while this dialog was open, survive)
        # with only the dialog's own keys overwritten.
        owned = {
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
                "fold_system_into_prompt": self.fold_system_into_prompt.isChecked(),
            },
            "wizard": {
                "max_concurrent_sessions": self.wizard_max_sessions.value(),
            },
        }
        try:
            settings = read_settings()  # never a fallback to {}: that would wipe the rest
            settings["llm_backend"] = self.backend_combo.currentText()
            for section, values in owned.items():
                stored = settings.get(section, {})
                if not isinstance(stored, dict):
                    raise ValueError(f"settings.json: the '{section}' section is not an object")
                settings[section] = {**stored, **values}
            save_settings(settings)
        except Exception as e:  # the dialog stays open, its values kept: the user can retry
            QMessageBox.critical(self, "Save Failed", str(e))
            return
        self._settings = settings
        try:  # Validator tab — the project's validator_loop section, said apart when it fails
            self._save_validator_values()
        except Exception as e:  # a project config unreachable or damaged: never replaced
            QMessageBox.critical(self, "Project Settings Not Saved",
                                 "The application settings were saved; the project's Validator "
                                 f"settings were not:\n\n{e}")
        self.accept()

    
    def get_settings(self) -> dict:
        """Get the current settings."""
        return self._settings
