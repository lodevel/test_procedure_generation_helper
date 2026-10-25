"""MCP-config helpers — single-sourced in ``opencode_client`` (migration doc
§3a). Thin re-export; see ``backend_base`` for the anti-drift rationale.
"""

from opencode_client.mcp_config import (  # noqa: F401
    build_pdf_tools_mcp_block,
    build_project_tools_mcp_block,
    build_run_skill_mcp_block,
    build_skill_tools_mcp_block,
    skill_tool_overrides,
    win_to_wsl_path,
)
