"""What Studio shows of a code test, or of a file of shared phases, from the engine's *describe*
of it (boards StudioCode, StudioPanel): the Phases tab's list — one entry per phase, read once,
folded into a summary or open on its details — its Equipment line, and the Check's results line.
Qt-free: the Phases tab (``widgets.phases_panel``) and the code view paint them."""

from __future__ import annotations

import re
from dataclasses import dataclass

from procedo.core import test_format
from procedo.core.code_test_check import CodeCheck

#: The file a phase in it is written in: a phase's ``file`` in describe.
TEST_FILE = test_format.CODE_SCRIPT


def is_shared(phase: dict) -> bool:
    """The phase is one of the project's reusable phases (``functions/``)."""
    return str(phase.get("file", "")).startswith("functions/")


def _unit(measurement: dict) -> str:
    """The measurement's unit as describe gives it: OpenHTF's suffix (``V``)."""
    return str(measurement.get("units") or "")


def _with_unit(value, unit: str) -> str:
    return f"{value} {unit}" if unit else str(value)


def limits(measurement: dict) -> str:
    """``4.75–5.25 V``, ``= 5 V``, ``≥ 4.75 V``, ``≤ 5.25 V``, else its validators as OpenHTF
    writes them ("" without any)."""
    low, high, unit = measurement.get("lower"), measurement.get("upper"), _unit(measurement)
    if low is not None and low == high:
        return f"= {_with_unit(low, unit)}"
    if low is not None and high is not None:
        return f"{low}–{_with_unit(high, unit)}"
    if low is not None:
        return f"≥ {_with_unit(low, unit)}"
    if high is not None:
        return f"≤ {_with_unit(high, unit)}"
    return "; ".join(measurement.get("validators") or ())


_PIN = re.compile(r"(.+) pin (\d+)")


def pin_ranges(captions) -> tuple[str, ...]:
    """The test points as shown, each once: a connector's pins as ranges at its first place —
    ``J2 pin 6`` … ``J2 pin 10``, ``J2 pin 1`` … ``J2 pin 5`` read ``J2 pins 1–10``; a lone one
    ``P15 pin 29``; anything else as it is."""
    pins: dict[str, set[int]] = {}
    order: list[str | tuple[str]] = []
    for caption in dict.fromkeys(captions):
        found = _PIN.fullmatch(caption)
        if found is None:
            order.append(caption)
            continue
        connector = found[1]
        if connector not in pins:
            pins[connector] = set()
            order.append((connector,))
        pins[connector].add(int(found[2]))
    out = []
    for item in order:
        if isinstance(item, str):
            out.append(item)
            continue
        numbers = sorted(pins[item[0]])
        runs = [[numbers[0]]]
        for n in numbers[1:]:
            if n == runs[-1][-1] + 1:
                runs[-1].append(n)
            else:
                runs.append([n])
        out += [
            f"{item[0]} pin {run[0]}" if len(run) == 1 else f"{item[0]} pins {run[0]}–{run[-1]}"
            for run in runs
        ]
    return tuple(out)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass(frozen=True)
class PhaseEntry:
    """One phase of the Phases tab, as describe read it: ``line`` in its ``file``, ``here`` when
    that is the file the editor holds (a click jumps to the line; else it opens the file);
    ``used_by``: the tests that list it (None: not asked — a test's own phases)."""

    number: int
    name: str
    function: str
    file: str
    line: int
    here: bool
    shared: bool
    teardown: bool
    description: str
    questions: tuple[str, ...]
    substeps: tuple[tuple[str, int], ...]
    measurements: tuple[str, ...]
    test_points: tuple[str, ...]
    used_by: tuple[str, ...] | None = None

    @property
    def key(self) -> tuple[str, str]:
        """What stays the same while the code is edited: its file and its function."""
        return self.file, self.function

    @property
    def mark(self) -> str:
        """Right of its name: ``L94`` here, else ``shared · teardown`` (``shared``)."""
        if self.here:
            return f"L{self.line}"
        return " · ".join(
            m for m, on in (("shared", self.shared), ("teardown", self.teardown)) if on
        )

    def _where(self) -> str:
        return f"{self.file.removeprefix('functions/')}, line {self.line} (opens the file)"

    def _points(self) -> str:
        one = len(self.test_points) == 1 and " pins " not in self.test_points[0]
        label = "Test point" if one else "Test points"
        return f"{label}: {', '.join(self.test_points)}"

    def _use(self) -> str:
        return f"used by {', '.join(self.used_by) or 'no test'}"

    def summary(self) -> list[str]:
        """Folded: ``6 sub-steps · 1 question · J2 pins 1–10``, then its measurements
        ``V_4A_load 15.2–16.8 V · I_4A_load 3.88–4.12 A``; a phase of another file, where it is."""
        if not self.here:
            return [self._where()]
        counts = [
            _plural(len(self.substeps), "sub-step") if self.substeps else "",
            _plural(len(self.questions), "question") if self.questions else "",
            ", ".join(self.test_points),
            self._use() if self.used_by is not None else "",
        ]
        lines = [" · ".join(part for part in counts if part), " · ".join(self.measurements)]
        return [line for line in lines if line]

    def details(self) -> list[tuple[str, str, int]]:
        """Open: ``(kind, text, line)`` — its description, each question ``? “…”``, each sub-step
        ``4.1 …`` (its line), each measurement with its limits, its test points; in a file of
        shared phases, which tests use it."""
        out = [("description", self.description, self.line)] if self.description else []
        out += [("question", f"? “{q}”", self.line) for q in self.questions]
        out += [
            ("substep", f"{self.number}.{k} {text}", line)
            for k, (text, line) in enumerate(self.substeps, 1)
        ]
        out += [("measurement", m, self.line) for m in self.measurements]
        if self.test_points:
            out.append(("points", self._points(), self.line))
        if self.used_by is not None:
            use = self._use()
            out.append(("use", use if self.substeps else f"no sub-steps yet · {use}", self.line))
        if not self.here:
            out.append(("where", self._where(), self.line))
        return out


def phase_entries(
    described: dict, open_file: str = TEST_FILE, used_by: dict | None = None
) -> list[PhaseEntry]:
    """The phases of *described* as the Phases tab lists them; *open_file*: the file the editor
    holds; *used_by*: ``{(file, function): [test, …]}`` for a file of shared phases."""
    entries = []
    for n, phase in enumerate(described.get("phases") or (), 1):
        here = phase.get("file") == open_file
        key = (phase.get("file", ""), phase.get("function", ""))
        entry = PhaseEntry(
            number=n,
            name=phase["name"],
            function=phase.get("function", ""),
            file=phase.get("file", ""),
            line=phase["line"],
            here=here,
            shared=is_shared(phase) and not here,
            teardown=bool(phase.get("teardown")),
            description=phase.get("description") or "",
            questions=tuple(q["text"] for q in phase.get("questions") or ()),
            substeps=tuple((s["text"], s["line"]) for s in phase.get("substeps") or ()),
            measurements=tuple(
                " ".join(t for t in (m["name"], limits(m)) if t)
                for m in phase.get("measurements") or ()
            ),
            test_points=pin_ranges(m["caption"] for m in phase.get("media") or ()),
            used_by=tuple(used_by.get(key, ())) if used_by is not None else None,
        )
        entries.append(entry)
    return entries


def equipment(described: dict) -> str:
    """``Equipment: Dmm (DMM1), Psu (PSU1)``: each instrument and the address constant named
    after it ("" without any)."""
    names = [c["name"].split("_")[0] for c in described.get("constants") or ()]
    out = []
    for kind in described.get("instruments") or ():
        mine = [name for name in names if name.upper().startswith(kind.upper())]
        out.append(f"{kind} ({', '.join(mine)})" if mine else kind)
    return f"Equipment: {', '.join(out)}" if out else ""


def check_lines(found: CodeCheck, loads: bool = True) -> list[tuple[str, str, str]]:
    """The Check's results line, ``(tone, text, tooltip)`` each: ``✓ Syntax OK``, ``✓ Loads in
    the bundle's environment`` (*loads*: the Check loaded it — a file of shared phases is loaded
    by its tests), ``✓ 2 steps · 1 measurement · 2 instruments``; ``✗ why`` in place of what
    failed (a problem: it cannot run); then ``⚠ warning`` for each warning (what to look at: a
    measurement without units, code a file runs when it is loaded…)."""
    if found.describe is None:  # a syntax error, or no engine: nothing else was read
        return [("danger", f"✗ {problem}", "") for problem in found.problems]
    lines = [("success", "✓ Syntax OK", "")]
    lines += [("danger", f"✗ {problem}", "") for problem in found.problems]
    if loads and not found.problems:
        lines.append(("success", "✓ Loads in the bundle's environment", ""))
    lines.append(("success", f"✓ {found.summary}", ""))
    return lines + [("warning", f"⚠ {warning}", "") for warning in found.warnings]
