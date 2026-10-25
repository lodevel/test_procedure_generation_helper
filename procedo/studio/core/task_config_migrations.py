"""Legacy task-config migrations: the pre-Phase-1 per-project
``tab_contexts.json`` and the old ``button_labels`` tab shape."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from procedo.core import config_manager
from procedo.core.paths import unreachable

from ..llm.backend_base import LLMTask
from .fileio import atomic_write_text
from .task_config_defaults import DEFAULT_TASK_CONFIGS
from .task_config_model import TaskConfig, clone_selected_rules

log = logging.getLogger(__name__)


def migrate_project_tab_contexts(project_root: Path) -> None:
    """Lift ``<project>/config/tab_contexts.json`` into the project's
    ``config.json:workflows`` (then delete the orphan).

    Strict-read both files; abort migration on read failure or
    type-mismatch so a corrupt project config cannot be silently
    replaced. Verifies the post-write file before deleting the legacy
    orphan so a failed write doesn't leave both files stale.
    """
    legacy = project_root / "config" / "tab_contexts.json"
    if reason := unreachable(legacy):
        log.warning(
            "Could not migrate %s: %s", legacy, reason
        )  # never write beside a config that cannot be read
        return
    if not legacy.exists():
        return
    project_cfg_path = project_root / "config" / "config.json"

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
            log.error(
                "Project config %s is unreadable (%s); aborting migration to "
                "avoid clobbering. Repair config.json or remove tab_contexts.json.",
                project_cfg_path,
                e,
            )
            return
        if not isinstance(existing_full, dict):
            log.error(
                "Project config %s is not a JSON object; aborting migration.", project_cfg_path
            )
            return
        # If workflows section already exists, the migration already
        # happened on a prior load (or a user wrote one manually) —
        # don't overwrite it from a stale legacy file.
        if isinstance(existing_full.get("workflows"), dict) and existing_full["workflows"]:
            log.info(
                "Project %s already has a workflows section; deleting legacy %s.",
                project_cfg_path,
                legacy,
            )
            try:
                legacy.unlink()
            except OSError as e:
                log.warning("Could not delete legacy %s: %s", legacy, e)
            return
    else:
        existing_full = {}

    # Run button-label migration on the legacy data first.
    migrate_button_labels_in_place(legacy_data)

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
                    t = {**t, "selected_rules": clone_selected_rules(tab_level)}
                new_tasks.append(t)
            cleaned["tasks"] = new_tasks
        migrated_workflows[tab_id] = cleaned

    # Preserve-and-overlay write.
    existing_full["workflows"] = migrated_workflows
    try:
        atomic_write_text(project_cfg_path, json.dumps(existing_full, indent=2, ensure_ascii=False))
    except OSError as e:
        log.error(
            "Failed to write migrated workflows to %s: %s — leaving %s in place.",
            project_cfg_path,
            e,
            legacy,
        )
        return

    # Read-back verification before unlinking the legacy file.
    verify = config_manager.read_unified_config(project_cfg_path.parent)
    if not (
        isinstance(verify, dict)
        and isinstance(verify.get("workflows"), dict)
        and verify["workflows"]
    ):
        log.error(
            "Read-back verification failed for %s; leaving %s untouched.", project_cfg_path, legacy
        )
        return

    try:
        legacy.unlink()
        log.info("Migrated %s → %s:workflows", legacy, project_cfg_path)
    except OSError as e:
        log.warning("Migrated workflows but could not delete %s: %s", legacy, e)


def migrate_button_labels_in_place(raw: Dict[str, Any]) -> bool:
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
            new_tasks.append(
                TaskConfig(
                    id=default_task.id,
                    name=default_task.name,
                    button_label=custom if custom else default_task.button_label,
                    prompt_template=default_task.prompt_template,
                    enabled=default_task.enabled,
                )
            )
        tab_config["tasks"] = [t.to_dict() for t in new_tasks]
        del tab_config["button_labels"]
        migrated = True
        log.info("Migrated tab '%s' from button_labels to tasks format", tab_id)
    return migrated


# ------------------------------------------------------------------
# Snapshot stamping (Codex H2: pack defaults never round-trip through save)
# ------------------------------------------------------------------
