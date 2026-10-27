"""A code test's ``openhtf/test.py`` as Studio edits it, Qt-free: its text (line breaks ``"\\n"``)
and how it goes back to the very bytes it came from — its UTF-8 byte-order mark and its CRLF
line endings kept —, and what was on disk when it was read or last written, so a save never
overwrites a change made outside Studio unasked.
"""

from __future__ import annotations

import codecs
from pathlib import Path

from procedo.core import atomic_write


class CodeFile:
    """The file at *path*: :attr:`text` as read, :attr:`disk` the bytes Studio last saw there."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.disk = path.read_bytes() if path.exists() else b""
        self.bom = self.disk.startswith(codecs.BOM_UTF8)
        self.crlf = b"\r\n" in self.disk
        body = self.disk[len(codecs.BOM_UTF8) :] if self.bom else self.disk
        self.text = body.decode("utf-8").replace("\r\n", "\n")  # UnicodeDecodeError: not UTF-8

    def encode(self, text: str) -> bytes:
        """*text* (line breaks ``"\\n"``) as this file's bytes: its BOM, its line endings."""
        data = (text.replace("\n", "\r\n") if self.crlf else text).encode("utf-8")
        return codecs.BOM_UTF8 + data if self.bom else data

    def on_disk(self) -> bytes:
        """The file's bytes now (b"" when it is gone); OSError when it cannot be read."""
        return self.path.read_bytes() if self.path.exists() else b""

    def changed_outside(self) -> bool:
        """The file on disk is no longer what Studio read or wrote; OSError when unreadable."""
        return self.on_disk() != self.disk

    def write(self, text: str) -> None:
        """Write *text* in this file's form; OSError when it cannot."""
        data = self.encode(text)
        self.path.parent.mkdir(exist_ok=True)
        atomic_write.write_bytes(self.path, data)
        self.disk = data
