"""A change waiting for the user's approval: the proposal card in the chat (owner, 2026-10-15:
"AI changes are approved as proposal cards in the chat").

An AI reply or a ⚡ action that would rewrite an artifact makes a :class:`Proposal`: what it
would write (one :class:`FileChange` per file) and what applying it does (the same steps the
review window's Accept ran). Nothing is applied until the user accepts it.

The safety rules:

* a proposal is for the test that was open when it was asked (``test``: a :func:`current_test`):
  it is never offered to, nor applied in, another one — nor the same folder opened again;
* it is applied straight from its card only while every file still reads as it did when the
  proposal was made (or already reads as proposed: a second Accept after a partial failure).
  Once one changed (the user typed, another proposal was accepted, a save reloaded it) the
  proposal is *stale*: only the diff window, which compares it with the CURRENT text, can apply
  it — as it alone can apply one that failed validation (its ``banner``). Once a file it was made
  FROM changed (an *input*: Parse + Generate's procedure_text.md) it is *outdated*: nothing
  applies it, it has to be made again;
* applying it either completes or raises: a failure leaves it waiting, its ``error`` saying what
  happened (:class:`PartlyApplied`: which files were written all the same);
* once decided or discarded it keeps only what its card shows: its texts and actions are let go.

Qt-free; the diff here is the one the diff window shows (:func:`diff_lines`).
"""
from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PENDING, APPLIED, REJECTED, DISCARDED = "pending", "applied", "rejected", "discarded"
#: How many changed lines a card previews.
PREVIEW_LINES = 3


def current_test(artifact_manager) -> Optional[tuple[object, Path]]:
    """The test open in *artifact_manager* (the main window's): that manager — one per opening,
    so a folder deleted, recreated and opened again is another test — and its folder; None when
    no test is open."""
    test_dir = getattr(artifact_manager, "test_dir", None)
    return None if test_dir is None else (artifact_manager, test_dir)


class PartlyApplied(Exception):
    """Applying a proposal wrote some of its files, not all: the message says which."""


def diff_lines(original: str, proposed: str) -> list[str]:
    """The unified diff, line by line: a final newline present on one side only is no change."""
    return list(difflib.unified_diff(original.splitlines(), proposed.splitlines(),
                                     fromfile="Original", tofile="Proposed", lineterm=""))


#: A hunk's header: where it starts in each text and how many lines it spans there (1 if omitted).
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: What a :func:`diff_rows` row is: a header (a file's name, a hunk's ``@@``), a line removed,
#: added, or kept.
HEADER, REMOVED, ADDED, KEPT = "header", "-", "+", " "


def diff_rows(lines: Iterable[str]) -> list[tuple[str, int, str]]:
    """Each line of a :func:`diff_lines` diff (or several, one after the other) read as (kind,
    number, text): a removed line's number in the original, an added or kept one's in the
    proposed text (1-based), its text without the mark; a header is (HEADER, 0, the line). The
    hunk headers' counts say which lines are a hunk's, so no line passes for a header ("----"
    is a removed "---")."""
    rows = []
    old = new = old_left = new_left = 0
    for line in lines:
        if not (old_left or new_left):  # between hunks: the file names, the next hunk's @@
            if hunk := _HUNK.match(line):
                old, old_left = int(hunk[1]), int(hunk[2] or 1)
                new, new_left = int(hunk[3]), int(hunk[4] or 1)
            rows.append((HEADER, 0, line))
            continue
        kind = line[:1] if line[:1] in (REMOVED, ADDED) else KEPT
        rows.append((kind, old if kind == REMOVED else new, line[1:]))
        if kind != ADDED:
            old, old_left = old + 1, old_left - 1
        if kind != REMOVED:
            new, new_left = new + 1, new_left - 1
    return rows


def changed_lines(lines: Iterable[str]) -> tuple[list[int], list[int]]:
    """The numbers (1-based) of the lines a :func:`diff_lines` diff removes from the original,
    and of those it adds to the proposed text."""
    rows = diff_rows(lines)
    return ([number for kind, number, _ in rows if kind == REMOVED],
            [number for kind, number, _ in rows if kind == ADDED])


def count_changes(lines: Iterable[str]) -> tuple[int, int]:
    """(added, removed) lines of a :func:`diff_lines` diff."""
    removed, added = changed_lines(lines)
    return len(added), len(removed)


class FileText:
    """A file a proposal depends on: *read* gives its text now; ``base`` is the text read when
    the proposal was made."""

    def __init__(self, file_name: str, read: Callable[[], str]) -> None:
        self.file_name = file_name
        self.read = read
        self.base = read()

    def changed_since(self) -> bool:
        return self.read() != self.base


class FileChange(FileText):
    """One file a proposal rewrites: *proposed* is what it would become (what Accept replaces
    is its text now)."""

    def __init__(self, file_name: str, read: Callable[[], str], proposed: str) -> None:
        super().__init__(file_name, read)
        self.proposed = proposed
        self.lines = diff_lines(self.base, proposed)

    def written(self) -> bool:
        """The file already reads as proposed (line by line)."""
        return self.read().splitlines() == self.proposed.splitlines()

    def changed_since(self) -> bool:
        """Changed since the proposal was made, other than to what it proposes."""
        return super().changed_since() and not self.written()


class Proposal:
    """A change waiting in the chat: its files (those whose lines differ), *apply* (what Accept
    does; it raises when it cannot complete), *rejected* (what Reject does), an optional warning
    *banner* (a proposal that failed validation, shown for the user to fix or reject), the
    *inputs* it was made from (:class:`FileText`) and the *test* it was made for (a
    :func:`current_test`). What its card shows — ``file_names``, :meth:`counts`,
    :meth:`preview` — is kept once it is decided; the rest is let go then."""

    def __init__(self, changes: Iterable[FileChange], apply: Callable[[], None],
                 rejected: Callable[[], None], banner: str = "", *,
                 inputs: Iterable[FileText] = (), test: Optional[tuple] = None) -> None:
        self.changes = [change for change in changes if change.lines]
        self._apply = apply
        self._rejected = rejected
        self.banner = banner
        self.inputs = list(inputs)
        self.test = test
        self.state = PENDING
        #: What went wrong in the last Accept ("" when nothing did).
        self.error = ""
        lines = [line for change in self.changes for line in change.lines]
        self.file_names = [change.file_name for change in self.changes]
        self._counts = count_changes(lines)
        rows = diff_rows(lines)
        self._preview = ([text for kind, _, text in rows if kind == ADDED] or
                         [f"− {text}" for kind, _, text in rows if kind == REMOVED])[:PREVIEW_LINES]

    def counts(self) -> tuple[int, int]:
        """(added, removed) lines over all its files."""
        return self._counts

    def preview(self) -> list[str]:
        """The first changed lines: the added ones as they will read, or — when it only
        removes — the removed ones marked −."""
        return self._preview

    def summary(self) -> str:
        """"Proposal · procedure_text.md +3 −2" (the chat history's line for it)."""
        added, removed = self._counts
        return f"Proposal · {' + '.join(self.file_names)} +{added} −{removed}"

    def written(self) -> list[str]:
        """The files that already read as proposed (an Accept that failed part-way)."""
        return [change.file_name for change in self.changes if change.written()]

    def needs_review(self) -> bool:
        """Only the diff window may apply it: stale, or failed validation."""
        return self.stale() or bool(self.banner)

    def outdated(self) -> list[str]:
        """Still waiting, but made from files that changed since: their names."""
        if self.state != PENDING:
            return []
        return [source.file_name for source in self.inputs if source.changed_since()]

    def stale(self) -> bool:
        """Still waiting, but a file it rewrites — or one it was made from — changed since."""
        return self.state == PENDING and (
            any(change.changed_since() for change in self.changes) or bool(self.outdated()))

    def accept(self, reviewed: bool = False) -> bool:
        """Apply it, unless it was decided already, is outdated, or — not *reviewed* in the diff
        window against the current text — needs review. True when applied; when applying raises
        it stays waiting, ``error`` saying what happened."""
        if self.state != PENDING or self.outdated() or (self.needs_review() and not reviewed):
            return False
        try:
            self._apply()
        except Exception as exc:  # noqa: BLE001 — said on its card; it stays waiting
            log.exception("Applying %s failed", self.summary())
            reason = str(exc) or type(exc).__name__
            self.error = reason if isinstance(exc, PartlyApplied) else f"Not applied: {reason}"
            return False
        self._decide(APPLIED)
        return True

    def reject(self) -> None:
        if self.state == PENDING:
            self._rejected()  # may still read the files (what stays written)
            self._decide(REJECTED)

    def discard(self) -> None:
        """Its test was closed: it can no longer be applied."""
        if self.state == PENDING:
            self._decide(DISCARDED)

    def _decide(self, state: str) -> None:
        """Record the decision and let go of what only a waiting proposal needs."""
        self.state, self.error = state, ""
        self.changes, self.inputs, self.test = [], [], None
        self._apply = self._rejected = None  # type: ignore[assignment]  # only called while PENDING


def discard_pending(messages: Iterable) -> int:
    """Discard the pending proposals among a chat's *messages*; how many there were."""
    pending = [m.proposal for m in messages
               if getattr(m, "proposal", None) is not None and m.proposal.state == PENDING]
    for proposal in pending:
        proposal.discard()
    return len(pending)
