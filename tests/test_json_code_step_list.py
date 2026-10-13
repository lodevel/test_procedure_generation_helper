"""The JSON+Code tab's step list shows every numbered step, including the
ones inside loop and if blocks (owner, 2026-10-13: "all the steps are not
listed, it seems to stop at the for loop")."""

import json
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QListWidget, QPlainTextEdit

from workflow_editor.core import StepMarkerParser
from workflow_editor.core.artifact_manager import ArtifactManager
from workflow_editor.core.procedure_steps import numbered_steps
from workflow_editor.tabs.json_code_tab import JsonCodeTab

STEPS = [
    {"n": 1, "text": "set psu"},
    {"op": "block_for", "body": [{"n": 2, "text": "measure"}, {"n": 3, "text": "log"}]},
    {"op": "block_if", "branches": [{"body": [{"n": 4, "text": "branch a"}]},
                                    {"body": [{"n": 5, "text": "branch b"}]}]},
    {"op": "block_do", "body": [{"n": 6, "text": "retry"}]},
    {"op": "block_alloc"},
    {"n": 7, "text": "off"},
]


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_numbered_steps_expand_every_block():
    assert [s["n"] for s in numbered_steps(STEPS)] == [1, 2, 3, 4, 5, 6, 7]


def test_the_json_code_list_shows_the_steps_inside_loops(qapp):
    fake = MagicMock()
    fake.json_editor = QPlainTextEdit(json.dumps({"steps": STEPS}))
    fake.code_editor = QPlainTextEdit("# Step 2\nx = 1\n")
    fake.step_list, fake.step_status = QListWidget(), QLabel()
    fake._parser = StepMarkerParser()
    fake.artifact_manager = ArtifactManager()
    JsonCodeTab._update_step_markers(fake)
    rows = [fake.step_list.item(i).text() for i in range(fake.step_list.count())]
    assert [r.split(":")[0] for r in rows] == [f"{'✓' if n == 2 else '✗'} Step {n}" for n in range(1, 8)]
    assert fake.step_status.text() == "7 steps in JSON (1 with code)"
