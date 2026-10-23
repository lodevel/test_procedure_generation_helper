"""A skill declares the context it cannot work without (frontmatter ``needs-context``, item
keys of the chat's context picker, e.g. the DCDC classifier's ``component_ids``): the picker
ticks those items by default, and a fresh conversation does not start while one is unticked,
missing or empty — the chat says which one instead of running into a useless reply."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from procedo.studio.authoring import ArtifactProvider, ArtifactsSource
from procedo.studio.authoring.skill import Skill, SkillSource
from procedo.studio.dock import skill_chat_widget as scw

from .test_main_window_characterization import qapp as qapp
from .test_project_rules_skills import _delete_now

_IDS = "Component part numbers (ICs — U*, IC*):\n- U5: Type='DC-DC'"


def _skill(needs=None):
    meta = {"needs-context": needs} if needs is not None else {}
    return Skill(skill_id="dcdc_classifier", title="DCDC classifier", system_prompt="SYSTEM",
                 path=Path("/x"), source=SkillSource.BUNDLED, metadata=meta)


@pytest.fixture
def chat(qapp, monkeypatch):
    """A skill chat over an Artifacts source, its sends recorded instead of fired."""
    monkeypatch.setattr("procedo.studio.dialogs.settings_dialog.load_settings", lambda: {})
    sent: list = []
    made: list = []

    def make(skill, ids=_IDS):
        def provide():
            return ids() if callable(ids) else ids
        source = ArtifactsSource([
            ArtifactProvider("netlist", "Netlist (connectivity)", lambda: "- U5: 1=VIN"),
            ArtifactProvider("component_ids", "Component part numbers (ICs: U, IC)", provide,
                             empty_hint="no ODB++ board archive in the project?"),
        ])
        widget = scw.SkillChatWidget([skill], SimpleNamespace(), sources=[source])
        widget._really_dispatch = lambda prompt, files: sent.append(prompt)
        made.append(widget)
        return widget

    yield make, sent
    for widget in made:
        widget._worker = None
        widget.shutdown()
        _delete_now(widget)


def _ticked(widget):
    return {s.source_id: keys for s, keys in widget._picker.selections()}["artifacts"]


def test_the_needed_part_numbers_are_ticked_by_default_and_ride_the_kickoff(chat):
    make, sent = chat
    widget = make(_skill(["component_ids"]))
    assert _ticked(widget) == ["component_ids"]
    widget.run_kickoff()
    assert len(sent) == 1 and "U5: Type='DC-DC'" in sent[0]


def test_a_skill_that_needs_nothing_starts_with_nothing_ticked(chat):
    make, sent = chat
    widget = make(_skill())
    assert _ticked(widget) == []
    widget.run_kickoff()
    assert len(sent) == 1


def test_a_fresh_run_ticks_the_needed_item_again(chat):
    """The wizard's Classify re-pins its skill (a fresh session): the defaults come back."""
    make, _sent = chat
    skill = _skill(["component_ids"])
    widget = make(skill)
    widget._picker.set_checked("artifacts", [])
    widget.set_skill(skill)
    assert _ticked(widget) == ["component_ids"]


def test_an_unticked_needed_item_stops_the_run_and_says_which(chat):
    make, sent = chat
    widget = make(_skill(["component_ids"]))
    widget._picker.set_checked("artifacts", ["netlist"])
    widget.run_kickoff()
    assert sent == [] and not widget.is_busy
    status = widget._status.full
    assert "Component part numbers (ICs: U, IC)" in status and "tick" in status.lower()
    widget._picker.set_checked("artifacts", ["component_ids"])
    widget.run_kickoff()
    assert len(sent) == 1


def test_an_empty_needed_item_stops_the_run(chat):
    """No board archive, or no IC part numbers on it: the classifier would have nothing."""
    make, sent = chat
    widget = make(_skill(["component_ids"]), ids="")
    assert _ticked(widget) == ["component_ids"]
    widget.run_kickoff()
    assert sent == []
    assert "Component part numbers (ICs: U, IC)" in widget._status.full
    assert "empty" in widget._status.full and "no ODB++ board archive" in widget._status.full


def test_a_needed_item_the_picker_does_not_offer_stops_the_run(chat):
    make, sent = chat
    widget = make(_skill(["bom_table"]))
    widget.run_kickoff()
    assert sent == [] and "bom_table" in widget._status.full


def test_a_send_that_starts_the_conversation_is_checked_too_and_keeps_the_typed_text(chat):
    make, sent = chat
    widget = make(_skill(["component_ids"]))
    widget._picker.set_checked("artifacts", [])
    widget._input.setPlainText("classify the ICs")
    widget._on_send()
    assert sent == [] and widget._input.toPlainText() == "classify the ICs"
    widget._picker.set_checked("artifacts", ["component_ids"])
    widget._on_send()
    assert len(sent) == 1 and widget._input.toPlainText() == ""


@pytest.mark.parametrize("value", [5, True, "component_ids"])
def test_a_needs_context_that_is_not_a_list_never_crashes(chat, value):
    make, sent = chat
    widget = make(_skill(value))
    widget.run_kickoff()
    if value == "component_ids":
        assert len(sent) == 1
    else:
        assert sent == [] and f"'{value}'" in widget._status.full


def test_what_only_this_skill_needs_is_not_handed_on(chat):
    """The wizard hands the classify page's context to every per-IC build: not the BOM."""
    make, _sent = chat
    widget = make(_skill(["component_ids"]))
    widget._picker.tick(["netlist"])
    handed = widget.resolved_context(without_needed=True).text
    assert "U5: 1=VIN" in handed and "Type='DC-DC'" not in handed
    assert "Type='DC-DC'" in widget.resolved_context().text


def _wait(qapp, done, seconds=3.0):
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    return done()


def test_sizing_a_slow_default_item_does_not_freeze_the_window(chat, qapp):
    """The board behind the part numbers takes ~0.5 s to load: the context card's cue says it
    is sizing, on a worker thread; the window keeps running."""
    make, _sent = chat

    def slow():
        time.sleep(0.4)
        return _IDS

    widget = make(_skill(["component_ids"]), ids=slow)
    start = time.perf_counter()
    widget._picker._update_readout()
    assert time.perf_counter() - start < 0.1
    assert widget._picker._readout.text() == "Context: 1 items"
    assert widget._picker.loading.active and widget._picker.loading.label.text() == "Sizing the context…"
    assert _wait(qapp, lambda: "tokens (est.)" in widget._picker._readout.text())
    assert not widget._picker.loading.active


def test_the_conversation_says_working_while_the_skill_runs(chat):
    """Owner 2026-10-23: a request in flight shows — the Conversation card's cue follows the
    chat's busy state (sent, answered or stopped)."""
    from procedo.core.ui_kit import LoadingCue

    make, _sent = chat
    widget = make(_skill())
    [cue] = [c for c in widget.findChildren(LoadingCue) if not widget._picker.isAncestorOf(c)]
    widget._set_busy(True)
    assert cue.active and cue.label.text() == "Working…"
    widget._set_busy(True)  # said twice: still one load
    widget._set_busy(False)
    assert not cue.active
