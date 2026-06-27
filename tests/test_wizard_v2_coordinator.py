"""Tests for the V2 DCDC wizard COORDINATOR (dcdc_wizard_dialog.py).

The wizard owns the per-IC sessions + the shared scheduler + the signal routing by
turn (rail-read vs build). These drive the state machine with a stub chat (no LLM, no
threads); one test uses a REAL SkillChatWidget to exercise the P2→P3 re-parenting.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import logging
import pytest
from PySide6.QtWidgets import QApplication

from workflow_editor.dock.dcdc_wizard_dialog import (
    DcdcWizardDialog, _IcState,
    _PENDING, _RAILED, _RAIL_FAILED, _READY, _ACCEPTED, _ABANDONED,
)
from workflow_editor.authoring.wizard.list_parse import IcRow

logging.disable(logging.CRITICAL)  # silence the no-project skill_menu warning


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _StubChat:
    """Stands in for SkillChatWidget's drive surface — no LLM, no QThread."""

    def __init__(self):
        self.is_busy = False
        self.sent = []

    def run_kickoff(self, priming=""):
        self.sent.append(("kick", priming))

    def send_user_turn(self, text):
        self.sent.append(("turn", text))

    def stop(self):
        pass

    def shutdown(self):
        pass

    def setParent(self, parent):
        pass


@pytest.fixture
def wiz(app):
    class FakeMW:
        backend_factory = None
        project_manager = None
    return DcdcWizardDialog(FakeMW())


def _session(wiz, refdes, part="X", kind="DC-DC"):
    st = _IcState(IcRow(refdes, part, kind, ""), _StubChat())
    st.panel = None
    wiz.sessions[refdes] = st
    return st


def test_four_pages(wiz):
    assert len(wiz.pageIds()) == 4


def test_rail_reply_sets_rail_and_railed(wiz):
    st = _session(wiz, "U5", "RBBA3000-50")
    st.awaiting = "rail"; st.phase = _PENDING
    wiz._on_reply("U5", "U5 -> +CAP_30V ; pin 11 is +Vout on +IN_28V.4")
    assert st.row.rail == "+CAP_30V" and st.phase == _RAILED


def test_rail_reply_without_a_net_marks_failed(wiz):
    st = _session(wiz, "U7")
    st.awaiting = "rail"; st.phase = _PENDING
    wiz._on_reply("U7", "I'm not sure which net — please clarify")
    assert st.phase == _RAIL_FAILED and st.row.rail == ""


def test_build_reply_ready_then_switchable_validate_abandon(wiz):
    st = _session(wiz, "U5", "RBBA3000-50")
    st.row.rail = "+CAP_30V"; st.phase = _RAILED; st.awaiting = "build"
    wiz._on_reply("U5", "## Equipment\nPSU1 : psu\n## Steps\n1. x\n## Expected\n{1} < 100 mV")
    assert st.test_block and st.phase == _READY
    wiz._build._on_validate("U5")
    assert "U5" in wiz.accepted and st.phase == _ACCEPTED
    wiz._build._on_abandon("U5")                    # switchable: drop a validated test
    assert "U5" not in wiz.accepted and st.phase == _ABANDONED
    wiz._build._on_validate("U5")                   # and re-accept
    assert "U5" in wiz.accepted and st.phase == _ACCEPTED


def test_late_build_reply_does_not_override_decision(wiz):
    # A reply that lands after the operator accepted must not revert the phase.
    st = _session(wiz, "U5")
    st.row.rail = "+CAP_30V"; st.phase = _ACCEPTED; st.awaiting = "build"
    wiz.accepted["U5"] = (st.row, "OLD")
    wiz._on_reply("U5", "## Equipment\nE\n## Steps\nS\n## Expected\nX")
    assert st.phase == _ACCEPTED              # stays accepted (terminal not overridden)
    assert st.test_block                       # but the test text DID update


def test_build_pending_guard_defers_then_fires_on_completion(wiz):
    st = _session(wiz, "U9", "LP5907", "LDO")
    st.chat.is_busy = True; st.awaiting = "rail"; st.phase = _PENDING
    wiz.request_build("U9")
    assert st.build_pending and not st.chat.sent           # deferred — nothing fired yet
    st.chat.is_busy = False
    wiz._on_reply("U9", "U9 -> +AUX_3V3")                  # rail lands → RAILED
    wiz._on_busy("U9", False)                               # turn-1 worker done → fire build
    assert not st.build_pending
    turns = [s for s in st.chat.sent if s[0] == "turn"]
    assert len(turns) == 1 and "+AUX_3V3" in turns[0][1]


def test_build_pending_never_double_fires(wiz):
    # _maybe_fire_pending_build is called from BOTH _on_reply and _on_busy — fire ONCE.
    st = _session(wiz, "U9", "LP5907", "LDO")
    st.chat.is_busy = True; st.awaiting = "rail"; st.phase = _PENDING
    wiz.request_build("U9")
    st.chat.is_busy = False
    wiz._on_reply("U9", "U9 -> +AUX_3V3")    # _maybe_fire (not busy, idle) → fires
    wiz._on_busy("U9", False)                 # _maybe_fire again → build_pending now False
    assert len([s for s in st.chat.sent if s[0] == "turn"]) == 1


def test_request_build_noop_when_already_built_or_terminal(wiz):
    st = _session(wiz, "U5"); st.row.rail = "+X"; st.phase = _READY; st.test_block = "T"
    wiz.request_build("U5")
    assert not st.chat.sent and not st.build_pending      # already built → no-op


def test_panel_adopts_then_releases_the_chat_reparent(wiz):
    """Real SkillChatWidget: P3 adopts (re-parents in) the wizard-owned chat; dropping
    the panel releases it back to the holder, keeping the session."""
    from workflow_editor.dock.skill_chat_widget import SkillChatWidget
    chat = SkillChatWidget([], None, backend_tab_id="t_u5",
                           show_skill_selector=False, show_run_button=False,
                           parent=wiz._session_holder)
    row = IcRow("U5", "X", "DC-DC", "+CAP_30V")
    st = _IcState(row, chat); wiz.sessions["U5"] = st
    wiz.picked = [row]
    wiz._build.initializePage()                  # builds the panel + re-parents the chat
    panel = wiz._build._panels.get("U5")
    assert panel is not None and chat.parent() is panel
    wiz._build._drop_panel("U5")                 # release: chat → holder, session kept
    assert "U5" in wiz.sessions and chat.parent() is wiz._session_holder
