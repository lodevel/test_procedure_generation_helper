"""Procedo Studio's settings folder is named in ONE place (``settings_dialog.get_settings_path``,
through the host's ``app_identity``), and Studio migrates the old folder at start-up before
anything reads a setting."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from project_services import app_identity, app_identity_migration

from workflow_editor import __main__ as studio_main
from workflow_editor import main_window
from workflow_editor.dialogs import settings_dialog

_PKG = Path(__file__).resolve().parents[1] / "workflow_editor"
_THE_ONE_SITE = "dialogs/settings_dialog.py"


def test_no_site_but_the_settings_dialog_names_the_settings_folder():
    """A second place building the path would split the settings again: Studio reaches its
    folder through ``get_settings_path`` alone, and never writes QSettings of its own."""
    folder = re.compile(r"studio_settings_dir\(|roaming_data_dir\(|\.workflow_editor\b")
    offenders = []
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        text = path.read_text(encoding="utf-8-sig")
        if re.search(r"\bQSettings\(", text) or (rel != _THE_ONE_SITE and folder.search(text)):
            offenders.append(rel)
    assert offenders == []


def test_the_tests_never_reach_the_real_folders_settings_or_environment(tmp_path):
    """The sandbox (conftest -> project_services.testing) is in force for every editor test."""
    import os

    for folder in (
        app_identity.roaming_data_dir(),
        app_identity.local_data_dir(),
        app_identity.studio_settings_dir(),
        app_identity.legacy_roaming_data_dir(),
        app_identity.legacy_local_data_dir(),
        app_identity.legacy_studio_settings_dir(),
        settings_dialog.get_settings_path(),
        Path.home(),
        *(Path(os.environ[name]) for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME")),
    ):
        assert folder.is_relative_to(tmp_path), folder
    store = app_identity.settings("MainWindow")
    store.setValue("probe", 1)
    store.sync()
    assert Path(store.fileName()).is_relative_to(tmp_path)
    for old, new in app_identity.legacy_settings_pairs():
        assert Path(old.fileName()).is_relative_to(tmp_path)
        assert Path(new.fileName()).is_relative_to(tmp_path)


def test_the_settings_live_in_studio_s_folder():
    path = settings_dialog.get_settings_path()

    assert path == app_identity.studio_settings_dir() / "settings.json"
    assert settings_dialog.get_opencode_master_path().is_relative_to(path.parent)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        debug=False, log_file=None, project_root=None, rules_root=None, test_name=None,
        test_dir=None, llm_backend=None, llm_profile=None,
    )


def test_studio_start_migrates_before_anything_reads_its_settings(monkeypatch):
    seen: list[str] = []
    organizations: list[str] = []
    read = settings_dialog.get_settings_path

    class _App:
        def __init__(self, argv):
            pass

        def setOrganizationName(self, name):  # noqa: N802 — Qt's spelling
            organizations.append(name)

        def setApplicationName(self, name):  # noqa: N802
            pass

        def setApplicationVersion(self, version):  # noqa: N802
            pass

        def setAttribute(self, *attribute):  # noqa: N802
            pass

        def exec(self):
            return 0

    class _Window:
        """The real window's first act: ``self._settings = load_settings()``."""

        def __init__(self, **kwargs):
            main_window.load_settings()

        def showMaximized(self):  # noqa: N802
            pass

    monkeypatch.setattr(studio_main, "parse_args", _args)
    monkeypatch.setattr("workflow_editor.logging_config.setup_logging", lambda **kw: None)
    monkeypatch.setattr(studio_main, "QApplication", _App)
    monkeypatch.setattr(studio_main, "MainWindow", _Window)
    monkeypatch.setattr(studio_main, "apply_app_theme", lambda name, app: name)
    monkeypatch.setattr(app_identity_migration, "migrate", lambda: seen.append("migrate"))
    monkeypatch.setattr(
        settings_dialog, "get_settings_path", lambda: (seen.append("settings"), read())[1]
    )

    assert studio_main.main() == 0

    assert seen[0] == "migrate" and "settings" in seen
    # One identity: Studio's Qt organization is Procedo's.
    assert organizations == [app_identity.SETTINGS_ORGANIZATION]


def test_studio_s_migration_copies_the_old_settings_folder():
    """The same migrate() Bench runs: Studio's old folder lands in the new one (fake keys)."""
    old = app_identity.legacy_studio_settings_dir()
    (old / "opencode" / "launch").mkdir(parents=True)
    (old / "settings.json").write_text('{"api_key": "sk-fake"}', encoding="utf-8")
    (old / "opencode" / "master.json").write_text('{"provider": {}}', encoding="utf-8")
    (old / "opencode" / "launch" / "opencode.pid").write_text("{}", encoding="utf-8")

    app_identity_migration.migrate()

    assert settings_dialog.load_settings() == {"api_key": "sk-fake"}
    assert settings_dialog.get_opencode_master_path().read_text(encoding="utf-8") == (
        '{"provider": {}}'
    )
    assert not (app_identity.studio_settings_dir() / "opencode" / "launch").exists()
