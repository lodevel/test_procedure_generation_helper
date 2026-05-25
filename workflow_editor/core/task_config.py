"""
Task Configuration Manager — per-project workflow config.

Stores LLM task definitions and chat configurations under the project's
``config/config.json:workflows`` key. Falls back to a shared repo file
(``external/test_procedure_generation_helper/config/tab_contexts.json``)
when a project has no ``workflows`` section, and finally to baked-in
``DEFAULT_TASK_CONFIGS`` when neither file is available.

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
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Union

from ..llm.backend_base import LLMTask

log = logging.getLogger(__name__)


# Default retry budget for the validator-in-the-loop FSM when no per-task
# ``TaskConfig.max_validator_attempts`` override is set. 1 original + 2 retries
# = 3 total — empirically ~95% of fixable validator failures resolve in one
# retry; 3 total is the sweet spot before token cost dominates.
DEFAULT_MAX_VALIDATOR_ATTEMPTS: int = 3


# Either a list of rule filenames, the sentinel string ``"all"``, or ``None``
# (inherit from tab-level legacy ``selected_rules`` field at load time).
SelectedRules = Union[List[str], str, None]


@dataclass
class TaskConfig:
    """Configuration for a single LLM task.

    ``selected_rules`` resolves at load time: if a task has its own value
    it wins; otherwise the loader copies the deprecated tab-level
    ``selected_rules`` onto each task. ``None`` after load means "use all
    available rules" (consumer-side expansion).
    """
    id: str
    name: str
    button_label: str
    prompt_template: Optional[str] = None
    enabled: bool = True
    max_validator_attempts: Optional[int] = None
    selected_rules: SelectedRules = None
    # Per-task section-ownership override: the section names the LLM may
    # author for this task. ``None`` → use the bundle/wheel default
    # (current behavior). A list (incl. ``[]`` = LLM authors nothing) is
    # the authoritative LLM-owned set. Threaded into both the prompt
    # emit-list and reconstruction so they stay consistent.
    llm_owned_sections: Optional[list[str]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskConfig":
        """Tolerant constructor — unknown keys are dropped so older configs
        (without newer fields) keep loading."""
        valid_keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid_keys})


@dataclass
class ChatConfig:
    """Per-tab configuration for the AD_HOC_CHAT feature (chat panel)."""
    enabled: bool = True
    system_prompt: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChatConfig":
        return cls(**{k: v for k, v in data.items() if k in ("enabled", "system_prompt")})


# ---------------------------------------------------------------------------
# Workflow editor defaults
# ---------------------------------------------------------------------------
#
# DEFAULT_TASK_CONFIGS / DEFAULT_CHAT_CONFIG are loaded from
# ``default_workflows.json`` (next to this module) at import time so the
# parent app's Project Configuration → Workflows tab can read the exact
# same defaults via a simple JSON read — no cross-package import. The
# JSON file is the source of truth; the in-code dicts mirror it.
#
# If the JSON file is missing or malformed, we fall back to a minimal
# baked-in set so tests that monkey-patch the file path still work.

DEFAULT_WORKFLOWS_JSON_PATH: Path = (
    Path(__file__).parent / "default_workflows.json"
)


def _load_default_workflows() -> Dict[str, Any]:
    """Read ``default_workflows.json``; return empty dict on any error."""
    try:
        with open(DEFAULT_WORKFLOWS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load %s: %s — using empty defaults.",
                    DEFAULT_WORKFLOWS_JSON_PATH, exc)
        return {}


def _build_default_task_configs(
    raw: Dict[str, Any]
) -> Dict[str, List[TaskConfig]]:
    out: Dict[str, List[TaskConfig]] = {}
    for tab_id, tab_cfg in raw.items():
        if not isinstance(tab_cfg, dict):
            continue
        tasks = tab_cfg.get("tasks")
        if not isinstance(tasks, list):
            continue
        out[tab_id] = [
            TaskConfig.from_dict(t) for t in tasks if isinstance(t, dict)
        ]
    return out


def _build_default_chat_config(raw: Dict[str, Any]) -> Dict[str, ChatConfig]:
    out: Dict[str, ChatConfig] = {}
    for tab_id, tab_cfg in raw.items():
        if not isinstance(tab_cfg, dict):
            continue
        chat = tab_cfg.get("chat_config")
        if isinstance(chat, dict):
            out[tab_id] = ChatConfig.from_dict(chat)
    return out


_DEFAULT_WORKFLOWS_RAW: Dict[str, Any] = _load_default_workflows()
DEFAULT_TASK_CONFIGS: Dict[str, List[TaskConfig]] = _build_default_task_configs(
    _DEFAULT_WORKFLOWS_RAW
)
DEFAULT_CHAT_CONFIG: Dict[str, ChatConfig] = _build_default_chat_config(
    _DEFAULT_WORKFLOWS_RAW
)


# Keys we model directly on the tab dict. Everything else (including the
# legacy ``selected_rules`` field, which we lift onto each task) gets stashed
# in ``_raw_extras`` and round-tripped untouched (Codex Q3). In project mode
# the migration step pops ``selected_rules`` from extras so it doesn't get
# rewritten — see ``_populate_from_workflows``'s ``drop_tab_selected_rules``.
_KNOWN_TAB_KEYS: frozenset[str] = frozenset({"tasks", "chat_config", "button_labels"})


class TaskConfigManager:
    """Manages task configurations across all tabs with thread-safe operations.

    Two construction modes:

    * **Legacy single-file mode** — ``TaskConfigManager(path)``. Reads and
      writes the given path as a self-contained tab-contexts JSON. Used
      by unit tests and the repo's shared fallback.
    * **Project mode** — ``TaskConfigManager(fallback_path, project_root)``.
      Reads ``<project>/config/config.json:workflows`` (falling back to
      ``fallback_path`` and finally to ``DEFAULT_TASK_CONFIGS``). Writes
      the ``workflows`` section back into the project's ``config.json``
      using preserve-and-overlay.

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
        # Only ``workflows.<tab>.*`` keys outside ``_KNOWN_TAB_KEYS`` end up
        # here (e.g. ``validators``).
        self._raw_extras: Dict[str, Dict[str, Any]] = {}

        # Project-only write view of ``workflows``. Populated at load from
        # the project's on-disk ``config.json:workflows`` block — pack
        # defaults are NEVER merged in here. Mutation APIs stamp the
        # affected sub-keys back so the snapshot stays current; save
        # writes ONLY this dict. Keeps pack-inherited values from
        # leaking into project config (Codex H2). Empty in legacy mode.
        self._project_workflow_snapshot: Dict[str, Any] = {}

        # Pack defaults indexed for snapshot-stamp filtering: stamping a
        # task identical to its pack default is a no-op (no leak via
        # bulk callers like SettingsDialog's set_all_tasks_for_tab,
        # Codex Q1). Populated at load time from
        # ``_load_pack_workflow_defaults``.
        self._pack_task_dicts_by_tab: Dict[str, Dict[str, Dict[str, Any]]] = {}

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

        self._load_config()
        log.info(
            "TaskConfigManager initialized (project_root=%s, fallback=%s, active=%s)",
            self._project_root, self._fallback_path, self._active_source(),
        )

    # ------------------------------------------------------------------
    # Path / source helpers
    # ------------------------------------------------------------------

    def _project_config_path(self) -> Optional[Path]:
        """Path to ``<project>/config/config.json``, or None if not in project mode."""
        if self._project_root is None:
            return None
        return self._project_root / "config" / "config.json"

    def _project_tab_contexts_path(self) -> Optional[Path]:
        """Legacy per-project tab_contexts.json (pre-Phase-1 layout)."""
        if self._project_root is None:
            return None
        return self._project_root / "config" / "tab_contexts.json"

    def _active_source(self) -> str:
        """Label for the data source currently feeding the caches. Diagnostic."""
        if self._project_root is None:
            return f"file:{self._fallback_path}"
        proj = self._project_config_path()
        if proj and proj.exists():
            data = _safe_read_json(proj)
            if isinstance(data, dict) and isinstance(data.get("workflows"), dict):
                return f"project:{proj}"
        return f"fallback:{self._fallback_path}"

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
        # load pack defaults + project workflows and merge them.
        if self._project_root is not None:
            self._migrate_project_tab_contexts()

            pack_defaults = self._load_pack_workflow_defaults()
            # Index pack tasks for the stamp filter (Codex Q1).
            self._pack_task_dicts_by_tab = {}
            for tab_id, tab_cfg in pack_defaults.items():
                if not isinstance(tab_cfg, dict):
                    continue
                pack_tasks = tab_cfg.get("tasks", [])
                if not isinstance(pack_tasks, list):
                    continue
                index: Dict[str, Dict[str, Any]] = {}
                for t in pack_tasks:
                    if isinstance(t, dict) and isinstance(t.get("id"), str):
                        index[t["id"]] = deepcopy(t)
                if index:
                    self._pack_task_dicts_by_tab[tab_id] = index

            project_workflows: Dict[str, Any] = {}
            project_path = self._project_config_path()
            if project_path and project_path.exists():
                full = _safe_read_json(project_path)
                if isinstance(full, dict):
                    candidate = full.get("workflows")
                    if isinstance(candidate, dict):
                        project_workflows = candidate

            # Snapshot the project's verbatim workflows section BEFORE merging.
            # This is the only thing save_config() writes — pack defaults
            # never leak into project config.json (Codex H2).
            self._project_workflow_snapshot = deepcopy(project_workflows)

            if pack_defaults or project_workflows:
                merged = _merge_workflows(pack_defaults, project_workflows)
                # Run button-label migration on the merged dict so older
                # per-project configs upgrade cleanly. Also migrate the
                # snapshot so future saves persist the upgraded labels.
                self._migrate_button_labels_in_place(merged)
                self._migrate_button_labels_in_place(self._project_workflow_snapshot)
                self._populate_from_workflows(merged, drop_tab_selected_rules=True)
                self._fill_missing_defaults()
                return

        # Fall back to the repo-shared tab_contexts file.
        if self._fallback_path.exists():
            try:
                with open(self._fallback_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                migrated = self._migrate_button_labels_in_place(raw)
                # In legacy mode (no project_root) we own the fallback file,
                # so we want round-trip fidelity for the tab-level
                # ``selected_rules`` field. In project mode we lift+drop.
                drop_tab_selected = self._project_root is not None
                self._populate_from_workflows(
                    raw, drop_tab_selected_rules=drop_tab_selected
                )
                self._fill_missing_defaults()
                # Persist the in-place migration only when we own the file
                # (legacy single-file mode). In project mode the shared
                # fallback is read-only.
                if migrated and self._project_root is None:
                    self._save_config_internal()
                return
            except json.JSONDecodeError as e:
                log.error("Corrupted config file %s: %s.", self._fallback_path, e)
                if self._project_root is None:
                    # Legacy mode: we own the file — back it up and rebuild.
                    self._auto_recover_config()
                else:
                    # Project mode: shared fallback is read-only. Use
                    # in-memory defaults; don't touch the repo file.
                    log.warning("Shared fallback %s unreadable in project mode; "
                                "using in-memory defaults.", self._fallback_path)
                    self._initialize_defaults()
                return
            except Exception as e:
                log.error("Error loading config from %s: %s.",
                          self._fallback_path, e, exc_info=True)
                if self._project_root is None:
                    self._auto_recover_config()
                else:
                    self._initialize_defaults()
                return

        # No file anywhere — initialize from baked-in defaults.
        log.info("No config file present (project=%s, fallback=%s); using defaults.",
                 self._project_root, self._fallback_path)
        self._initialize_defaults()

    # ------------------------------------------------------------------
    # Pack discovery (Phase 3)
    # ------------------------------------------------------------------

    def _load_pack_workflow_defaults(self) -> Dict[str, Any]:
        """Return the workflows-block defaults injected by the parent.

        Phase 5h: the legacy manifest-walk path (resolve
        ``packs.selected_packs`` + read each pack's
        ``pack_workflow_defaults.json``) was removed. The parent app
        owns bundle resolution and hands the editor a path via
        ``TPG_BUNDLE_DEFAULTS_PATH``; this method just reads that
        file's ``workflows`` block.

        Returns ``{}`` when the env var is unset, the file is missing
        or malformed, or the bundle ships no workflows block. The
        editor's merge with its baked-in defaults degrades gracefully
        to editor-defaults-only.
        """
        import os
        path_str = os.environ.get("TPG_BUNDLE_DEFAULTS_PATH")
        if not path_str:
            return {}
        defaults_path = Path(path_str)
        if not defaults_path.is_file():
            return {}
        data = _safe_read_json(defaults_path)
        if not isinstance(data, dict):
            return {}
        workflows = data.get("workflows")
        return workflows if isinstance(workflows, dict) else {}

    def _populate_from_workflows(
        self,
        workflows: Dict[str, Any],
        *,
        drop_tab_selected_rules: bool,
    ) -> None:
        """Fill caches from a ``workflows``-shaped dict.

        Args:
            workflows: ``workflows`` section or legacy fallback root.
            drop_tab_selected_rules: When True (project mode), tab-level
                ``selected_rules`` is lifted onto each task AND removed
                from ``_raw_extras`` so subsequent saves don't write it
                back — completes the migration. When False (legacy
                single-file mode), the tab-level field is kept in
                ``_raw_extras`` and round-trips through save.
        """
        for tab_id, tab_config in workflows.items():
            if not isinstance(tab_config, dict):
                continue

            tab_level_selected: SelectedRules = tab_config.get("selected_rules")

            tasks_data = tab_config.get("tasks")
            tasks: List[TaskConfig] = []
            if isinstance(tasks_data, list) and tasks_data:
                for entry in tasks_data:
                    if not isinstance(entry, dict):
                        continue
                    task = TaskConfig.from_dict(entry)
                    if task.selected_rules is None and tab_level_selected is not None:
                        task.selected_rules = _clone_selected_rules(tab_level_selected)
                    tasks.append(task)
            elif tab_level_selected is not None and tab_id in DEFAULT_TASK_CONFIGS:
                # Tab declares selected_rules but no tasks: fill in defaults
                # so the rules propagate (was a silent drop pre-Codex review).
                for d in DEFAULT_TASK_CONFIGS[tab_id]:
                    t = TaskConfig(**asdict(d))
                    t.selected_rules = _clone_selected_rules(tab_level_selected)
                    tasks.append(t)
            self._task_configs[tab_id] = tasks

            chat_data = tab_config.get("chat_config")
            if isinstance(chat_data, dict):
                self._chat_configs[tab_id] = ChatConfig.from_dict(chat_data)

            # Preserve unknown keys verbatim (Codex Q3). ``selected_rules``
            # lives in extras (not ``_KNOWN_TAB_KEYS``) so legacy round-trip
            # is intact; project mode pops it below.
            extras = {k: v for k, v in tab_config.items() if k not in _KNOWN_TAB_KEYS}
            if drop_tab_selected_rules:
                extras.pop("selected_rules", None)
            if extras:
                self._raw_extras[tab_id] = extras

    def _fill_missing_defaults(self) -> None:
        """Add baked-in defaults for any tabs the loaded data didn't cover."""
        for tab_id, defaults in DEFAULT_TASK_CONFIGS.items():
            if tab_id not in self._task_configs or not self._task_configs[tab_id]:
                self._task_configs[tab_id] = [TaskConfig(**asdict(t)) for t in defaults]
        for tab_id, default_chat in DEFAULT_CHAT_CONFIG.items():
            if tab_id not in self._chat_configs:
                self._chat_configs[tab_id] = ChatConfig(
                    enabled=default_chat.enabled,
                    system_prompt=default_chat.system_prompt,
                )

    def _initialize_defaults(self) -> None:
        with self._lock:
            self._task_configs = {
                tab_id: [TaskConfig(**asdict(t)) for t in default_tasks]
                for tab_id, default_tasks in DEFAULT_TASK_CONFIGS.items()
            }
            self._chat_configs = {
                tab_id: ChatConfig(enabled=c.enabled, system_prompt=c.system_prompt)
                for tab_id, c in DEFAULT_CHAT_CONFIG.items()
            }
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
    # Migration helpers
    # ------------------------------------------------------------------

    def _migrate_project_tab_contexts(self) -> None:
        """Lift ``<project>/config/tab_contexts.json`` into the project's
        ``config.json:workflows`` (then delete the orphan).

        Strict-read both files; abort migration on read failure or
        type-mismatch so a corrupt project config cannot be silently
        replaced. Verifies the post-write file before deleting the legacy
        orphan so a failed write doesn't leave both files stale.
        """
        legacy = self._project_tab_contexts_path()
        if legacy is None or not legacy.exists():
            return
        project_cfg_path = self._project_config_path()
        if project_cfg_path is None:
            return

        # Strict read of legacy.
        try:
            legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not migrate %s (unreadable): %s — leaving in place.", legacy, e)
            return
        if not isinstance(legacy_data, dict):
            log.warning("Legacy %s is not a JSON object; skipping migration.", legacy)
            return

        # Strict read of project config — corrupt project config must not be
        # silently overwritten with `{"workflows": ...}` that loses the rest.
        if project_cfg_path.exists():
            try:
                existing_full = json.loads(project_cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.error("Project config %s is unreadable (%s); aborting migration to "
                          "avoid clobbering. Repair config.json or remove tab_contexts.json.",
                          project_cfg_path, e)
                return
            if not isinstance(existing_full, dict):
                log.error("Project config %s is not a JSON object; aborting migration.",
                          project_cfg_path)
                return
            # If workflows section already exists, the migration already
            # happened on a prior load (or a user wrote one manually) —
            # don't overwrite it from a stale legacy file.
            if isinstance(existing_full.get("workflows"), dict) and existing_full["workflows"]:
                log.info("Project %s already has a workflows section; deleting legacy %s.",
                         project_cfg_path, legacy)
                try:
                    legacy.unlink()
                except OSError as e:
                    log.warning("Could not delete legacy %s: %s", legacy, e)
                return
        else:
            existing_full = {}

        # Run button-label migration on the legacy data first.
        self._migrate_button_labels_in_place(legacy_data)

        # Lift tab-level selected_rules onto each task so the persisted
        # ``workflows`` section is already in the per-task shape.
        migrated_workflows: Dict[str, Any] = {}
        for tab_id, tab_cfg in legacy_data.items():
            if not isinstance(tab_cfg, dict):
                continue
            cleaned = dict(tab_cfg)
            tab_level = cleaned.pop("selected_rules", None)
            tasks = cleaned.get("tasks")
            if isinstance(tasks, list) and tab_level is not None:
                new_tasks = []
                for t in tasks:
                    if isinstance(t, dict) and t.get("selected_rules") is None:
                        t = {**t, "selected_rules": _clone_selected_rules(tab_level)}
                    new_tasks.append(t)
                cleaned["tasks"] = new_tasks
            migrated_workflows[tab_id] = cleaned

        # Preserve-and-overlay write.
        existing_full["workflows"] = migrated_workflows
        try:
            _atomic_write_json(project_cfg_path, existing_full)
        except OSError as e:
            log.error("Failed to write migrated workflows to %s: %s — leaving %s in place.",
                      project_cfg_path, e, legacy)
            return

        # Read-back verification before unlinking the legacy file.
        verify = _safe_read_json(project_cfg_path)
        if not (isinstance(verify, dict)
                and isinstance(verify.get("workflows"), dict)
                and verify["workflows"]):
            log.error("Read-back verification failed for %s; leaving %s untouched.",
                      project_cfg_path, legacy)
            return

        try:
            legacy.unlink()
            log.info("Migrated %s → %s:workflows", legacy, project_cfg_path)
        except OSError as e:
            log.warning("Migrated workflows but could not delete %s: %s", legacy, e)

    def _migrate_button_labels_in_place(self, raw: Dict[str, Any]) -> bool:
        """Migrate old ``button_labels`` map into ``tasks`` list, mutating ``raw``."""
        migrated = False
        task_name_to_value = {task.name: task.value for task in LLMTask}

        for tab_id, tab_config in raw.items():
            if not isinstance(tab_config, dict):
                continue
            if "button_labels" not in tab_config or "tasks" in tab_config:
                continue
            button_labels = tab_config.get("button_labels", {})
            defaults = DEFAULT_TASK_CONFIGS.get(tab_id, [])
            if not defaults:
                continue
            new_tasks = []
            for default_task in defaults:
                name = next(
                    (n for n, v in task_name_to_value.items() if v == default_task.id),
                    None,
                )
                custom = button_labels.get(name) if name else None
                if not custom:
                    custom = button_labels.get(default_task.id)
                new_tasks.append(TaskConfig(
                    id=default_task.id,
                    name=default_task.name,
                    button_label=custom if custom else default_task.button_label,
                    prompt_template=default_task.prompt_template,
                    enabled=default_task.enabled,
                ))
            tab_config["tasks"] = [t.to_dict() for t in new_tasks]
            del tab_config["button_labels"]
            migrated = True
            log.info("Migrated tab '%s' from button_labels to tasks format", tab_id)
        return migrated

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

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

        tab_snap_existing = self._project_workflow_snapshot.get(tab_id, {})
        snapshot_tasks = (
            tab_snap_existing.get("tasks", [])
            if isinstance(tab_snap_existing, dict) else []
        )
        already_in_snapshot = any(
            isinstance(t, dict) and t.get("id") == task_id
            for t in snapshot_tasks if isinstance(snapshot_tasks, list)
        )
        pack_task = self._pack_task_dicts_by_tab.get(tab_id, {}).get(task_id)
        if pack_task is not None and not already_in_snapshot:
            if _task_dicts_equal(new_entry, pack_task):
                return  # Pack-identical and never owned by project — no leak.

        tab_snap = self._project_workflow_snapshot.setdefault(tab_id, {})
        tasks_list = tab_snap.setdefault("tasks", [])
        for i, existing in enumerate(tasks_list):
            if isinstance(existing, dict) and existing.get("id") == task_id:
                tasks_list[i] = new_entry
                return
        tasks_list.append(new_entry)

    def _remove_task_from_snapshot(self, tab_id: str, task_id: str) -> None:
        """Drop *task_id* from the snapshot's tasks list if present."""
        tab_snap = self._project_workflow_snapshot.get(tab_id)
        if not isinstance(tab_snap, dict):
            return
        tasks_list = tab_snap.get("tasks")
        if not isinstance(tasks_list, list):
            return
        tab_snap["tasks"] = [
            t for t in tasks_list
            if not (isinstance(t, dict) and t.get("id") == task_id)
        ]

    # ------------------------------------------------------------------
    # Single-writer injection (Codex H1.D / Q7)
    # ------------------------------------------------------------------

    def register_workflows_writer(
        self, writer: Optional[Callable[[Dict[str, Any]], bool]]
    ) -> None:
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
            target.parent.mkdir(parents=True, exist_ok=True)
            log.warning(
                "Direct project-mode save (no single-writer registered). "
                "Phase 4 wires register_workflows_writer to close Codex H1; "
                "concurrent ProjectConfigDialog commits could race."
            )
            full = _safe_read_json(target) if target.exists() else {}
            if not isinstance(full, dict):
                full = {}
            full["workflows"] = payload
            _atomic_write_json(target, full)
            return

        # Legacy single-file mode: write merged workflows as the file root.
        if self._fallback_path is None:
            log.error("No save target — project_root=None and no fallback path")
            return
        self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._fallback_path, self._build_workflows_section())

    def save_config(self) -> bool:
        """Thread-safe atomic save to the active sink."""
        try:
            with self._lock:
                self._save_config_internal()
            return True
        except Exception as e:
            log.error("Failed to save config: %s", e, exc_info=True)
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
            self._project_root = new_root
            self._load_config()

        log.info("TaskConfigManager reloaded (project_root=%s, active=%s)",
                 self._project_root, self._active_source())
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
        overrides (see ``_load_pack_workflow_defaults``). Validator
        specs live in ``_raw_extras`` since the Phase-1 schema treats
        ``validators`` as an unknown-to-Phase-1 key preserved verbatim.
        """
        with self._lock:
            extras = self._raw_extras.get(tab_id, {})
            validators = extras.get("validators", [])
            if not isinstance(validators, list):
                return []
            return [dict(v) for v in validators if isinstance(v, dict) and v.get("id")]

    def get_selected_rules_for_task(
        self, tab_id: str, task_id: str
    ) -> SelectedRules:
        """Per-task selected_rules. Returns the raw value (list, ``"all"``,
        or ``None``); consumer expands ``"all"`` against the project's
        available rule files."""
        task = self.get_task_config(tab_id, task_id)
        if task is None:
            return None
        return _clone_selected_rules(task.selected_rules)

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
            differs = (
                len(current) != len(defaults)
                or any(asdict(c) != asdict(d) for c, d in zip(current, defaults))
            )
            # Backup the live file (legacy mode) before clobbering caches.
            source = self._project_config_path() if self._project_root else self._fallback_path
            if differs and source and source.exists():
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
                self._task_configs[tab_id] = [TaskConfig(**asdict(t)) for t in DEFAULT_TASK_CONFIGS[tab_id]]
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
                    kept = [
                        t for t in existing
                        if isinstance(t, dict) and t.get("id") in new_ids
                    ]
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

    def set_selected_rules_for_tab(
        self, tab_id: str, selected: SelectedRules
    ) -> None:
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
                task.selected_rules = _clone_selected_rules(selected)
                # Persist intent: a rule-set edit applies to every task
                # in this tab, so every task gets stamped (including
                # previously-pack-only tasks the user is implicitly
                # claiming ownership of by editing rules).
                self._stamp_task_in_snapshot(tab_id, task.id)


# ---------------------------------------------------------------------------
# Small JSON helpers — module-private.
# ---------------------------------------------------------------------------


def _safe_read_json(path: Path) -> Any:
    """Read JSON, returning ``{}`` on any failure. Diagnostics are logged."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read JSON from %s: %s — treating as empty.", path, e)
        return {}


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic JSON write: temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _clone_selected_rules(value: SelectedRules) -> SelectedRules:
    """Defensive copy so callers can't mutate cached state."""
    if isinstance(value, list):
        return list(value)
    return value


def _task_dicts_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True iff two task dicts represent the same TaskConfig values.

    Compares the canonical set of TaskConfig fields only — extras in
    either dict are ignored. This is what the snapshot-stamp filter
    uses to decide "is this task bit-identical to its pack default".
    """
    keys = {f.name for f in fields(TaskConfig)}
    return all(a.get(k) == b.get(k) for k in keys)


def _merge_workflows(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tab merge: ``overlay`` (e.g. project) wins per-entry within
    each section, NOT wholesale per section.

    Phase 4.2 deepens the merge so overriding one task no longer drops
    the pack/editor's other tasks. Per-section semantics:

    * ``tasks`` and ``validators``: list-of-dicts merged by ``id``.
      Overlay entries override matching ids; base entries with ids not
      in overlay are kept. Result order is overlay-first then base
      tail (overlay-declared entries surface to the user first).
    * ``chat_config``: dict; overlay wins shallow (sub-keys preserved
      from base when overlay omits them, e.g. project sets ``enabled``
      but leaves ``system_prompt`` to pack/editor).
    * Unknown keys: overlay wins on conflict, otherwise base value
      survives.

    Project tasks lists with ids not in any base are appended verbatim
    (the user has added something new).
    """
    merged: Dict[str, Any] = {}
    for tab_id in set(base) | set(overlay):
        b = base.get(tab_id, {})
        o = overlay.get(tab_id, {})
        if not isinstance(b, dict):
            b = {}
        if not isinstance(o, dict):
            o = {}
        merged[tab_id] = _merge_tab(b, o)
    return merged


def _merge_tab(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Helper: merge a single tab's dict per the Phase-4.2 rules."""
    out: Dict[str, Any] = {}
    # Start from base; overlay overwrites per-key below.
    for k, v in base.items():
        out[k] = v
    for k, v in overlay.items():
        if k in ("tasks", "validators") and isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = _merge_list_by_id(out[k], v)
        elif k == "chat_config" and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _merge_list_by_id(base: list, overlay: list) -> list:
    """Per-id, per-field merge for list-of-dicts.

    For ids present in both lists, fields are merged at the field
    level: overlay's value wins ONLY when it's not ``None``. ``None``
    in the overlay means "fall through to the base value" — required
    so projects that save a task with ``prompt_template: null``
    (because the user only edited button_label) don't stomp the
    editor's default prompt.

    Result order: overlay-id first, then base ids not in overlay.
    Orphans (entries without an id) tail-appended.
    """
    base_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in base:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            base_by_id[entry["id"]] = entry

    out: list = []
    seen_ids: set = set()
    for entry in overlay:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            eid = entry["id"]
            if eid in seen_ids:
                continue
            base_entry = base_by_id.get(eid, {})
            merged = dict(base_entry)
            for k, v in entry.items():
                if v is not None:
                    merged[k] = v
            out.append(merged)
            seen_ids.add(eid)
    for entry in base:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            if entry["id"] not in seen_ids:
                out.append(entry)
                seen_ids.add(entry["id"])
    # Orphans (no id) — overlay first then base.
    for entry in overlay:
        if not (isinstance(entry, dict) and isinstance(entry.get("id"), str)):
            out.append(entry)
    for entry in base:
        if not (isinstance(entry, dict) and isinstance(entry.get("id"), str)):
            out.append(entry)
    return out
