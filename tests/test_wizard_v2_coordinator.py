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


class _StubSignal:
    def connect(self, *a):
        pass

    def disconnect(self):
        pass


class _StubChat:
    """Stands in for SkillChatWidget's drive surface — no LLM, no QThread."""

    def __init__(self):
        self.is_busy = False
        self.sent = []
        self.busy_changed = _StubSignal()
        self.reply_finished = _StubSignal()
        self.reply_failed = _StubSignal()
        self.conversation_reset = _StubSignal()

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

    def deleteLater(self):
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


def _real_session(wiz, refdes, rail=""):
    """A session backed by a REAL SkillChatWidget — needed wherever a panel/host adds the
    chat to its layout (re-parent paths)."""
    from workflow_editor.dock.skill_chat_widget import SkillChatWidget
    chat = SkillChatWidget([], None, backend_tab_id=f"t_{refdes}",
                           show_skill_selector=False, show_run_button=False,
                           parent=wiz._session_holder)
    st = _IcState(IcRow(refdes, "X", "DC-DC", rail), chat)
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
    assert panel is not None and panel.isAncestorOf(chat)   # chat lives in the panel's split
    wiz._build._drop_panel("U5")                 # release: chat → holder, session kept
    assert "U5" in wiz.sessions and chat.parent() is wiz._session_holder


def test_inline_rail_edit_survives_a_refresh(wiz):
    """An operator's manual rail correction must NOT be clobbered when a later refresh
    fires (e.g. another IC's rail-read completes)."""
    from workflow_editor.dock.dcdc_wizard_dialog import _RailHost
    st = _real_session(wiz, "U5", "+CAP_30V"); st.phase = _RAILED
    host = _RailHost(st, lambda k: None, ["+CAP_30V", "+IN_28V", "DISCH_16V"])
    wiz._rail._hosts = {"U5": host}
    host.set_rail("+CAP_30V")                        # initial read fills the field
    host.rail_combo.setCurrentText("+CAP_30V_FIX")   # operator corrects it
    wiz._rail.refresh()                              # a full refresh (another IC updated)
    assert host.rail_value() == "+CAP_30V_FIX"      # preserved, not reverted


def test_reask_reply_overwrites_the_field(wiz):
    """A re-ask that returns a corrected rail DOES update the field."""
    from workflow_editor.dock.dcdc_wizard_dialog import _RailHost
    st = _real_session(wiz, "U5", "+CAP_30V"); st.phase = _RAILED
    host = _RailHost(st, lambda k: None, ["+CAP_30V", "+IN_28V", "DISCH_16V"])
    wiz._rail._hosts = {"U5": host}
    host.set_rail("+CAP_30V")
    st.row.rail = "+IN_28V"                           # re-ask produced a different rail
    wiz._rail.on_rail_update("U5")
    assert host.rail_value() == "+IN_28V"


def test_p2_does_not_auto_read_until_the_trigger(wiz):
    """P2 creates sessions/hosts on entry but does NOT start the reads — the operator
    presses 🔌 Read rails."""
    from workflow_editor.dock.dcdc_wizard_dialog import SlotState
    wiz.checked = [IcRow("U5", "X", "DC-DC", "")]
    wiz._rail.initializePage()
    assert "U5" in wiz.sessions and "U5" in wiz._rail._hosts
    assert wiz._scheduler.state_of("U5") is SlotState.IDLE        # not auto-started
    assert wiz.sessions["U5"].phase == _PENDING


def test_per_ic_read_or_reread_kicks_off_first_then_reasks(wiz):
    """The per-IC button: first press kicks off (run_kickoff), then re-asks (a turn)."""
    st = _session(wiz, "U5", "RBBA3000-50")
    assert not st.rail_read_started
    wiz.read_or_reread("U5")                                      # 1st press → kickoff
    assert st.rail_read_started
    assert any(s[0] == "kick" for s in st.chat.sent)
    wiz.read_or_reread("U5")                                      # 2nd press → re-ask
    assert any(s[0] == "turn" for s in st.chat.sent)


def test_trash_resets_ic_back_to_read_rail(wiz):
    """A trashed chat (conversation_reset) resets that IC so its next action is Read rail."""
    st = _session(wiz, "U5", "RBBA3000-50")
    st.rail_read_started = True
    st.phase = _RAILED
    st.row.rail = "+CAP_30V"
    st.test_block = "T"
    wiz._on_chat_reset("U5")                                      # the trash handler
    assert not st.rail_read_started
    assert st.phase == _PENDING
    assert st.row.rail == "" and st.test_block == ""


def test_chat_reparents_p2_host_to_p3_panel_and_back(wiz):
    """The one per-IC chat ping-pongs: P2 host → P3 panel → back to P2 host."""
    from workflow_editor.dock.dcdc_wizard_dialog import _RailHost
    st = _real_session(wiz, "U5", "+CAP_30V"); st.phase = _RAILED
    host = _RailHost(st, lambda k: None, ["+CAP_30V", "+IN_28V", "DISCH_16V"])
    wiz._rail._hosts = {"U5": host}; host.adopt_chat()
    assert st.chat.parent() is host
    wiz.picked = [st.row]
    wiz._build.initializePage()                       # P3 adopts the chat (into its split)
    assert wiz._build._panels["U5"].isAncestorOf(st.chat)
    host.adopt_chat()                                 # back to P2
    assert st.chat.parent() is host


def test_chats_survive_next_then_back_via_showevent(wiz):
    """Next (P2->P3) re-parents the chat into P3; Back (->P2) must re-claim it. QWizard does
    NOT re-run P2.initializePage on Back, so P2.showEvent does the re-adopt — without it the
    chats vanish from P2 (the reported bug)."""
    from PySide6.QtGui import QShowEvent
    st = _real_session(wiz, "U5", "+CAP_30V"); st.phase = _RAILED
    wiz.checked = [st.row]; wiz._rail.initializePage()
    host = wiz._rail._hosts["U5"]
    wiz.picked = [st.row]; wiz._build.initializePage()        # Next: P3 panel takes the chat
    assert st.panel.isAncestorOf(st.chat)
    wiz._rail.showEvent(QShowEvent())                         # Back: P2 shown -> re-claim
    assert host.isAncestorOf(st.chat)                         # chat is back in P2's host


def test_is_rail_turn_uses_awaiting_then_active_page(wiz):
    st = _session(wiz, "U5")
    st.awaiting = "rail"; assert wiz._is_rail_turn(st) is True
    st.awaiting = "build"; assert wiz._is_rail_turn(st) is False
    st.awaiting = None; assert wiz._is_rail_turn(st) is False     # default page P1, not P2


# --- review-fix regressions (capped / raced / failed paths) ----------------- #

def test_drop_session_frees_a_running_slot(wiz):
    """#1 BLOCKER: de-selecting an IC whose rail-read is RUNNING must free its slot —
    busy_changed is disconnected first, so the normal complete() path can't run."""
    from workflow_editor.dock.dcdc_wizard_dialog import SlotState
    st = _session(wiz, "U5")
    wiz._scheduler.submit("U5", lambda: None)          # take the slot
    assert wiz._scheduler.state_of("U5") is SlotState.RUNNING
    st.chat.is_busy = True
    wiz.drop_session("U5")
    assert wiz._scheduler.state_of("U5") is SlotState.IDLE
    assert wiz._scheduler.running_count == 0           # not stranded


def test_on_failed_clears_awaiting_and_marks_rail_failed(wiz):
    """#4: a failed turn-1 read clears awaiting + marks RAIL_FAILED + drops a deferred
    build (else the router strands and the deferred build never fires)."""
    st = _session(wiz, "U5")
    st.awaiting = "rail"; st.phase = _PENDING; st.build_pending = True
    wiz._on_failed("U5", "backend error")
    assert st.awaiting is None and st.phase == _RAIL_FAILED and not st.build_pending


def test_late_rail_reply_does_not_clobber_confirmed_rail(wiz):
    """#5: operator typed a rail + advanced (phase RAILED, via P2 validatePage) while
    turn-1 was still streaming; the late auto-read must be ignored."""
    st = _session(wiz, "U5")
    st.row.rail = "+CAP_30V_TYPED"; st.phase = _RAILED; st.awaiting = "rail"
    wiz._on_reply("U5", "U5 -> +SOMETHING_ELSE")
    assert st.row.rail == "+CAP_30V_TYPED" and st.awaiting is None
