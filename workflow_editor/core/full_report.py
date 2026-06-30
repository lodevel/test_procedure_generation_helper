"""Full report (.docx) export for the workflow editor.

Reuses the SAME engine as the main app — ``project_services.report_export`` — so
the editor produces an identical report. Pure logic (no Qt): the UI hands it the
selected test folders + output path; it loads each folder's ``procedure.json``
(the opaque doc the shared core expects), resolves the template, and renders.

Editor MVP: no ODB image pre-generation (that lives main-app side in
``test_procedure_gui.export_media``); media refs render as their placeholder text.
The editor edits live text but the core reads ``procedure.json`` — so this exports
the LAST GENERATED json for each test, not unsaved edits.
"""
from __future__ import annotations

from pathlib import Path

from project_services import pack_extractors, report_export


class FullReportError(Exception):
    """Report could not be produced (no readable tests, or no template)."""


def export_full_report(
    project_root: Path,
    test_folders: list[Path],
    output_path: Path,
    *,
    sidecar: dict | None = None,
    template_path: Path | None = None,
) -> Path:
    """Render a .docx report for ``test_folders`` (each must hold a procedure.json).

    Returns the written path. Raises :class:`FullReportError` when nothing is
    exportable or no template is found.
    """
    project_root = Path(project_root)
    procedures = []
    for folder in test_folders:
        doc = pack_extractors.load_procedure_file(Path(folder) / "procedure.json")
        if doc is not None:
            procedures.append(doc)
    if not procedures:
        raise FullReportError(
            "None of the selected tests have a readable procedure.json.")

    if sidecar is None:
        sidecar = report_export.create_default_sidecar()
    if template_path is None:
        template_path = report_export.get_project_template(
            project_path=project_root, sidecar=sidecar)
    if not template_path or not Path(template_path).is_file():
        raise FullReportError(
            "No Word template found in the project's config/templates/exports/ "
            "directory.")

    report_export.render_report(
        Path(template_path), sidecar, procedures, project_root, Path(output_path))
    return Path(output_path)
