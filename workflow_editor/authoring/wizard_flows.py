"""Discover + launch wizard FLOWS from the wizard skill folders (data-driven).

A *wizard* the user launches is a FLOW: one or more wizard skills sharing a
``flow:`` id, exactly one of which (the HEAD) declares a ``launch:`` entry-point
(``<dotted module>:<Attr>``). The editor resolves + launches that entry from the
head skill's folder — replacing the old hardcoded ``_WIZARD_FLOWS`` list.

A head declares the authoring-API version its code needs (``authoring-api:``); a newer
one than this Studio's is refused here. What the entry receives is built by
:mod:`.wizard_api`.

Imports ONLY :func:`registry.load_wizards` + :class:`Skill`/:class:`SkillSource` (and
the host's ``API_VERSION``) — never ``skill_menu`` — so there is no import cycle
(``skill_menu`` imports this), and stays Qt-free.
"""
from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from project_services.authoring_api import API_VERSION

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

    Puts the head skill folder first on ``sys.path`` so its internal package resolves,
    then imports the dotted module and returns the attribute. Another head's copy of a
    same-named package imported earlier in this process (an old project's bundle, then
    a new one) is dropped first: the code that runs is always this head's.
    TRUST-GATED to builtin/bundled sources — this executes the package's Python.
    Raises :class:`WizardLaunchError` on a bad spec, untrusted source, a newer
    authoring API than this Studio's, or import failure (the caller surfaces it)."""
    mod_part, sep, attr = flow.launch_spec.partition(":")
    if not sep or not attr.strip():
        raise WizardLaunchError(
            f"launch must be '<module>:<Attr>', got {flow.launch_spec!r}")
    if flow.head.source not in (SkillSource.BUILTIN, SkillSource.BUNDLED):
        raise WizardLaunchError(
            "launchable wizard code is allowed only from builtin/bundled sources")
    needed = required_api(flow)
    if needed is not None and needed > API_VERSION:
        raise WizardLaunchError(
            f"this wizard needs authoring API {needed}; this Procedo Studio provides "
            f"{API_VERSION}. Update Procedo Studio.")
    head_dir = Path(flow.head.path)
    if str(head_dir) in sys.path:
        sys.path.remove(str(head_dir))
    sys.path.insert(0, str(head_dir))
    module_name = mod_part.strip()
    _forget_other_copy(module_name.split(".")[0], head_dir)
    try:
        module = importlib.import_module(module_name)
        entry = getattr(module, attr.strip())
    except Exception as exc:  # noqa: BLE001
        raise WizardLaunchError(
            f"could not load {flow.launch_spec!r}: {exc}. If it was just installed, "
            "rebuild and re-apply the bundle.") from exc
    if not _is_under(module, head_dir):
        raise WizardLaunchError(
            f"{module_name} resolved outside the wizard's folder: {module.__file__}")
    return entry


def _is_under(module, folder: Path) -> bool:
    file = getattr(module, "__file__", None)
    return bool(file) and Path(file).resolve().is_relative_to(folder.resolve())


def _forget_other_copy(package: str, head_dir: Path) -> None:
    """Drop ``package`` and its submodules from ``sys.modules`` when the cached copy is
    not this head's, so the next import loads the head's own copy."""
    cached = sys.modules.get(package)
    if cached is None or _is_under(cached, head_dir):
        return
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def required_api(flow: WizardFlow) -> Optional[int]:
    """The authoring-API version the flow's head declares (``authoring-api:``), or None
    when it declares none. Raises :class:`WizardLaunchError` on a value that is not a
    whole number."""
    value = (flow.head.metadata or {}).get("authoring-api")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise WizardLaunchError(f"authoring-api must be a whole number, got {value!r}")
    return value


__all__ = ["WizardFlow", "WizardLaunchError", "discover_flows", "required_api",
           "resolve_launch"]
