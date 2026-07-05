"""Controller objects carved out of the MainWindow god-class.

Each controller takes the MainWindow at construction and owns a cohesive
slice of its behavior; MainWindow keeps thin delegating methods so the
test-pinned / Qt-connected surface is unchanged. The task-#39 watcher
slice lives as module-level functions in ``project_controller`` (see its
docstring for the harness seam that forces this).
"""

from .document_controller import DocumentController
from .project_controller import ProjectController

__all__ = ["DocumentController", "ProjectController"]
