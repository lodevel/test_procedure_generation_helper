"""
ProcedureTextParser
===================
Deterministic parser for structured procedure_text.md files.

This file is a **reference implementation** intended to be copied into a
project's ``config/text_parser.py``.  The workflow editor loads it from there
at runtime — if the file is absent the Quick Parse button is hidden.

The conversion rules implemented here are derived from the canonical LLM rules
document (see RULES_VERSION below).  If that document is updated, review this
parser for required changes before bumping RULES_VERSION.

Supported section headings (h1 or h2):
    ## Id / ## Title / ## Description
    ## Equipment  (optional, structured)
    ## Test steps / ## Steps / ## Procedure Steps
    ## Success conditions / ## Expected Results / ## Expected results

Two-pass parenthetical handling:
    Pass 1 — extract component refs hidden inside parentheses
             e.g. "EPO (P6#2)" → P6 pin 2
    Pass 2 — strip all parenthetical content, then extract from clean text
             e.g. "P9 pin 1 (+SWITCH_28V)" → P9 pin 1  (net label stripped)

Macro lines starting with '@' are preserved verbatim with a warning.
"""

import re
import logging

log = logging.getLogger(__name__)

# Version of test_rules_llm_ready.md that this parser was written against.
# If the rules document is updated, review this file and bump this constant.
RULES_VERSION = "v1.3.0"

# Equipment sort buckets — within each bucket, entries are ordered by
# numeric suffix (bare = 0) then full id alphabetically.
_EQUIPMENT_SORT_BUCKETS: dict[str, int] = {
    "PSU": 0, "ELOAD": 1, "SCOPE": 2, "DMM": 3,
    "FNCORE": 4, "DSC": 5, "HXT": 6,
}

# Controller families that are all variants of the fncore-mockup driver.
# Each distinct id (FNCORE1, DSC, DSC2, HXT, HXT7, ...) is a separate target
# for the fncore-mockup driver. They never overlap; no cross-family inference.
_FNCORE_MOCKUP_FAMILIES: list[tuple[str, str]] = [
    # (regex pattern matching the family prefix, canonical family name for ids)
    (r'FNCORE|FN[- ]?CORE', "FNCORE"),
    (r'DSC',                "DSC"),
    (r'HXT',                "HXT"),
]
# Bare-name default when a family appears without any digit and no numbered
# instance of the same family is present in the procedure.
_FNCORE_MOCKUP_BARE_DEFAULT: dict[str, str] = {
    "FNCORE": "FNCORE1",  # historical convention
    "DSC":    "DSC",
    "HXT":    "HXT",
}

# Verbs accepted at the head of an fncore-mockup command. Per
# fncore_mockup_client_llm_usage.md, the command grammar is verb-led, so
# detection is anchored on these words. Adding a new verb is a one-line
# change here; the regexes below are rebuilt automatically from this list.
_FNCORE_MOCKUP_VERBS: tuple[str, ...] = (
    "Set", "Read", "Write", "Configure", "Get", "Trigger",
    "Pulse", "Toggle", "Assert", "Deassert", "Drive",
    "Sample", "Sense", "Reset",
)

# Precomputed regex fragments and compiled patterns for controller detection.
# Built once at import time so we don't pay the construction cost per call.
_FNCORE_MOCKUP_VERB_ALT: str = "|".join(_FNCORE_MOCKUP_VERBS)
_FNCORE_MOCKUP_FAMILY_ALT: str = "|".join(p for p, _ in _FNCORE_MOCKUP_FAMILIES)
# Token: family name optionally followed by digits. The negative lookbehind
# `(?<![#\w])` rejects resource references like `IO#DSC18`, `PWM#HXT0`.
_FNCORE_MOCKUP_TOKEN_RE: str = (
    rf'(?<![#\w])(?:{_FNCORE_MOCKUP_FAMILY_ALT})\d*\b'
)
# Form 1: `<verb> <CONTROLLER> <TARGET> <RESOURCE>` — emit only the controller.
_FNCORE_MOCKUP_FORM1_RE = re.compile(
    rf'\b(?:{_FNCORE_MOCKUP_VERB_ALT})\s+'
    rf'({_FNCORE_MOCKUP_TOKEN_RE})\s+'
    rf'(?:{_FNCORE_MOCKUP_TOKEN_RE})\b',
    re.IGNORECASE,
)
# Form 2: `<verb> <CONTROLLER>` — short form where TARGET acts as controller.
_FNCORE_MOCKUP_FORM2_RE = re.compile(
    rf'\b(?:{_FNCORE_MOCKUP_VERB_ALT})\s+({_FNCORE_MOCKUP_TOKEN_RE})',
    re.IGNORECASE,
)


def _classify_fncore_token(token: str) -> tuple[str | None, str | None]:
    """Map a raw token to its `(family, suffix)`.

    Examples
    --------
    >>> _classify_fncore_token("FNCORE2")
    ('FNCORE', '2')
    >>> _classify_fncore_token("dsc")
    ('DSC', '')
    >>> _classify_fncore_token("fn-core")
    ('FNCORE', '')
    >>> _classify_fncore_token("PSU1")
    (None, None)
    """
    for fam_pattern, fam_name in _FNCORE_MOCKUP_FAMILIES:
        m = re.fullmatch(rf'(?:{fam_pattern})(\d*)', token, re.IGNORECASE)
        if m:
            return fam_name, m.group(1)
    return None, None

# Mapping from lowercase heading text to logical section key
_HEADING_MAP: dict[str, list[str]] = {
    "id":          ["id"],
    "title":       ["title"],
    "description": ["description"],
    "equipment":   ["equipment"],
    "steps":       ["test steps", "steps", "procedure steps", "procedure step"],
    "expected":    [
        "success conditions", "expected results",
        "expected result", "expected",
    ],
}

# Equipment type label → canonical type string
_EQUIP_TYPE_MAP: dict[str, str] = {
    "psu": "psu",
    "eload": "eload",
    "dmm": "dmm",
    "scope": "scope",
    "controller": "controller",
    "ctrl": "controller",
}


class ProcedureTextParser:
    """
    Parse structured procedure_text.md into a procedure.json dict.

    Usage::

        parser = ProcedureTextParser()
        result, warnings = parser.parse(text)

    Returns:
        result   — dict compatible with procedure.json schema
        warnings — list of human-readable warning strings (empty = fully parsed)
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, text: str) -> tuple[dict, list[str]]:
        """Parse markdown procedure text, return (procedure_json_dict, warnings)."""
        warnings: list[str] = []
        sections = self._split_sections(text)

        # Header fields
        test_id = self._section(sections, "id").strip()
        title = self._section(sections, "title").strip()
        description = self._section(sections, "description").strip()
        name = test_id or title or ""

        # Steps
        steps_text = self._section(sections, "steps")
        if not steps_text:
            warnings.append(
                "No steps section found (expected '## Test steps', '## Steps', etc.)"
            )
        steps, step_warnings = self._parse_steps(steps_text)
        warnings.extend(step_warnings)

        # Expected / success conditions
        expected_text = self._section(sections, "expected")
        expected, exp_warnings = self._parse_expected(expected_text)
        warnings.extend(exp_warnings)

        # Equipment — prefer structured section, fall back to scanning steps
        equip_section = self._section(sections, "equipment")
        if equip_section:
            equipment = self._parse_equipment_from_section(equip_section)
            if not equipment:
                warnings.append(
                    "## Equipment section found but format not recognized; scanning steps instead."
                )
                equipment = self._scan_equipment_from_steps([s["text"] for s in steps])
        else:
            equipment = self._scan_equipment_from_steps([s["text"] for s in steps])

        return (
            {
                "id": test_id,
                "requirement": "",
                "name": name,
                "description": description,
                "board": "",
                "equipment": equipment,
                "steps": steps,
                "expected": expected,
            },
            warnings,
        )

    # ------------------------------------------------------------------
    # Section splitting
    # ------------------------------------------------------------------

    def _split_sections(self, text: str) -> dict[str, str]:
        """
        Split markdown into sections keyed by normalized heading name.
        Accepts h1–h3 headings. Lines inside fenced code blocks are ignored
        for heading detection so a ``# comment`` inside a code fence does
        not cut the surrounding section.
        """
        # Build a censored copy where fence content is blanked (spaces) so
        # character positions stay aligned with the original text.
        lines = text.split('\n')
        in_fence = False
        censored_lines: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('```') or stripped.startswith('~~~'):
                in_fence = not in_fence
                censored_lines.append(' ' * len(line))
                continue
            censored_lines.append(' ' * len(line) if in_fence else line)
        censored = '\n'.join(censored_lines)

        heading_re = re.compile(r'^#{1,3}\s+(.+?)\s*$', re.MULTILINE)
        positions = [
            (m.start(), m.end(), m.group(1).strip())
            for m in heading_re.finditer(censored)
        ]

        sections: dict[str, str] = {}
        for i, (start, end, heading) in enumerate(positions):
            next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            # Slice the ORIGINAL text — fence content inside a section is preserved.
            content = text[end:next_start].strip()
            key = self._normalize_heading(heading)
            if key and key not in sections:  # first occurrence wins
                sections[key] = content

        return sections

    def _normalize_heading(self, heading: str) -> str | None:
        """Return logical section key, or None if unrecognized.

        Tolerates trailing colons and parenthetical qualifiers, e.g.
        ``Test steps:``, ``Test steps (mandatory)``.
        """
        norm = heading.lower().strip()
        norm = re.sub(r'\s*\(.*?\)\s*$', '', norm)   # strip trailing parenthetical
        norm = norm.rstrip(':').strip()
        for key, aliases in _HEADING_MAP.items():
            if norm in aliases:
                return key
        return None

    def _section(self, sections: dict, key: str) -> str:
        return sections.get(key, "")

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    # Combined regex for step / condition list markers:
    #   1. text     1) text     (1) text     - text     * text
    _LIST_LINE_RE = re.compile(r'^(?:\(?\d+[.)]|[-*])\s+(.+)$')

    def _parse_steps(self, text: str) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        steps: list[dict] = []
        has_macros = False
        nested_count = 0

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            indent = len(raw_line) - len(raw_line.lstrip())
            if stripped.startswith('@'):
                steps.append({"text": stripped, "media": []})
                has_macros = True
                continue
            m = self._LIST_LINE_RE.match(stripped)
            if not m:
                continue
            step_text = re.sub(r'`', '', m.group(1))
            # Nested bullet (indented) → append to previous step as continuation
            if indent > 0 and steps and not steps[-1]["text"].startswith('@'):
                merged = steps[-1]["text"].rstrip() + ' — ' + step_text
                steps[-1]["text"] = merged
                steps[-1]["media"] = self._extract_media_refs(merged)
                nested_count += 1
            else:
                steps.append({"text": step_text, "media": self._extract_media_refs(step_text)})

        if nested_count:
            warnings.append(
                f"{nested_count} nested bullet line(s) merged into parent step text."
            )
        if has_macros:
            warnings.append(
                "Macro directives (@FOR, @LET, etc.) detected — preserved verbatim. Review manually."
            )
        return steps, warnings

    # ------------------------------------------------------------------
    # Expected / success conditions
    # ------------------------------------------------------------------

    # Placeholder "{N}" used in success-condition lines like "{1} = OK"
    _PLACEHOLDER_RE = re.compile(r'\{[^{}]+\}')

    def _parse_expected(self, text: str) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        expected: list[dict] = []
        has_macros = False

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('@'):
                expected.append({"text": stripped, "media": []})
                has_macros = True
                continue
            m = self._LIST_LINE_RE.match(stripped)
            if m:
                expected.append({"text": re.sub(r'`', '', m.group(1)), "media": []})

        # Fallback: plain lines without list prefix (e.g. "{1} = OK").
        # Only triggers on lines containing a {placeholder} — prevents capturing
        # arbitrary prose as conditions.
        if not expected and text.strip():
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('@'):
                    continue
                if not self._PLACEHOLDER_RE.search(stripped):
                    continue
                expected.append({"text": re.sub(r'`', '', stripped), "media": []})
            if expected:
                warnings.append(
                    "Expected conditions have no list prefix; captured plain "
                    "placeholder lines — verify format."
                )

        if has_macros:
            warnings.append(
                "Macro directives in expected results — preserved verbatim. Review manually."
            )
        return expected, warnings

    # ------------------------------------------------------------------
    # Media reference extraction
    # ------------------------------------------------------------------

    def _extract_media_refs(self, step_text: str) -> list[dict]:
        """
        Extract board component references from a single step text.

        Two-pass strategy:
        - Pass 1: scan content *inside* parentheses for component patterns.
                  Handles "EPO (P6#2)" where the component ID is parenthesised.
        - Pass 2: strip all parenthetical content, then scan the clean text.
                  Strips net labels like (+SWITCH_28V), (GND_AUX0_POW), (PPU Connector).
        """
        refs: list[tuple[str, int | None, bool]] = []

        # Pass 1 — components inside parentheses
        for parens_content in re.findall(r'\(([^)]+)\)', step_text):
            refs.extend(self._extract_component_patterns(parens_content))

        # Pass 2 — components in clean (parenthetical-stripped) text
        clean = re.sub(r'\([^)]+\)', '', step_text)
        refs.extend(self._extract_component_patterns(clean))

        # Deduplicate while preserving order
        seen: set[tuple] = set()
        unique: list[tuple[str, int | None, bool]] = []
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                unique.append(ref)

        return [self._make_media_entry(comp, pin, is_tp) for comp, pin, is_tp in unique]

    def _extract_component_patterns(self, text: str) -> list[tuple[str, int | None, bool]]:
        """
        Return (component, pin, is_tp) tuples from text using priority-ordered regex patterns.

        v1.3.0 TP rule:
        - Glued TP tokens (TP9, TP_VOUT) keep the prefix as the designator.
        - Space-separated "TP NAME" stores ref.component=NAME (prefix stripped),
          caption is rebuilt as "TP NAME" via is_tp=True.

        Consumed character spans prevent double-matching.
        """
        refs: list[tuple[str, int | None, bool]] = []
        consumed: list[tuple[int, int]] = []

        def overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in consumed)

        # 1. Component + "pin" keyword + slash dual-pin   P15 pin 29/30
        for m in re.finditer(r'\b([A-Z]\d+)\s+pins?\s+(\d+)/(\d+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), int(m.group(2)), False))
                refs.append((m.group(1), int(m.group(3)), False))

        # 2. Component + "pin" keyword                    P9 pin 1,  C114 pin 2
        for m in re.finditer(r'\b([A-Z]\d+)\s+pins?\s+(\d+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), int(m.group(2)), False))

        # 3. Hash pin notation                            P6#2,  P3#6
        for m in re.finditer(r'\b([A-Z]\d+)#(\d+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), int(m.group(2)), False))

        # 4. Bare P-connector                             P4,  P9
        #    (only P-prefix is safe without pin; other single-letter IDs are too broad)
        for m in re.finditer(r'\b(P\d+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), None, False))

        # 5. TP numeric                                   TP9,  TP45
        for m in re.finditer(r'\b(TP\d+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), None, True))

        # 6. TP underscore                                TP_VOUT,  TP_SIGNAL
        for m in re.finditer(r'\b(TP_\w+)\b', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                refs.append((m.group(1), None, True))

        # 7. TP named (space-separated)                   TP EPO_SR,  TP +HIGH_28V,  TP SAFE.DISCONNECT
        #    Component stored WITHOUT "TP " prefix per v1.3.0 rules.
        #    Name must start with '+' or an UPPER-CASE letter (real net-name shape) to
        #    avoid false positives like "TP and", "TP in the schematic".
        #    Allows '.', '+', '-', '#' inside the name; trailing punctuation stripped.
        #    Runs AFTER parens are stripped so only unparenthesised TPs match.
        for m in re.finditer(r'\bTP\s+([+]?[A-Z][\w+.\-#]*)', text):
            if not overlaps(m.start(), m.end()):
                consumed.append((m.start(), m.end()))
                name = m.group(1).rstrip('.')   # strip trailing period from sentence end
                if name:
                    refs.append((name, None, True))

        return refs

    def _make_media_entry(self, component: str, pin: int | None, is_tp: bool = False) -> dict:
        if pin is not None:
            caption = f"{component} pin {pin}"
        elif is_tp and not component.upper().startswith("TP"):
            # Space-separated "TP NAME" — rebuild caption from stripped component.
            caption = f"TP {component}"
        else:
            caption = component
        ref: dict = {"component": component, "pin": pin}
        if is_tp:
            ref["is_tp"] = True
        return {
            "type": "image",
            "ref": ref,
            "caption": caption,
        }

    # ------------------------------------------------------------------
    # Equipment — structured ## Equipment section
    # ------------------------------------------------------------------

    def _parse_equipment_from_section(self, text: str) -> list[dict]:
        """
        Parse a structured ## Equipment section.

        Expected format (nested bullets):
            - PSU1 (psu)
              - CH1: 28 V max, 5 A max
            - ELOAD (eload)
            - SCOPE (scope)
              - CH1
              - CH2
            - DMM (dmm)
        """
        equipment: dict[str, dict] = {}
        current_id: str | None = None
        current_type: str | None = None

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip())

            # Top-level bullet (indent == 0): equipment entry
            # ID must be at least 2 ALL-CAPS chars to avoid matching "- SuperCapacitor" as 'S'
            # or "- Thermal interface" as 'T'.
            if stripped.startswith('-') and indent == 0:
                m = re.match(r'^-\s+([A-Z][A-Z0-9_]+)\b\s*(?:\((\w+)\))?', stripped)
                if m:
                    current_id = m.group(1)
                    type_label = (m.group(2) or "").lower()
                    current_type = _EQUIP_TYPE_MAP.get(type_label) or self._infer_equip_type(current_id)
                    if current_id not in equipment:
                        entry: dict = {
                            "id": current_id,
                            "type": current_type,
                            "channels": [],
                        }
                        # Auto-add fncore-mockup subtype for known controller families
                        if current_type == "controller" and self._is_fncore_mockup_id(current_id):
                            entry["subtype"] = "fncore-mockup"
                        equipment[current_id] = entry
                else:
                    current_id = None
                    current_type = None
                continue

            # Sub-level bullet (indent > 0): channel entry
            if current_id and stripped.startswith('-') and indent > 0:
                eq_type = equipment[current_id]["type"]

                if eq_type == "scope":
                    # SCOPE channels stored as plain integers
                    ch_m = re.match(r'-\s+CH(\d+)', stripped, re.IGNORECASE)
                    if ch_m:
                        ch = int(ch_m.group(1))
                        if ch not in equipment[current_id]["channels"]:
                            equipment[current_id]["channels"].append(ch)
                else:
                    # PSU / ELOAD: channel with optional voltage/current limits
                    # "- CH1: 28 V max, 5 A max"
                    ch_limits = re.match(
                        r'-\s+CH(\d+):\s*([\d.]+)\s*V\s*(?:max)?,?\s*([\d.]+)\s*A(?:\s*max)?',
                        stripped, re.IGNORECASE,
                    )
                    if ch_limits:
                        equipment[current_id]["channels"].append({
                            "channel": int(ch_limits.group(1)),
                            "voltage_max": f"{ch_limits.group(2)} V",
                            "current_max": f"{ch_limits.group(3)} A",
                        })
                        continue
                    # Channel with no limits
                    ch_only = re.match(r'-\s+CH(\d+)\b', stripped, re.IGNORECASE)
                    if ch_only:
                        ch = int(ch_only.group(1))
                        if not any(c.get("channel") == ch for c in equipment[current_id]["channels"]):
                            equipment[current_id]["channels"].append({
                                "channel": ch,
                                "voltage_max": None,
                                "current_max": None,
                            })

        return self._sort_equipment(list(equipment.values()))

    # ------------------------------------------------------------------
    # Equipment — scan step texts (fallback when no ## Equipment section)
    # ------------------------------------------------------------------

    def _scan_equipment_from_steps(self, step_texts: list[str]) -> list[dict]:
        """Detect and configure equipment by scanning step text."""
        equipment: dict[str, dict] = {}
        scope_channels: set[int] = set()

        def get_or_create(eq_id: str, eq_type: str) -> dict:
            if eq_id not in equipment:
                entry: dict = {"id": eq_id, "type": eq_type, "channels": []}
                if eq_type == "controller" and self._is_fncore_mockup_id(eq_id):
                    entry["subtype"] = "fncore-mockup"
                equipment[eq_id] = entry
            return equipment[eq_id]

        for text in step_texts:
            # PSU<n> — detect ALL referenced PSU instances, not just PSU1/PSU2
            for psu_id in sorted(set(re.findall(r'\b(PSU\d+)\b', text))):
                entry = get_or_create(psu_id, "psu")
                # "Configure PSU1 CH1 to 28 V / 15 A"  or  "... 28 V, 15 A"
                cfg = re.search(
                    rf'Configure\s+{psu_id}\s+CH(\d+)\s+to\s+([\d.]+)\s*V\s*[/,]\s*([\d.]+)\s*A',
                    text, re.IGNORECASE,
                )
                if cfg:
                    ch, v, a = int(cfg.group(1)), f"{cfg.group(2)} V", f"{cfg.group(3)} A"
                    existing = next((c for c in entry["channels"] if c["channel"] == ch), None)
                    if existing:
                        existing["voltage_max"] = v
                        existing["current_max"] = a
                    else:
                        entry["channels"].append({"channel": ch, "voltage_max": v, "current_max": a})
                else:
                    ch_m = re.search(rf'\b{psu_id}\s+CH(\d+)\b', text)
                    if ch_m:
                        ch = int(ch_m.group(1))
                        if not any(c["channel"] == ch for c in entry["channels"]):
                            entry["channels"].append({"channel": ch, "voltage_max": None, "current_max": None})

            # ELOAD / electronic load
            if re.search(r'\bELOAD\b|\belectronic\s+load\b|\be-load\b', text, re.IGNORECASE):
                entry = get_or_create("ELOAD", "eload")
                ch_m = re.search(r'\bELOAD\s+CH(\d+)\b', text, re.IGNORECASE)
                ch = int(ch_m.group(1)) if ch_m else 1
                # "Configure ELOAD CH1 to 10 A" or "10 mA"
                cfg = re.search(r'Configure\s+ELOAD\s+CH\d+\s+to\s+([\d.]+)\s*(m?A)\b', text, re.IGNORECASE)
                # "constant-current mode, 2 A" or "100 mA"
                cc = re.search(r'constant.current\s+mode,?\s*([\d.]+)\s*(m?A)\b', text, re.IGNORECASE)
                hit = cfg or cc
                if hit:
                    unit = "mA" if hit.group(2).lower() == "ma" else "A"
                    current_a = f"{hit.group(1)} {unit}"
                else:
                    current_a = None
                existing = next((c for c in entry["channels"] if c["channel"] == ch), None)
                if existing:
                    if current_a:
                        existing["current_max"] = current_a
                else:
                    entry["channels"].append({"channel": ch, "voltage_max": None, "current_max": current_a})

            # SCOPE / oscilloscope
            if re.search(r'\bSCOPE\b|\boscilloscope\b', text, re.IGNORECASE):
                get_or_create("SCOPE", "scope")
                for ch_m in re.finditer(r'(?:SCOPE|oscilloscope)\s+CH(\d+)', text, re.IGNORECASE):
                    scope_channels.add(int(ch_m.group(1)))

            # DMM / multimeter — explicit mention or unnamed scalar measurement
            if re.search(r'\bDMM\b|\bmultimeter\b', text, re.IGNORECASE):
                get_or_create("DMM", "dmm")
            elif re.search(
                r'\bMeasure\s+(?:DC\s+)?(?:voltage|current|resistance|temperature|frequency)\b',
                text, re.IGNORECASE,
            ):
                # Unnamed scalar measurement → DMM unless scope or controller is the instrument
                if not re.search(r'\boscilloscope\b|\bSCOPE\b|(?:CH|channel)\s*\d\b', text, re.IGNORECASE):
                    if not re.search(r'\b(?:IO|DAC|ADC|PWM|QEP)#', text, re.IGNORECASE):
                        get_or_create("DMM", "dmm")

        # Controllers (fncore-mockup family) — verb-anchored, form-aware.
        # See `_detect_fncore_controllers` for the grammar handled.
        per_family = self._detect_fncore_controllers("\n".join(step_texts))
        for fam_name, info in per_family.items():
            numbered: set[int] = info["numbered"]
            saw_bare: bool = info["saw_bare"]
            bare_resolved_id = self._resolve_fncore_bare_name(
                fam_name, numbered, saw_bare,
            )
            for n in sorted(numbered):
                eq_id = f"{fam_name}{n}"
                if eq_id not in equipment:
                    equipment[eq_id] = {
                        "id": eq_id, "type": "controller",
                        "subtype": "fncore-mockup", "channels": [],
                    }
            if bare_resolved_id and bare_resolved_id not in equipment:
                equipment[bare_resolved_id] = {
                    "id": bare_resolved_id, "type": "controller",
                    "subtype": "fncore-mockup", "channels": [],
                }

        # SCOPE channels are plain integers
        if "SCOPE" in equipment and scope_channels:
            equipment["SCOPE"]["channels"] = sorted(scope_channels)

        return self._sort_equipment(list(equipment.values()))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _infer_equip_type(self, eq_id: str) -> str:
        u = eq_id.upper()
        if u.startswith("PSU"):
            return "psu"
        if "LOAD" in u:
            return "eload"
        if u == "SCOPE":
            return "scope"
        if u == "DMM":
            return "dmm"
        return "controller"

    def _is_fncore_mockup_id(self, eq_id: str) -> bool:
        """True if id belongs to a known fncore-mockup family (FNCORE/DSC/HXT)."""
        fam, _ = _classify_fncore_token(eq_id)
        return fam is not None

    def _detect_fncore_controllers(self, joined_text: str) -> dict[str, dict]:
        """Verb-anchored, form-aware scan for fncore-mockup controllers.

        Per ``fncore_mockup_client_llm_usage.md``, controller commands are
        verb-led with two surface forms::

            Form 1:  <verb> <CONTROLLER_ID> <TARGET> <RESOURCE> = <VALUE>
            Form 2:  <verb> <CONTROLLER_ID> <RESOURCE> = <VALUE>

        TARGET (DSC/HXT/MCU/...) is a *namespace inside* the controller, not
        a separate equipment instance. In Form 2 the target name itself
        plays the role of the controller id (``"Set DSC IO#DSC17 = '1'"``).

        Algorithm:
        1. Pass 1 finds Form 1 matches and emits ONLY the controller token.
           Each match span is masked so pass 2 can't re-mine the target.
        2. Pass 2 runs Form 2 on the masked residual.

        Newlines are preserved when masking so that any future line-anchored
        regex on the residual still sees the original line structure.

        Prose mentions (``"Plug a FN-CORE..."``) and macro keys
        (``"@ROW ... dsc=IO#DSC42"``) carry no verb and are ignored. The
        ``IO#DSC18`` family of resource refs is filtered by the negative
        lookbehind in ``_FNCORE_MOCKUP_TOKEN_RE``.

        Returns
        -------
        dict
            ``{family_name: {"numbered": set[int], "saw_bare": bool}}``
            with one entry per family in ``_FNCORE_MOCKUP_FAMILIES``.
        """
        per_family: dict[str, dict] = {
            fam_name: {"numbered": set(), "saw_bare": False}
            for _, fam_name in _FNCORE_MOCKUP_FAMILIES
        }

        def _record(token: str) -> None:
            fam, suffix = _classify_fncore_token(token)
            if fam is None:
                return
            if suffix:
                per_family[fam]["numbered"].add(int(suffix))
            else:
                per_family[fam]["saw_bare"] = True

        # Pass 1 — Form 1
        consumed_spans: list[tuple[int, int]] = []
        for m in _FNCORE_MOCKUP_FORM1_RE.finditer(joined_text):
            _record(m.group(1))
            consumed_spans.append((m.start(), m.end()))

        # Pass 2 — Form 2 on masked residual
        residual = self._mask_spans(joined_text, consumed_spans)
        for m in _FNCORE_MOCKUP_FORM2_RE.finditer(residual):
            _record(m.group(1))

        return per_family

    @staticmethod
    def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
        """Return ``text`` with each ``(start, end)`` span replaced by spaces.

        Newlines are preserved so that line-anchored regexes applied to the
        result still see the original line layout.
        """
        if not spans:
            return text
        chars = list(text)
        for start, end in spans:
            for i in range(start, end):
                if chars[i] != "\n":
                    chars[i] = " "
        return "".join(chars)

    @staticmethod
    def _resolve_fncore_bare_name(
        fam_name: str,
        numbered: set[int],
        saw_bare: bool,
    ) -> str | None:
        """Resolve the id assigned to a bare family mention.

        - Bare alone               → canonical default (``DSC``, ``HXT``,
          ``FNCORE1``).
        - Bare mixed with numbered → promote to the lowest unused
          ``N >= 1`` and add ``N`` to ``numbered`` (mutates the caller's set).
        - No bare mention          → ``None``.
        """
        if saw_bare and numbered:
            n = 1
            while n in numbered:
                n += 1
            numbered.add(n)
            return f"{fam_name}{n}"
        if saw_bare:
            return _FNCORE_MOCKUP_BARE_DEFAULT[fam_name]
        return None

    def _sort_equipment(self, equipment: list[dict]) -> list[dict]:
        """
        Sort by family bucket, then numeric suffix, then full id.

        Order: PSU1, PSU2, PSU3, ..., ELOAD, SCOPE, DMM, FNCORE1, FNCORE2, ...,
        DSC, DSC1, DSC2, ..., HXT, HXT7, ..., then any other id alphabetically.
        """
        other_bucket = max(_EQUIPMENT_SORT_BUCKETS.values()) + 1

        def sort_key(e: dict) -> tuple[int, int, str]:
            eid = e["id"]
            m = re.match(r'^([A-Z]+)(\d*)$', eid)
            if m:
                family, digits = m.group(1), m.group(2)
                bucket = _EQUIPMENT_SORT_BUCKETS.get(family, other_bucket)
                num = int(digits) if digits else 0
                return (bucket, num, eid)
            return (other_bucket, 0, eid)

        return sorted(equipment, key=sort_key)
