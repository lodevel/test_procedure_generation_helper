"""The OpenCode config tree Studio owns (the ``master.json`` blueprint, the ``launch/`` and
``launch_test/`` dirs) and the per-launch ``opencode.json`` with its MCP blocks, built fresh
at each launch."""

import copy
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from ..core.fileio import atomic_write_text
from ..core import studio_settings

log = logging.getLogger(__name__)


def get_opencode_config_dir() -> Path:
    """The editor-OWNED OpenCode config directory (sibling of settings.json).

    Holds the project-agnostic ``master.json`` blueprint and the per-launch
    ``launch/opencode.json`` derived config. One config tree, tied to the editor,
    not per project.
    """
    d = studio_settings.get_settings_path().parent / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_opencode_master_path() -> Path:
    """The editor's project-agnostic MASTER blueprint (``master.json``).

    Holds ONLY providers + general config — NO ``model``/``small_model`` (so
    OpenCode auto-picks a supported model) and NO ``mcp`` (the MCP blocks are
    built FRESH at each launch by :func:`build_launch_config`, computing every
    install/project path at runtime). Fully portable; generated once; user-editable.
    """
    return get_opencode_config_dir() / "master.json"


def get_opencode_launch_dir() -> Path:
    """The stable directory the per-launch derived ``opencode.json`` is written
    to (and that ``opencode serve`` is launched with / pointed at via
    ``OPENCODE_CONFIG``)."""
    d = get_opencode_config_dir() / "launch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_opencode_launch_test_dir() -> Path:
    """A SEPARATE, throwaway launch dir used ONLY by Test Connection.

    Test Connection spins up its own ``opencode serve`` to validate the config.
    That probe must NOT share the live ``launch/`` dir: a test manager's
    ``start()`` would overwrite this process's ``launch/`` pid file and its ``stop()`` would
    DELETE it, wiping the LIVE server's orphan-sweep record (the exact
    orphan-left bug class). Isolating the probe in ``launch_test/`` keeps its pid
    file + derived ``opencode.json`` from ever colliding with the live ones."""
    d = get_opencode_config_dir() / "launch_test"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_master_config(seed_from: Optional[Path] = None) -> Path:
    """Return the editor's MASTER blueprint path, GENERATING it ONCE (if absent)
    from the project's opencode.json (``seed_from``): keep its PROVIDERS + general
    config but strip ``model``/``small_model`` (so OpenCode auto-picks a supported
    model — the reliable Default) AND ``mcp`` (the MCP blocks are project/install
    specific and are always built fresh at launch, never baked into the portable
    master).

    Generated only once so the user can edit it; delete it to regenerate from the
    current project. The project's own opencode.json is a relic, never used directly.
    """
    master = get_opencode_master_path()
    if not master.exists() and seed_from is not None:
        try:
            src = Path(seed_from)
            if src.is_file():
                data = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.pop("model", None)
                    data.pop("small_model", None)
                    data.pop("mcp", None)
                    master.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    log.info("Generated editor OpenCode master blueprint from %s", src)
        except Exception:
            log.exception("Failed to generate editor OpenCode master blueprint")
    return master


def _python_win() -> str:
    """The editor venv's Windows python.exe (pythonw has no console; prefer
    python.exe for reliable MCP stdio pipes). Computed at runtime via
    ``sys.executable`` so it self-heals on reinstall/move."""
    return sys.executable.replace("pythonw.exe", "python.exe")


def _mcp_script_win(name: str) -> str:
    """Windows path to an ``authoring/_*_mcp.py`` MCP script, computed at runtime
    relative to this file so it self-heals on move/reinstall."""
    return str(Path(__file__).resolve().parents[1] / "authoring" / name)


def _build_pdf_tools_block(project_root: Optional[Path]) -> dict:
    """Build the ``pdf_tools`` MCP block FRESH for ``project_root``: the venv python, the
    script, its documents dir, its active bundle's rules (``active_rules_dir``, as Studio) and
    its own rule documents' folder, at RUNTIME; else a placeholder under the config dir.
    """
    from procedo.core import bundle_registry
    from ..core.project_manager import PROJECT_RULES_DIR
    from ..llm.mcp_config import build_pdf_tools_mcp_block

    if project_root is not None:
        documents_dir, own_rules = project_root / "documents", project_root / PROJECT_RULES_DIR
        rules_dir = bundle_registry.active_rules_dir(project_root)
    else:
        documents_dir, own_rules, rules_dir = get_opencode_config_dir() / "documents", "", None
    rules_dir = rules_dir or get_opencode_config_dir() / "rules"
    try:
        documents_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.exception("Could not create documents dir %s", documents_dir)
    return build_pdf_tools_mcp_block(
        venv_python_win=_python_win(),
        mcp_script_win=_mcp_script_win("_pdf_tool_mcp.py"),
        documents_dir_win=str(documents_dir),
        rules_dir_win=str(rules_dir),
        project_rules_dir_win=str(own_rules),
    )


def _build_project_tools_block(project_root: Optional[Path]) -> Optional[dict]:
    """Build the ``project_tools`` MCP block FRESH for ``project_root``, or
    ``None`` when there is no project.

    The server uses the same configured ODB++ archive as the board panel,
    with the shared root-level ``*.tgz`` fallback, via ``--odb-tgz``. No
    project -> return ``None``. The server resolves the archive again when
    queried, so replacing the configured board does not require a restart."""
    from ..llm.mcp_config import build_project_tools_mcp_block

    from ..core.odb_inspect import find_odb_tgz

    if project_root is None:
        return None
    archive = find_odb_tgz(project_root)
    return build_project_tools_mcp_block(
        venv_python_win=_python_win(),
        mcp_script_win=_mcp_script_win("_project_tools_mcp.py"),
        odb_tgz_win=str(archive) if archive is not None else "",
        project_root_win=str(project_root),
    )


def _build_skill_tools_blocks(project_root: Optional[Path]) -> dict:
    """Build an MCP block for EVERY discovered tool folder (skill-owned + common),
    auto-registered — no per-tool host code. One generic ``_skill_tools_mcp.py``
    backs each folder, pointed at it via ``--tools-dir``. Discovery (BUILTIN/BUNDLED
    tiers only; reserved infra names skipped) lives in ``authoring.tool_folders``."""
    from ..llm.mcp_config import build_skill_tools_mcp_block
    from ..authoring.tool_folders import discover_tool_folders
    from ..authoring.locations import skill_roots, tool_roots

    blocks: dict = {}
    for tf in discover_tool_folders(skill_roots(project_root) + tool_roots(project_root)):
        blocks.update(build_skill_tools_mcp_block(
            server_name=tf.server,
            venv_python_win=_python_win(),
            mcp_script_win=_mcp_script_win("_skill_tools_mcp.py"),
            tools_dir_win=str(tf.path),
        ))
    return blocks


def _build_run_skill_block(project_root, launch_dir, run_skill_secret):
    """Build the ``run_skill`` infra MCP block, or ``None`` when there is no
    secret (recursion then stays unavailable). Passes the launch pid-file (the
    server reads the live server port from it at call time), the skill roots in
    precedence order (to resolve a child skill_id), the skill-tools universe file
    (so the child force-scopes to its OWN declared tools), and the HMAC secret via
    the block environment."""
    if not run_skill_secret:
        return None
    from ..llm.mcp_config import build_run_skill_mcp_block
    from .server_manager import pid_file_path
    from ..authoring.locations import skill_roots
    from ..authoring.tool_folders import build_skill_tools_universe
    # Sort roots by SkillSource (ASCENDING) so the child resolves the same body
    # the registry would (highest source wins), not by list order.
    roots = [str(pth) for pth, _src in
             sorted(skill_roots(project_root), key=lambda ps: ps[1])]
    # Persist the skill-tools gate universe beside the config so the child can
    # force every NON-declared skill tool off (mirrors the parent gating).
    universe_file = ""
    try:
        _uni = build_skill_tools_universe(project_root)
        if _uni:
            _uf = Path(launch_dir) / "run_skill_universe.json"
            _uf.write_text(json.dumps(_uni), encoding="utf-8")
            universe_file = str(_uf)
    except Exception:
        log.exception("Failed to write run_skill universe file")
    return build_run_skill_mcp_block(
        venv_python_win=_python_win(),
        mcp_script_win=_mcp_script_win("_run_skill_mcp.py"),
        server_port_file_win=str(pid_file_path(launch_dir)),
        skill_roots_win=roots,
        secret=run_skill_secret,
        universe_file_win=universe_file,
    )


def build_launch_config(
    project_root: Optional[Path] = None,
    launch_dir: Optional[Path] = None,
    run_skill_secret: Optional[str] = None,
) -> Path:
    """Derive the per-launch ``opencode.json`` from the MASTER blueprint and the
    CURRENT project, returning the LAUNCH DIR (what ``opencode serve`` is pointed
    at via ``OPENCODE_CONFIG`` / launched from).

    Reads ``master.json`` (providers + general, no model/mcp), deep-copies it, and
    BUILDS the MCP blocks FRESH — ``pdf_tools`` (documents/rules) always present,
    ``project_tools`` for the active project (board reads remain lazy), plus one
    block per discovered tool folder (e.g. ``board_tools``). Every path (venv python via ``sys.executable``,
    scripts via ``__file__``, documents/rules/tgz under ``project_root``) is
    computed at RUNTIME, so the derived config self-heals on reinstall/move and
    follows the active project. The result is written ATOMICALLY (temp file in the
    SAME launch dir + ``os.replace``) so a half-written config can never be loaded.

    ``launch_dir`` overrides the live ``launch/`` dir — Test Connection passes a
    throwaway ``launch_test/`` so its derived config + pid file never collide
    with the live server's (see :func:`get_opencode_launch_test_dir`).
    """
    master = get_opencode_master_path()
    data: dict = {}
    if master.exists():
        try:
            loaded = json.loads(master.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = copy.deepcopy(loaded)
        except Exception:
            log.exception("could not parse master.json; deriving from empty config")

    # Master must NOT carry model/mcp — strip BOTH defensively in case the user
    # edited them back in, so the blueprint stays project-agnostic (OpenCode then
    # auto-picks a supported model; MCP is always built fresh below).
    data.pop("model", None)
    data.pop("small_model", None)
    data.pop("mcp", None)

    if launch_dir is None:
        launch_dir = get_opencode_launch_dir()
    else:
        launch_dir.mkdir(parents=True, exist_ok=True)

    mcp: dict = {}
    try:
        mcp.update(_build_pdf_tools_block(project_root))
    except Exception:
        log.exception("Failed to build pdf_tools MCP block")
    proj_block = None
    try:
        proj_block = _build_project_tools_block(project_root)
    except Exception:
        log.exception("Failed to build project_tools MCP block")
    if proj_block:
        mcp.update(proj_block)
        from ..llm.board_chat import allow_complete_board_output
        allow_complete_board_output(data)
    run_skill_block = None
    try:
        run_skill_block = _build_run_skill_block(
            project_root, launch_dir, run_skill_secret)
    except Exception:
        log.exception("Failed to build run_skill MCP block")
    if run_skill_block:
        mcp.update(run_skill_block)
    try:
        # Auto-discover + register every skill-owned / common tool folder. Infra
        # blocks (pdf_tools/project_tools) are added FIRST and their names are
        # reserved in tool_folders, so a tool folder can't shadow them.
        mcp.update(_build_skill_tools_blocks(project_root))
    except Exception:
        log.exception("Failed to build skill-tools MCP blocks")
    data["mcp"] = mcp

    target = launch_dir / "opencode.json"
    payload = json.dumps(data, indent=2)
    atomic_write_text(target, payload)  # never a half-written config
    log.info("Built launch config (project=%s, mcp=%s)",
             project_root, sorted(mcp.keys()))
    return launch_dir
