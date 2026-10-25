"""Task #39 — a bundle imported into the open project goes live immediately.

Covers the three seams:

* ``TaskConfigManager`` resolves bundle defaults project-root-first
  (``<project>/bundle/defaults.json`` beats a stale
  ``PROCEDO_BUNDLE_DEFAULTS_PATH``), and ``reload()`` picks up a CHANGED
  defaults.json — tasks appear/disappear and prompt templates swap.
* ``bundle_capabilities.load_bundle_equipment_profiles`` resolves
  project-root-first with the env var as legacy fallback.
* The editor's bundle watcher plumbing (the real ``project_controller``
  functions driven on a light harness): bundle-dir / project-root events fire ONE
  debounced reload, unrelated writes fire none, and the
  ``.bundle_installed.json`` stamp is a belt-and-braces trigger.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QFileSystemWatcher, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from procedo.studio.core.task_config import TaskConfigManager  # noqa: E402
from procedo.core.studio_launch_env import BUNDLE_DEFAULTS_PATH  # noqa: E402
from procedo.studio.pack_ops import bundle_capabilities  # noqa: E402
from procedo.studio.controllers import project_controller  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(task_id: str, prompt: str) -> dict:
    return {
        "id": task_id,
        "name": task_id,
        "button_label": task_id,
        "prompt_template": prompt,
        "enabled": True,
    }


def _write_defaults(root: Path, tasks: list, profiles: list | None = None) -> None:
    """Atomic write of <root>/bundle/defaults.json (temp + os.replace),
    matching how real writers land files."""
    payload = {
        "workflows": {"text_json": {"tasks": tasks}},
        "equipment_profiles": profiles or [],
    }
    p = root / "bundle" / "defaults.json"
    tmp = p.parent / "defaults.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, p)


def _age_bundle(root: Path, seconds: int = 10) -> None:
    """Back-date the bundle dir + files so a subsequent write lands on a
    strictly newer mtime tick. NTFS timestamps within one test can all
    fall on the SAME clock tick — sleeps are flaky, explicit utime is
    deterministic. Real imports never hit this: the baseline is seeded
    at project open, minutes before any import."""
    t = time.time_ns() - seconds * 1_000_000_000
    bundle = root / "bundle"
    for p in (bundle / "bundle.json", bundle / "defaults.json", bundle):
        if p.exists():
            os.utime(p, ns=(t, t))


def _make_project(tmp_path: Path, tasks: list, name: str = "proj") -> Path:
    root = tmp_path / name
    (root / "config").mkdir(parents=True)
    (root / "bundle" / "wheels").mkdir(parents=True)
    (root / "bundle" / "bundle.json").write_text(
        json.dumps({"name": "b", "id": "b", "version": "1.0.0"}), encoding="utf-8"
    )
    # The install stamp lives beside the venv, and the venv is keyed by the bundle:
    # a manifest plus a wheels/requirements.txt are what make a folder one the host
    # can answer about at all. Without them the stamp below is simply ``None``.
    (root / "bundle" / "wheels" / "requirements.txt").write_text("", encoding="utf-8")
    _write_defaults(root, tasks)
    _age_bundle(root)
    return root


def _spin_until(app, cond, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ---------------------------------------------------------------------------
# TaskConfigManager: project-root-first + reload picks up a changed bundle
# ---------------------------------------------------------------------------


def test_project_bundle_beats_stale_env_var(tmp_path, monkeypatch):
    """The launch-time env var is a snapshot; the ACTIVE project's
    bundle/defaults.json must win."""
    decoy = tmp_path / "decoy.json"
    decoy.write_text(
        json.dumps({"workflows": {"text_json": {"tasks": [_task("decoy_task", "DECOY")]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(decoy))

    root = _make_project(tmp_path, [_task("task_a", "PROMPT V1")])
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json", project_root=root)

    ids = mgr.get_task_ids_for_tab("text_json")
    assert "task_a" in ids
    assert "decoy_task" not in ids


def test_reload_rebuilds_gating_from_changed_defaults(tmp_path, monkeypatch):
    """Import-while-open: rewrite defaults.json, call reload() — a task
    disappears, a new one appears, an existing prompt template swaps,
    and the reload callback (the button-rebuild chain) fires."""
    monkeypatch.delenv(BUNDLE_DEFAULTS_PATH, raising=False)
    root = _make_project(tmp_path, [_task("task_a", "PROMPT V1"), _task("task_old", "OLD")])
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json", project_root=root)

    # The bundle's tasks, then the editor's own (listed, not runnable).
    assert mgr.get_task_ids_for_tab("text_json")[:2] == ["task_a", "task_old"]
    assert mgr.get_task_config("text_json", "task_a").prompt_template == "PROMPT V1"

    fired = []
    mgr.register_reload_callback(lambda: fired.append(True))

    # The "import": defaults.json replaced with a new bundle's view.
    _write_defaults(root, [_task("task_a", "PROMPT V2"), _task("task_new", "NEW")])
    mgr.reload(root)

    ids = mgr.get_task_ids_for_tab("text_json")
    assert ids[:2] == ["task_a", "task_new"]
    assert "task_old" not in ids
    assert mgr.get_task_config("text_json", "task_a").prompt_template == "PROMPT V2"
    assert mgr.get_task_config("text_json", "task_new").prompt_template == "NEW"
    assert fired


def test_env_fallback_when_project_has_no_bundle(tmp_path, monkeypatch):
    """No <project>/bundle → the env var still works (legacy launch)."""
    env_defaults = tmp_path / "envd.json"
    env_defaults.write_text(
        json.dumps({"workflows": {"text_json": {"tasks": [_task("env_task", "ENV")]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(env_defaults))

    root = tmp_path / "nobundle"
    (root / "config").mkdir(parents=True)
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json", project_root=root)
    assert "env_task" in mgr.get_task_ids_for_tab("text_json")


# ---------------------------------------------------------------------------
# pack_parsers equipment profiles: project-root-first
# ---------------------------------------------------------------------------


def test_equipment_profiles_project_first(tmp_path, monkeypatch):
    env_profile = {"equipment_type": "psu", "id_pattern": "^psu"}
    proj_profile = {"equipment_type": "controller", "id_pattern": "^fn"}

    decoy = tmp_path / "envd.json"
    decoy.write_text(json.dumps({"equipment_profiles": [env_profile]}), encoding="utf-8")
    monkeypatch.setenv(BUNDLE_DEFAULTS_PATH, str(decoy))

    root = _make_project(tmp_path, [], name="proj_eq")
    _write_defaults(root, [], profiles=[proj_profile])

    assert bundle_capabilities.load_bundle_equipment_profiles(root) == [proj_profile]
    # No project root → env fallback unchanged.
    assert bundle_capabilities.load_bundle_equipment_profiles(None) == [env_profile]


# ---------------------------------------------------------------------------
# Watcher plumbing — real project_controller functions on a light harness
# ---------------------------------------------------------------------------


class _WatcherHarness:
    """Carries exactly the state the bundle-watch functions touch; the
    functions themselves are the REAL ``project_controller`` ones."""

    def __init__(self, project_root: Path, debounce_ms: int = 60):
        config_dir = project_root / "config"
        self.project_manager = SimpleNamespace(
            project_root=project_root,
            get_config_dir=lambda: config_dir,
        )
        self._config_watcher = QFileSystemWatcher()
        self._config_watcher.fileChanged.connect(
            lambda path: project_controller.on_config_file_changed(self, path)
        )
        self._config_watcher.directoryChanged.connect(
            lambda path: project_controller.on_config_dir_changed(self, path)
        )
        self._watched_config_path = None
        self._watched_config_dir = None
        self._watched_bundle_dir = None
        self._watched_project_root = None
        self._bundle_signature = None
        self._bundle_stamp_mtime = None
        self._bundle_change_timer = QTimer()
        self._bundle_change_timer.setSingleShot(True)
        self._bundle_change_timer.setInterval(debounce_ms)
        self._bundle_change_timer.timeout.connect(
            lambda: project_controller.on_bundle_change_debounced(self)
        )
        self.funnel_calls = 0

    def _update_project_rules_indicators(self):
        pass

    def _handle_config_change(self):
        self.funnel_calls += 1


def _armed_harness(tmp_path, qapp, monkeypatch, tasks=None, name="wproj"):
    root = _make_project(tmp_path, tasks or [_task("t", "P")], name=name)
    # A project_id: the install stamp moved beside the machine-local venv it
    # describes, and that folder is keyed by the id (share slice 1).
    (root / "config" / "config.json").write_text(
        json.dumps({"project_id": "aaaaaaaa-1111-2222-3333-444444444444"}), encoding="utf-8"
    )
    h = _WatcherHarness(root)
    # The funnel counts on the harness (the real one regates the window's tabs).
    monkeypatch.setattr(
        project_controller, "handle_config_change", lambda mw: mw._handle_config_change()
    )
    project_controller.watch_project_config(h)
    return root, h


def test_watch_arms_bundle_and_root(qapp, tmp_path, monkeypatch):
    root, h = _armed_harness(tmp_path, qapp, monkeypatch)
    assert h._watched_bundle_dir == root / "bundle"
    assert h._watched_project_root == root
    assert h._bundle_signature is not None  # seeded, not None-baseline


def test_bundle_event_fires_one_debounced_reload(qapp, tmp_path, monkeypatch):
    """defaults.json touched (atomic replace, as real imports do) + a
    burst of directory events → exactly ONE funnel call."""
    root, h = _armed_harness(tmp_path, qapp, monkeypatch)

    time.sleep(0.02)  # ensure the mtime actually moves
    _write_defaults(root, [_task("t2", "P2")])

    project_controller.on_config_dir_changed(h, str(root / "bundle"))
    assert h._bundle_change_timer.isActive()
    # Same import also raises a project-root event: must coalesce.
    project_controller.on_config_dir_changed(h, str(root))

    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    qapp.processEvents()
    assert h.funnel_calls == 1


def test_bundle_dir_swap_rearms_watch(qapp, tmp_path, monkeypatch):
    """An import swaps bundle/ wholesale (drops the watch): the
    project-root event must re-arm the bundle-dir watch and fire."""
    root, h = _armed_harness(tmp_path, qapp, monkeypatch)

    staging = root / "bundle.new"
    staging.mkdir()
    (staging / "bundle.json").write_text(
        json.dumps({"name": "b2", "version": "2.0.0"}), encoding="utf-8"
    )
    (staging / "defaults.json").write_text(
        json.dumps(
            {"workflows": {"text_json": {"tasks": [_task("t3", "P3")]}}, "equipment_profiles": []}
        ),
        encoding="utf-8",
    )
    old = root / "bundle.old"
    os.replace(root / "bundle", old)
    os.replace(staging, root / "bundle")

    project_controller.on_config_dir_changed(h, str(root))
    assert h._watched_bundle_dir == root / "bundle"  # re-armed
    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    assert h.funnel_calls == 1


def test_no_reload_storm_on_unrelated_writes(qapp, tmp_path, monkeypatch):
    """Unrelated project-root and config-dir writes must NOT fire the
    bundle funnel (the signature/stamp filters absorb the noise)."""
    root, h = _armed_harness(tmp_path, qapp, monkeypatch)

    (root / "notes.txt").write_text("x", encoding="utf-8")
    project_controller.on_config_dir_changed(h, str(root))

    (root / "config" / "session.json").write_text("{}", encoding="utf-8")
    project_controller.on_config_dir_changed(h, str(root / "config"))

    assert not h._bundle_change_timer.isActive()
    _spin_until(qapp, lambda: False, 0.2)
    assert h.funnel_calls == 0


def test_install_stamp_is_belt_and_braces_trigger(qapp, tmp_path, monkeypatch):
    """The wheel-install stamp is written as the import's final step — a stamp
    change alone must fire the debounced reload. It sits beside the machine-local
    venv it describes, so the bridge is asked where rather than composing it from
    the project: composed, it would name a file nothing writes any more and this
    trigger would be dead while every pin stayed green."""
    from procedo.studio import _project_local_state

    root, h = _armed_harness(tmp_path, qapp, monkeypatch)

    stamp = _project_local_state.install_record(root)
    assert stamp is not None and root not in stamp.parents
    # The install writes it; resolving where it goes never creates the folder.
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"bundle": "b", "version": "2.0.0"}), encoding="utf-8")
    project_controller.on_config_dir_changed(h, str(root / "config"))

    assert h._bundle_change_timer.isActive()
    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    assert h.funnel_calls == 1


def test_real_fs_events_reach_the_funnel(qapp, tmp_path, monkeypatch):
    """End-to-end through a live QFileSystemWatcher: an atomic rewrite
    of defaults.json inside bundle/ produces real directory events that
    reach the debounced funnel (no direct handler calls)."""
    root, h = _armed_harness(tmp_path, qapp, monkeypatch, name="wproj_real")

    time.sleep(0.05)
    _write_defaults(root, [_task("t_live", "LIVE")])

    assert _spin_until(qapp, lambda: h.funnel_calls >= 1, 5.0)
