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

from . import odb_inspect


class FullReportError(Exception):
    """Report could not be produced (no readable tests, or no template)."""


def has_active_bundle(project_root) -> bool:
    """True when the project has an active bundle. Without one the pack extractors
    return empty, so the Word report would contain metadata only (no steps/expected)
    — the caller should warn before exporting."""
    try:
        from project_services import bundle_registry
        return bundle_registry.read_active_bundle(Path(project_root)) is not None
    except Exception:
        return False


def export_full_report(
    project_root: Path,
    test_folders: list[Path],
    output_path: Path,
    *,
    sidecar: dict | None = None,
    template_path: Path | None = None,
    generate_images: bool = True,
    progress=None,
) -> Path:
    """Render a .docx report for ``test_folders`` (each must hold a procedure.json).

    When ``generate_images`` is set, any missing board images are rendered into the
    shared ``.media_cache`` first (via the editor's ODB tooling), so they EMBED in
    the document exactly like the main app — cache-first, and a no-op when there is
    no ODB archive/CLI. ``progress(done, total) -> bool`` is called during image
    generation; return ``False`` to cancel.

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

    if generate_images:
        _generate_images(project_root, procedures, progress)

    report_export.render_report(
        Path(template_path), sidecar, procedures, project_root, Path(output_path))
    return Path(output_path)


def _generate_images(project_root: Path, procedures: list, progress=None) -> None:
    """Render every referenced component/pin image into the shared ``.media_cache``
    via ``odb_inspect.render_target`` (cache-first; graceful no-op without ODB/CLI)
    so they embed in the report — the editor's equivalent of the main app's ODB
    pre-step. Reuses the shared ref collector so the ref shape can't drift."""
    refs = report_export._collect_all_media_refs(procedures, project_root)
    total = len(refs)
    for i, ref in enumerate(refs, start=1):
        component = ref.get("component") or ""
        pin = ref.get("pin")
        if component:
            odb_inspect.render_target(
                project_root, component, str(pin) if pin is not None else None)
        if progress is not None and not progress(i, total):
            break  # operator cancelled
