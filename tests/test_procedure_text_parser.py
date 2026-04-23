"""Unit tests for ProcedureTextParser — focused per-pattern coverage."""
import pytest
from workflow_editor.core.procedure_text_parser import ProcedureTextParser


@pytest.fixture
def parser():
    return ProcedureTextParser()


# ----------------------------------------------------------------------
# Section splitter
# ----------------------------------------------------------------------

class TestSectionSplitter:
    def test_basic_h2_sections(self, parser):
        text = "## Title\nFoo\n\n## Test steps\n1. Do thing.\n"
        result, _ = parser.parse(text)
        assert result["name"] == "Foo"
        assert len(result["steps"]) == 1

    def test_h3_headings_accepted(self, parser):
        text = "### Title\nMyTest\n\n### Test steps\n1. Step one.\n"
        result, _ = parser.parse(text)
        assert result["name"] == "MyTest"
        assert len(result["steps"]) == 1

    def test_heading_with_trailing_colon(self, parser):
        text = "## Test steps:\n1. Foo.\n"
        result, _ = parser.parse(text)
        assert len(result["steps"]) == 1

    def test_heading_with_parenthetical(self, parser):
        text = "## Test steps (mandatory)\n1. Foo.\n"
        result, _ = parser.parse(text)
        assert len(result["steps"]) == 1

    def test_code_fence_does_not_split_section(self, parser):
        # Hash inside code fence must NOT create a phantom section
        text = (
            "## Test steps\n"
            "1. Do thing.\n"
            "```\n"
            "## Not a real heading\n"
            "# Also not a heading\n"
            "```\n"
            "2. Another step.\n"
        )
        result, _ = parser.parse(text)
        assert len(result["steps"]) == 2


# ----------------------------------------------------------------------
# Step parser
# ----------------------------------------------------------------------

class TestStepParser:
    def test_numbered_dot(self, parser):
        result, _ = parser.parse("## Test steps\n1. First.\n2. Second.\n")
        assert [s["text"] for s in result["steps"]] == ["First.", "Second."]

    def test_numbered_paren(self, parser):
        result, _ = parser.parse("## Test steps\n1) First.\n2) Second.\n")
        assert [s["text"] for s in result["steps"]] == ["First.", "Second."]

    def test_paren_numbered(self, parser):
        result, _ = parser.parse("## Test steps\n(1) First.\n(2) Second.\n")
        assert [s["text"] for s in result["steps"]] == ["First.", "Second."]

    def test_dash_bullet(self, parser):
        result, _ = parser.parse("## Test steps\n- First.\n- Second.\n")
        assert [s["text"] for s in result["steps"]] == ["First.", "Second."]

    def test_star_bullet(self, parser):
        result, _ = parser.parse("## Test steps\n* First.\n* Second.\n")
        assert [s["text"] for s in result["steps"]] == ["First.", "Second."]

    def test_backticks_stripped(self, parser):
        result, _ = parser.parse("## Test steps\n1. Set `value` to `0xFF`.\n")
        assert result["steps"][0]["text"] == "Set value to 0xFF."

    def test_macro_lines_preserved(self, parser):
        result, warns = parser.parse(
            "## Test steps\n1. Setup.\n@FOR j IN 0..9\n- Loop body.\n@ENDFOR\n"
        )
        texts = [s["text"] for s in result["steps"]]
        assert "@FOR j IN 0..9" in texts
        assert "@ENDFOR" in texts
        assert any("Macro directives" in w for w in warns)

    def test_indented_sub_bullet_merges_into_parent(self, parser):
        text = (
            "## Test steps\n"
            "1. Configure PSU.\n"
            "   - With output OFF.\n"
            "   - Range 28V.\n"
            "2. Next step.\n"
        )
        result, warns = parser.parse(text)
        assert len(result["steps"]) == 2
        assert "Configure PSU." in result["steps"][0]["text"]
        assert "With output OFF." in result["steps"][0]["text"]
        assert "Range 28V." in result["steps"][0]["text"]
        assert any("nested bullet" in w for w in warns)


# ----------------------------------------------------------------------
# Expected / success conditions parser
# ----------------------------------------------------------------------

class TestExpectedParser:
    def test_bullet_conditions(self, parser):
        text = "## Test steps\n1. x.\n## Success conditions\n- {1} > 5V\n- {2} < 1V\n"
        result, _ = parser.parse(text)
        assert len(result["expected"]) == 2

    def test_numbered_conditions(self, parser):
        text = "## Test steps\n1. x.\n## Expected Results\n1. {1} = 5V\n2. {2} = 3V\n"
        result, _ = parser.parse(text)
        assert len(result["expected"]) == 2

    def test_plain_placeholder_lines(self, parser):
        text = "## Test steps\n1. x.\n## Success conditions\n{1} = OK\n{2} = OK\n"
        result, warns = parser.parse(text)
        assert len(result["expected"]) == 2
        assert any("plain placeholder" in w for w in warns)

    def test_plain_prose_does_not_become_conditions(self, parser):
        text = "## Test steps\n1. x.\n## Success conditions\nThe device works.\nNo errors occur.\n"
        result, _ = parser.parse(text)
        assert result["expected"] == []


# ----------------------------------------------------------------------
# Media reference extraction
# ----------------------------------------------------------------------

class TestMediaRefs:
    def _refs(self, parser, step_text):
        text = f"## Test steps\n1. {step_text}\n"
        result, _ = parser.parse(text)
        return result["steps"][0]["media"]

    def test_p_with_pin_keyword(self, parser):
        refs = self._refs(parser, "Probe P9 pin 1.")
        assert len(refs) == 1
        assert refs[0]["ref"] == {"component": "P9", "pin": 1}
        assert refs[0]["caption"] == "P9 pin 1"

    def test_p_dual_pin_slash(self, parser):
        refs = self._refs(parser, "Probe P15 pin 29/30.")
        comps = [(r["ref"]["component"], r["ref"]["pin"]) for r in refs]
        assert ("P15", 29) in comps and ("P15", 30) in comps

    def test_p_hash_pin(self, parser):
        refs = self._refs(parser, "Connect P6#2 to ground.")
        assert refs[0]["ref"] == {"component": "P6", "pin": 2}

    def test_bare_p_connector(self, parser):
        refs = self._refs(parser, "Plug into P4.")
        assert refs[0]["ref"] == {"component": "P4", "pin": None}

    def test_tp_numeric_keeps_prefix(self, parser):
        refs = self._refs(parser, "Probe TP9.")
        r = refs[0]
        assert r["ref"]["component"] == "TP9"
        assert r["ref"]["is_tp"] is True
        assert r["caption"] == "TP9"

    def test_tp_underscore_keeps_prefix(self, parser):
        refs = self._refs(parser, "Probe TP_VOUT.")
        r = refs[0]
        assert r["ref"]["component"] == "TP_VOUT"
        assert r["ref"]["is_tp"] is True
        assert r["caption"] == "TP_VOUT"

    def test_tp_named_strips_prefix(self, parser):
        refs = self._refs(parser, "Measure voltage on TP EPO_SR as {1}.")
        r = refs[0]
        assert r["ref"]["component"] == "EPO_SR"
        assert r["ref"]["is_tp"] is True
        assert r["caption"] == "TP EPO_SR"

    def test_tp_named_with_plus(self, parser):
        refs = self._refs(parser, "Measure on TP +HIGH_28V.")
        r = refs[0]
        assert r["ref"]["component"] == "+HIGH_28V"
        assert r["caption"] == "TP +HIGH_28V"

    def test_tp_named_with_dot(self, parser):
        refs = self._refs(parser, "Measure on TP SAFE.DISCONNECT.")
        r = refs[0]
        assert r["ref"]["component"] == "SAFE.DISCONNECT"

    def test_tp_followed_by_lowercase_word_is_ignored(self, parser):
        # "TP and probe" must NOT capture "and" as a ref
        refs = self._refs(parser, "Verify TP and probe are aligned.")
        for r in refs:
            assert r["ref"]["component"].lower() not in ("and", "in", "the", "is")

    def test_parenthesized_pin_extracted(self, parser):
        refs = self._refs(parser, "EPO (P6#2) is high.")
        comps = [(r["ref"]["component"], r["ref"]["pin"]) for r in refs]
        assert ("P6", 2) in comps

    def test_net_label_in_parens_stripped_from_caption(self, parser):
        refs = self._refs(parser, "Measure P9 pin 1 (+SWITCH_28V).")
        comps = [(r["ref"]["component"], r["ref"]["pin"]) for r in refs]
        assert ("P9", 1) in comps

    def test_bare_net_name_no_ref(self, parser):
        # "+SWITCH28V" alone (no TP) is a net name, not a physical ref
        refs = self._refs(parser, "Measure +SWITCH28V as {1}.")
        assert refs == []


# ----------------------------------------------------------------------
# Equipment scanner
# ----------------------------------------------------------------------

class TestEquipmentScanner:
    def test_psu_with_slash_separator(self, parser):
        text = "## Test steps\n1. Configure PSU1 CH1 to 28 V / 5 A.\n"
        result, _ = parser.parse(text)
        psu = next(e for e in result["equipment"] if e["id"] == "PSU1")
        assert psu["channels"][0]["voltage_max"] == "28 V"
        assert psu["channels"][0]["current_max"] == "5 A"

    def test_psu_with_comma_separator(self, parser):
        text = "## Test steps\n1. Configure PSU1 CH1 to 28 V, 5 A.\n"
        result, _ = parser.parse(text)
        psu = next(e for e in result["equipment"] if e["id"] == "PSU1")
        assert psu["channels"][0]["voltage_max"] == "28 V"
        assert psu["channels"][0]["current_max"] == "5 A"

    def test_psu3_detected(self, parser):
        text = "## Test steps\n1. Configure PSU3 CH1 to 12 V / 1 A.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "PSU3" in ids

    def test_eload_milliamp(self, parser):
        text = "## Test steps\n1. Set ELOAD CH1 in constant-current mode, 100 mA.\n"
        result, _ = parser.parse(text)
        eload = next(e for e in result["equipment"] if e["id"] == "ELOAD")
        assert eload["channels"][0]["current_max"] == "100 mA"

    def test_eload_amp(self, parser):
        text = "## Test steps\n1. Configure ELOAD CH1 to 10 A.\n"
        result, _ = parser.parse(text)
        eload = next(e for e in result["equipment"] if e["id"] == "ELOAD")
        assert eload["channels"][0]["current_max"] == "10 A"

    def test_dmm_explicit(self, parser):
        text = "## Test steps\n1. Connect DMM to TP9.\n"
        result, _ = parser.parse(text)
        assert any(e["id"] == "DMM" for e in result["equipment"])

    def test_dmm_implicit_from_unnamed_measurement(self, parser):
        text = "## Test steps\n1. Measure voltage on TP9 as {1}.\n"
        result, _ = parser.parse(text)
        assert any(e["id"] == "DMM" for e in result["equipment"])

    def test_dmm_not_added_when_scope_used(self, parser):
        text = "## Test steps\n1. Measure voltage on SCOPE CH1 as {1}.\n"
        result, _ = parser.parse(text)
        assert not any(e["id"] == "DMM" for e in result["equipment"])

    def test_dmm_not_added_when_controller_used(self, parser):
        text = "## Test steps\n1. Measure voltage on DSC ADC#DSC9 as {1}.\n"
        result, _ = parser.parse(text)
        assert not any(e["id"] == "DMM" for e in result["equipment"])

    def test_controller_dsc_with_io_token(self, parser):
        text = "## Test steps\n1. Set DSC IO#DSC18 = '1'.\n"
        result, _ = parser.parse(text)
        assert any(e["id"] == "DSC" for e in result["equipment"])

    def test_controller_hxt_with_io_token(self, parser):
        text = "## Test steps\n1. Set HXT IO#HXT7 = '1'.\n"
        result, _ = parser.parse(text)
        assert any(e["id"] == "HXT" for e in result["equipment"])


# ----------------------------------------------------------------------
# Equipment section parser
# ----------------------------------------------------------------------

class TestEquipmentSection:
    def test_supercapacitor_does_not_become_equipment(self, parser):
        # Lowercase 'S' in "SuperCapacitor" must NOT be extracted as an ID
        text = (
            "## Equipment\n"
            "- PSU1 (psu)\n"
            "- SuperCapacitor assembly\n"
            "  - 2 parallel pack\n"
            "## Test steps\n1. Foo.\n"
        )
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "S" not in ids
        assert "PSU1" in ids

    def test_thermal_interface_does_not_become_equipment(self, parser):
        text = (
            "## Equipment\n"
            "- PSU1 (psu)\n"
            "- Thermal interface for Mosfets\n"
            "## Test steps\n1. Foo.\n"
        )
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "T" not in ids

    def test_unknown_format_falls_back_to_scanner(self, parser):
        # Bold-markdown format that the section parser cannot read
        text = (
            "## Equipment\n"
            "- **Power Supply 1** (`PSU1`): CH1, up to 24 V / 2 A\n"
            "## Test steps\n1. Configure PSU1 CH1 to 24 V / 2 A.\n"
        )
        result, warns = parser.parse(text)
        assert any(e["id"] == "PSU1" for e in result["equipment"])
        assert any("format not recognized" in w for w in warns)


# ----------------------------------------------------------------------
# Per-instance fncore-mockup family detection (FNCORE / DSC / HXT)
# ----------------------------------------------------------------------

class TestFncoreMockupFamilies:
    def test_dsc_numbered_instances_separate(self, parser):
        text = (
            "## Test steps\n"
            "1. Set DSC1 IO#GPIO1 = '1'.\n"
            "2. Set DSC2 IO#GPIO2 = '0'.\n"
        )
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "DSC1" in ids and "DSC2" in ids
        assert "DSC" not in ids

    def test_fncore_numbered_instances_separate(self, parser):
        text = (
            "## Test steps\n"
            "1. Set FNCORE1 IO#A = '1'.\n"
            "2. Set FNCORE2 IO#B = '1'.\n"
        )
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "FNCORE1" in ids and "FNCORE2" in ids

    def test_hxt_numbered(self, parser):
        text = "## Test steps\n1. Read HXT7 ADC#X = {1}.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "HXT7" in ids
        assert "HXT" not in ids

    def test_bare_dsc_alone_kept_bare(self, parser):
        text = "## Test steps\n1. Set DSC IO#GPIO1 = '1'.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "DSC" in ids
        assert not any(i.startswith("DSC") and i != "DSC" for i in ids)

    def test_bare_fncore_alone_maps_to_fncore1(self, parser):
        text = "## Test steps\n1. Set FNCORE IO#A = '1'.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "FNCORE1" in ids

    def test_bare_plus_numbered_promotes_bare(self, parser):
        # `DSC` (bare) + `DSC2` (numbered) → bare promoted to DSC1
        text = (
            "## Test steps\n"
            "1. Set DSC IO#A = '1'.\n"
            "2. Set DSC2 IO#B = '1'.\n"
        )
        result, _ = parser.parse(text)
        ids = sorted(e["id"] for e in result["equipment"] if e["id"].startswith("DSC"))
        assert ids == ["DSC1", "DSC2"]

    def test_bare_plus_numbered_promotes_to_lowest_unused(self, parser):
        # Bare + DSC1 + DSC3 → bare promoted to DSC2
        text = (
            "## Test steps\n"
            "1. Set DSC IO#A = '1'.\n"
            "2. Set DSC1 IO#B = '1'.\n"
            "3. Set DSC3 IO#C = '1'.\n"
        )
        result, _ = parser.parse(text)
        ids = sorted(e["id"] for e in result["equipment"] if e["id"].startswith("DSC"))
        assert ids == ["DSC1", "DSC2", "DSC3"]

    def test_no_cross_family_inference(self, parser):
        # HXT alone must NOT add an FNCORE entry
        text = "## Test steps\n1. Read HXT ADC#X = {1}.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert "HXT" in ids
        assert not any(i.startswith("FNCORE") for i in ids)
        assert not any(i.startswith("DSC") for i in ids)

    def test_subtype_emitted_on_controller_entries(self, parser):
        text = (
            "## Test steps\n"
            "1. Set DSC1 IO#A = '1'.\n"
            "2. Set HXT IO#B = '1'.\n"
            "3. Set FNCORE2 IO#C = '1'.\n"
        )
        result, _ = parser.parse(text)
        for e in result["equipment"]:
            if e["id"] in ("DSC1", "HXT", "FNCORE2"):
                assert e.get("subtype") == "fncore-mockup", f"{e['id']} missing subtype"

    def test_io_token_does_not_create_phantom_instance(self, parser):
        # `IO#DSC18` is a pin reference on bare DSC, not a separate DSC18.
        text = "## Test steps\n1. Set DSC IO#DSC18 = '1'.\n"
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        assert ids == ["DSC"]  # exactly one DSC, no DSC18

    def test_sort_order_with_numbered_controllers(self, parser):
        text = (
            "## Test steps\n"
            "1. Configure PSU3 CH1 to 5 V / 1 A.\n"
            "2. Set DSC2 IO#A = '1'.\n"
            "3. Set FNCORE1 IO#B = '1'.\n"
            "4. Configure PSU1 CH1 to 28 V / 5 A.\n"
        )
        result, _ = parser.parse(text)
        ids = [e["id"] for e in result["equipment"]]
        # PSUs first (by number), then FNCORE, then DSC
        assert ids.index("PSU1") < ids.index("PSU3")
        assert ids.index("PSU3") < ids.index("FNCORE1")
        assert ids.index("FNCORE1") < ids.index("DSC2")

    def test_section_controller_gets_subtype(self, parser):
        # Equipment section with explicit (controller) type for a fncore-mockup id
        text = (
            "## Equipment\n"
            "- PSU1 (psu)\n"
            "- DSC2 (controller)\n"
            "## Test steps\n1. Set DSC2 IO#A = '1'.\n"
        )
        result, _ = parser.parse(text)
        dsc = next(e for e in result["equipment"] if e["id"] == "DSC2")
        assert dsc.get("subtype") == "fncore-mockup"
