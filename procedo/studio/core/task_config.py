"""
Task Configuration Manager — per-project workflow config.

Stores LLM task definitions and chat configurations under the project's
``config/config.json:workflows`` key; a project's tasks are the host's
``workflow_defaults.effective_workflows`` of its bundle and that key, the
listing Project Configuration shows too. Without a project, a shared repo file
(``external/test_procedure_generation_helper/config/tab_contexts.json``) is
read, and the baked-in ``DEFAULT_TASK_CONFIGS`` when it is absent.

Schema (project ``config.json``):

    {
      "workflows": {
        "text_json": {
          "tasks": [
            {
              "id": "derive_json_from_text",
              "name": "Derive JSON from Text",
              "button_label": "Text → JSON",
              "prompt_template": null,
              "enabled": true,
              "max_validator_attempts": null,
              "selected_rules": ["01_*.md", "02_*.md"]   # per-task
            }, ...
          ],
          "chat_config": {...},
          "validators": [...]   # unknown to Phase 1; preserved verbatim
        },
        ...
      }
    }

Single-writer contract: the workflows section is owned by
``TaskConfigManager`` (and any GUI editor that routes through it).
``ProjectConfigDialog._commit_shadow`` preserves the on-disk workflows
section across its shadow-staging commit (see config_manager_dialog.py).
"""

from __future__ import annotations

import json
import logging
import shutil
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

from procedo.core import config_manager
from procedo.core.paths import exists_quiet
from procedo.core.workflow_defaults import effective_workflows, is_runnable

from . import task_config_defaults, task_config_migrations
from .fileio import atomic_write_text
from .task_config_defaults import DEFAULT_CHAT_CONFIG, DEFAULT_TASK_CONFIGS
from .task_config_model import ChatConfig, SelectedRules, TaskConfig, clone_selected_rules

log = logging.getLogger(__name__)


# Default retry budget for the validator-in-the-loop FSM when no per-task
# ``TaskConfig.max_validator_attempts`` override is set. 1 original + 2 retries
# = 3 total — empirically ~95% of fixable validator failures resolve in one
# retry; 3 total is the sweet spot before token cost dominates.
DEFAULT_MAX_VALIDATOR_ATTEMPTS: int = 3


class TaskConfigManager:
    """Manages task configurations across all tabs with thread-safe operations.

    Two construction modes:

    * **Legacy single-file mode** — ``TaskConfigManager(path)``. Reads and
      writes the given path as a self-contained tab-contexts JSON. Used
      by unit tests and the repo's shared fallback.
    * **Project mode** — ``TaskConfigManager(fallback_path, project_root)``.
      Lists ``effective_workflows`` of the project's bundle and its
      ``<project>/config/config.json:workflows``. Writes the ``workflows``
      section back into the project's ``config.json`` using preserve-and-overlay.

    Call ``reload(project_root)`` to switch projects after construction.
    """

    def __init__(
        self,
        fallback_path: Path,
        project_root: Optional[Path] = None,
    ) -> None:
        self._fallback_path = Path(fallback_path)
        self._project_root: Optional[Path] = Path(project_root) if project_root else None
        # RLock so internal helpers that re-acquire (auto-recovery,
        # initialize-defaults) don't self-deadlock when called from
        # already-locked ``reload()`` / public methods.
        self._lock = RLock()

        # Typed caches.
        self._task_configs: Dict[str, List[TaskConfig]] = {}
        self._chat_configs: Dict[str, ChatConfig] = {}

        # Per-tab unknown keys preserved verbatim across load/save (Q3).
        # Only ``workflows.<tab>.*`` keys outside the known tab keys end up
        # here (e.g. ``validators``).
        self._raw_extras: Dict[str, Dict[str, Any]] = {}

        # Project-only write view of ``workflows``. Populated at load from
        # the project's on-disk ``config.json:workflows`` block — pack
        # defaults are NEVER merged in here. Mutation APIs stamp the
        # affected sub-keys back so the snapshot stays current; save
        # writes ONLY this dict. Keeps pack-inherited values from
        # leaking into project config (Codex H2). Empty in legacy mode.
        self._project_workflow_snapshot: Dict[str, Any] = {}

        # Inherited (editor + bundle) tasks indexed for snapshot-stamp
        # filtering: stamping a task identical to its inherited default is a
        # no-op (no leak via bulk callers like set_all_tasks_for_tab, Codex Q1).
        # Populated at load time, with the bundle's own workflows block (which
        # tasks a bulk rule edit may claim, ``_stamp_owned_task``).
        self._pack_task_dicts_by_tab: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._bundle_workflows: Dict[str, Any] = {}

        # Subscribers notified after ``reload()``. Lets the main window
        # refresh button labels without TaskConfigManager importing Qt.
        self._reload_callbacks: List[Callable[[], None]] = []

        # Single-writer injection point (Codex H1.D / Q7). When
        # registered, project-mode saves route the workflows payload
        # through this callback instead of writing directly — lets the
        # parent app (Phase 4) funnel both ProjectConfigDialog AND
        # workflow-editor saves through one path. Unregistered today;
        # direct write is the legacy fallback.
        self._workflows_writer: Optional[Callable[[Dict[str, Any]], bool]] = None
        self._load_error = self.save_error = ""  # why the project load / the last save failed
        self._load_config()
        log.info(
            "TaskConfigManager initialized (project_root=%s, fallback=%s, active=%s)",
            self._project_root,
            self._fallback_path,
            self._active_source(),
        )

    # ------------------------------------------------------------------
    # Path / source helpers
    # ------------------------------------------------------------------

    def _project_config_path(self) -> Optional[Path]:
        """Path to ``<project>/config/config.json``, or None if not in project mode."""
        if self._project_root is None:
            return None
        return self._project_root / "config" / "config.json"

    def _active_source(self) -> str:
        """Label for the data source currently feeding the caches. Diagnostic."""
        if self._project_root is None:
            return f"file:{self._fallback_path}"
        return f"project:{self._project_config_path()}"

    @property
    def project_root(self) -> Optional[Path]:
        return self._project_root

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Populate caches from the active source.

        Note: this method does NOT take ``self._lock``. Call from a path
        that already holds it (init runs single-threaded; ``reload()``
        takes it explicitly).
        """
        # Project mode: first migrate any legacy tab_contexts.json, then
        # list the bundle + project workflows from the listing Project Configuration shows.
        if self._project_root is not None:
            task_config_migrations.migrate_project_tab_contexts(self._project_root)

            bundle = task_config_defaults.load_pack_workflow_defaults(self._project_root)
            self._bundle_workflows = bundle
            self._pack_task_dicts_by_tab = {}
            for tab_id, tab_cfg in effective_workflows(bundle, {}).items():
                index = {
                    t["id"]: t
                    for t in tab_cfg.get("tasks") or []
                    if isinstance(t, dict) and isinstance(t.get("id"), str)
                }
                if index:
                    self._pack_task_dicts_by_tab[tab_id] = index

            project_workflows: Dict[str, Any] = {}
            try:  # unreadable now: a save of the (empty) snapshot is refused (_load_error)
                full = config_manager.load_unified_config(self._project_root / "config")
                self._load_error, candidate = "", full.get("workflows")
            except (OSError, ValueError) as exc:  # logged by the save it refuses
                self._load_error, candidate = str(exc), None
            if isinstance(candidate, dict):
                project_workflows = candidate

            # Snapshot the project's verbatim workflows section BEFORE merging.
            # This is the only thing save_config() writes — pack defaults
            # never leak into project config.json (Codex H2).
            self._project_workflow_snapshot = deepcopy(project_workflows)

            merged = effective_workflows(bundle, project_workflows)
            for tab in (
                merged.values()
            ):  # no runnable task: the tab lists none (the snapshot keeps its rows)
                if not any(
                    isinstance(t, dict) and is_runnable(t.get("prompt_template"))
                    for t in tab.get("tasks") or []
                ):
                    tab["tasks"] = []
            # Run button-label migration on the merged dict so older
            # per-project configs upgrade cleanly. Also migrate the
            # snapshot so future saves persist the upgraded labels.
            task_config_migrations.migrate_button_labels_in_place(merged)
            task_config_migrations.migrate_button_labels_in_place(self._project_workflow_snapshot)
            self._populate_caches(merged, drop_tab_selected_rules=True)
            return

        # No project: the repo-shared tab_contexts file, which this mode owns.
        if self._fallback_path.exists():
            try:
                with open(self._fallback_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                migrated = task_config_migrations.migrate_button_labels_in_place(raw)
                # Round-trip fidelity for the tab-level ``selected_rules`` field.
                self._populate_caches(raw, drop_tab_selected_rules=False)
                task_config_defaults.fill_missing_defaults(self._task_configs, self._chat_configs)
                if migrated:
                    self._save_config_internal()
                return
            except json.JSONDecodeError as e:
                log.error("Corrupted config file %s: %s.", self._fallback_path, e)
                self._auto_recover_config()  # back it up and rebuild
                return
            except Exception as e:
                log.error(
                    "Error loading config from %s: %s.", self._fallback_path, e, exc_info=True
                )
                self._auto_recover_config()
                return

        # No file anywhere — initialize from baked-in defaults.
        log.info("No config file present (fallback=%s); using defaults.", self._fallback_path)
        self._initialize_defaults()

    # ------------------------------------------------------------------
    # Pack discovery (Phase 3)
    # ------------------------------------------------------------------

    def _populate_caches(self, workflows: Dict[str, Any], *, drop_tab_selected_rules: bool) -> None:
        tasks, chats, extras = task_config_defaults.workflows_to_caches(
            workflows, drop_tab_selected_rules=drop_tab_selected_rules
        )
        self._task_configs.update(tasks)
        self._chat_configs.update(chats)
        self._raw_extras.update(extras)

    def _initialize_defaults(self) -> None:
        with self._lock:
            self._task_configs, self._chat_configs = task_config_defaults.default_caches()
            self._raw_extras = {}

    def _auto_recover_config(self) -> None:
        """Back up the corrupted source and recreate with defaults."""
        try:
            corrupted = self._fallback_path
            if corrupted.exists():
                backup = corrupted.with_suffix(".json.corrupted")
                shutil.copy2(corrupted, backup)
                log.warning("Backed up corrupted config to %s", backup)
            self._initialize_defaults()
            self._save_config_internal()
            log.info("Auto-recovered config file with defaults")
        except Exception as e:
            log.error("Failed to auto-recover config: %s", e, exc_info=True)
            self._initialize_defaults()

    # ------------------------------------------------------------------
    # Snapshot stamping (Codex H2: pack defaults never round-trip through save)
    # ------------------------------------------------------------------

    def _stamp_task_in_snapshot(self, tab_id: str, task_id: str) -> None:
        """Mirror the typed-cache state of ``<tab_id, task_id>`` into
        ``_project_workflow_snapshot``. Called from every mutation API
        so the snapshot stays current; save writes only the snapshot.

        Two filters keep pack data from leaking when bulk callers like
        ``SettingsDialog._on_save`` round-trip merged caches back
        through ``set_all_tasks_for_tab`` (Codex Q1):

        * If the task is bit-identical to its pack default AND the
          snapshot didn't already declare it, this is a no-op (pack
          data stays in the pack file, not in project config).
        * If the snapshot already had the task OR the task differs from
          pack defaults, the snapshot is updated — the user has taken
          ownership of these fields.
        """
        task = next(
            (t for t in self._task_configs.get(tab_id, []) if t.id == task_id),
            None,
        )
        if task is None:
            return
        new_entry = task.to_dict()

        pack_task = self._pack_task_dicts_by_tab.get(tab_id, {}).get(task_id)
        if pack_task is not None and not self._in_snapshot(tab_id, task_id):
            if _task_dicts_equal(new_entry, pack_task):
                return  # Pack-identical and never owned by project — no leak.

        tab_snap = self._project_workflow_snapshot.setdefault(tab_id, {})
        tasks_list = tab_snap.setdefault("tasks", [])
        for i, existing in enumerate(tasks_list):
            if isinstance(existing, dict) and existing.get("id") == task_id:
                tasks_list[i] = new_entry
                return
        tasks_list.append(new_entry)

    def _in_snapshot(self, tab_id: str, task_id: str) -> bool:
        """The project's own workflows declare *task_id* in *tab_id*."""
        return _declares(self._project_workflow_snapshot, tab_id, task_id)

    def _stamp_owned_task(self, tab_id: str, task_id: str) -> None:
        """Stamp a bulk rule edit of a task the bundle or the project declares; an
        editor-only task stays inherited (its frozen editor fields never reach config.json)."""
        if _declares(self._bundle_workflows, tab_id, task_id) or self._in_snapshot(tab_id, task_id):
            self._stamp_task_in_snapshot(tab_id, task_id)

    def _remove_task_from_snapshot(self, tab_id: str, task_id: str) -> None:
        """Drop *task_id* from the snapshot's tasks list if present."""
        tab_snap = self._project_workflow_snapshot.get(tab_id)
        if not isinstance(tab_snap, dict):
            return
        tasks_list = tab_snap.get("tasks")
        if not isinstance(tasks_list, list):
            return
        tab_snap["tasks"] = [
            t for t in tasks_list if not (isinstance(t, dict) and t.get("id") == task_id)
        ]

    # ------------------------------------------------------------------
    # Single-writer injection (Codex H1.D / Q7)
    # ------------------------------------------------------------------

    def register_workflows_writer(self, writer: Optional[Callable[[Dict[str, Any]], bool]]) -> None:
        """Register the function that persists the workflows section in
        project mode. When set, ``save_config()`` calls ``writer(payload)``
        instead of writing ``<project>/config/config.json`` directly,
        letting the parent app funnel BOTH ``ProjectConfigDialog`` AND
        workflow-editor saves through one path. Pass ``None`` to clear.

        Phase 4 wires this; without a registered writer, project-mode
        saves fall back to a direct atomic write (legacy behavior).
        """
        with self._lock:
            self._workflows_writer = writer

    def _build_workflows_section(self) -> Dict[str, Any]:
        """Reassemble the workflows section from typed caches + extras."""
        out: Dict[str, Any] = {}
        all_tabs = set(self._task_configs) | set(self._chat_configs) | set(self._raw_extras)
        for tab_id in all_tabs:
            tab_out: Dict[str, Any] = {}
            # Preserve unknown keys first; known keys overwrite below.
            tab_out.update(self._raw_extras.get(tab_id, {}))
            if tab_id in self._task_configs:
                tab_out["tasks"] = [t.to_dict() for t in self._task_configs[tab_id]]
            if tab_id in self._chat_configs:
                tab_out["chat_config"] = self._chat_configs[tab_id].to_dict()
            out[tab_id] = tab_out
        return out

    def _save_config_internal(self) -> None:
        """Atomically persist current state to the active sink. Lock-held.

        Project mode writes ONLY the project-authored snapshot
        (``_project_workflow_snapshot``). Pack defaults stay in pack
        files and are re-merged at load — preventing inherited
        validators / tasks from being baked into project config.json
        (Codex H2).

        If a single-writer callback is registered via
        ``register_workflows_writer``, project-mode saves route through
        it instead of writing directly. Unregistered = legacy direct
        write (Codex H1.D).
        """
        if self._project_root is not None:
            if self._load_error:  # a save would replace the workflows it could not read
                raise OSError(
                    f"the project's config could not be read when it was opened "
                    f"({self._load_error}); reopen the project"
                )
            payload = deepcopy(self._project_workflow_snapshot)
            if self._workflows_writer is not None:
                ok = bool(self._workflows_writer(payload))
                if not ok:
                    log.error("Registered workflows writer reported failure.")
                return

            target = self._project_config_path()
            if target is None:
                log.error("No save target — project_root set but path resolution failed")
                return
            log.warning("Direct project-mode save (no single-writer): commits could race (H1).")
            # Raises, writing nothing, when config.json cannot be read: its other sections stay.
            config_manager.write_config_section(target.parent, "workflows", payload)
            return

        # Legacy single-file mode: write merged workflows as the file root.
        if self._fallback_path is None:
            log.error("No save target — project_root=None and no fallback path")
            return
        atomic_write_text(
            self._fallback_path,
            json.dumps(self._build_workflows_section(), indent=2, ensure_ascii=False),
        )

    def save_config(self) -> bool:
        """Thread-safe atomic save to the active sink; False when it failed (``save_error``)."""
        self.save_error = ""
        try:
            with self._lock:
                self._save_config_internal()
            return True
        except Exception as e:
            log.error("Failed to save config: %s", e, exc_info=True)
            self.save_error = str(e)
            return False

    # ------------------------------------------------------------------
    # Reload (Codex Q8)
    # ------------------------------------------------------------------

    def reload(self, project_root: Optional[Path]) -> None:
        """Switch to a new project (or detach) and reload from disk.

        Clears the in-memory caches under lock so the previous project's
        data can't leak into the new view. Subscribers registered via
        ``register_reload_callback`` are fired after the reload completes.
        """
        new_root = Path(project_root) if project_root else None
        with self._lock:
            self._task_configs.clear()
            self._chat_configs.clear()
            self._raw_extras.clear()
            self._project_workflow_snapshot = {}
            self._pack_task_dicts_by_tab = {}
            self._bundle_workflows = {}
            self._project_root = new_root
            self._load_config()

        log.info(
            "TaskConfigManager reloaded (project_root=%s, active=%s)",
            self._project_root,
            self._active_source(),
        )
        for cb in list(self._reload_callbacks):
            try:
                cb()
            except Exception as e:
                log.error("Reload callback %r raised: %s", cb, e, exc_info=True)

    def register_reload_callback(self, callback: Callable[[], None]) -> None:
        """Register a callable to be invoked after each ``reload()``."""
        self._reload_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_task_config(self, tab_id: str, task_id: str) -> Optional[TaskConfig]:
        with self._lock:
            for task in self._task_configs.get(tab_id, []):
                if task.id == task_id:
                    return TaskConfig(**asdict(task))
            for default_task in DEFAULT_TASK_CONFIGS.get(tab_id, []):
                if default_task.id == task_id:
                    return TaskConfig(**asdict(default_task))
        return None

    def get_all_tasks_for_tab(self, tab_id: str) -> List[TaskConfig]:
        with self._lock:
            if tab_id in self._task_configs:
                return [TaskConfig(**asdict(t)) for t in self._task_configs[tab_id]]
            if tab_id in DEFAULT_TASK_CONFIGS:
                return [TaskConfig(**asdict(t)) for t in DEFAULT_TASK_CONFIGS[tab_id]]
            log.warning("Unknown tab_id: %s", tab_id)
            return []

    def get_enabled_tasks_for_tab(self, tab_id: str) -> List[TaskConfig]:
        return [t for t in self.get_all_tasks_for_tab(tab_id) if t.enabled]

    def get_task_ids_for_tab(self, tab_id: str) -> List[str]:
        return [t.id for t in self.get_all_tasks_for_tab(tab_id)]

    def is_task_enabled(self, tab_id: str, task_id: str) -> bool:
        task = self.get_task_config(tab_id, task_id)
        return task.enabled if task else True

    def is_task_invocable(self, tab_id: str, task_id: str) -> bool:
        """True when the EFFECTIVE config declares this task with a
        non-empty ``prompt_template`` (task #22 capability gating).

        The editor layer ships no prompt_template (default_workflows.json
        has them all null), so a non-empty template can only come from the
        bundle's pack_workflow_defaults, the project's workflows override,
        or the legacy fallback file. Undeclared tasks are not invocable:
        their buttons are greyed out and PromptBuilder raises
        TaskPromptNotDeclaredError if invoked programmatically.

        AD_HOC_CHAT is exempt — it is grammar-neutral and resolves through
        ``chat_config.system_prompt`` / the editor-native default.
        """
        if task_id == "ad_hoc_chat":
            return True
        task = self.get_task_config(tab_id, task_id)
        return task is not None and is_runnable(task.prompt_template)

    def get_chat_config(self, tab_id: str) -> ChatConfig:
        with self._lock:
            if tab_id in self._chat_configs:
                c = self._chat_configs[tab_id]
                return ChatConfig(enabled=c.enabled, system_prompt=c.system_prompt)
            if tab_id in DEFAULT_CHAT_CONFIG:
                c = DEFAULT_CHAT_CONFIG[tab_id]
                return ChatConfig(enabled=c.enabled, system_prompt=c.system_prompt)
            return ChatConfig()

    def get_validator_specs_for_tab(self, tab_id: str) -> List[Dict[str, Any]]:
        """Return the ``workflows.<tab>.validators`` list as a list of
        spec dicts (each carries at least ``id``; optionally ``label`` and
        ``enabled``). Returns an empty list when the tab has no
        validators configured.

        The list comes from the layered merge of pack defaults + project
        overrides (see ``task_config_defaults.load_pack_workflow_defaults``). Validator
        specs live in ``_raw_extras`` since the Phase-1 schema treats
        ``validators`` as an unknown-to-Phase-1 key preserved verbatim.
        """
        with self._lock:
            extras = self._raw_extras.get(tab_id, {})
            validators = extras.get("validators", [])
            if not isinstance(validators, list):
                return []
            return [dict(v) for v in validators if isinstance(v, dict) and v.get("id")]

    def get_selected_rules_for_task(self, tab_id: str, task_id: str) -> SelectedRules:
        """Per-task selected_rules. Returns the raw value (list, ``"all"``,
        or ``None``); consumer expands ``"all"`` against the project's
        available rule files."""
        task = self.get_task_config(tab_id, task_id)
        if task is None:
            return None
        return clone_selected_rules(task.selected_rules)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def add_task(self, tab_id: str, task_config: TaskConfig) -> bool:
        with self._lock:
            if tab_id not in self._task_configs:
                self._task_configs[tab_id] = []
            for existing in self._task_configs[tab_id]:
                if existing.id == task_config.id:
                    log.warning("Task '%s' already exists in tab '%s'", task_config.id, tab_id)
                    return False
            self._task_configs[tab_id].append(task_config)
            self._stamp_task_in_snapshot(tab_id, task_config.id)
            return True

    def update_task(self, tab_id: str, task_config: TaskConfig) -> bool:
        with self._lock:
            if tab_id not in self._task_configs:
                return False
            for i, existing in enumerate(self._task_configs[tab_id]):
                if existing.id == task_config.id:
                    self._task_configs[tab_id][i] = task_config
                    self._stamp_task_in_snapshot(tab_id, task_config.id)
                    return True
        return False

    def delete_task(self, tab_id: str, task_id: str) -> bool:
        with self._lock:
            if tab_id not in self._task_configs:
                return False
            for i, task in enumerate(self._task_configs[tab_id]):
                if task.id == task_id:
                    self._task_configs[tab_id].pop(i)
                    self._remove_task_from_snapshot(tab_id, task_id)
                    return True
        return False

    def reset_to_defaults(self, tab_id: str) -> bool:
        with self._lock:
            if tab_id not in DEFAULT_TASK_CONFIGS:
                return False
            current = self._task_configs.get(tab_id, [])
            defaults = DEFAULT_TASK_CONFIGS[tab_id]
            differs = len(current) != len(defaults) or any(
                asdict(c) != asdict(d) for c, d in zip(current, defaults)
            )
            # Backup the live file (legacy mode) before clobbering caches.
            source = self._project_config_path() if self._project_root else self._fallback_path
            if differs and source and exists_quiet(source):
                try:
                    shutil.copy2(source, source.with_suffix(".json.old"))
                except OSError as e:
                    log.warning("Failed to back up config: %s", e)
            self._task_configs[tab_id] = [TaskConfig(**asdict(t)) for t in defaults]
            # Drop the tab from the snapshot so save reverts to the
            # pack/baked-in defaults rather than persisting them.
            self._project_workflow_snapshot.pop(tab_id, None)
            return True

    def set_task_enabled(self, tab_id: str, task_id: str, enabled: bool) -> bool:
        with self._lock:
            if tab_id not in self._task_configs and tab_id in DEFAULT_TASK_CONFIGS:
                self._task_configs[tab_id] = [
                    TaskConfig(**asdict(t)) for t in DEFAULT_TASK_CONFIGS[tab_id]
                ]
            for task in self._task_configs.get(tab_id, []):
                if task.id == task_id:
                    task.enabled = enabled
                    self._stamp_task_in_snapshot(tab_id, task_id)
                    return True
        return False

    def set_all_tasks_for_tab(self, tab_id: str, tasks: List[TaskConfig]) -> None:
        """Replace this tab's tasks. The typed cache takes the full list
        (callers reading via ``get_all_tasks_for_tab`` see everything),
        but the snapshot stamps per-task with the pack-identical filter
        so pack tasks the caller round-tripped untouched don't get
        persisted as project overrides (Codex Q1).
        """
        with self._lock:
            self._task_configs[tab_id] = list(tasks)
            new_ids = [task.id for task in tasks]
            # Per-task stamp (filters pack-identical entries away).
            for task in tasks:
                self._stamp_task_in_snapshot(tab_id, task.id)
            # Drop snapshot entries for tasks no longer in the input —
            # the user deleted them (or filtered them out).
            tab_snap = self._project_workflow_snapshot.get(tab_id)
            if isinstance(tab_snap, dict):
                existing = tab_snap.get("tasks")
                if isinstance(existing, list):
                    kept = [t for t in existing if isinstance(t, dict) and t.get("id") in new_ids]
                    if kept:
                        tab_snap["tasks"] = kept
                    else:
                        tab_snap.pop("tasks", None)
                # Clean up an empty tab entry left behind by the filter
                # (no tasks AND no sibling keys → nothing to persist).
                if not tab_snap:
                    self._project_workflow_snapshot.pop(tab_id, None)

    def set_chat_config(self, tab_id: str, chat_config: ChatConfig) -> None:
        with self._lock:
            self._chat_configs[tab_id] = chat_config
            tab_snap = self._project_workflow_snapshot.setdefault(tab_id, {})
            tab_snap["chat_config"] = chat_config.to_dict()

    def update_task_config(
        self,
        tab_id: str,
        task_id: str,
        button_label: Optional[str] = None,
        prompt_template: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        """Update specific fields of a task. Returns False if not found."""
        task = self.get_task_config(tab_id, task_id)
        if task is None:
            return False
        if button_label is not None:
            task.button_label = button_label
        if prompt_template is not None:
            task.prompt_template = prompt_template
        if enabled is not None:
            task.enabled = enabled
        return self.update_task(tab_id, task)

    def set_selected_rules_for_tab(self, tab_id: str, selected: SelectedRules) -> None:
        """Propagate a selected_rules list to every task in *tab_id*.

        Phase 1 keeps the rule-selector UI tab-level (one list per tab).
        The selection is duplicated onto each task in the tab so the
        per-task schema stays canonical; Phase 4's editor will allow
        per-task customization.
        """
        with self._lock:
            tasks = self._task_configs.get(tab_id)
            if not tasks:
                log.warning("set_selected_rules_for_tab: unknown tab '%s'", tab_id)
                return
            for task in tasks:
                task.selected_rules = clone_selected_rules(selected)
                # Persist intent: a rule-set edit claims every bundle or
                # project task of this tab (bundle-only ones included).
                self._stamp_owned_task(tab_id, task.id)

    def add_rule_to_selections(self, key: str) -> None:
        """Tick rule document *key* (one just added to the project) in every task's non-empty
        ``selected_rules`` list, in every tab: "all" / None include it already, [] sends none."""
        with self._lock:
            for tab_id, tasks in self._task_configs.items():
                for task in tasks:
                    if (
                        task.selected_rules
                        and isinstance(task.selected_rules, list)
                        and key not in task.selected_rules
                    ):
                        task.selected_rules.append(key)
                        self._stamp_owned_task(tab_id, task.id)


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


def _declares(workflows: Dict[str, Any], tab_id: str, task_id: str) -> bool:
    """*workflows* (a workflows block) lists *task_id* under tab *tab_id*."""
    tab = workflows.get(tab_id, {})
    tasks = tab.get("tasks", []) if isinstance(tab, dict) else []
    return isinstance(tasks, list) and any(
        isinstance(t, dict) and t.get("id") == task_id for t in tasks
    )


def _task_dicts_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True iff two task dicts represent the same TaskConfig values.

    Compares the canonical set of TaskConfig fields only — extras in
    either dict are ignored. This is what the snapshot-stamp filter
    uses to decide "is this task bit-identical to its pack default".
    """
    keys = {f.name for f in fields(TaskConfig)}
    return all(a.get(k) == b.get(k) for k in keys)
