"""Editor-facing views of engine replies: the parse failure, the findings and
reports of validate / reconstruct, and the guided-manual run plan of
``build_manual_run`` — each built from the op's JSON dict."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, NoReturn, Optional


class ParseFailure(Exception):
    """Raised by :func:`parse_text` when the wheel's parser produced
    error-severity findings. Duck-typed against the legacy ParseError
    (carries ``.code``, ``.line``, ``.column``, ``.fix_hint``,
    ``.findings``)."""


# ---------------------------------------------------------------------------
# Reconstruction helpers: JSON dicts → editor-facing types
# ---------------------------------------------------------------------------


def _reconstruct_parse_failure(result: dict[str, Any]) -> "NoReturn":
    """Raise ParseFailure reconstructed from the engine's parse_text error dict."""
    findings_dicts = result.get("findings", [])
    issues = [_IssueFromDict(d) for d in findings_dicts]
    exc = ParseFailure(result.get("error", "Parsing failed."))
    exc.code = result.get("code", "PARSE_ERROR")
    exc.line = result.get("line")
    exc.column = result.get("column")
    exc.fix_hint = result.get("fix_hint", "")
    exc.findings = issues
    raise exc


def _reconstruct_issues_from_dicts(findings: list[dict]) -> list["_IssueFromDict"]:
    return [_IssueFromDict(d) for d in findings]


class _IssueFromDict:
    """Editor-facing finding reconstructed from the engine's finding dict
    (``_engine_ops._finding_to_dict``); ``validation_outcome.issue_from_validator``
    consumes it."""

    def __init__(self, d: dict) -> None:
        self.code = d.get("code", "") or ""
        self.message = d.get("message", "") or ""
        self.severity = d.get("severity", "error") or "error"
        line = d.get("line", 0) or 0
        col = d.get("col", 0) or 0
        loc: dict[str, Any] = {}
        if line:
            loc["line"] = line
        if col:
            loc["column"] = col
        self.location = loc
        self.fix_hint = d.get("fix_hint", "") or ""
        self.fixable_by = d.get("fixable_by", "either") or "either"


class _ValidateReport:
    """Duck-typed report shape consumed by ``validator_dispatch``."""

    def __init__(self, findings: list["_IssueFromDict"]) -> None:
        self._issues = list(findings)
        self.ok = not any(i.severity == "error" for i in self._issues)
        self.errors = [i for i in self._issues if i.severity == "error"]
        self.warnings = [i for i in self._issues if i.severity == "warning"]


class _ReconstructReport:
    """Duck-typed report for :func:`reconstruct_text`.

    Mirrors ``_ValidateReport``'s finding normalization while also carrying
    the reconstructed ``.text``/``.json`` payload. Build via :meth:`from_dict`
    rather than ``__init__`` directly.
    """

    def __init__(
        self,
        success: bool,
        text: Optional[str],
        json: Optional[dict],
        findings: list[Any],
    ) -> None:
        self.success = success
        self.text = text
        self.json = json
        self.findings = findings
        self.ok = success
        self.errors = [i for i in self.findings if i.severity == "error"]
        self.warnings = [i for i in self.findings if i.severity == "warning"]

    @classmethod
    def from_dict(cls, d: dict) -> "_ReconstructReport":
        """Wrap the engine's reconstruct result dict."""
        return cls(
            success=d["success"],
            text=d.get("text"),
            json=d.get("json"),
            findings=_reconstruct_issues_from_dicts(d.get("findings", [])),
        )


# ---------------------------------------------------------------------------
# Guided-manual run plan: editor-facing views over the wheel's ManualRun /
# StepDescriptor as the build_manual_run engine op returns them (JSON).
#
# These two are PUBLIC (unlike the private _Report/_IssueFromDict siblings above): they are
# the GUI-facing RETURN type of build_manual_run, whose field names the modal binds
# directly. Collections are tuples so the frozen views are genuinely immutable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManualStep:
    """One step in a guided-manual run — a mirror of the wheel's ``StepDescriptor``
    (a CI test asserts field-name parity) plus what the build_manual_run op adds
    (a Wait's delay, what the Values panel shows). Built only via :meth:`from_dict`,
    which ignores unknown keys."""

    node_path: str = ""
    n: int = 0
    op: str = ""
    text: str = ""
    is_binding: bool = False
    kind: str = ""
    ref: Optional[int | str] = None  # {3} -> 3, {Vout} -> "Vout", {Vrail[3]} -> "Vrail[3]"
    quantity: str = ""
    target: str = ""
    expected_text: str = ""
    verdict: str = ""               # PASS | FAIL | SKIP | "" (pending)
    media: tuple[dict, ...] = ()    # raw step["media"] entries, pass-through
    status: str = "pending"         # pending | answered | skipped
    control_deciding: bool = False  # blocks forward skip/jump until answered
    iteration: int = 0
    index: int = 0
    # The dict fields (resolved_op: the op with this turn's substitutions, StepDescriptor's) stay out of eq/hash.
    resolved_op: Optional[dict] = field(default=None, compare=False)
    wait_s: Optional[float] = None  # a Wait's delay, read by the engine (the op adds it)
    wait_refused: str = ""          # or why the engine refused its value (test.py's message)
    value_text: str = ""            # its {N}'s value as shown, with its unit ("" = none yet)
    value_note: str = ""            # why a Bind has none (the engine's words)
    outputs: tuple[dict, ...] = field(default=(), compare=False)  # a Call's: {ref, unit, value_text, note, verdict, check}
    call: Optional[dict] = field(default=None, compare=False)  # a Call to make: {function, args, kwargs, timeout, args_hash}
    names: tuple[dict, ...] = field(default=(), compare=False)  # its loop turns' ${…}: {name, value, origin}

    @classmethod
    def from_dict(cls, d: dict) -> "ManualStep":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        for key in ("media", "names", "outputs"):  # JSON/asdict give a list; freeze it
            if isinstance(data.get(key), list):
                data[key] = tuple(data[key])
        return cls(**data)


@dataclass(frozen=True)
class ManualRunResult:
    """Editor-facing guided-manual run plan: the ``build_manual_run`` engine op's result.
    ``steps`` is the on-path plan the operator walks; ``skipped`` holds off-path bindings
    (retained as ``SKIP``); ``aborted`` is set when an ``@ABORT`` truncated the plan;
    ``channels``: each instrument's declared power channels (its safe-off's);
    ``constants``: the procedure's own names (@LET, @ALLOC, @TABLE), read-only in a run."""

    test_name: str = ""
    steps: tuple[ManualStep, ...] = ()       # on-path plan
    skipped: tuple[ManualStep, ...] = ()     # off-path bindings (retained as SKIP)
    aborted: bool = False
    error: str = ""                          # a step's value refused: the run ended there (its message)
    channels: dict = field(default_factory=dict)  # device -> its declared channels, the engine's
    constants: tuple[dict, ...] = ()         # {name, value, origin}, the engine's

    @classmethod
    def from_dict(cls, d: dict) -> "ManualRunResult":
        """Wrap a subprocess JSON result dict."""
        return cls(
            test_name=d.get("test_name", ""),
            steps=tuple(ManualStep.from_dict(s) for s in d.get("steps", [])),
            skipped=tuple(ManualStep.from_dict(s) for s in d.get("skipped", [])),
            aborted=bool(d.get("aborted", False)),
            error=str(d.get("error") or ""),
            channels=dict(d.get("channels") or {}),
            constants=tuple(d.get("constants") or ()),
        )
