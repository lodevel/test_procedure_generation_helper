"""Discover + launch wizard FLOWS from the wizard skill folders (data-driven).

A *wizard* the user launches is a FLOW: one or more wizard skills sharing a
``flow:`` id, exactly one of which (the HEAD) declares a ``launch:`` entry-point
(``<dotted module>:<Attr>``). The editor resolves + launches that entry from the
head skill's folder — replacing the old hardcoded ``_WIZARD_FLOWS`` list.

Imports ONLY :func:`registry.load_wizards` + :class:`Skill`/:class:`SkillSource`
— never ``skill_menu`` — so there is no import cycle (``skill_menu`` imports this).
"""
from __future__ import annotations

import importlib
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from . import load_wizards
from .skill import Skill, SkillSource

log = logging.getLogger(__name__)


class WizardLaunchError(Exception):
    """A wizard flow's ``launch:`` entry could not be resolved or loaded."""


@dataclass(frozen=True)
class WizardFlow:
    """One launchable wizard: a flow id + its head skill's launch entry."""

    flow_id: str
    label: str
    tooltip: str
    launch_spec: str          # "<dotted module>:<Attr>"
    head: Skill               # the skill folder that owns the code + launch:
    skills: List[Skill]       # all skills in this flow (head + members)


def discover_flows(project_root: Optional[Path] = None) -> List[WizardFlow]:
    """All LAUNCHABLE wizard flows, sorted by label.

    Groups discovered wizards by ``flow:``, takes the one HEAD declaring
    ``launch:``, and gates on ``requires`` (default: every skill in the flow) being
    present. A flow whose UI is not built (no ``launch:`` head) is skipped silently;
    a flow missing a required stage is gated off. Never raises — a broken flow just
    doesn't appear."""
    try:
        wizards = load_wizards(project_root=project_root)
    except Exception:  # noqa: BLE001 — discovery must never break the menu
        log.exception("wizard discovery failed")
        return []

    by_flow: dict[str, List[Skill]] = {}
    for w in wizards:
        flow_id = (w.metadata or {}).get("flow")
        if flow_id:
            by_flow.setdefault(str(flow_id), []).append(w)

    out: List[WizardFlow] = []
    for flow_id, skills in by_flow.items():
        heads = [s for s in skills if (s.metadata or {}).get("launch")]
        if not heads:
            continue  # prompts shipped, launchable UI not built — skip silently
        if len(heads) > 1:  # deterministic tiebreak: highest source, then skill_id
            heads.sort(key=lambda s: (int(s.source), s.skill_id), reverse=True)
            log.warning("wizard flow %r has %d launch heads; using %s",
                        flow_id, len(heads), heads[0].skill_id)
        head = heads[0]
        ids = {s.skill_id for s in skills}
        requires = (head.metadata or {}).get("requires") or list(ids)
        if not set(requires) <= ids:
            continue  # a required stage is missing → not launchable
        meta = head.metadata or {}
        out.append(WizardFlow(
            flow_id=flow_id,
            label=str(meta.get("wizard-label") or head.title),
            tooltip=str(meta.get("wizard-tooltip") or head.when_to_use or ""),
            launch_spec=str(meta["launch"]),
            head=head,
            skills=skills,
        ))
    return sorted(out, key=lambda f: f.label.lower())


def resolve_launch(flow: WizardFlow) -> Any:
    """Import + return a flow's launch entry (a class or callable).

    Inserts the head skill folder on ``sys.path`` (idempotent) so its internal
    package resolves, then imports the dotted module and returns the attribute.
    TRUST-GATED to builtin/bundled sources — this executes the package's Python.
    Raises :class:`WizardLaunchError` on a bad spec, untrusted source, or import
    failure (the caller surfaces it)."""
    mod_part, sep, attr = flow.launch_spec.partition(":")
    if not sep or not attr.strip():
        raise WizardLaunchError(
            f"launch must be '<module>:<Attr>', got {flow.launch_spec!r}")
    if flow.head.source not in (SkillSource.BUILTIN, SkillSource.BUNDLED):
        raise WizardLaunchError(
            "launchable wizard code is allowed only from builtin/bundled sources")
    head_dir = str(Path(flow.head.path))
    if head_dir not in sys.path:
        sys.path.insert(0, head_dir)
    try:
        module = importlib.import_module(mod_part.strip())
        entry = getattr(module, attr.strip())
    except Exception as exc:  # noqa: BLE001
        raise WizardLaunchError(f"could not load {flow.launch_spec!r}: {exc}") from exc
    # Guard against a same-named internal package shadowing another wizard's: the
    # resolved module must live under the head folder (skill-relative) OR be an
    # editor module (the in-tree form during the transition).
    mf = getattr(module, "__file__", "") or ""
    if not (mf.startswith(head_dir) or mod_part.strip().startswith("workflow_editor.")):
        log.warning("wizard %r resolved %s outside its package (%s)",
                    flow.flow_id, mod_part, mf)
    return entry


__all__ = ["WizardFlow", "WizardLaunchError", "discover_flows", "resolve_launch"]
