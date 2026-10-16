"""Test-id sanitization + folder-sync helpers.

The procedure test id MUST always equal the (sanitized) test FOLDER
name. The folder is the single source of truth; the ``# <id>`` header
line of ``procedure_text.md`` and the top-level ``"id"`` of
``procedure.json`` are kept in sync automatically at save time.

Sanitize rule (matches ``^[A-Za-z][A-Za-z0-9_-]{0,63}$``):
- every char outside ``[A-Za-z0-9_-]`` becomes ``_``
- the id must start with a letter (prefix ``T`` if it does not)
- truncate to 64 chars
"""

import json
import re

_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_test_id(name: str) -> str:
    """Return ``name`` coerced to a valid test id (== folder rule)."""
    s = _ALLOWED.sub("_", name or "")
    if not s or not s[0].isascii() or not s[0].isalpha():
        s = "T" + s
    return s[:64]


def force_text_id_line(content: str, test_id: str) -> str:
    """Force line 1 of ``procedure_text.md`` to ``# <test_id>``.

    Preserves the rest of the document and the original line-1 EOL
    style (``\\r\\n`` vs ``\\n``). If line 1 is not already a ``#``
    header the header is inserted as a new first line rather than
    overwriting real content.
    """
    header = f"# {test_id}"
    if not content:
        return header + "\n"
    nl = content.find("\n")
    if nl == -1:
        first, rest = content, ""
    else:
        first, rest = content[:nl], content[nl:]
    cr = "\r" if first.endswith("\r") else ""
    stripped = first.lstrip()
    if stripped.startswith("#") and not stripped.startswith("##"):
        # Line 1 is the `# <id>` header (single '#'): replace it in place.
        # A `## Section` heading (e.g. a file that starts directly with
        # `## Title`) is CONTENT, not the id header — never clobber it.
        return header + cr + rest
    # Line 1 is not the id header: prepend one, keep everything else.
    return header + "\n" + content


def force_json_id(content: str, test_id: str) -> str:
    """Set the top-level ``"id"`` of a procedure.json string to ``test_id``.

    Invalid JSON is returned unchanged (never corrupt the file). When
    the id already matches, the original text is returned verbatim to
    avoid reformatting churn.
    """
    try:
        data = json.loads(content)
    except Exception:
        return content
    if not isinstance(data, dict):
        return content
    if data.get("id") == test_id:
        return content
    data["id"] = test_id
    return json.dumps(data, indent=2, ensure_ascii=False)
