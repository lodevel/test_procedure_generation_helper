"""Tests for the deterministic DC-DC bring-up test generator.

Covers the three structural branches of the v0.13.0 methodology plus param
validation:
  * +MAIN_5V0 (always-on enable, PG present) reproduces the benchmark structure.
  * a controllable-enable case ADDS the enable-off <100 mV check.
  * a no-PG case OMITS the PG / output→PG delay steps and the CH2 channel.
  * a missing required field raises DcDcParamError.
"""
import os
import sys

import pytest

# Make the editor's workflow_editor package importable when run from the tests/
# dir (mirrors the other tests' sys.path bootstrap).
_HERE = os.path.dirname(os.path.abspath(__file__))
_EDITOR_ROOT = os.path.dirname(_HERE)
if _EDITOR_ROOT not in sys.path:
    sys.path.insert(0, _EDITOR_ROOT)

from workflow_editor.authoring.dcdc_test_generator import (  # noqa: E402
    DcDcParamError,
    DcDcTestParams,
    EnableParams,
    PowerGoodParams,
    PsuParams,
    generate_dcdc_test,
)


def _main_5v0_params() -> DcDcTestParams:
    """The benchmark +MAIN_5V0 case: always-on enable, PG present."""
    return DcDcTestParams(
        rail_name="+MAIN_5V0",
        ic_refdes="U86",
        ic_part="LT8609A",
        vout_nominal_v=5.0,
        rail_test_point="MAIN_5V0",
        psu=PsuParams(input_voltage_v=28.0, input_current_a=10.0,
                      entry_pos="P4", entry_neg="P2"),
        enable=EnableParams(present=True, always_on=True),
        power_good=PowerGoodParams(present=True, test_point="PG_5V0"),
    )


def _sections(text: str) -> dict:
    """Split the generated text into {Equipment, Steps, Expected} line lists."""
    out: dict = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None and line.strip():
            out[cur].append(line.rstrip())
    return out


# ---------------------------------------------------------------------------
# +MAIN_5V0 — benchmark structure
# ---------------------------------------------------------------------------

def test_main_5v0_reproduces_benchmark_structure():
    text = generate_dcdc_test(_main_5v0_params())
    sec = _sections(text)

    # Equipment: a 28 V / 10 A PSU + a 2-channel scope.
    assert sec["Equipment"] == [
        "PSU1 : psu channels=[{1, max_voltage=28.0 V, max_current=10.0 A}]",
        "SCOPE1 : scope channels=[1, 2]",
    ]

    steps = sec["Steps"]
    joined = "\n".join(steps)

    # PSU set V/I then OFF FIRST, then wire.
    assert steps[0].endswith("Set PSU1 CH1 voltage = 28.0 V.")
    assert steps[1].endswith("Set PSU1 CH1 current = 10.0 A.")
    assert steps[2].endswith("Set PSU1 CH1 output = OFF.")
    assert "Connect PSU1 CH1 + to P4, - to P2." in joined

    # Scope CH1 setup order: enable -> atten=10 -> config with 2.0 V/div (5 V rail).
    assert "Configure SCOPE1 CH1: enabled=on." in joined
    assert "Configure SCOPE1 CH1: probe_attenuation=10." in joined
    assert "Configure SCOPE1 CH1: coupling=DC, offset=0.0 V, scale=2.0 V/div." in joined
    # CH2 present (PG) with a 1.0 V/div (3.3 V PG).
    assert "Configure SCOPE1 CH2: coupling=DC, offset=0.0 V, scale=1.0 V/div." in joined

    # Timebase 10 ms/div, trigger CH1 rising at half-rail (2.5 V).
    assert "Configure SCOPE1 timebase: position=0.0 s, scale=10.0 ms/div." in joined
    assert "Configure SCOPE1 trigger: source=CH1, level=2.5 V, slope=rising, sweep=normal." in joined

    # Always-on rail: soft-start driven by PSU output ON (NOT an enable assert).
    assert "Operator: assert the enable" not in joined
    # rise-time + output->PG delay + DC mean + PG mean + ripple RMS measured.
    assert "Measure SCOPE1 CH1 rise time as {1}." in joined
    assert "Measure SCOPE1 delay between rising CH1 and rising CH2 as {2}." in joined
    assert "Measure SCOPE1 CH1 mean voltage as {3}." in joined
    assert "Measure SCOPE1 CH2 mean voltage as {4}." in joined
    assert "Measure SCOPE1 CH1 RMS voltage as {5}." in joined

    # Ripple branch: AC coupling + auto-scale CH1.
    assert "Configure SCOPE1 CH1: coupling=AC." in joined
    assert "Auto-scale SCOPE1 CH1." in joined

    # Three screenshots (soft-start, DC, ripple), each tied to a measurement.
    assert '"main_5v0_softstart.jpg"' in joined
    assert '"main_5v0_dc.jpg"' in joined
    assert '"main_5v0_ripple.jpg"' in joined

    # Finish PSU OFF (last step).
    assert steps[-1].endswith("Set PSU1 CH1 output = OFF.")

    # Expected: every measured value has a pass/fail with the v0.13.0 defaults.
    exp = sec["Expected"]
    assert "{1} <= 10.0 ms" in exp                  # rise time <= 10 ms
    assert "{2} <= 10.0 ms" in exp                  # output->PG delay <= 10 ms
    assert "{3} = 5.0 V +/- 3.0 %" in exp           # DC +/-3%
    assert "{4} = 3.3 V +/- 10.0 %" in exp          # PG level
    assert "{5} <= 100.0 mV" in exp                 # ripple <= 2% of 5 V = 100 mV
    # No enable-off line for an always-on rail.
    assert not any("< 100 mV" in e for e in exp)


# ---------------------------------------------------------------------------
# Controllable enable — ADDS the enable-off check
# ---------------------------------------------------------------------------

def test_controllable_enable_adds_enable_off_check():
    p = _main_5v0_params()
    p.enable = EnableParams(present=True, always_on=False,
                            control_target="connector net EN_5V0")
    text = generate_dcdc_test(p)
    sec = _sections(text)
    joined = "\n".join(sec["Steps"])

    # The enable-off check appears: VOUT measured before the enable is asserted,
    # and its Expected line is < 100 mV.
    assert "Operator: assert the enable (connector net EN_5V0)." in joined
    # Soft-start is enable-driven, NOT a PSU-output-ON.
    # (PSU ON still happens once, for the enable-off check power-up.)
    assert joined.count("Set PSU1 CH1 output = ON.") == 1
    # An enable-off mean-voltage measurement exists with a <100 mV pass/fail.
    assert any("< 100 mV" in e for e in sec["Expected"]), sec["Expected"]
    # enable-off screenshot present.
    assert "_enable_off.jpg" in joined


# ---------------------------------------------------------------------------
# No power-good — OMITS the PG / delay steps and CH2
# ---------------------------------------------------------------------------

def test_no_power_good_omits_pg_steps():
    p = _main_5v0_params()
    p.power_good = PowerGoodParams(present=False)
    text = generate_dcdc_test(p)
    sec = _sections(text)
    joined = "\n".join(sec["Steps"])

    # Single-channel scope (no CH2).
    assert sec["Equipment"][1] == "SCOPE1 : scope channels=[1]"
    assert "CH2" not in joined
    # No output->PG delay measurement, no PG mean voltage, no PG-related Expected.
    assert "delay between" not in joined
    assert not any("delay" in e.lower() for e in sec["Expected"])
    # Still has rise time, DC mean, ripple RMS.
    assert "Measure SCOPE1 CH1 rise time as {1}." in joined
    assert "Measure SCOPE1 CH1 mean voltage as {2}." in joined
    assert "Measure SCOPE1 CH1 RMS voltage as {3}." in joined


# ---------------------------------------------------------------------------
# Validation — missing required field raises
# ---------------------------------------------------------------------------

def test_missing_required_field_raises():
    p = _main_5v0_params()
    p.rail_test_point = ""   # required, now blank
    with pytest.raises(DcDcParamError):
        generate_dcdc_test(p)


def test_missing_psu_entry_raises():
    p = _main_5v0_params()
    p.psu.entry_pos = ""
    with pytest.raises(DcDcParamError):
        generate_dcdc_test(p)


def test_controllable_enable_without_target_raises():
    p = _main_5v0_params()
    p.enable = EnableParams(present=True, always_on=False, control_target=None)
    with pytest.raises(DcDcParamError):
        generate_dcdc_test(p)


def test_power_good_without_test_point_raises():
    p = _main_5v0_params()
    p.power_good = PowerGoodParams(present=True, test_point=None)
    with pytest.raises(DcDcParamError):
        generate_dcdc_test(p)
