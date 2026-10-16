"""Sync utilities — content normalization for hash-based sync detection.

Equipment configuration lines (VISA addresses, COM ports, baud rates,
remote flags, controller overrides) change routinely when switching
environments or instruments.  These changes should NOT mark artifacts
as out-of-sync because they don't affect the logical procedure ↔ code
relationship.

The ``normalize_for_hash`` function strips such lines before hashing so
that only structural/logic changes trigger sync warnings.
"""

from __future__ import annotations

import re
from typing import Sequence

# Only test.py needs line-level filtering.
_CODE_FILENAME = "test.py"


def normalize_for_hash(
    content: str,
    filename: str,
    patterns: Sequence[re.Pattern[str]],
) -> str:
    """Return *content* with equipment-config lines blanked out.

    Parameters
    ----------
    content:
        Raw file text.
    filename:
        Basename of the artifact (e.g. ``"test.py"``).  Only code files
        are filtered; JSON is returned unchanged.
    patterns:
        Compiled regexes that match equipment-configuration lines to
        exclude.  Each pattern is tested against individual lines.

    Returns
    -------
    str
        The normalised text suitable for hashing.
    """
    if filename != _CODE_FILENAME or not patterns:
        return content

    lines = content.splitlines(keepends=True)
    normalised: list[str] = []
    for line in lines:
        if any(pat.search(line) for pat in patterns):
            # Replace with a fixed sentinel so line count is preserved
            # (keeps diffs meaningful if we ever need them).
            normalised.append("\n")
        else:
            normalised.append(line)
    return "".join(normalised)
