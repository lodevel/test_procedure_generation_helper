"""run_skill core — pure logic for skill-invokes-skill (no Qt, no host imports).

Dependency-light (stdlib + ``requests`` + ``yaml``) so the dedicated MCP server
(:mod:`_run_skill_mcp`) imports this as a bare sibling WITHOUT dragging in the
heavy ``workflow_editor.authoring`` Qt ``__init__`` chain (same discipline as
``_project_tools_mcp``):

  * **chain token** — HMAC-signed ``{depth, visited}``; the model only ever sees
    ITS OWN token (its true depth) and cannot forge a shallower one. OpenCode
    passes NO caller/session id to a local MCP tool (verified), so this token IS
    the caller identity.
  * **guard** — a depth cap AND a visited-set cycle check, both host-enforced.
  * **run_child_skill** — resolve ``skill_id`` across the skill roots (precedence),
    then spawn a child OpenCode session scoped to the CHILD skill's OWN declared
    tools + read-only project/datasheet data (never the filesystem/shell built-ins),
    and return its prose. The server URL is read from the launch pid-file at call
    time (the port is OS-assigned after the config is written).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import requests
import yaml

# Default recursion ceiling: depth 0 (top chat) -> 1 -> 2 -> 3; the 4th is refused.
DEFAULT_MAX_DEPTH = 3

_SKILL_FILE_NAMES = ("SKILL.md", "skill.md")

# Built-in tools the editor NEVER exposes to the LLM (parent OR child): filesystem,
# shell, sub-agent, todo. Mirrors opencode_backend._build_message_body's OFF block.
_BUILTINS_OFF = {
    "bash": False, "edit": False, "write": False, "patch": False,
    "apply_patch": False, "read": False, "glob": False, "grep": False,
    "list": False, "task": False, "todowrite": False, "todoread": False,
}
# Read-only, sandboxed, no-network infra a child MAY use (datasheet + project data).
_CHILD_READONLY_ON = {
    "pdf_tools_list_documents": True, "pdf_tools_read_document": True,
    "pdf_tools_list_rules": True, "pdf_tools_read_rule": True,
    "project_tools_list_property_fields": True, "project_tools_list_components": True,
    "project_tools_get_component": True, "project_tools_query_net": True,
    "project_tools_netlist": True, "project_tools_get_bom": True,
    "project_tools_list_test_points": True,
}
# Network / write infra a child never gets (no human in the loop to consent).
_CHILD_NETWORK_OFF = {
    "webfetch": False, "websearch": False,
    "pdf_tools_read_pdf": False, "pdf_tools_save_pdf": False,
}


# --------------------------------------------------------------------------- #
# chain token                                                                 #
# --------------------------------------------------------------------------- #
def sign(payload: dict, secret: bytes) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    mac = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify(token: str, secret: bytes):
    """Return the payload dict, or None if absent/forged/corrupt. A blank secret
    always yields None (fail-closed)."""
    if not token or not secret:
        return None
    try:
        body, mac = token.rsplit(".", 1)
        expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        return json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except Exception:  # noqa: BLE001
        return None


def guard(parent_payload: dict, child_skill_id: str, max_depth: int):
    """Return ``('ok', child_payload)`` or ``('refused', reason)`` — a depth cap
    AND a visited-set cycle check."""
    depth = int(parent_payload.get("depth", 0))
    visited = list(parent_payload.get("visited", []))
    if depth + 1 > max_depth:
        return "refused", f"max recursion depth {max_depth} reached (chain={visited})"
    if child_skill_id in visited:
        return "refused", f"cycle: '{child_skill_id}' already in chain {visited}"
    return "ok", {"depth": depth + 1, "visited": visited + [child_skill_id]}


# --------------------------------------------------------------------------- #
# skill resolution (precedence across roots; body + declared mcp_tools)        #
# --------------------------------------------------------------------------- #
def _find_skill_file(folder: Path):
    for name in _SKILL_FILE_NAMES:
        p = folder / name
        if p.is_file():
            return p
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file() and child.name.lower() == "skill.md":
                return child
    return None


def _split_frontmatter(text: str):
    """Return ``(meta_dict, body)`` for a ``---``-fenced YAML frontmatter block."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                try:
                    meta = yaml.safe_load("\n".join(lines[1:i])) or {}
                except Exception:  # noqa: BLE001
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                return meta, "\n".join(lines[i + 1:]).lstrip("\n").strip()
    return {}, text.strip()


def resolve_skill(skill_id: str, roots):
    """Return ``(body, declared_mcp_tools)`` for ``skill_id``.

    ``roots`` is a list of ``(path, precedence_int)`` pairs; the HIGHEST-precedence
    root that holds ``<root>/<skill_id>/SKILL.md`` wins (matching the registry's
    "higher SkillSource wins"). Legacy: a plain list of paths is treated as ASCENDING
    precedence. ``body`` is frontmatter-stripped (the child's system prompt);
    ``declared_mcp_tools`` is the skill's ``mcp_tools`` list. Raises ValueError if
    no root has it.
    """
    best = None  # (precedence, skill_file)
    for idx, item in enumerate(roots):
        root, prec = item if isinstance(item, (tuple, list)) else (item, idx)
        sf = _find_skill_file(Path(root) / skill_id)
        if sf is not None and (best is None or prec >= best[0]):
            best = (prec, sf)
    if best is None:
        raise ValueError(f"skill '{skill_id}' not found in any skill root")
    meta, body = _split_frontmatter(best[1].read_text(encoding="utf-8-sig"))
    if not body:
        raise ValueError(f"skill '{skill_id}' has an empty SKILL.md body")
    declared = [str(s) for s in (meta.get("mcp_tools") or [])]
    return body, declared


# --------------------------------------------------------------------------- #
# server URL + universe (read at call time)                                   #
# --------------------------------------------------------------------------- #
def read_server_url(port_file) -> str:
    """``{"port": N}`` in the launch pid-file -> ``http://127.0.0.1:N`` (read on
    each call; robust to the OS-assigned-port retry loop)."""
    data = json.loads(Path(port_file).read_text(encoding="utf-8"))
    return f"http://127.0.0.1:{int(data['port'])}"


def load_universe(universe_file) -> dict:
    """Load ``{server: [tool names]}`` (the skill-tools gate universe) from a file,
    or ``{}`` if absent/unreadable (then no skill-owned tools are force-scoped)."""
    if not universe_file:
        return {}
    try:
        data = json.loads(Path(universe_file).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# child session                                                               #
# --------------------------------------------------------------------------- #
def child_tools(declared, universe, run_skill_enabled: bool) -> dict:
    """The child session's tool overrides (OpenCode's override is ADDITIVE, so we
    emit an explicit bool for everything the child could otherwise inherit):

      * built-ins (fs/shell/sub-agent/todo) -> OFF (never given to any session);
      * read-only project + datasheet infra -> ON (a delegated skill can read the
        board / its datasheets);
      * network + write infra (web, url-fetch, save) -> OFF (no human consents);
      * every skill-owned tool in ``universe`` -> ON iff its server is one the CHILD
        skill declares in ``mcp_tools`` (mirrors the parent's ``skill_tool_overrides``),
        so the child gets ITS OWN tools and no other skill's;
      * ``run_skill`` -> depth-gated (the hard recursion backstop).
    """
    tools = dict(_BUILTINS_OFF)
    tools.update(_CHILD_READONLY_ON)
    tools.update(_CHILD_NETWORK_OFF)
    active = set(declared or ())
    for server, names in (universe or {}).items():
        for name in names:
            tools[f"{server}_{name}"] = server in active
    tools["run_skill_run_skill"] = run_skill_enabled
    return tools


def _delete_session(server_url: str, sid: str) -> None:
    try:
        requests.delete(f"{server_url}/session/{sid}", timeout=10)
    except Exception:  # noqa: BLE001 — best-effort cleanup, never fail the call
        pass


def run_child_skill(server_url: str, roots, skill_id: str, prompt: str,
                    child_payload: dict, child_token: str, max_depth: int,
                    universe=None, model: str = "", timeout: float = 600.0):
    """Spawn a child session running ``skill_id`` and return (text, child_sid).

    The child's SKILL.md governs it (message ``system``); a ``[HOST]`` preamble
    hands the child ITS chain_token; the child is scoped to its own declared tools
    + read-only data (:func:`child_tools`). run_skill is force-disabled at the cap
    so recursion can't exceed ``max_depth``. The child session is DELETED after the
    reply is harvested. ``model`` blank -> OpenCode auto-picks (as the top session).
    """
    skill_body, declared = resolve_skill(skill_id, roots)
    child_depth = int(child_payload["depth"])
    system = (
        f"[HOST] Your run_skill chain_token is: {child_token}\n"
        f"[HOST] If (and only if) you call run_skill, you MUST pass this exact "
        f"chain_token as the chain_token argument.\n\n{skill_body}"
    )

    r = requests.post(f"{server_url}/session",
                      json={"title": f"run_skill:{skill_id}"}, timeout=15)
    r.raise_for_status()
    child_sid = r.json()["id"]
    try:
        msg = {
            "parts": [{"type": "text", "text": prompt or ""}],
            "system": system,
            "tools": child_tools(declared, universe, child_depth < max_depth),
        }
        if model and "/" in model:
            provider, model_id = model.split("/", 1)
            msg["model"] = {"providerID": provider, "modelID": model_id}
        resp = requests.post(f"{server_url}/session/{child_sid}/message",
                             json=msg, timeout=timeout)
        resp.raise_for_status()
        text = ""
        try:
            posted = resp.json()
            if isinstance(posted, dict) and posted.get("parts"):
                text = _last_assistant_text([posted])
        except Exception:  # noqa: BLE001
            pass
        if not text:
            g = requests.get(f"{server_url}/session/{child_sid}/message",
                             params={"limit": 20}, timeout=20)
            g.raise_for_status()
            text = _last_assistant_text(g.json())
        return text, child_sid
    finally:
        _delete_session(server_url, child_sid)


def _last_assistant_text(messages: list) -> str:
    for msg in reversed(messages):
        if (msg.get("info", {}) or {}).get("role") == "assistant":
            parts = msg.get("parts", []) or []
            texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
            return "\n".join(t for t in texts if t).strip()
    return ""
