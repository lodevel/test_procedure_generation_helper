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

# Canonical equipment sort order
_EQUIPMENT_CANONICAL_ORDER = ["PSU1", "PSU2", "ELOAD", "SCOPE", "DMM"]

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
        """Split markdown into sections keyed by normalized heading name."""
        # Accept both h1 (#) and h2 (##) headings
        heading_re = re.compile(r'^#{1,2}\s+(.+)$', re.MULTILINE)
        positions = [
            (m.start(), m.end(), m.group(1).strip())
            for m in heading_re.finditer(text)
        ]

        sections: dict[str, str] = {}
        for i, (start, end, heading) in enumerate(positions):
            next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            content = text[end:next_start].strip()
            key = self._normalize_heading(heading)
            if key and key not in sections:  # first occurrence wins
                sections[key] = content

        return sections

    def _normalize_heading(self, heading: str) -> str | None:
        """Return logical section key, or None if unrecognized."""
        norm = heading.lower().strip()
        for key, aliases in _HEADING_MAP.items():
            if norm in aliases:
                return key
        return None

    def _section(self, sections: dict, key: str) -> str:
        return sections.get(key, "")

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _parse_steps(self, text: str) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        steps: list[dict] = []
        has_macros = False

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('@'):
                steps.append({"text": stripped, "media": []})
                has_macros = True
                continue
            # Numbered "1. text" or bullet "- text"
            m = re.match(r'^\d+\.\s+(.+)$', stripped) or re.match(r'^-\s+(.+)$', stripped)
            if m:
                step_text = re.sub(r'`', '', m.group(1))   # strip markdown backticks
                steps.append({"text": step_text, "media": self._extract_media_refs(step_text)})

        if has_macros:
            warnings.append(
                "Macro directives (@FOR, @LET, etc.) detected — preserved verbatim. Review manually."
            )
        return steps, warnings

    # ------------------------------------------------------------------
    # Expected / success conditions
    # ------------------------------------------------------------------

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
            # Bullet "- cond" or numbered "1. cond"
            m = re.match(r'^-\s+(.+)$', stripped) or re.match(r'^\d+\.\s+(.+)$', stripped)
            if m:
                expected.append({"text": re.sub(r'`', '', m.group(1)), "media": []})

        # Fallback: plain lines without list prefix (e.g. "{1} = OK")
        if not expected and text.strip():
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('@'):
                    continue
                expected.append({"text": re.sub(r'`', '', stripped), "media": []})
            if expected:
                warnings.append(
                    "Expected conditions have no list prefix ('- ' or '1. '); "
                    "captured as plain lines — verify format."
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
        #    Allows '.', '+', '-', '#' inside the name; trailing punctuation stripped.
        #    Runs AFTER parens are stripped so only unparenthesised TPs match.
        for m in re.finditer(r'\bTP\s+([+\w][\w+.\-#]*)', text):
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
        return {
            "type": "image",
            "ref": {"component": component, "pin": pin},
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
                        equipment[current_id] = {
                            "id": current_id,
                            "type": current_type,
                            "channels": [],
                        }
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
                equipment[eq_id] = {"id": eq_id, "type": eq_type, "channels": []}
            return equipment[eq_id]

        for text in step_texts:
            # PSU1 / PSU2
            for psu_id in ("PSU1", "PSU2"):
                if not re.search(rf'\b{psu_id}\b', text):
                    continue
                entry = get_or_create(psu_id, "psu")
                # "Configure PSU1 CH1 to 28 V / 15 A"
                cfg = re.search(
                    rf'Configure\s+{psu_id}\s+CH(\d+)\s+to\s+([\d.]+)\s*V\s*/\s*([\d.]+)\s*A',
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
                if cfg:
                    current_a = f"{cfg.group(1)} {cfg.group(2).upper()}"
                elif cc:
                    current_a = f"{cc.group(1)} {cc.group(2).upper()}"
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

            # Controllers — keep FNCORE name detection; widen DSC/HXT to also match DSC18, HXT7, etc.
            for ctrl_pattern, ctrl_id in [
                (r'\bFNCORE\d*\b|\bFN-CORE\d*\b|\bFN-core\b', "FNCORE1"),
                (r'\bDSC\d*\b', "DSC"),
                (r'\bHXT\d*\b', "HXT"),
            ]:
                if re.search(ctrl_pattern, text, re.IGNORECASE):
                    get_or_create(ctrl_id, "controller")

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

    def _sort_equipment(self, equipment: list[dict]) -> list[dict]:
        """Sort in canonical order: PSU1, PSU2, ELOAD, SCOPE, DMM, then controllers."""
        def sort_key(e: dict) -> tuple[int, str]:
            try:
                return (_EQUIPMENT_CANONICAL_ORDER.index(e["id"]), e["id"])
            except ValueError:
                return (len(_EQUIPMENT_CANONICAL_ORDER), e["id"])

        return sorted(equipment, key=sort_key)
