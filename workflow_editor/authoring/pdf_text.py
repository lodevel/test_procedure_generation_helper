"""Best-effort PDF → text for the Documents context source.

Datasheets arrive as PDFs but the skill chat pushes plain text, so the editor
decodes the PDF here and hands the text to the model as context (simpler and
more portable than a model-side tool — works offline, with any model/provider).

This recovers the pin-function TABLES that matter for power-supply probing. It
does NOT recover vector/raster pinout DIAGRAMS, which have no text layer — a
vision read is the future upgrade for those (the procedure wizard's domain).
"""
from __future__ import annotations

import io
import logging
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Cap one document so a 200-page datasheet can't swamp the context window.
DEFAULT_MAX_CHARS = 40_000


def _extract_from_reader(reader, max_chars: int) -> Optional[str]:
    """Walk a pypdf reader's pages → joined text, or ``None`` if no text layer.

    Shared by every entry point so the page loop, per-page error swallowing and
    truncation behaviour can never drift between path/bytes/url callers.
    """
    parts: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # a single bad page shouldn't drop the whole datasheet
            continue
        text = text.strip()
        if not text:
            continue
        parts.append(f"[page {index + 1}]\n{text}")
        total += len(text)
        if total >= max_chars:
            parts.append(f"[... truncated at ~{max_chars} characters]")
            break

    if not parts:
        return None
    return "\n\n".join(parts)


def extract_pdf_bytes(data: bytes, max_chars: int = DEFAULT_MAX_CHARS) -> Optional[str]:
    """Return the text of an in-memory PDF, or ``None`` if it can't be decoded.

    Same None-on-unreadable contract as :func:`extract_pdf_text` — ``None``
    means no pypdf, encrypted/malformed, or an image-only PDF with no text
    layer.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed; cannot decode PDF bytes")
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # encrypted / malformed / not really a PDF
        log.info("could not open PDF bytes: %s", exc)
        return None

    return _extract_from_reader(reader, max_chars)


def extract_pdf_text(path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> Optional[str]:
    """Return the PDF's extracted text, or ``None`` if it can't be decoded.

    ``None`` (not ``""``) is the signal that the file is present but unreadable —
    no pypdf, encrypted/malformed, or an image-only (scanned) PDF with no text
    layer — so the caller can tell the model the datasheet couldn't be read
    instead of silently proceeding as if it were absent.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        log.info("could not read PDF %s: %s", path, exc)
        return None
    return extract_pdf_bytes(data, max_chars)


def fetch_and_extract(
    url: str,
    *,
    max_download_bytes: int = 10_000_000,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """Download ``url`` and extract its PDF text, or ``None`` on any failure.

    Never raises: every network/parse error (timeout, DNS, non-PDF body,
    oversized download, malformed PDF) collapses to ``None`` so a model tool
    can report "couldn't read it" rather than crashing the caller. The download
    is capped at ``max_download_bytes`` and a non-PDF body (by Content-Type and
    by magic bytes) is rejected before handing anything to pypdf.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "test-procedure-editor/1.0 (pdf-tools)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            # Read at most max_download_bytes + 1 so we can tell "exactly at cap"
            # from "over cap" without trusting Content-Length.
            data = resp.read(max_download_bytes + 1)
    except Exception as exc:  # noqa: BLE001 — any failure → None by contract
        log.info("could not fetch PDF %s: %s", url, exc)
        return None

    if len(data) > max_download_bytes:
        log.info("PDF download exceeded %d bytes: %s", max_download_bytes, url)
        return None

    looks_like_pdf_header = data[:5] == b"%PDF-" or data[:4] == b"%PDF"
    looks_like_pdf_type = "pdf" in content_type
    if not looks_like_pdf_type and not looks_like_pdf_header:
        log.info("URL is not a PDF (type=%r, no %%PDF header): %s", content_type, url)
        return None

    return extract_pdf_bytes(data, max_chars)
