"""The JSON+Code tab's step list shows every numbered step, including the
ones inside loop and if blocks (owner, 2026-10-13: "all the steps are not
listed, it seems to stop at the for loop")."""

import json
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from procedo.studio.core import StepMarkerParser
from procedo.studio.core.artifact_manager import ArtifactManager
from procedo.studio.core.procedure_steps import numbered_steps
from procedo.studio.tabs.json_code_tab import JsonCodeTab
from procedo.studio.widgets.step_list import StepListCard

STEPS = [
    {"n": 1, "text": "set psu"},
    {"op": "block_for", "body": [{"n": 2, "text": "measure"}, {"n": 3, "text": "log"}]},
    {
        "op": "block_if",
        "branches": [
            {"body": [{"n": 4, "text": "branch a"}]},
            {"body": [{"n": 5, "text": "branch b"}]},
        ],
    },
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
    fake.steps_card = StepListCard("Steps")
    fake._parser = StepMarkerParser()
    fake.artifact_manager = ArtifactManager()
    JsonCodeTab._update_step_markers(fake)
    rows = fake.steps_card.rows()
    assert [(mark, n) for mark, n, _text in rows] == [
        ("✓" if n == 2 else "✗", n) for n in range(1, 8)
    ]
    assert rows[1][2] == "measure"
    assert fake.steps_card.note.text() == "7 steps in JSON · 1 in code"
    assert fake.steps_card.pill.text() == "1 of 7"


CALL_STEPS = [
    {"n": 1, "text": "read"},
    {"n": 2, "op": "call", "function": "analysis.rise_time", "args": [], "outputs": []},
    {"n": 3, "op": "call", "function": "analysis.unknown", "args": [], "outputs": []},
]
HINT = "rise_time(trace, lo=...) returns us\nRise time of an edge."


def test_a_call_step_s_tooltip_shows_its_function_once_the_engine_answers(qapp, tmp_path):
    fake = MagicMock()
    fake.json_editor = QPlainTextEdit(json.dumps({"steps": CALL_STEPS}))
    fake.code_editor = QPlainTextEdit("")
    fake.steps_card = StepListCard("Steps")
    fake._parser = StepMarkerParser()
    fake.artifact_manager = ArtifactManager()
    fake._call_hints = {}
    fake._update_step_markers = lambda: JsonCodeTab._update_step_markers(fake)
    fake._hints_asked = (0, None)
    fake.project_manager.project_root = tmp_path  # no functions/ folder: nothing asked
    JsonCodeTab._fetch_call_hints(fake)
    assert fake._call_hints == {} and fake._hints_asked == (1, tmp_path)
    hints = {"analysis.rise_time": HINT}
    JsonCodeTab._show_call_hints(fake, (0, tmp_path), lambda: hints)  # asked again since
    JsonCodeTab._show_call_hints(fake, (1, tmp_path / "other"), lambda: hints)  # another project
    assert fake._call_hints == {}
    JsonCodeTab._show_call_hints(fake, (1, tmp_path), lambda: hints)
    tips = [fake.steps_card.tree.topLevelItem(i).toolTip(2) for i in range(3)]
    assert tips[1] == f"(no text — op=call)\n\n{HINT}\n\n(no code block — regenerate test.py)"
    assert (
        "returns" not in tips[0] and "returns" not in tips[2]
    )  # a step that is no Call, a function not found


def test_an_answer_for_a_closed_tab_is_dropped(qapp, tmp_path):
    fake = MagicMock()
    fake._hints_asked = (1, tmp_path)
    fake.project_manager.project_root = tmp_path
    fake._update_step_markers.side_effect = RuntimeError("Internal C++ object already deleted.")
    JsonCodeTab._show_call_hints(fake, (1, tmp_path), lambda: {"a.f": "f()"})  # no raise
