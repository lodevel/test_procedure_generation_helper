"""Pins what Studio loads from editor defaults + a bundle + a project.

Characterization of ``TaskConfigManager``'s three-layer load on a realistic
fixture: the host's real ``default_workflows.json`` (editor layer), a bundle
``defaults.json:workflows`` and a project ``config.json:workflows``. The listing is the
host's ``workflow_defaults.effective_workflows``, the one Project Configuration
shows too: a bundle-declared tab lists the editor-only tasks as well, a tab
with no runnable task lists none, and a bundle ``chat_config`` inherits no
editor system prompt. Any change to what a Studio tab lists, runs or shows
makes this fail.
"""

from __future__ import annotations

import json
from pathlib import Path

from procedo.studio.core.task_config import TaskConfigManager

_BUNDLE_WORKFLOWS = {
    "text_json": {
        "tasks": [
            {"id": "derive_json_from_text", "prompt_template": "BUNDLE derive"},
            {"id": "review_json", "prompt_template": "BUNDLE review", "enabled": None},
            {
                "id": "bundle_only",
                "name": "Bundle only",
                "button_label": "B",
                "prompt_template": "BUNDLE only",
            },
        ],
        "validators": [
            {"id": "bundle.v1", "label": "V1"},
            {"id": "bundle.v2", "label": "V2", "enabled": True},
        ],
        "chat_config": {"enabled": False},
        "bundle_key": "from-bundle",
    },
    "json_code": {
        "tasks": [
            {"id": "generate_code_from_json", "prompt_template": "BUNDLE codegen"},
        ],
        "chat_config": {"enabled": False},
    },
    "bundle_tab": {
        "tasks": [{"id": "t", "name": "T", "button_label": "T", "prompt_template": "P"}],
    },
}

_PROJECT_WORKFLOWS = {
    "text_json": {
        "tasks": [
            {
                "id": "review_json",
                "button_label": "Proj review",
                "prompt_template": None,
                "selected_rules": ["01_*.md"],
            },
            {
                "id": "project_only",
                "name": "Proj only",
                "button_label": "P",
                "prompt_template": "PROJECT only",
                "enabled": False,
            },
        ],
        "validators": [{"id": "bundle.v2", "enabled": False}, {"id": "proj.v"}],
        "chat_config": {"system_prompt": "PROJECT chat"},
        "project_key": [1, 2],
    },
    "traceability": {"chat_config": {"enabled": True}},
}


def _manager(tmp_path: Path) -> TaskConfigManager:
    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    (project / "bundle").mkdir()
    (project / "bundle" / "defaults.json").write_text(
        json.dumps({"workflows": _BUNDLE_WORKFLOWS}), encoding="utf-8"
    )
    (project / "config" / "config.json").write_text(
        json.dumps({"workflows": _PROJECT_WORKFLOWS}), encoding="utf-8"
    )
    return TaskConfigManager(fallback_path=tmp_path / "no_fallback.json", project_root=project)


def _tasks(mgr: TaskConfigManager, tab: str) -> list[tuple]:
    return [
        (t.id, t.name, t.button_label, t.prompt_template, t.enabled, t.selected_rules)
        for t in mgr.get_all_tasks_for_tab(tab)
    ]


def _chat(mgr: TaskConfigManager, tab: str) -> tuple:
    c = mgr.get_chat_config(tab)
    return c.enabled, (c.system_prompt or "")[:40]


_EDITOR_CHAT = "Task: Respond to user question or reques"


def test_studio_layered_load_is_pinned(tmp_path):
    mgr = _manager(tmp_path)

    assert _tasks(mgr, "text_json") == [
        ("review_json", "Review JSON", "Proj review", "BUNDLE review", True, ["01_*.md"]),
        ("project_only", "Proj only", "P", "PROJECT only", False, None),
        (
            "derive_json_from_text",
            "Derive JSON from Text",
            "Text → JSON",
            "BUNDLE derive",
            True,
            None,
        ),
        ("bundle_only", "Bundle only", "B", "BUNDLE only", True, None),
        # The editor-only tasks of a bundle-declared tab are listed (no prompt: not runnable).
        ("render_text_from_json", "Render Text from JSON", "JSON → Text", None, True, None),
        ("review_text_procedure", "Review Text Procedure", "Review Text", None, True, None),
        ("review_text_vs_json", "Review Text vs JSON", "Check Text↔JSON", None, True, None),
    ]
    assert not mgr.is_task_invocable("text_json", "render_text_from_json")
    assert [t[0] for t in _tasks(mgr, "json_code")] == [
        "generate_code_from_json",
        "derive_json_from_code",
        "review_json",
        "review_code",
        "review_code_vs_json",
    ]
    assert _tasks(mgr, "bundle_tab") == [("t", "T", "T", "P", True, None)]
    # A tab with no runnable task lists none.
    assert _tasks(mgr, "text_only") == []
    assert _tasks(mgr, "traceability") == []

    assert mgr.get_validator_specs_for_tab("text_json") == [
        {"id": "bundle.v2", "label": "V2", "enabled": False},
        {"id": "proj.v"},
        {"id": "bundle.v1", "label": "V1"},
    ]
    assert mgr.get_validator_specs_for_tab("json_code") == []

    assert _chat(mgr, "text_json") == (False, "PROJECT chat")
    # A partial bundle chat_config inherits no editor system prompt.
    assert _chat(mgr, "json_code") == (False, "")
    assert _chat(mgr, "text_only") == (True, _EDITOR_CHAT)
    assert _chat(mgr, "traceability") == (True, "")

    assert mgr._raw_extras["text_json"] == {
        "validators": mgr.get_validator_specs_for_tab("text_json"),
        "bundle_key": "from-bundle",
        "project_key": [1, 2],
    }
    assert set(mgr._raw_extras) == {"text_json"}
