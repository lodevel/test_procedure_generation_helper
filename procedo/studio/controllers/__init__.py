"""Controller objects carved out of the Studio workspace god-class.

Each controller takes the workspace at construction and owns a cohesive
slice of its behavior; the workspace's menus, shell and signals connect
straight to them. The task-#39 watcher slice lives as module-level
functions in ``project_controller`` (see its docstring for the harness
seam that forces this).
"""

from .assistant_controller import AssistantController
from .document_controller import DocumentController
from .export_controller import ExportController
from .llm_server_controller import LLMServerController
from .project_controller import ProjectController

__all__ = [
    "AssistantController", "DocumentController", "ExportController",
    "LLMServerController", "ProjectController",
]
