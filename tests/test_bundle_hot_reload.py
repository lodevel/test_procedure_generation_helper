"""Task #39 — a bundle imported into the open project goes live immediately.

Covers the three seams:

* ``TaskConfigManager`` resolves bundle defaults project-root-first
  (``<project>/bundle/defaults.json`` beats a stale
  ``TPG_BUNDLE_DEFAULTS_PATH``), and ``reload()`` picks up a CHANGED
  defaults.json — tasks appear/disappear and prompt templates swap.
* ``pack_parsers._load_bundle_equipment_profiles`` resolves
  project-root-first with the env var as legacy fallback.
* The editor's bundle watcher plumbing (real ``MainWindow`` methods
  bound to a light harness): bundle-dir / project-root events fire ONE
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

from workflow_editor.core.task_config import TaskConfigManager  # noqa: E402
from workflow_editor.llm import pack_parsers as pp  # noqa: E402
from workflow_editor.main_window import MainWindow  # noqa: E402


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
    (root / "bundle").mkdir()
    (root / "bundle" / "bundle.json").write_text(
        json.dumps({"name": "b", "version": "1.0.0"}), encoding="utf-8")
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
    decoy.write_text(json.dumps(
        {"workflows": {"text_json": {"tasks": [_task("decoy_task", "DECOY")]}}}
    ), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(decoy))

    root = _make_project(tmp_path, [_task("task_a", "PROMPT V1")])
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json",
                            project_root=root)

    ids = mgr.get_task_ids_for_tab("text_json")
    assert "task_a" in ids
    assert "decoy_task" not in ids


def test_reload_rebuilds_gating_from_changed_defaults(tmp_path, monkeypatch):
    """Import-while-open: rewrite defaults.json, call reload() — a task
    disappears, a new one appears, an existing prompt template swaps,
    and the reload callback (the button-rebuild chain) fires."""
    monkeypatch.delenv("TPG_BUNDLE_DEFAULTS_PATH", raising=False)
    root = _make_project(
        tmp_path, [_task("task_a", "PROMPT V1"), _task("task_old", "OLD")])
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json",
                            project_root=root)

    assert set(mgr.get_task_ids_for_tab("text_json")) == {"task_a", "task_old"}
    assert mgr.get_task_config("text_json", "task_a").prompt_template == "PROMPT V1"

    fired = []
    mgr.register_reload_callback(lambda: fired.append(True))

    # The "import": defaults.json replaced with a new bundle's view.
    _write_defaults(
        root, [_task("task_a", "PROMPT V2"), _task("task_new", "NEW")])
    mgr.reload(root)

    ids = set(mgr.get_task_ids_for_tab("text_json"))
    assert ids == {"task_a", "task_new"}
    assert "task_old" not in ids
    assert mgr.get_task_config("text_json", "task_a").prompt_template == "PROMPT V2"
    assert mgr.get_task_config("text_json", "task_new").prompt_template == "NEW"
    assert fired


def test_env_fallback_when_project_has_no_bundle(tmp_path, monkeypatch):
    """No <project>/bundle → the env var still works (legacy launch)."""
    env_defaults = tmp_path / "envd.json"
    env_defaults.write_text(json.dumps(
        {"workflows": {"text_json": {"tasks": [_task("env_task", "ENV")]}}}
    ), encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(env_defaults))

    root = tmp_path / "nobundle"
    (root / "config").mkdir(parents=True)
    mgr = TaskConfigManager(fallback_path=tmp_path / "none.json",
                            project_root=root)
    assert "env_task" in mgr.get_task_ids_for_tab("text_json")


# ---------------------------------------------------------------------------
# pack_parsers equipment profiles: project-root-first
# ---------------------------------------------------------------------------

def test_equipment_profiles_project_first(tmp_path, monkeypatch):
    env_profile = {"equipment_type": "psu", "id_pattern": "^psu"}
    proj_profile = {"equipment_type": "controller", "id_pattern": "^fn"}

    decoy = tmp_path / "envd.json"
    decoy.write_text(json.dumps({"equipment_profiles": [env_profile]}),
                     encoding="utf-8")
    monkeypatch.setenv("TPG_BUNDLE_DEFAULTS_PATH", str(decoy))

    root = _make_project(tmp_path, [], name="proj_eq")
    _write_defaults(root, [], profiles=[proj_profile])

    assert pp._load_bundle_equipment_profiles(root) == [proj_profile]
    # No project root → env fallback unchanged.
    assert pp._load_bundle_equipment_profiles(None) == [env_profile]


# ---------------------------------------------------------------------------
# Watcher plumbing — real MainWindow methods on a light harness
# ---------------------------------------------------------------------------

class _WatcherHarness:
    """Carries exactly the state the bundle-watch methods touch; the
    methods themselves are the REAL MainWindow implementations."""

    _watch_project_config = MainWindow._watch_project_config
    _watch_project_bundle = MainWindow._watch_project_bundle
    _bundle_signature_changed = MainWindow._bundle_signature_changed
    _bundle_stamp_changed = MainWindow._bundle_stamp_changed
    _on_config_dir_changed = MainWindow._on_config_dir_changed
    _on_config_file_changed = MainWindow._on_config_file_changed
    _on_bundle_change_debounced = MainWindow._on_bundle_change_debounced

    def __init__(self, project_root: Path, debounce_ms: int = 60):
        config_dir = project_root / "config"
        self.project_manager = SimpleNamespace(
            project_root=project_root,
            get_config_dir=lambda: config_dir,
        )
        self._config_watcher = QFileSystemWatcher()
        self._config_watcher.fileChanged.connect(self._on_config_file_changed)
        self._config_watcher.directoryChanged.connect(self._on_config_dir_changed)
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
            self._on_bundle_change_debounced)
        self.funnel_calls = 0

    def _update_project_rules_indicators(self):
        pass

    def _handle_config_change(self):
        self.funnel_calls += 1


def _armed_harness(tmp_path, qapp, tasks=None, name="wproj"):
    root = _make_project(tmp_path, tasks or [_task("t", "P")], name=name)
    (root / "config" / "config.json").write_text("{}", encoding="utf-8")
    h = _WatcherHarness(root)
    h._watch_project_config()
    return root, h


def test_watch_arms_bundle_and_root(qapp, tmp_path):
    root, h = _armed_harness(tmp_path, qapp)
    assert h._watched_bundle_dir == root / "bundle"
    assert h._watched_project_root == root
    assert h._bundle_signature is not None  # seeded, not None-baseline


def test_bundle_event_fires_one_debounced_reload(qapp, tmp_path):
    """defaults.json touched (atomic replace, as real imports do) + a
    burst of directory events → exactly ONE funnel call."""
    root, h = _armed_harness(tmp_path, qapp)

    time.sleep(0.02)  # ensure the mtime actually moves
    _write_defaults(root, [_task("t2", "P2")])

    h._on_config_dir_changed(str(root / "bundle"))
    assert h._bundle_change_timer.isActive()
    # Same import also raises a project-root event: must coalesce.
    h._on_config_dir_changed(str(root))

    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    qapp.processEvents()
    assert h.funnel_calls == 1


def test_bundle_dir_swap_rearms_watch(qapp, tmp_path):
    """An import swaps bundle/ wholesale (drops the watch): the
    project-root event must re-arm the bundle-dir watch and fire."""
    root, h = _armed_harness(tmp_path, qapp)

    staging = root / "bundle.new"
    staging.mkdir()
    (staging / "bundle.json").write_text(
        json.dumps({"name": "b2", "version": "2.0.0"}), encoding="utf-8")
    (staging / "defaults.json").write_text(json.dumps(
        {"workflows": {"text_json": {"tasks": [_task("t3", "P3")]}},
         "equipment_profiles": []}), encoding="utf-8")
    old = root / "bundle.old"
    os.replace(root / "bundle", old)
    os.replace(staging, root / "bundle")

    h._on_config_dir_changed(str(root))
    assert h._watched_bundle_dir == root / "bundle"  # re-armed
    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    assert h.funnel_calls == 1


def test_no_reload_storm_on_unrelated_writes(qapp, tmp_path):
    """Unrelated project-root and config-dir writes must NOT fire the
    bundle funnel (the signature/stamp filters absorb the noise)."""
    root, h = _armed_harness(tmp_path, qapp)

    (root / "notes.txt").write_text("x", encoding="utf-8")
    h._on_config_dir_changed(str(root))

    (root / "config" / "session.json").write_text("{}", encoding="utf-8")
    h._on_config_dir_changed(str(root / "config"))

    assert not h._bundle_change_timer.isActive()
    _spin_until(qapp, lambda: False, 0.2)
    assert h.funnel_calls == 0


def test_install_stamp_is_belt_and_braces_trigger(qapp, tmp_path):
    """The wheel-install stamp lands in config/ as the import's final
    step — a stamp change alone must fire the debounced reload."""
    root, h = _armed_harness(tmp_path, qapp)

    (root / "config" / ".bundle_installed.json").write_text(
        json.dumps({"bundle": "b", "version": "2.0.0"}), encoding="utf-8")
    h._on_config_dir_changed(str(root / "config"))

    assert h._bundle_change_timer.isActive()
    assert _spin_until(qapp, lambda: h.funnel_calls > 0, 3.0)
    assert h.funnel_calls == 1


def test_real_fs_events_reach_the_funnel(qapp, tmp_path):
    """End-to-end through a live QFileSystemWatcher: an atomic rewrite
    of defaults.json inside bundle/ produces real directory events that
    reach the debounced funnel (no direct handler calls)."""
    root, h = _armed_harness(tmp_path, qapp, name="wproj_real")

    time.sleep(0.05)
    _write_defaults(root, [_task("t_live", "LIVE")])

    assert _spin_until(qapp, lambda: h.funnel_calls >= 1, 5.0)
