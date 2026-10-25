"""Procedo Studio's Settings (redesign slice 7b, board S6): Bench's paged dialog — LLM backend,
Skills, Validator — that keeps every setting where it was saved before (settings.json, and the
project's config for the Validator page) and never overlaps at its minimum size."""

from __future__ import annotations

import json
import time

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from procedo.ui import ui_kit
from procedo.core.board_netlist import read_hide_prefixes
from procedo.studio.dialogs import settings_dialog
from procedo.studio.core.studio_settings import load_settings, save_settings
from procedo.studio.dialogs.settings_dialog import SettingsDialog
from procedo.studio.llm import launch_config
from procedo.studio.llm import validator_loop_settings

from .test_main_window_characterization import qapp as qapp

PAGES = ("LLM backend", "Skills", "Validator")


@pytest.fixture
def no_network(monkeypatch):
    """The model list a dialog asks for on opening: an empty answer, never a socket."""
    monkeypatch.setattr(settings_dialog, "fetch_opencode_models", lambda url: [])


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(json.dumps({"project_id": "p"}), encoding="utf-8")
    return root


def _open(qapp, project_root=None):
    dlg = SettingsDialog(None, project_root=project_root)
    deadline = time.monotonic() + 5
    while getattr(dlg, "_models_probe_in_flight", False) and time.monotonic() < deadline:
        qapp.processEvents()  # the model probe answers on the UI thread
    assert not dlg._models_probe_in_flight
    return dlg


def _page_content(dlg, title: str) -> QWidget:
    dlg._paged.select(title)
    return dlg._paged.stack.currentWidget().widget()  # the page's scroll area holds it


def test_the_dialog_is_benchs_frame_with_three_pages_and_their_fields(qapp, no_network, project):
    dlg = _open(qapp, project)
    header = dlg.findChild(ui_kit.HeaderBand)
    assert header.title.text() == "Settings"
    assert dlg.findChild(ui_kit.FooterBand) is not None
    assert [
        b.text() for b in dlg.findChildren(QPushButton) if b.objectName() == "primaryButton"
    ] == ["Save"]
    titles = [dlg._paged.page_list.item(i).text() for i in range(dlg._paged.page_list.count())]
    assert titles == list(PAGES)
    fields = {
        "LLM backend": (
            dlg.backend_combo,
            dlg.context_window,
            dlg.request_timeout,
            dlg.opencode_host,
            dlg.opencode_port,
            dlg.opencode_model,
            dlg.opencode_model_refresh,
            dlg.opencode_start_server,
            dlg.opencode_wsl_path,
            dlg.opencode_startup_timeout,
            dlg.wizard_max_sessions,
            dlg.opencode_config_path,
            dlg.open_opencode_config_btn,
            dlg.test_connection_btn,
        ),
        "Skills": (
            dlg.web_default,
            dlg.save_docs_default,
            dlg.project_tools_default,
            dlg.fold_system_into_prompt,
        ),
        "Validator": (dlg.validator_enabled, dlg.validator_max_attempts, dlg.net_hide_prefixes),
    }
    for title, widgets in fields.items():
        content = _page_content(dlg, title)
        for widget in widgets:
            assert content.isAncestorOf(widget), (title, widget)
    dlg.reject()


def test_every_setting_round_trips_through_settings_json_skills_included(qapp, no_network):
    saved = {
        "llm_backend": "opencode",
        "common_llm": {"context_window": 400000, "request_timeout": 312.5},
        "opencode": {
            "port": 4100,
            "host": "10.0.0.2",
            "model": "openai/gpt-5.4",
            "wsl_path": "/mnt/c/oc",
            "startup_timeout": 90.25,
            "web_default": True,
            "save_docs_default": False,
            "project_tools_default": True,
            "fold_system_into_prompt": True,
        },
        "wizard": {"max_concurrent_sessions": 7},
    }
    save_settings(saved)
    dlg = _open(qapp)
    assert dlg.web_default.isChecked() and dlg.project_tools_default.isChecked()
    assert dlg.fold_system_into_prompt.isChecked() and not dlg.save_docs_default.isChecked()
    assert dlg.opencode_config_path.text() == str(launch_config.get_opencode_master_path())
    dlg._on_save()
    assert load_settings() == saved

    # The Skills page's boxes write the same keys under "opencode" as before.
    dlg = _open(qapp)
    dlg.web_default.setChecked(False)
    dlg.save_docs_default.setChecked(True)
    dlg.project_tools_default.setChecked(False)
    dlg.fold_system_into_prompt.setChecked(False)
    dlg._on_save()
    opencode = load_settings()["opencode"]
    assert (
        opencode["web_default"],
        opencode["save_docs_default"],
        opencode["project_tools_default"],
        opencode["fold_system_into_prompt"],
    ) == (False, True, False, False)


def test_save_keeps_every_key_the_dialog_does_not_own_as_stored_at_save_time(qapp, no_network):
    """Save overwrites only its own fields: the wizard's keys (``set_wizard_setting``), keys of
    a newer Studio and keys written while the dialog was open all survive."""
    save_settings(
        {
            "llm_backend": "opencode",
            "custom_output_format": "fmt",
            "opencode": {"port": 4100, "a_newer_key": 1},
            "wizard": {"max_concurrent_sessions": 3, "last_skill": "dcdc"},
        }
    )
    dlg = _open(qapp)
    while_open = load_settings()  # the wizard writes a key while Settings is open
    while_open["wizard"]["written_while_open"] = True
    save_settings(while_open)
    dlg.wizard_max_sessions.setValue(5)
    dlg._on_save()
    saved = load_settings()
    assert saved["custom_output_format"] == "fmt"
    assert saved["opencode"]["a_newer_key"] == 1 and saved["opencode"]["port"] == 4100
    assert saved["wizard"] == {
        "max_concurrent_sessions": 5,
        "last_skill": "dcdc",
        "written_while_open": True,
    }
    assert dlg.get_settings() == saved


def test_save_replaces_a_section_it_owns_that_is_not_an_object(qapp, no_network):
    """A damaged section is the dialog's own to replace: Save is not refused until the file
    is fixed by hand, and the keys it does not own survive."""
    save_settings({"custom_output_format": "fmt", "wizard": ["last_skill"]})
    dlg = _open(qapp)
    dlg.wizard_max_sessions.setValue(4)
    dlg._on_save()
    saved = load_settings()
    assert saved["wizard"] == {"max_concurrent_sessions": 4}
    assert saved["custom_output_format"] == "fmt"


def test_save_refuses_when_the_stored_settings_cannot_be_read_and_writes_nothing(
    qapp, no_network, monkeypatch
):
    """Never fall back to {} at Save: writing only the dialog's keys would wipe the rest."""
    stored = '{"wizard": {"last_skill": "dcdc"}'  # damaged (or half-read)
    from procedo.studio.core.studio_settings import get_settings_path

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "critical",
        lambda parent, title, text: errors.append((title, text)),
    )
    dlg = _open(qapp)
    get_settings_path().write_text(stored, encoding="utf-8")
    dlg._on_save()
    assert [title for title, _ in errors] == ["Save Failed"]
    assert "settings.json" in errors[0][1]
    assert get_settings_path().read_text(encoding="utf-8") == stored
    assert dlg.result() != dlg.DialogCode.Accepted
    dlg.reject()


def test_a_wizard_setting_is_never_written_over_settings_it_cannot_read():
    """``set_wizard_setting`` keeps every other key, and fails on a damaged file instead of
    writing only its own key (the wizard's caller treats the save as best-effort)."""
    from procedo.studio.authoring.wizard_api import StudioAuthoringApi
    from procedo.studio.core.studio_settings import get_settings_path

    save_settings({"llm_backend": "none", "wizard": {"other": 1}})
    StudioAuthoringApi.set_wizard_setting(None, "max_concurrent_sessions", 4)
    assert load_settings() == {
        "llm_backend": "none",
        "wizard": {"other": 1, "max_concurrent_sessions": 4},
    }
    damaged = '{"llm_backend": "none", "wizard": '
    get_settings_path().write_text(damaged, encoding="utf-8")
    with pytest.raises(ValueError, match="settings.json"):
        StudioAuthoringApi.set_wizard_setting(None, "max_concurrent_sessions", 5)
    assert get_settings_path().read_text(encoding="utf-8") == damaged


def test_the_validator_page_saves_to_the_project_config(qapp, no_network, project):
    dlg = _open(qapp, project)
    dlg.validator_enabled.setChecked(False)
    dlg.validator_max_attempts.setValue(5)
    dlg.net_hide_prefixes.setText("Net, GND_")
    dlg._on_save()
    section = validator_loop_settings.load_settings(project)
    assert section["enabled"] is False and section["max_attempts"] == 5
    assert read_hide_prefixes(project / "config") == ["Net", "GND_"]
    assert "validator_loop" not in load_settings()  # the project's, never settings.json

    dlg = _open(qapp, project)
    assert not dlg.validator_enabled.isChecked()
    assert not dlg.validator_max_attempts.isEnabled()  # no loop to run while it is off
    assert dlg.validator_max_attempts.value() == 5
    dlg.reject()


def test_a_project_config_that_cannot_be_saved_is_said_and_the_app_settings_still_save(
    qapp, no_network, project, monkeypatch
):
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "critical",
        lambda parent, title, text: errors.append((title, text)),
    )
    dlg = _open(qapp, project)
    (project / "config" / "config.json").write_text('{"project_id": ', encoding="utf-8")
    dlg.validator_enabled.setChecked(False)
    dlg.request_timeout.setValue(277.0)
    dlg._on_save()
    assert [title for title, _ in errors] == ["Project Settings Not Saved"]
    assert "config.json" in errors[0][1]
    assert load_settings()["common_llm"]["request_timeout"] == 277.0  # the app's part saved
    assert dlg.result() == dlg.DialogCode.Accepted
    assert (project / "config" / "config.json").read_text(encoding="utf-8") == '{"project_id": '


def test_without_a_project_the_validator_page_is_disabled_and_says_why(qapp, no_network):
    dlg = _open(qapp)
    for widget in (dlg.validator_enabled, dlg.validator_max_attempts, dlg.net_hide_prefixes):
        assert not widget.isEnabled()
    dlg.validator_enabled.setChecked(False)
    dlg.validator_enabled.setChecked(True)  # toggling never makes the attempts editable
    assert not dlg.validator_max_attempts.isEnabled()
    notes = [label.text() for label in _page_content(dlg, "Validator").findChildren(QLabel)]
    assert any(text.startswith("No project open") for text in notes)
    dlg.reject()


def test_the_server_pill_says_connected_with_the_model_count_only_when_models_came_back(
    qapp, no_network
):
    dlg = _open(qapp)
    dlg.show()
    assert dlg.opencode_server_pill.isHidden()  # the opening probe found no server
    dlg._apply_opencode_models(["openai/gpt-5.4", "openai/gpt-5.5"], "")
    assert not dlg.opencode_server_pill.isHidden()
    assert dlg.opencode_server_pill.text() == "✓ Connected · 2 models"
    assert dlg.opencode_server_pill.property("pill") == "success"
    dlg._apply_opencode_models([], "")
    assert dlg.opencode_server_pill.isHidden()
    dlg.reject()


def _leaves(content: QWidget) -> list[QWidget]:
    """The page's visible fields, labels and buttons (not the parts inside a combo or a spin box)."""
    kinds = (QLabel, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractButton)
    shown = [w for w in content.findChildren(QWidget) if isinstance(w, kinds) and w.isVisible()]
    return [w for w in shown if not any(o is not w and o.isAncestorOf(w) for o in shown)]


@pytest.mark.parametrize("title", PAGES)
def test_nothing_overlaps_or_is_cut_at_the_minimum_size(qapp, no_network, project, title):
    dlg = _open(qapp, project)
    dlg._apply_opencode_models(["openai/gpt-5.4"], "")  # the pill shown: the widest Server row
    dlg.resize(dlg.minimumSize())
    dlg.show()
    content = _page_content(dlg, title)
    for _ in range(5):
        qapp.processEvents()
    assert dlg.size() == dlg.minimumSize()
    rects = [(w, QRect(w.mapTo(content, QPoint(0, 0)), w.size())) for w in _leaves(content)]
    assert len(rects) >= 4
    for widget, rect in rects:
        assert rect.right() < content.width(), f"{widget!r} {widget.property('text')} is cut"
        assert widget.height() >= widget.minimumSizeHint().height(), f"{widget!r} is crushed"
    for i, (a, ra) in enumerate(rects):
        for b, rb in rects[i + 1 :]:
            assert not ra.intersects(rb), f"{a!r} overlaps {b!r}"
    for button in (dlg.save_btn, dlg.cancel_btn):
        assert button.visibleRegion().boundingRect().height() == button.height()
    dlg.reject()


def test_a_dialog_deleted_while_its_model_probe_runs_is_never_touched(qapp, monkeypatch):
    """The probe's worker posts its answer, never emits on the dialog: deleting the dialog
    while the probe runs drops the answer instead of crashing the worker."""
    import shiboken6
    import threading

    gate, errors = threading.Event(), []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    monkeypatch.setattr(settings_dialog, "fetch_opencode_models", lambda url: gate.wait(5) and [])
    dlg = SettingsDialog(None)
    assert dlg._models_probe_in_flight
    shiboken6.delete(dlg)
    gate.set()
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)
    assert errors == []


class _SlowManager:
    """A server manager whose start / health check each take ~1 s (never a real server)."""

    instances: list = []

    def __init__(self, config):
        self.last_status = None
        self.stopped = 0
        _SlowManager.instances.append(self)

    def is_available(self):
        return True

    def start(self):
        time.sleep(1.0)
        return True

    def health_check(self):
        time.sleep(1.0)
        return True

    def stop(self):
        self.stopped += 1


class _SlowBackend:
    is_running = False

    def __init__(self, config, lifecycle=None):
        pass

    def start(self):
        return True

    def stop(self):
        pass

    def send_request(self, request):
        time.sleep(1.0)
        return type("R", (), {"error_message": ""})()


def _slow_test_connection(monkeypatch, tmp_path):
    from procedo.studio.dialogs import settings_backend_probe as probe
    from procedo.studio.llm import opencode_backend, server_manager

    shown, cleaned = [], []
    monkeypatch.setattr(probe, "ensure_master_config", lambda seed_from=None: None)
    monkeypatch.setattr(probe, "build_launch_config", lambda root, launch_dir=None: tmp_path)
    monkeypatch.setattr(probe, "get_opencode_launch_test_dir", lambda: tmp_path)
    monkeypatch.setattr(probe, "remove_launch_files", lambda d: cleaned.append(d))
    monkeypatch.setattr(server_manager, "OpenCodeServerManager", _SlowManager)
    monkeypatch.setattr(opencode_backend, "OpenCodeBackend", _SlowBackend)
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(
            probe.QMessageBox,
            kind,
            staticmethod(lambda parent, title, text, kind=kind: shown.append((kind, title, text))),
        )
    _SlowManager.instances.clear()
    return shown, cleaned


def test_test_connection_runs_off_the_gui_thread_and_shows_the_same_result(
    qapp, no_network, project, monkeypatch, tmp_path
):
    from PySide6.QtCore import QTimer

    shown, cleaned = _slow_test_connection(monkeypatch, tmp_path)
    dlg = _open(qapp, project)
    dlg.backend_combo.setCurrentText("opencode")
    last, gaps = [time.monotonic()], []

    def tick():
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(10)
    dlg.test_connection_btn.click()
    assert not dlg.test_connection_btn.isEnabled()
    assert dlg.test_connection_cue.active
    dlg.test_connection_btn.click()  # a second click while it runs does nothing
    deadline = time.monotonic() + 15
    while not shown and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    timer.stop()
    assert max(gaps) < 0.5
    assert len(shown) == 1 and len(_SlowManager.instances) == 1
    kind, title, text = shown[0]
    assert (kind, title) == ("information", "Test Connection")
    assert text.startswith("✓ OpenCode is working\n\nHost: 127.0.0.1\nPort: ")
    assert dlg.test_connection_btn.isEnabled() and not dlg.test_connection_cue.active
    assert _SlowManager.instances[0].stopped == 1 and cleaned
    dlg.reject()


def test_test_connection_with_no_backend_says_so_and_creates_no_launch_test_folder(
    qapp, no_network, project, monkeypatch, tmp_path
):
    from procedo.studio.dialogs import settings_backend_probe as probe

    config = tmp_path / "opencode"
    monkeypatch.setattr(launch_config, "get_opencode_config_dir", lambda: config)
    shown = []
    monkeypatch.setattr(
        probe.QMessageBox,
        "information",
        staticmethod(lambda parent, title, text: shown.append((title, text))),
    )
    dlg = _open(qapp, project)
    dlg.backend_combo.setCurrentText("none")
    dlg.test_connection_btn.click()
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.005)  # a test worker, had one started, would have run by now
    assert shown == [("Test Connection", "No backend selected.")]
    assert dlg.test_connection_btn.isEnabled() and not dlg.test_connection_cue.active
    assert not config.exists()
    dlg.reject()


def test_closing_the_dialog_mid_test_shows_nothing_and_still_stops_its_server(
    qapp, no_network, project, monkeypatch, tmp_path
):
    shown, cleaned = _slow_test_connection(monkeypatch, tmp_path)
    dlg = _open(qapp, project)
    dlg.backend_combo.setCurrentText("opencode")
    dlg.test_connection_btn.click()
    dlg.reject()
    dlg.deleteLater()
    deadline = time.monotonic() + 15
    while not cleaned and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert cleaned and _SlowManager.instances[0].stopped == 1
    for _ in range(20):
        qapp.processEvents()
    assert shown == []


def test_start_server_is_off_while_a_test_runs_and_comes_back_as_it_was(
    qapp, no_network, project, monkeypatch, tmp_path
):
    from unittest.mock import MagicMock

    shown, _ = _slow_test_connection(monkeypatch, tmp_path)
    dlg = _open(qapp, project)
    dlg.backend_combo.setCurrentText("opencode")
    dlg._probe._server_manager = MagicMock()
    for was in (True, False):
        shown.clear()
        dlg.opencode_start_server.setEnabled(was)
        dlg.test_connection_btn.click()
        assert not dlg.opencode_start_server.isEnabled()
        dlg._probe.start_server()  # even called directly: nothing starts meanwhile
        dlg._probe._server_manager.ensure_running.assert_not_called()
        deadline = time.monotonic() + 15
        while not shown and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert dlg.opencode_start_server.isEnabled() is was
    dlg.reject()


def test_test_connection_does_nothing_while_a_start_server_poll_runs(
    qapp, no_network, project, monkeypatch, tmp_path
):
    from PySide6.QtCore import QTimer

    shown, _ = _slow_test_connection(monkeypatch, tmp_path)
    dlg = _open(qapp, project)
    dlg.backend_combo.setCurrentText("opencode")
    poll = QTimer(dlg)
    poll.start(1000)
    dlg._probe._start_poll_timer = poll
    dlg.test_connection_btn.click()
    assert dlg.test_connection_btn.isEnabled() and not dlg.test_connection_cue.active
    time.sleep(0.2)
    qapp.processEvents()
    assert not _SlowManager.instances and shown == []
    poll.stop()  # the poll ended: a test may run again
    dlg.test_connection_btn.click()
    assert not dlg.test_connection_btn.isEnabled()
    deadline = time.monotonic() + 15
    while not shown and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert len(shown) == 1
    dlg.reject()


class _RecordingManager:
    """A server manager that records what the Test connection asks of it (never a real server)."""

    def __init__(self, config, events):
        self.config, self.events, self.last_status = config, events, None
        self.server_url = "http://127.0.0.1:5005"
        self.is_alive = True

    def is_available(self):
        return True

    def start(self):
        self.events.append("start")
        return True

    def health_check(self):
        return True

    def ensure_running(self):
        return True

    def stop(self):
        self.events.append("stop")


class _LifecycleBackend:
    """A backend that, like the real client, asks its lifecycle for the server's URL on start."""

    def __init__(self, config, lifecycle, events):
        self._lifecycle, self.events = lifecycle, events
        self.is_running = False  # as the real one: no session until started

    def start(self):
        from opencode_client.lifecycle import ServerUnavailable

        try:
            self.events.append(self._lifecycle.ensure_running())
        except ServerUnavailable:
            return False
        self.is_running = True
        return True

    def send_request(self, request):
        self.events.append("request")
        return type("R", (), {"error_message": ""})()


def test_the_chat_test_runs_on_the_manager_s_server_and_the_manager_stops_it_after(
    monkeypatch, tmp_path
):
    """No second server: one only the manager can stop by its Linux pid (stopping wsl.exe
    alone can leave it running)."""
    import opencode_client.lifecycle as package_lifecycle
    from procedo.studio.dialogs import settings_backend_probe as probe
    from procedo.studio.llm import opencode_backend, server_manager

    events, spawned = [], []
    monkeypatch.setattr(probe, "ensure_master_config", lambda seed_from=None: None)
    monkeypatch.setattr(probe, "build_launch_config", lambda root, launch_dir=None: tmp_path)
    monkeypatch.setattr(probe, "get_opencode_launch_test_dir", lambda: tmp_path)
    monkeypatch.setattr(probe, "remove_launch_files", lambda d: None)
    monkeypatch.setattr(
        server_manager, "OpenCodeServerManager", lambda config: _RecordingManager(config, events)
    )
    monkeypatch.setattr(
        opencode_backend,
        "OpenCodeBackend",
        lambda config, lifecycle: _LifecycleBackend(config, lifecycle, events),
    )
    monkeypatch.setattr(package_lifecycle, "_health_ok", lambda *a, **k: False)

    def popen(*args, **kwargs):
        spawned.append(args)
        raise OSError("no second server")

    monkeypatch.setattr(package_lifecycle.subprocess, "Popen", popen)
    answer = probe.SettingsBackendProbe._run_test("opencode", None, "127.0.0.1", 5005, None)
    assert spawned == []
    assert answer[0] == "information", answer
    assert events == ["start", "http://127.0.0.1:5005", "request", "stop"]


def test_studio_exiting_while_a_test_connection_runs_stops_its_server(monkeypatch, tmp_path):
    """The test runs on a daemon thread: the manager's stop is an exit hook while it runs,
    and only then."""
    from types import SimpleNamespace

    from procedo.studio.dialogs import settings_backend_probe as probe
    from procedo.studio.llm import opencode_backend, server_manager

    hooks, seen = [], []
    monkeypatch.setattr(
        probe,
        "atexit",
        SimpleNamespace(register=hooks.append, unregister=hooks.remove),
        raising=False,
    )
    monkeypatch.setattr(probe, "ensure_master_config", lambda seed_from=None: None)
    monkeypatch.setattr(probe, "build_launch_config", lambda root, launch_dir=None: tmp_path)
    monkeypatch.setattr(probe, "get_opencode_launch_test_dir", lambda: tmp_path)
    monkeypatch.setattr(probe, "remove_launch_files", lambda d: None)

    class _Booting(_RecordingManager):
        def start(self):
            seen.append(self.stop in hooks)  # Studio exits during the boot
            return False

    monkeypatch.setattr(
        server_manager, "OpenCodeServerManager", lambda config: _Booting(config, [])
    )
    monkeypatch.setattr(opencode_backend, "OpenCodeBackend", _LifecycleBackend)
    answer = probe.SettingsBackendProbe._run_test("opencode", None, "127.0.0.1", 5005, None)
    assert seen == [True]
    assert hooks == []
    assert answer[0] == "warning"
