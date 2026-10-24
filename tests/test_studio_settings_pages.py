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
    assert [b.text() for b in dlg.findChildren(QPushButton) if b.objectName() == "primaryButton"] == ["Save"]
    titles = [dlg._paged.page_list.item(i).text() for i in range(dlg._paged.page_list.count())]
    assert titles == list(PAGES)
    fields = {
        "LLM backend": (dlg.backend_combo, dlg.context_window, dlg.request_timeout, dlg.opencode_host,
                        dlg.opencode_port, dlg.opencode_model, dlg.opencode_model_refresh,
                        dlg.opencode_start_server, dlg.opencode_wsl_path, dlg.opencode_startup_timeout,
                        dlg.wizard_max_sessions, dlg.opencode_config_path, dlg.open_opencode_config_btn,
                        dlg.test_connection_btn),
        "Skills": (dlg.web_default, dlg.save_docs_default, dlg.project_tools_default,
                   dlg.fold_system_into_prompt),
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
            "port": 4100, "host": "10.0.0.2", "model": "openai/gpt-5.4", "wsl_path": "/mnt/c/oc",
            "startup_timeout": 90.25, "web_default": True, "save_docs_default": False,
            "project_tools_default": True, "fold_system_into_prompt": True,
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
    assert (opencode["web_default"], opencode["save_docs_default"], opencode["project_tools_default"],
            opencode["fold_system_into_prompt"]) == (False, True, False, False)


def test_save_keeps_every_key_the_dialog_does_not_own_as_stored_at_save_time(qapp, no_network):
    """Save overwrites only its own fields: the wizard's keys (``set_wizard_setting``), keys of
    a newer Studio and keys written while the dialog was open all survive."""
    save_settings({
        "llm_backend": "opencode", "custom_output_format": "fmt",
        "opencode": {"port": 4100, "a_newer_key": 1},
        "wizard": {"max_concurrent_sessions": 3, "last_skill": "dcdc"},
    })
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
        "max_concurrent_sessions": 5, "last_skill": "dcdc", "written_while_open": True}
    assert dlg.get_settings() == saved


@pytest.mark.parametrize("stored, said", [
    ('{"wizard": {"last_skill": "dcdc"}', "settings.json"),  # damaged (or half-read)
    ('{"wizard": ["last_skill"]}', "wizard"),  # a section the dialog writes is not an object
])
def test_save_refuses_when_the_stored_settings_cannot_be_read_and_writes_nothing(
        qapp, no_network, monkeypatch, stored, said):
    """Never fall back to {} at Save: writing only the dialog's keys would wipe the rest."""
    from procedo.studio.core.studio_settings import get_settings_path

    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(settings_dialog.QMessageBox, "critical",
                        lambda parent, title, text: errors.append((title, text)))
    dlg = _open(qapp)
    get_settings_path().write_text(stored, encoding="utf-8")
    dlg._on_save()
    assert [title for title, _ in errors] == ["Save Failed"]
    assert said in errors[0][1]
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
    assert load_settings() == {"llm_backend": "none",
                               "wizard": {"other": 1, "max_concurrent_sessions": 4}}
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
        qapp, no_network, project, monkeypatch):
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(settings_dialog.QMessageBox, "critical",
                        lambda parent, title, text: errors.append((title, text)))
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


def test_the_server_pill_says_connected_with_the_model_count_only_when_models_came_back(qapp, no_network):
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
        for b, rb in rects[i + 1:]:
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
    monkeypatch.setattr(settings_dialog, "fetch_opencode_models",
                        lambda url: gate.wait(5) and [])
    dlg = SettingsDialog(None)
    assert dlg._models_probe_in_flight
    shiboken6.delete(dlg)
    gate.set()
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)
    assert errors == []
