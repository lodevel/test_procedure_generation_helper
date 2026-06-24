"""Tests for workflow_editor.authoring.pdf_text (path / bytes / url extraction)."""
from workflow_editor.authoring import pdf_text


def make_pdf(path, text="HELLO VIN VOUT GND"):
    content = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"; offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(pdf)); pdf += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(pdf); pdf += b"xref\n0 %d\n" % (len(objs)+1) + b"0000000000 65535 f \n"
    for off in offs: pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs)+1, x)
    path.write_bytes(pdf)


def test_extract_pdf_text_recovers_string(tmp_path):
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, "HELLO VIN VOUT GND")
    text = pdf_text.extract_pdf_text(pdf)
    assert text is not None
    assert "VIN" in text and "VOUT" in text and "GND" in text


def test_extract_pdf_bytes_recovers_string(tmp_path):
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, "SECRET MARKER 4242")
    data = pdf.read_bytes()
    text = pdf_text.extract_pdf_bytes(data)
    assert text is not None
    assert "SECRET MARKER 4242" in text


def test_extract_pdf_text_garbage_returns_none(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all\x00\x01\x02")
    assert pdf_text.extract_pdf_text(junk) is None


def test_extract_pdf_bytes_garbage_returns_none():
    assert pdf_text.extract_pdf_bytes(b"not a pdf") is None


def test_extract_pdf_text_missing_file_returns_none(tmp_path):
    assert pdf_text.extract_pdf_text(tmp_path / "nope.pdf") is None


def test_fetch_and_extract_unreachable_url_returns_none():
    # Unroutable port; the short timeout keeps this fast even if it connects.
    assert pdf_text.fetch_and_extract("http://127.0.0.1:1/x.pdf") is None


def test_fetch_and_extract_non_pdf_returns_none(monkeypatch):
    """A 200 OK that is HTML (not a PDF) → None, no pypdf call."""
    import io as _io

    class _FakeResp:
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"<html>not a pdf</html>"

    monkeypatch.setattr(
        pdf_text.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
    )
    assert pdf_text.fetch_and_extract("http://example.com/x.pdf") is None


def test_fetch_and_extract_pdf_body_recovers_string(monkeypatch, tmp_path):
    """A 200 OK with a real PDF body → text extracted via the url path."""
    pdf = tmp_path / "net.pdf"
    make_pdf(pdf, "NETWORK PDF OK")
    body = pdf.read_bytes()

    class _FakeResp:
        headers = {"Content-Type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return body

    monkeypatch.setattr(
        pdf_text.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
    )
    text = pdf_text.fetch_and_extract("http://example.com/net.pdf")
    assert text is not None
    assert "NETWORK PDF OK" in text


def test_fetch_and_extract_oversized_download_returns_none(monkeypatch, tmp_path):
    pdf = tmp_path / "big.pdf"
    make_pdf(pdf, "TOO BIG")
    body = pdf.read_bytes()

    class _FakeResp:
        headers = {"Content-Type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            # Return more bytes than requested → triggers the over-cap guard.
            return body + b"\x00" * 100

    monkeypatch.setattr(
        pdf_text.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
    )
    assert (
        pdf_text.fetch_and_extract(
            "http://example.com/big.pdf", max_download_bytes=len(body)
        )
        is None
    )
