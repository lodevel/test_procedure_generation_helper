"""AI changes are approved as proposal cards in the chat (owner, 2026-10-15): a proposal waits as a
card in the chat of the view that asked, nothing is applied before Accept, Accept applies it the
way the review window's Accept did, Reject drops it, View diff… is the review window with the
same two decisions; a card whose file changed since is never applied without the diff against
the current text; cards survive a view switch and are discarded when their test closes; a reply
is only ever proposed for the test it was asked on; Parse + Generate's card is outdated once the
text it was made from changed; an Accept that fails is never reported as applied."""

import json
import stat
from types import SimpleNamespace

import pytest

from procedo.studio.core.proposal import (
    APPLIED,
    DISCARDED,
    PENDING,
    REJECTED,
    FileChange,
    FileText,
    Proposal,
    discard_pending,
)
from procedo.studio.dialogs.diff_viewer import DiffViewer
from procedo.studio.dock.chat_messages import CLOSED, ProposalCard, ProposalMessage
from procedo.studio.llm import LLMTask
import procedo.studio.tabs.llm_tab_mixin as llm_tab_mixin
import procedo.studio.tabs.llm_validator_loop as llm_validator_loop
import procedo.studio.tabs.text_actions as text_actions

from .test_main_window_characterization import (
    _make_project,
    _open_project,
    _settle,
    make_window as make_window,
    mbox as mbox,
    qapp as qapp,
)

TEXT = "# t1\n\nBody of t1.\n"
PROPOSED_TEXT = "# t1\n\nBody of t1.\n\n## Steps\n1. Set PSU1 CH1 voltage = 28 V.\n"


# --- the proposal itself (Qt-free) ------------------------------------------------------------


def _proposal(text, proposed, log):
    box = {"text": text}
    change = FileChange("procedure_text.md", lambda: box["text"], proposed)
    return box, Proposal([change], lambda: log.append("applied"), lambda: log.append("rejected"))


def test_counts_preview_and_summary_come_from_the_diff_window_diff(qapp):
    log = []
    _, p = _proposal("a\nb\nc\n", "a\nB\nc\nd\ne\nf\n", log)
    assert p.counts() == (4, 1)
    assert p.preview() == ["B", "d", "e"]  # the first changed lines, as they will read
    assert p.summary() == "Proposal · procedure_text.md +4 −1"
    dialog = DiffViewer("a\nb\nc\n", "a\nB\nc\nd\ne\nf\n")
    assert dialog.diff_view.toPlainText().splitlines() == p.changes[0].lines
    _, removal = _proposal("a\nb\n", "a\n", log)
    assert removal.preview() == ["− b"]


def test_a_proposal_whose_lines_all_match_has_nothing_to_review():
    _, p = _proposal("x\nmain()", "x\nmain()\n", [])
    assert p.changes == []


def test_accept_applies_once_reject_drops_and_neither_twice():
    log = []
    _, p = _proposal("a\n", "b\n", log)
    assert p.accept() and p.state == APPLIED and log == ["applied"]
    assert not p.accept()
    p.reject()
    assert log == ["applied"]
    _, q = _proposal("a\n", "b\n", log)
    q.reject()
    assert q.state == REJECTED and log == ["applied", "rejected"]
    assert not q.accept()


def test_a_stale_proposal_applies_only_once_reviewed():
    log = []
    box, p = _proposal("a\n", "b\n", log)
    box["text"] = "a\ntyped\n"
    assert p.stale()
    assert not p.accept() and p.state == PENDING and log == []
    assert p.accept(reviewed=True) and log == ["applied"]
    assert not p.stale()  # decided: no longer waiting


def test_an_outdated_proposal_never_applies_not_even_reviewed():
    log = []
    source = {"text": "x\n"}
    box = {"text": "a\n"}
    p = Proposal(
        [FileChange("procedure.json", lambda: box["text"], "b\n")],
        lambda: log.append("applied"),
        lambda: log.append("rejected"),
        inputs=[FileText("procedure_text.md", lambda: source["text"])],
    )
    source["text"] = "x\ny\n"
    assert p.outdated() == ["procedure_text.md"] and p.stale()
    assert not p.accept() and not p.accept(reviewed=True)
    assert p.state == PENDING and log == []


def test_an_accept_that_raises_leaves_it_waiting_with_the_error():
    def fail():
        raise PermissionError("test.py is read-only")

    p = Proposal([FileChange("test.py", lambda: "a\n", "b\n")], fail, lambda: None)
    assert not p.accept()
    assert (p.state, p.error) == (PENDING, "Not applied: test.py is read-only")


@pytest.mark.parametrize("decide", ["accept", "reject", "discard"])
def test_a_decided_proposal_keeps_only_what_its_card_shows(decide):
    log = []
    _, p = _proposal("a\nb\n", "a\nB\n", log)
    shown = (p.summary(), p.counts(), p.preview(), p.file_names)
    getattr(p, decide)()
    assert (p.changes, p.inputs, p.test, p._apply, p._rejected) == ([], [], None, None, None)
    assert (p.summary(), p.counts(), p.preview(), p.file_names) == shown


def test_a_proposal_that_failed_validation_applies_only_reviewed():
    log = []
    p = Proposal(
        [FileChange("procedure_text.md", lambda: "a\n", "b\n")],
        lambda: log.append("applied"),
        lambda: None,
        "Proposal failed validation",
    )
    assert p.needs_review() and not p.accept() and log == []
    assert p.accept(reviewed=True) and log == ["applied"]


def test_discard_pending_leaves_decided_ones():
    log = []
    _, waiting = _proposal("a\n", "b\n", log)
    _, done = _proposal("a\n", "c\n", log)
    done.accept()
    messages = [
        SimpleNamespace(proposal=waiting),
        SimpleNamespace(proposal=done),
        SimpleNamespace(role="system"),
    ]
    assert discard_pending(messages) == 1
    assert (waiting.state, done.state) == (DISCARDED, APPLIED)
    assert not waiting.accept() and log == ["applied"]


# --- in the window ----------------------------------------------------------------------------


@pytest.fixture
def opened(make_window, tmp_path, monkeypatch):
    # The text reconstruction needs the grammar wheel: here it hands the proposal back as is.
    monkeypatch.setattr(
        text_actions, "reconstructed_or_error", lambda content, current, **kw: (content, None, None)
    )
    root = _make_project(tmp_path / "proj", tests=("t1", "t2"))
    w = make_window()
    _open_project(w, root)
    w.open_test(root / "tests" / "t1")
    w.root = root
    return w


def _llm(content):
    return SimpleNamespace(mode="replace", content=content)


def _cards(w):
    return [m for m in w.dock.chat_panel._messages() if isinstance(m, ProposalCard)]


def _last(view):
    return view.tab_context.messages[-1]


def _show(w, view):
    w.view_stack.setCurrentWidget(view)
    assert w.dock.chat_panel._current_tab_context is view.tab_context


@pytest.mark.parametrize("kind", ["text", "json", "code"])
def test_a_proposal_waits_as_a_card_and_accept_applies_it_as_the_dialog_did(opened, kind):
    w = opened
    if kind == "text":
        view, card, offer = w.text_only_tab, w.text_only_tab.text_card, PROPOSED_TEXT
        view._handle_text_proposal(_llm(offer))
        _settle()
        expected = offer
    elif kind == "json":
        view, card = w.json_code_tab, w.json_code_tab.json_card
        _show(w, view)
        offer = {"id": "t1", "steps": [{"n": 1, "note": "5 µA ± 1 Ω"}]}
        view._handle_json_proposal(_llm(offer))
        expected = json.dumps(offer, indent=2, ensure_ascii=False)  # µ ± Ω stay literal
    else:
        view, card = w.json_code_tab, w.json_code_tab.code_card
        _show(w, view)
        expected = "def main():\n    return 2\n"
        view._handle_code_proposal(_llm(expected))
    before = card.editor.toPlainText()

    message = _last(view)
    assert isinstance(message, ProposalMessage) and message.proposal.state == PENDING
    assert card.editor.toPlainText() == before  # nothing applied before Accept
    assert w.artifact_manager.get_content(card.kind) == before
    shown = _cards(w)
    assert [c.proposal for c in shown] == [message.proposal]
    assert shown[0].accept_button.isVisibleTo(shown[0])

    shown[0].accept_button.click()

    assert card.editor.toPlainText() == expected
    assert w.artifact_manager.get_content(card.kind) == expected
    assert card.pill.text() == "● Modified"  # an edit, as if typed: Save writes it
    assert (_last(view).content, _last(view).tone) == (
        f"✓ Applied changes to {card.file_name}",
        "success",
    )
    assert message.proposal.state == APPLIED
    assert not shown[0].actions.isVisibleTo(shown[0])
    assert shown[0].state_line.text() == "✓ Applied"


def test_reject_leaves_the_artifact_unchanged(opened):
    w = opened
    view = w.text_only_tab
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    _cards(w)[0].reject_button.click()
    assert view.text_card.editor.toPlainText() == TEXT
    assert w.artifact_manager.get_content(view.text_card.kind) == TEXT
    assert (_last(view).content, _last(view).tone) == (
        "✗ Rejected changes to procedure_text.md",
        "danger",
    )
    assert _cards(w)[0].state_line.text() == "✗ Rejected"


def _review(monkeypatch, button):
    """The diff window opens (its original / proposed recorded) and *button* is clicked."""
    shown = []

    def fake_exec(dialog):
        shown.append((dialog._original, dialog._proposed))
        if button:
            getattr(dialog, button).click()
        return 0

    monkeypatch.setattr(DiffViewer, "exec", fake_exec)
    return shown


def test_a_card_whose_file_changed_since_applies_only_from_the_diff_against_the_current_text(
    opened, monkeypatch
):
    w = opened
    view = w.text_only_tab
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    card = _cards(w)[0]
    view.text_card.editor.setPlainText(TEXT + "typed after the proposal\n")

    card.accept_button.click()  # refused: the card says so and offers View diff… only

    assert view.text_card.editor.toPlainText() == TEXT + "typed after the proposal\n"
    assert card.proposal.state == PENDING
    assert not card.accept_button.isVisibleTo(card)
    assert card.diff_button.isVisibleTo(card)
    assert card.state_line.text().startswith("⚠ The file changed since")

    shown = _review(monkeypatch, "accept_btn")
    card.diff_button.click()
    assert shown == [(TEXT + "typed after the proposal\n", PROPOSED_TEXT)]  # the CURRENT text
    assert view.text_card.editor.toPlainText() == PROPOSED_TEXT
    assert card.proposal.state == APPLIED


def test_accepting_one_card_makes_the_others_for_that_file_stale(opened):
    w = opened
    view = w.text_only_tab
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    view._handle_text_proposal(_llm(TEXT + "\nAnother idea.\n"))
    _settle()
    first, second = _cards(w)
    first.accept_button.click()
    assert second.proposal.stale()
    assert not second.accept_button.isVisibleTo(second)  # redrawn by the chat
    assert second.state_line.text().startswith("⚠ The file changed since")


@pytest.mark.parametrize(
    "button, state, text",
    [
        ("accept_btn", APPLIED, PROPOSED_TEXT),
        ("reject_btn", REJECTED, TEXT),
        ("", PENDING, TEXT),  # the window closed: nothing decided
    ],
)
def test_view_diff_decides_as_the_cards_buttons(opened, monkeypatch, button, state, text):
    w = opened
    view = w.text_only_tab
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    shown = _review(monkeypatch, button)
    _cards(w)[0].diff_button.click()
    assert shown == [(TEXT, PROPOSED_TEXT)]
    assert _cards(w)[0].proposal.state == state
    assert view.text_card.editor.toPlainText() == text


def test_a_card_survives_a_view_switch_and_waits_in_the_view_that_asked(opened):
    w = opened
    view = w.text_only_tab
    w.text_json_tab._handle_text_proposal(_llm(PROPOSED_TEXT))  # asked by a view not shown
    _settle()
    assert _cards(w) == []
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    proposal = _cards(w)[0].proposal

    _show(w, w.json_code_tab)
    assert _cards(w) == []
    _show(w, view)

    (card,) = _cards(w)
    assert card.proposal is proposal and proposal.state == PENDING
    card.accept_button.click()
    assert view.text_card.editor.toPlainText() == PROPOSED_TEXT
    _show(w, w.text_json_tab)
    assert len(_cards(w)) == 1


def test_opening_another_test_discards_the_waiting_cards(opened):
    w = opened
    view = w.text_only_tab
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    w.json_code_tab._handle_code_proposal(_llm("def main():\n    return 2\n"))
    waiting = [
        m.proposal
        for v in w._get_llm_tabs()
        for m in v.tab_context.messages
        if isinstance(m, ProposalMessage)
    ]
    assert len(waiting) == 2

    w.open_test(w.root / "tests" / "t2")

    assert [p.state for p in waiting] == [DISCARDED, DISCARDED]
    (card,) = _cards(w)
    assert not card.actions.isVisibleTo(card)
    assert card.state_line.text() == "Discarded: its test was closed"
    assert view.text_card.editor.toPlainText() == "# t2\n\nBody of t2.\n"


def test_a_quick_parse_result_waits_as_a_card_unless_the_json_is_empty(opened, monkeypatch):
    from procedo.studio.pack_ops import pack_parsers

    w = opened
    view = w.text_json_tab
    _show(w, view)
    monkeypatch.setattr(
        pack_parsers,
        "parse_text",
        lambda text, project_root=None: ({"id": "t1", "steps": [{"n": 1}]}, []),
    )
    expected = json.dumps({"id": "t1", "steps": [{"n": 1}]}, indent=2)
    before = view.json_card.editor.toPlainText()

    view._on_quick_parse()
    _settle()
    assert view.json_card.editor.toPlainText() == before
    (card,) = _cards(w)
    card.accept_button.click()
    assert view.json_card.editor.toPlainText() == expected
    assert _last(view).content == "⚡ Quick Parse complete — no warnings."

    view.json_card.set_text("")
    view._on_quick_parse()  # nothing there to overwrite: no card
    _settle()
    assert view.json_card.editor.toPlainText() == expected
    assert len(_cards(w)) == 1


# --- a reply for another test, Parse + Generate's input, a failed Accept --------------------


class _Asked:
    """The LLM worker of a request: never runs; the test delivers its reply."""

    def __init__(self, *args, **kwargs):
        self.finished = self.error = self.thinking_chunk = self.text_chunk = SimpleNamespace(
            connect=lambda slot: None
        )

    def start(self):
        pass

    def isRunning(self):
        return False


def test_a_reply_that_arrives_after_a_test_switch_is_never_a_card_for_the_new_test(
    opened, monkeypatch
):
    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # asked on t1
    t2 = w.root / "tests" / "t2"
    w.open_test(t2)
    on_disk = (t2 / "procedure_text.md").read_bytes()

    view._apply_proposals(SimpleNamespace(procedure_text=_llm(PROPOSED_TEXT)))  # t1's reply
    _settle()

    assert _cards(w) == []
    message = _last(view)
    assert not isinstance(message, ProposalMessage)
    assert message.content.endswith(CLOSED)
    assert view.text_card.editor.toPlainText() == "# t2\n\nBody of t2.\n"
    assert (t2 / "procedure_text.md").read_bytes() == on_disk

    w.open_test(w.root / "tests" / "t1")
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # asked on the test still open
    view._apply_proposals(SimpleNamespace(procedure_text=_llm(PROPOSED_TEXT)))
    _settle()
    assert [c.proposal.state for c in _cards(w)] == [PENDING]


def test_a_late_reply_is_dropped_whole_before_anything_reads_it(opened, monkeypatch, mbox):
    """Nothing of a reply for a closed test reaches the test open now: no validator run, no
    findings, no session or chat change, no error dialog."""
    from dataclasses import asdict

    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # asked on t1
    t2 = w.root / "tests" / "t2"
    w.open_test(t2)
    touched = []
    monkeypatch.setattr(
        llm_validator_loop, "validate_response", lambda *a, **k: touched.append("v")
    )
    monkeypatch.setattr(view, "_parse_response_to_dict", lambda r: touched.append("parse"))
    monkeypatch.setattr(
        w.dock, "show_validation_result_from_list", lambda issues: touched.append("findings")
    )
    session, messages = asdict(w.session_state), list(view.tab_context.messages)
    files = {p.name: p.read_bytes() for p in t2.iterdir() if p.is_file()}
    reply = SimpleNamespace(
        raw_response="{}", success=True, total_tokens=1, procedure_text=_llm(PROPOSED_TEXT)
    )

    view._handle_llm_response(reply)

    assert touched == []
    assert view._ensure_run_state().state.name == "IDLE"
    assert w.dock.chat_panel._streaming is None  # the spinner went
    assert asdict(w.session_state) == session
    assert view.tab_context.messages == messages
    assert {p.name: p.read_bytes() for p in t2.iterdir() if p.is_file()} == files

    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # asked on t2 ...
    w.open_test(w.root / "tests" / "t1")
    messages = list(view.tab_context.messages)
    view._handle_llm_error("timeout")  # ... failing once t2 closed: dropped too
    assert mbox.critical == [] and view.tab_context.messages == messages
    assert view._ensure_run_state().state.name == "IDLE"


@pytest.fixture
def generated(opened, monkeypatch):
    """Parse + Generate run on t1 (whose procedure.json and test.py already hold content): its
    card waits in the Text view."""
    from procedo.studio.pack_ops import pack_parsers

    w = opened
    monkeypatch.setattr(
        pack_parsers,
        "parse_text",
        lambda text, project_root=None: ({"id": "t1", "steps": [{"n": 1}]}, []),
    )
    monkeypatch.setattr(
        pack_parsers,
        "generate_code",
        lambda proc, project_root=None: ("def main():\n    return 1\n", []),
    )
    w.t1 = w.root / "tests" / "t1"
    w.on_disk = {
        name: (w.t1 / name).read_bytes()
        for name in ("procedure_text.md", "procedure.json", "test.py")
    }
    w.text_only_tab._on_parse_and_generate()
    _settle(lambda: _cards(w))
    (w.card,) = _cards(w)
    return w


def _unchanged(w, *names):
    return all((w.t1 / name).read_bytes() == w.on_disk[name] for name in names)


def test_parse_and_generate_is_outdated_once_its_text_changed(generated):
    w = generated
    view, card = w.text_only_tab, w.card
    view.text_card.editor.setPlainText(TEXT + "A step added after Parse + Generate.\n")

    card.accept_button.click()

    assert card.proposal.state == PENDING
    assert not card.proposal.accept(reviewed=True)  # not even from the diff window
    assert not card.accept_button.isVisibleTo(card)
    assert not card.diff_button.isVisibleTo(card)
    assert card.reject_button.isVisibleTo(card)
    assert card.state_line.text() == "⚠ procedure_text.md changed since: run it again"
    assert _unchanged(w, "procedure.json", "test.py")


@pytest.fixture
def read_only(tmp_path):
    """Make files read-only (writable again at teardown)."""
    made = []

    def make(path):
        path.chmod(stat.S_IREAD)
        made.append(path)

    yield make
    for path in made:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _json_editors(w):
    """Every view's procedure.json and test.py cards."""
    return [
        card
        for view in w._get_llm_tabs()
        for card in view.cards
        if card.artifact_name in ("procedure_json", "test_code")
    ]


def _fails_after_the_json(w, read_only):
    """Accept the Parse + Generate card with test.py read-only: procedure.json is written,
    test.py is not — and every editor shows what is on disk."""
    from procedo.studio.core import ArtifactType

    card = w.card
    read_only(w.t1 / "test.py")
    card.accept_button.click()

    assert card.proposal.state == PENDING
    assert card.state_line.text().startswith("✗ procedure.json saved, test.py not: ")
    assert card.state_line.toolTip() == card.proposal.error
    assert card.accept_button.isVisibleTo(card)  # Accept again once the cause is fixed
    assert not any(
        "Parse + Generate complete" in m.content for m in w.text_only_tab.tab_context.messages
    )
    am = w.artifact_manager
    json_on_disk = (w.t1 / "procedure.json").read_text(encoding="utf-8")
    assert json.loads(json_on_disk) == {"id": "t1", "steps": [{"n": 1}]}
    assert am.get_content(ArtifactType.PROCEDURE_JSON) == json_on_disk
    assert _unchanged(w, "test.py")
    assert am.get_content(ArtifactType.TEST_CODE) == (w.t1 / "test.py").read_text(encoding="utf-8")
    for editor in _json_editors(w):
        assert editor.editor.toPlainText() == am.get_content(editor.kind), editor.file_name
        assert editor.pill.text() == "✓ Saved", editor.file_name
    return card


def test_an_accept_that_fails_after_the_json_says_so_and_applies_again(generated, read_only):
    w = generated
    card = _fails_after_the_json(w, read_only)
    (w.t1 / "test.py").chmod(stat.S_IREAD | stat.S_IWRITE)  # the cause, fixed

    card.accept_button.click()

    assert card.proposal.state == APPLIED
    assert "return 1" in (w.t1 / "test.py").read_text(encoding="utf-8")
    assert _last(w.text_only_tab).content.startswith("⚡ Parse + Generate complete")


def test_rejecting_after_a_partial_accept_says_the_json_stays_written(generated, read_only):
    w = generated
    card = _fails_after_the_json(w, read_only)
    card.reject_button.click()
    assert _last(w.text_only_tab).content == (
        "✗ Parse + Generate — changes rejected. procedure.json stays as written."
    )


def test_an_accept_whose_text_save_fails_writes_nothing(generated, read_only, mbox):
    w = generated
    view, card = w.text_only_tab, w.card
    read_only(w.t1 / "procedure_text.md")
    view.text_card.editor.setPlainText(TEXT + "Edited.\n")
    # the text changed after the card: Parse + Generate is run again on the edited text
    asked = len(_cards(w))
    view._on_parse_and_generate()
    _settle(lambda: len(_cards(w)) > asked)
    card = _cards(w)[-1]

    card.accept_button.click()

    assert card.proposal.state == PENDING
    assert card.state_line.text() == "✗ Not applied: procedure_text.md could not be saved"
    assert [title for title, _ in mbox.critical] == ["Save Failed"]  # the save said why
    assert _unchanged(w, "procedure_text.md", "procedure.json", "test.py")


def test_a_request_that_failed_early_leaves_no_test_behind(opened, monkeypatch):
    """A request on t1 that fails after its run started (its prompt cannot be built) must not
    make the next request — on t2 — inherit t1: t2's reply is t2's."""
    from procedo.studio.core.proposal import current_test

    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)

    def refuse(self, request, **kwargs):
        raise llm_tab_mixin.TaskPromptNotDeclaredError("no prompt declared")

    monkeypatch.setattr(llm_tab_mixin.PromptBuilder, "build", refuse)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # on t1: ends early
    monkeypatch.undo()
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)
    w.open_test(w.root / "tests" / "t2")

    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)

    assert view._ensure_run_state().test == current_test(w.artifact_manager)
    assert not view._drop_late_reply()


def test_a_reply_for_a_test_deleted_and_recreated_is_dropped(opened, monkeypatch):
    import shutil

    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(llm_tab_mixin, "LLMWorker", _Asked)
    view._run_task_async(LLMTask.REVIEW_TEXT_PROCEDURE)  # asked on t1
    t1 = w.root / "tests" / "t1"
    w.open_test(w.root / "tests" / "t2")
    shutil.rmtree(t1)
    shutil.copytree(w.root / "tests" / "t2", t1)
    (t1 / "procedure_text.md").write_text("# t1\n\nA new t1.\n", encoding="utf-8")
    w.open_test(t1)  # the same folder, another test

    assert view._drop_late_reply()
    view._apply_proposals(SimpleNamespace(procedure_text=_llm(PROPOSED_TEXT)))
    _settle()
    assert _cards(w) == []
    assert view.text_card.editor.toPlainText() == "# t1\n\nA new t1.\n"


def test_a_proposal_that_failed_validation_shows_its_findings_before_any_accept(
    opened, monkeypatch
):
    w = opened
    view = w.text_only_tab
    monkeypatch.setattr(
        text_actions,
        "reconstructed_or_error",
        lambda content, current, **kw: (None, content, "Step 1: no verb"),
    )
    view._handle_text_proposal(_llm(PROPOSED_TEXT))
    _settle()
    (card,) = _cards(w)

    assert not card.accept_button.isVisibleTo(card)
    assert card.diff_button.isVisibleTo(card)
    assert card.state_line.text() == "⚠ Failed validation: see View diff…"
    assert "Step 1: no verb" in card.state_line.toolTip()
    assert not card.proposal.accept()

    banners = []

    def fake_exec(dialog):
        banners.append(dialog._banner)
        dialog.accept_btn.click()
        return 0

    monkeypatch.setattr(DiffViewer, "exec", fake_exec)
    card.diff_button.click()
    assert "Step 1: no verb" in banners[0]
    assert view.text_card.editor.toPlainText() == PROPOSED_TEXT
