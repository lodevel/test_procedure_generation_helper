"""The skill chat's per-chat tool toggles: web access, the project-data tools and saving
datasheets. Each starts as Settings says (``opencode.web_default`` /
``opencode.project_tools_default`` / ``opencode.save_docs_default``) and is overridable for
this chat only."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox

from procedo.ui import ui_kit


class ToolToggles:
    """The three check boxes (``web``, ``project_tools``, ``save_docs``) in one ``row``."""

    def __init__(self, opencode_settings: dict) -> None:
        # Web toggle — exposes OpenCode's webfetch + websearch tools for this
        # chat only (per-request override). OFF by default: with web on, even a
        # no-code skill could be steered into leaking the attached context via a
        # crafted URL, so the user opts in explicitly.
        self.web = QCheckBox("🌐 Web access (search + fetch)")
        self.web.setToolTip(
            "Let the skill search the web and fetch pages (e.g. to confirm a "
            "power IC or find a datasheet).\n"
            "Off by default — only enable it for skills you trust, since web "
            "access can be used to leak the context you attached."
        )
        # Default checked state comes from Settings (per-chat overridable).
        self.web.setChecked(bool(opencode_settings.get("web_default", False)))

        # Project-data tools toggle — exposes the project_tools MCP server for
        # this chat only, letting the LLM PULL board data (netlist, components,
        # test points) on demand instead of having it all pushed up front. OFF
        # by default (Settings can flip the default); per-chat overridable.
        self.project_tools = QCheckBox("🔧 Project data tools (pull)")
        self.project_tools.setToolTip(
            "Let the skill pull board data on demand — query the netlist, list "
            "or inspect components, and read test points — instead of pushing "
            "it all into the context up front.\n"
            "Off by default; enable it so the model can fetch exactly the "
            "board data it needs."
        )
        self.project_tools.setChecked(bool(opencode_settings.get("project_tools_default", False)))

        self.save_docs = QCheckBox("💾 Save datasheets")
        self.save_docs.setToolTip(
            "Let the model SAVE datasheets it downloads into the project's "
            "documents folder (sandboxed) for reuse by future tests. Needs "
            "🌐 web on. Off by default."
        )
        self.save_docs.setChecked(bool(opencode_settings.get("save_docs_default", False)))

        self.row = ui_kit.hbox(self.web, self.project_tools, self.save_docs, None)
        self.row.setSpacing(16)
