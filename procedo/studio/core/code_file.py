"""A code test's ``openhtf/test.py`` as Studio edits it, Qt-free: its text (line breaks ``"\\n"``)
and how it goes back to the very bytes it came from — its UTF-8 byte-order mark and its CRLF
line endings kept —, and what was on disk when it was read or last written, so a save never
overwrites a change made outside Studio unasked.
"""

from __future__ import annotations

import codecs
from pathlib import Path
from typing import Optional

from procedo.core import atomic_write


def _stamp(path: Path) -> Optional[tuple[int, int]]:
    """``(mtime_ns, size)`` of *path*, None when it is gone; OSError when it cannot be asked."""
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return found.st_mtime_ns, found.st_size


class CodeFile:
    """The file at *path*: :attr:`text` as read, :attr:`disk` the bytes Studio last saw there."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stamp = _stamp(path)  # what the disk said when :attr:`disk` was read or written
        self.disk = path.read_bytes() if self.stamp is not None else b""
        self.bom = self.disk.startswith(codecs.BOM_UTF8)
        self.crlf = b"\r\n" in self.disk
        body = self.disk[len(codecs.BOM_UTF8) :] if self.bom else self.disk
        self.text = body.decode("utf-8").replace("\r\n", "\n")  # UnicodeDecodeError: not UTF-8

    def encode(self, text: str) -> bytes:
        """*text* (line breaks ``"\\n"``) as this file's bytes: its BOM, its line endings."""
        data = (text.replace("\n", "\r\n") if self.crlf else text).encode("utf-8")
        return codecs.BOM_UTF8 + data if self.bom else data

    def changed_outside(self) -> bool:
        """The file on disk is no longer what Studio read or wrote; OSError when unreadable. Its
        time and size are asked first; its bytes are read only when those differ (a share)."""
        stamp = _stamp(self.path)
        if stamp == self.stamp:
            return False
        if stamp is None:
            return True  # deleted
        if self.path.read_bytes() != self.disk:
            return True
        self.stamp = stamp  # touched, same bytes
        return False

    def gone(self) -> bool:
        """The file is not on disk (deleted outside Studio)."""
        return not self.path.exists()

    def deleted(self) -> None:
        """Take the file's deletion as seen: a save writes it back, unasked."""
        self.disk, self.stamp = b"", None

    def write(self, text: str) -> None:
        """Write *text* in this file's form; OSError when it cannot."""
        data = self.encode(text)
        self.path.parent.mkdir(exist_ok=True)
        atomic_write.write_bytes(self.path, data)
        self.disk, self.stamp = data, _stamp(self.path)
