"""Workflow editor defaults and the builders that turn workflow dicts into
the typed task / chat caches of ``TaskConfigManager``."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from procedo.core import bundle_registry, studio_launch_env
from procedo.core.paths import is_file_quiet
from procedo.core.workflow_defaults import read_editor_defaults

from .task_config_model import ChatConfig, SelectedRules, TaskConfig, clone_selected_rules

# ---------------------------------------------------------------------------
# Workflow editor defaults
# ---------------------------------------------------------------------------
#
# DEFAULT_TASK_CONFIGS / DEFAULT_CHAT_CONFIG are built at import time from
# the host's ``procedo/core/default_workflows.json``, read by its one reader
# (``procedo.core.workflow_defaults.read_editor_defaults``) — the same file the
# Project Configuration → Workflows tab shows. An unreadable file reads as
# empty defaults.


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


_DEFAULT_WORKFLOWS_RAW: Dict[str, Any] = read_editor_defaults()
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
# rewritten — see ``workflows_to_caches``'s ``drop_tab_selected_rules``.
_KNOWN_TAB_KEYS: frozenset[str] = frozenset({"tasks", "chat_config", "button_labels"})


def load_pack_workflow_defaults(project_root: Optional[Path]) -> Dict[str, Any]:
    """Return the workflows-block defaults injected by the parent.

    Phase 5h: the legacy manifest-walk path (resolve
    ``packs.selected_packs`` + read each pack's
    ``pack_workflow_defaults.json``) was removed. The parent app
    owns bundle resolution; the bundle's ``defaults.json`` lives at
    ``<project>/bundle/defaults.json``.

    Task #39: resolve the ACTIVE project's bundle first, so a
    bundle imported while the editor runs is picked up by a plain
    ``reload()``. The ``PROCEDO_BUNDLE_DEFAULTS_PATH`` env var is a
    launch-time snapshot (unset when no bundle existed at editor
    launch, or in standalone launches) — kept only as the legacy
    fallback for no-project mode.

    Returns ``{}`` when no bundle resolves, the file is missing
    or malformed, or the bundle ships no workflows block. The
    editor's merge with its baked-in defaults degrades gracefully
    to editor-defaults-only.
    """
    defaults_path: Optional[Path] = None
    if project_root is not None:
        candidate = project_root / "bundle" / "defaults.json"
        if is_file_quiet(candidate):
            defaults_path = candidate
    if defaults_path is None:
        path_str = os.environ.get(studio_launch_env.BUNDLE_DEFAULTS_PATH)
        if not path_str:
            return {}
        defaults_path = Path(path_str)
    workflows = (bundle_registry.read_defaults(defaults_path) or {}).get("workflows")
    return workflows if isinstance(workflows, dict) else {}


def apply_editor_field_fallback(pack: Dict[str, Any]) -> Dict[str, Any]:
    """Fill None fields in pack-declared tasks from the editor's same-id
    task. Pack still defines the task universe — editor-only tasks/tabs
    are not pulled in. Returns a deep copy; ``pack`` is left untouched.

    Lets the bundle ship sparse ``{id, prompt_template}`` tasks while
    still satisfying ``TaskConfig.__init__`` (which needs ``name`` and
    ``button_label``).
    """
    enriched = deepcopy(pack) if isinstance(pack, dict) else {}
    for tab_id, tab_cfg in enriched.items():
        if not isinstance(tab_cfg, dict):
            continue
        pack_tasks = tab_cfg.get("tasks")
        if not isinstance(pack_tasks, list):
            continue
        editor_tab = _DEFAULT_WORKFLOWS_RAW.get(tab_id, {})
        editor_tasks = (
            editor_tab.get("tasks", [])
            if isinstance(editor_tab, dict) else []
        )
        editor_by_id: Dict[str, Dict[str, Any]] = {}
        for et in editor_tasks:
            if isinstance(et, dict) and isinstance(et.get("id"), str):
                editor_by_id[et["id"]] = et
        for i, t in enumerate(pack_tasks):
            if not isinstance(t, dict):
                continue
            editor_t = editor_by_id.get(t.get("id"), {})
            merged = dict(editor_t)
            for k, v in t.items():
                if v is not None:
                    merged[k] = v
            pack_tasks[i] = merged
    return enriched


def workflows_to_caches(
    workflows: Dict[str, Any],
    *,
    drop_tab_selected_rules: bool,
) -> Tuple[
    Dict[str, List[TaskConfig]], Dict[str, ChatConfig], Dict[str, Dict[str, Any]]
]:
    """Build the (tasks, chat configs, raw extras) caches from a
    ``workflows``-shaped dict.

    Args:
        workflows: ``workflows`` section or legacy fallback root.
        drop_tab_selected_rules: When True (project mode), tab-level
            ``selected_rules`` is lifted onto each task AND removed
            from the extras so subsequent saves don't write it
            back — completes the migration. When False (legacy
            single-file mode), the tab-level field is kept in
            the extras and round-trips through save.
    """
    task_configs: Dict[str, List[TaskConfig]] = {}
    chat_configs: Dict[str, ChatConfig] = {}
    raw_extras: Dict[str, Dict[str, Any]] = {}
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
                    task.selected_rules = clone_selected_rules(tab_level_selected)
                tasks.append(task)
        elif tab_level_selected is not None and tab_id in DEFAULT_TASK_CONFIGS:
            # Tab declares selected_rules but no tasks: fill in defaults
            # so the rules propagate (was a silent drop pre-Codex review).
            for d in DEFAULT_TASK_CONFIGS[tab_id]:
                t = TaskConfig(**asdict(d))
                t.selected_rules = clone_selected_rules(tab_level_selected)
                tasks.append(t)
        task_configs[tab_id] = tasks

        chat_data = tab_config.get("chat_config")
        if isinstance(chat_data, dict):
            chat_configs[tab_id] = ChatConfig.from_dict(chat_data)

        # Preserve unknown keys verbatim (Codex Q3). ``selected_rules``
        # lives in extras (not ``_KNOWN_TAB_KEYS``) so legacy round-trip
        # is intact; project mode pops it below.
        extras = {k: v for k, v in tab_config.items() if k not in _KNOWN_TAB_KEYS}
        if drop_tab_selected_rules:
            extras.pop("selected_rules", None)
        if extras:
            raw_extras[tab_id] = extras
    return task_configs, chat_configs, raw_extras


def fill_missing_defaults(
    task_configs: Dict[str, List[TaskConfig]],
    chat_configs: Dict[str, ChatConfig],
) -> None:
    """Add baked-in defaults for any tabs the loaded data didn't cover."""
    for tab_id, defaults in DEFAULT_TASK_CONFIGS.items():
        if tab_id not in task_configs or not task_configs[tab_id]:
            task_configs[tab_id] = [TaskConfig(**asdict(t)) for t in defaults]
    for tab_id, default_chat in DEFAULT_CHAT_CONFIG.items():
        if tab_id not in chat_configs:
            chat_configs[tab_id] = ChatConfig(
                enabled=default_chat.enabled,
                system_prompt=default_chat.system_prompt,
            )


def default_caches() -> Tuple[Dict[str, List[TaskConfig]], Dict[str, ChatConfig]]:
    """Fresh copies of the baked-in default task and chat configs."""
    return (
        {
            tab_id: [TaskConfig(**asdict(t)) for t in default_tasks]
            for tab_id, default_tasks in DEFAULT_TASK_CONFIGS.items()
        },
        {
            tab_id: ChatConfig(enabled=c.enabled, system_prompt=c.system_prompt)
            for tab_id, c in DEFAULT_CHAT_CONFIG.items()
        },
    )
