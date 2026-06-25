#!/usr/bin/env python3
"""Deterministic generator for a power-IC (LDO / DC-DC) scope bring-up test.

This module ENCODES the rail_check authoring methodology (SKILL.md v0.13.0) as a
pure function: instead of the LLM free-forming the procedure text (which the
benchmark showed is good but VARIABLE), the LLM's job shrinks to EXTRACTING a
small set of structural + numeric params, and :func:`generate_dcdc_test` turns
them into the canonical procedure text deterministically.

The output mirrors the project's existing power tests (``psu_main_5v0`` /
``pwr_aux``): a ``## Equipment`` / ``## Steps`` / ``## Expected`` block, one
operation per line, in the labscpi PSU + Oscilloscope grammar.

Structural branches (the whole reason a generator beats a fixed template):
  * ``enable.present and not enable.always_on`` → an ENABLE-OFF check
    (VOUT < 100 mV before the enable is asserted) AND the soft-start is driven
    by asserting the enable. Otherwise (always-on or no controllable enable) the
    soft-start is driven by turning the PSU output ON.
  * ``power_good.present`` → a second scope channel (CH2) on the PG test point,
    a PG mean-voltage measurement, and an output→PG delay measurement +
    pass/fail. Otherwise those steps are omitted entirely (single-channel scope).

There is intentionally NO "disable the rail and check PG de-asserts" sequence —
v0.13.0 explicitly forbids it.

The module is dependency-free (pure stdlib) and emits pure-LF text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# PARAM SCHEMA
# ---------------------------------------------------------------------------
# Every variation point in the v0.13.0 methodology is a field below. Fields are
# split into nested dataclasses mirroring the methodology's groupings (PSU /
# enable / power-good / scope). REQUIRED fields have no default and validation
# raises if they are missing/blank; DEFAULTED fields carry the v0.13.0 default
# (overridable when the datasheet or user gives a tighter number).


@dataclass
class PsuParams:
    """Bench PSU stimulus at the BOARD POWER ENTRY (never the IC's VIN pin).

    REQUIRED:
      input_voltage_v   – board nominal input voltage (the PSU set-point), V.
      input_current_a   – PSU current limit, A.
      entry_pos         – connector node the PSU '+' lead attaches to (e.g. 'P4').
      entry_neg         – connector node the PSU '-' / return lead attaches to
                          (e.g. 'P2'); also the scope GND reference.
    """

    input_voltage_v: float
    input_current_a: float
    entry_pos: str
    entry_neg: str


@dataclass
class EnableParams:
    """How the rail's enable behaves.

    REQUIRED:
      present     – does the IC have an EN/enable input at all?
    DEFAULTED / CONDITIONAL:
      always_on   – True ⇒ EN is tied permanently active (no controllable gate);
                    the rail comes up purely from input power. False + present
                    ⇒ a CONTROLLABLE enable (drives the enable-off check + the
                    enable-asserted soft-start). Ignored when present is False.

    CONTROLLABLE ENABLE — two flavours of "what asserts it":
      * CONTROLLER-DRIVEN (preferred): an on-board CONTROLLER drives the enable
        node. Supply controller_id + io_resource (and optionally assert_value /
        target / controller_subtype). The generator then declares the controller
        in ## Equipment and emits the fncore CONTROLLER-DRIVE grammar
        (`Set <ID> <TARGET> <res> = '<0|1>'.`) to assert / de-assert it — NOT a
        manual Operator step.
      * CONNECTOR-NET (no controller behind it): leave controller_id / io_resource
        unset and supply control_target; the generator emits an Operator step
        naming it (no controller grammar is assumed).

      controller_id  – the FNCORE controller's Equipment ID that drives the
                    enable (e.g. 'IF_PLM'). Present ⇒ controller-driven.
      io_resource    – the firmware resource id of the enable node on the
                    controller (e.g. 'CMD_AUX0'). Present ⇒ controller-driven.
                    A controllable enable is CONTROLLER-DRIVEN iff BOTH
                    controller_id AND io_resource are given.
      assert_value   – the digital value ('0' or '1') that ASSERTS (turns ON)
                    the enable. ACTIVE-LOW enables (e.g. nCMD_AUX0) assert at
                    '0'; active-high at '1'. Default '1'. The de-assert value is
                    the opposite.
      target         – the FNCORE controller TARGET (MCU id) the resource lives
                    on (e.g. 'DSC'). Default 'DSC' when unknown.
      controller_subtype – the controller subtype for the Equipment line.
                    Default 'fncore-mockup'.
      control_target – human-readable description of WHAT is commanded to assert
                    the enable (e.g. 'connector net EN_5V0'). REQUIRED for the
                    CONNECTOR-NET case (controllable + NOT controller-driven);
                    the generator emits an Operator step naming it. None
                    otherwise.
    """

    present: bool
    always_on: bool = True
    control_target: Optional[str] = None
    controller_id: Optional[str] = None
    io_resource: Optional[str] = None
    assert_value: str = "1"
    target: str = "DSC"
    controller_subtype: str = "fncore-mockup"

    @property
    def controller_driven(self) -> bool:
        """A controllable enable is controller-driven iff it names BOTH a
        controller_id AND an io_resource (otherwise it is a connector net)."""
        return bool(
            (self.controller_id or "").strip()
            and (self.io_resource or "").strip()
        )


@dataclass
class PowerGoodParams:
    """The IC's power-good (PG / PGOOD / PWRGD) output, if any.

    REQUIRED:
      present         – does the IC expose a PG output?
    CONDITIONAL / DEFAULTED:
      test_point      – the scope probe pad for PG (REQUIRED when present).
      nominal_v       – expected PG-high level for its mean-voltage check, V
                        (default 3.3 — typical logic-level PG).
      tolerance_pct   – tolerance on the PG level check, % (default 10).
      delay_limit_ms  – max allowed output→PG-assert delay, ms (default 10).
    """

    present: bool
    test_point: Optional[str] = None
    nominal_v: float = 3.3
    tolerance_pct: float = 10.0
    delay_limit_ms: float = 10.0


@dataclass
class ScopeParams:
    """Scope framing knobs.

    DEFAULTED:
      timebase_ms     – horizontal scale, ms/div (default 10 — covers most
                        soft-starts per v0.13.0).
      ch1             – CH1 channel number (rail test point), default 1.
      ch2             – CH2 channel number (power-good), default 2; used only
                        when power_good.present.
    """

    timebase_ms: float = 10.0
    ch1: int = 1
    ch2: int = 2


@dataclass
class DcDcTestParams:
    """The full param set the LLM must EXTRACT for one power-IC bring-up test.

    REQUIRED (no derivable default — the skill asks the LLM to fill these):
      rail_name       – the output rail / net name (e.g. '+MAIN_5V0').
      ic_refdes       – the regulator's reference designator (e.g. 'U86').
      ic_part         – the regulator's manufacturer part number.
      vout_nominal_v  – nominal regulated output voltage, V (drives the DC
                        pass/fail AND the CH1 V/div pick).
      rail_test_point – scope probe pad for the rail (e.g. 'MAIN_5V0').
      psu             – PsuParams (board power entry + set-points).
      enable          – EnableParams (present / always_on / control_target).
      power_good      – PowerGoodParams (present / test_point / limits).

    DEFAULTED (v0.13.0 defaults; override only on a tighter datasheet/user number):
      dc_tolerance_pct  – DC regulation window, % (default 3 → ±3%).
      ripple_limit_pct  – ripple AC-RMS limit as % of nominal (default 2 → ≤2%).
      ripple_limit_mv   – ABSOLUTE ripple limit in mV; when set it OVERRIDES the
                        percent form (None ⇒ use ripple_limit_pct). Lets a
                        datasheet/user pin an absolute number.
      rise_time_limit_ms – soft-start rise-time limit, ms (default 10 → ≤10 ms).
      gnd_label         – label used for the scope GND reference node
                        (default 'GND'); set to the return net if it differs.
      scope             – ScopeParams (timebase / channel numbers).
    """

    rail_name: str
    ic_refdes: str
    ic_part: str
    vout_nominal_v: float
    rail_test_point: str
    psu: PsuParams
    enable: EnableParams
    power_good: PowerGoodParams
    dc_tolerance_pct: float = 3.0
    ripple_limit_pct: float = 2.0
    ripple_limit_mv: Optional[float] = None
    rise_time_limit_ms: float = 10.0
    gnd_label: str = "GND"
    scope: ScopeParams = field(default_factory=ScopeParams)


# A machine-readable mirror of the schema above, for the skill / UI to render the
# "required fields" form. Kept in sync with the dataclasses by hand (small,
# rarely changes). REQUIRED entries have no "default" key.
PARAM_SCHEMA: dict = {
    "rail_name": {"type": "str", "required": True,
                  "meaning": "output rail / net name, e.g. +MAIN_5V0"},
    "ic_refdes": {"type": "str", "required": True,
                  "meaning": "regulator reference designator, e.g. U86"},
    "ic_part": {"type": "str", "required": True,
                "meaning": "regulator manufacturer part number"},
    "vout_nominal_v": {"type": "float", "required": True,
                       "meaning": "nominal regulated output voltage (V)"},
    "rail_test_point": {"type": "str", "required": True,
                        "meaning": "scope probe pad for the rail"},
    "dc_tolerance_pct": {"type": "float", "required": False, "default": 3.0,
                         "meaning": "DC regulation window (±%)"},
    "ripple_limit_pct": {"type": "float", "required": False, "default": 2.0,
                         "meaning": "ripple AC-RMS limit as % of nominal"},
    "ripple_limit_mv": {"type": "float|null", "required": False, "default": None,
                        "meaning": "absolute ripple limit (mV); overrides pct"},
    "rise_time_limit_ms": {"type": "float", "required": False, "default": 10.0,
                           "meaning": "soft-start rise-time limit (ms)"},
    "gnd_label": {"type": "str", "required": False, "default": "GND",
                  "meaning": "scope GND reference label"},
    "psu": {
        "type": "object", "required": True,
        "fields": {
            "input_voltage_v": {"type": "float", "required": True,
                                "meaning": "board nominal input voltage (V)"},
            "input_current_a": {"type": "float", "required": True,
                                "meaning": "PSU current limit (A)"},
            "entry_pos": {"type": "str", "required": True,
                          "meaning": "PSU + lead board entry node, e.g. P4"},
            "entry_neg": {"type": "str", "required": True,
                          "meaning": "PSU - lead / return node, e.g. P2"},
        },
    },
    "enable": {
        "type": "object", "required": True,
        "fields": {
            "present": {"type": "bool", "required": True,
                        "meaning": "does the IC have an EN input?"},
            "always_on": {"type": "bool", "required": False, "default": True,
                          "meaning": "EN tied permanently active (no gate)?"},
            "control_target": {"type": "str|null", "required": False,
                               "default": None,
                               "meaning": "CONNECTOR-NET enable only: what is "
                               "commanded to assert EN (required when present, "
                               "not always_on, AND no controller_id/io_resource "
                               "— emitted as an Operator step)"},
            "controller_id": {"type": "str|null", "required": False,
                              "default": None,
                              "meaning": "CONTROLLER-DRIVEN enable: the FNCORE "
                              "controller Equipment ID driving the enable node "
                              "(e.g. IF_PLM). Set with io_resource to drive EN "
                              "via the controller grammar instead of an Operator "
                              "step"},
            "io_resource": {"type": "str|null", "required": False,
                            "default": None,
                            "meaning": "CONTROLLER-DRIVEN enable: the firmware "
                            "resource id of the enable node on the controller "
                            "(e.g. CMD_AUX0)"},
            "assert_value": {"type": "str", "required": False, "default": "1",
                             "meaning": "digital value '0'|'1' that ASSERTS the "
                             "enable; active-low (nCMD...) asserts at '0'"},
            "target": {"type": "str", "required": False, "default": "DSC",
                       "meaning": "FNCORE controller TARGET (MCU id) the "
                       "resource lives on; assume DSC when unknown"},
            "controller_subtype": {"type": "str", "required": False,
                                   "default": "fncore-mockup",
                                   "meaning": "controller subtype for the "
                                   "Equipment line"},
        },
    },
    "power_good": {
        "type": "object", "required": True,
        "fields": {
            "present": {"type": "bool", "required": True,
                        "meaning": "does the IC expose a PG output?"},
            "test_point": {"type": "str|null", "required": False,
                           "default": None,
                           "meaning": "scope probe pad for PG "
                           "(required when present)"},
            "nominal_v": {"type": "float", "required": False, "default": 3.3,
                          "meaning": "expected PG-high level (V)"},
            "tolerance_pct": {"type": "float", "required": False, "default": 10.0,
                              "meaning": "PG level tolerance (±%)"},
            "delay_limit_ms": {"type": "float", "required": False, "default": 10.0,
                               "meaning": "max output→PG-assert delay (ms)"},
        },
    },
    "scope": {
        "type": "object", "required": False,
        "fields": {
            "timebase_ms": {"type": "float", "required": False, "default": 10.0,
                            "meaning": "horizontal scale (ms/div)"},
            "ch1": {"type": "int", "required": False, "default": 1,
                    "meaning": "rail test-point channel"},
            "ch2": {"type": "int", "required": False, "default": 2,
                    "meaning": "power-good channel (when PG present)"},
        },
    },
}


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

class DcDcParamError(ValueError):
    """A required param is missing/blank, or a conditional field is absent.

    The skill surfaces this back to the LLM as 'fill this field'."""


def _require_str(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DcDcParamError(f"missing required field: {name}")
    return value.strip()


def _require_num(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DcDcParamError(f"missing required numeric field: {name}")
    return float(value)


def validate_params(p: DcDcTestParams) -> None:
    """Raise :class:`DcDcParamError` if any required field is missing/blank.

    Top-level required identity + the nested PSU/enable/power-good required
    fields are all checked, INCLUDING the conditional requireds:
    enable.control_target (when controllable) and power_good.test_point (when
    present). Defaulted numeric fields are not re-checked here."""
    _require_str(p.rail_name, "rail_name")
    _require_str(p.ic_refdes, "ic_refdes")
    _require_str(p.ic_part, "ic_part")
    _require_num(p.vout_nominal_v, "vout_nominal_v")
    _require_str(p.rail_test_point, "rail_test_point")

    if p.psu is None:
        raise DcDcParamError("missing required field: psu")
    _require_num(p.psu.input_voltage_v, "psu.input_voltage_v")
    _require_num(p.psu.input_current_a, "psu.input_current_a")
    _require_str(p.psu.entry_pos, "psu.entry_pos")
    _require_str(p.psu.entry_neg, "psu.entry_neg")

    if p.enable is None:
        raise DcDcParamError("missing required field: enable")
    if not isinstance(p.enable.present, bool):
        raise DcDcParamError("missing required field: enable.present")
    if p.enable.present and not p.enable.always_on:
        # Controllable enable: either an on-board CONTROLLER drives it
        # (controller_id + io_resource) OR it is a bare connector net (Operator,
        # named by control_target).
        if p.enable.controller_driven:
            av = p.enable.assert_value
            if av not in ("0", "1"):
                raise DcDcParamError(
                    "enable.assert_value must be '0' or '1' "
                    "(controller-driven enable)")
            _require_str(p.enable.io_resource,
                         "enable.io_resource (controller-driven enable)")
            _require_str(p.enable.target,
                         "enable.target (controller-driven enable)")
        else:
            # Connector-net controllable enable: we must know WHAT to command.
            _require_str(p.enable.control_target,
                         "enable.control_target (controllable enable)")

    if p.power_good is None:
        raise DcDcParamError("missing required field: power_good")
    if not isinstance(p.power_good.present, bool):
        raise DcDcParamError("missing required field: power_good.present")
    if p.power_good.present:
        _require_str(p.power_good.test_point,
                     "power_good.test_point (power-good present)")


# ---------------------------------------------------------------------------
# NUMBER FORMATTING
# ---------------------------------------------------------------------------
# The reference tests render whole numbers as e.g. "28.0 V" / "2.0 A" and
# "5.0 V". We follow the benchmark +MAIN_5V0 block: a single decimal place for
# the PSU/scale/voltage quantities, trimming to a clean fixed form.

def _num(v: float) -> str:
    """Render a number the way the reference power tests do.

    Integers and one-decimal values render with one decimal place (28.0, 2.5);
    finer values keep up to 3 decimals with trailing zeros trimmed."""
    f = float(v)
    if abs(f - round(f, 1)) < 1e-9:
        return f"{f:.1f}"
    s = f"{f:.3f}".rstrip("0")
    return s if not s.endswith(".") else s + "0"


def _ripple_limit_mv(p: "DcDcTestParams") -> float:
    """Resolve the ripple pass/fail limit in mV, ALWAYS strictly positive.

    Ripple is an AC-RMS magnitude (>= 0), so its limit can never be <= 0. We
    prefer an explicit absolute ``ripple_limit_mv`` (only when given AND > 0),
    else the percent form (``ripple_limit_pct`` % of ``vout_nominal_v``). If
    either source yields a non-positive number (bad/missing params), we fall
    back to the 2%-of-nominal DEFAULT, and finally to an absolute floor so a
    non-positive ripple bound can NEVER be emitted."""
    # 1) Absolute override, but only if it is a usable positive number.
    if p.ripple_limit_mv is not None and p.ripple_limit_mv > 0:
        return p.ripple_limit_mv
    # 2) Percent-of-nominal form (the default path).
    ripple_mv = p.vout_nominal_v * (p.ripple_limit_pct / 100.0) * 1000.0
    if ripple_mv > 0:
        return ripple_mv
    # 3) Bad/missing params -> 2%-of-nominal default off a sane nominal.
    nominal = p.vout_nominal_v if p.vout_nominal_v > 0 else 5.0
    ripple_mv = nominal * (2.0 / 100.0) * 1000.0
    # 4) Ultimate floor: never emit a non-positive ripple limit.
    return ripple_mv if ripple_mv > 0 else 100.0


def _pg_scale_v_per_div(nominal_v: float) -> float:
    """Pick a CH2 (power-good) V/div from the PG level, half-screen-ish."""
    return _pick_scale(nominal_v)


def _pick_scale(voltage: float) -> float:
    """Choose a vertical V/div from a DC voltage so the trace sits ~half-screen.

    Mirrors v0.13.0's examples: ~5 V → 2 V/div, 16–28 V → 5 V/div, 3.3 V → 1
    V/div. A ~8-division screen ⇒ target the value near mid-screen (~4 div)."""
    v = abs(float(voltage))
    if v <= 0:
        return 1.0
    if v <= 1.5:
        return 0.5
    if v <= 4.0:
        return 1.0
    if v <= 7.0:
        return 2.0
    if v <= 15.0:
        return 5.0
    if v <= 35.0:
        return 5.0
    return 10.0


# ---------------------------------------------------------------------------
# GENERATOR
# ---------------------------------------------------------------------------

def generate_dcdc_test(params: DcDcTestParams) -> str:
    """Emit the canonical ## Equipment / ## Steps / ## Expected procedure text.

    Follows v0.13.0 EXACTLY with the structural branches described in the module
    docstring. Returns pure-LF text (no trailing newline)."""
    validate_params(params)

    p = params
    pg = p.power_good.present
    controllable_en = p.enable.present and not p.enable.always_on
    # A controllable enable is CONTROLLER-DRIVEN when an on-board controller
    # backs it (controller_id + io_resource); otherwise it is a connector net
    # asserted by an Operator step.
    controller_en = controllable_en and p.enable.controller_driven

    ch1 = p.scope.ch1
    ch2 = p.scope.ch2
    rail = p.rail_name
    tp = p.rail_test_point
    gnd = p.gnd_label

    ch1_scale = _pick_scale(p.vout_nominal_v)
    ch2_scale = _pg_scale_v_per_div(p.power_good.nominal_v)
    trig_level = round(p.vout_nominal_v / 2.0, 3)

    # screenshot file stem from the rail name (strip a leading '+', lowercase).
    stem = rail.lstrip("+").lower()

    # Controller-drive grammar (fncore): assert / de-assert the enable node.
    #   Set <ID> <TARGET> <res> = '<0|1>'.   (single-quoted, trailing period)
    if controller_en:
        en_assert = p.enable.assert_value
        en_deassert = "0" if en_assert == "1" else "1"

        def en_drive(value: str) -> str:
            return (f"Set {p.enable.controller_id} {p.enable.target} "
                    f"{p.enable.io_resource} = '{value}'.")

    # ---- Equipment ----------------------------------------------------------
    scope_channels = f"[{ch1}, {ch2}]" if pg else f"[{ch1}]"
    equip = [
        f"PSU1 : psu channels=[{{1, max_voltage={_num(p.psu.input_voltage_v)} V, "
        f"max_current={_num(p.psu.input_current_a)} A}}]",
        f"SCOPE1 : scope channels={scope_channels}",
    ]
    # A controller-driven enable adds its FNCORE controller to the bench.
    if controller_en:
        equip.append(
            f"{p.enable.controller_id} : controller "
            f"subtype={p.enable.controller_subtype}")

    # ---- Steps --------------------------------------------------------------
    steps: list[str] = []

    def add(line: str) -> None:
        steps.append(f"{len(steps) + 1}. {line}")

    # measurement-ref allocator (the {N} tokens used in Expected)
    refs: dict[str, int] = {}
    _ref_ctr = [0]

    def ref(name: str) -> str:
        _ref_ctr[0] += 1
        refs[name] = _ref_ctr[0]
        return f"{{{_ref_ctr[0]}}}"

    # PSU set V/I then OFF FIRST, then wire (safety: never connect to a live rail)
    add(f"Set PSU1 CH1 voltage = {_num(p.psu.input_voltage_v)} V.")
    add(f"Set PSU1 CH1 current = {_num(p.psu.input_current_a)} A.")
    add("Set PSU1 CH1 output = OFF.")
    add(f"Connect PSU1 CH1 + to {p.psu.entry_pos}, - to {p.psu.entry_neg}.")
    add(f"Connect SCOPE1 CH{ch1} to TP {tp} (with 10:1 probe).")
    add(f"Connect SCOPE1 CH{ch1} reference to TP {gnd}.")
    if pg:
        add(f"Connect SCOPE1 CH{ch2} to TP {p.power_good.test_point} "
            f"(with 10:1 probe).")
        add(f"Connect SCOPE1 CH{ch2} reference to TP {gnd}.")

    # Scope channel setup — separate ops, in order: enable, atten=10, config.
    add(f"Configure SCOPE1 CH{ch1}: enabled=on.")
    add(f"Configure SCOPE1 CH{ch1}: probe_attenuation=10.")
    add(f"Configure SCOPE1 CH{ch1}: coupling=DC, offset=0.0 V, "
        f"scale={_num(ch1_scale)} V/div.")
    if pg:
        add(f"Configure SCOPE1 CH{ch2}: enabled=on.")
        add(f"Configure SCOPE1 CH{ch2}: probe_attenuation=10.")
        add(f"Configure SCOPE1 CH{ch2}: coupling=DC, offset=0.0 V, "
            f"scale={_num(ch2_scale)} V/div.")

    # timebase + trigger on CH1 rising at ~half rail.
    add(f"Configure SCOPE1 timebase: position=0.0 s, "
        f"scale={_num(p.scope.timebase_ms)} ms/div.")
    add(f"Configure SCOPE1 trigger: source=CH{ch1}, level={_num(trig_level)} V, "
        f"slope=rising, sweep=normal.")

    # If controllable enable: enable-off check BEFORE asserting it.
    if controllable_en:
        # Board powered, enable not yet asserted → VOUT must be OFF (<100 mV).
        add("Set PSU1 CH1 output = ON.")
        add("Wait 1 s.")
        if controller_en:
            # Controller-driven: explicitly DE-ASSERT the enable node first.
            add(en_drive(en_deassert))
            add("Wait 1 s.")
        add("Arm SCOPE1 single acquisition.")
        add("Wait 1 s.")
        add("Force SCOPE1 trigger.")
        add("Acquire SCOPE1 (timeout = 5 s, force_on_timeout = yes).")
        r_enoff = ref("enable_off")
        add(f"Measure SCOPE1 CH{ch1} mean voltage as {r_enoff}.")
        add(f"Save SCOPE1 screenshot as \"{stem}_enable_off.jpg\" "
            f"labeled \"{rail} enable off\" for {r_enoff}.")

    # Soft-start / rise time: arm, THEN the stimulus that brings the rail up.
    add("Arm SCOPE1 single acquisition.")
    add("Wait 1 s.")
    if controller_en:
        # Controller-driven: ASSERT the enable node via the FNCORE grammar.
        add(en_drive(en_assert))
    elif controllable_en:
        add(f"Operator: assert the enable ({p.enable.control_target}).")
    else:
        add("Set PSU1 CH1 output = ON.")
    add("Acquire SCOPE1 (timeout = 5 s, force_on_timeout = no).")
    r_rise = ref("rise_time")
    add(f"Measure SCOPE1 CH{ch1} rise time as {r_rise}.")
    if pg:
        r_delay = ref("pg_delay")
        add(f"Measure SCOPE1 delay between rising CH{ch1} and rising CH{ch2} "
            f"as {r_delay}.")
    add(f"Save SCOPE1 screenshot as \"{stem}_softstart.jpg\" "
        f"labeled \"{rail} soft-start\" for {r_rise}.")

    # DC level (+ PG level if present): force a trigger, acquire, measure mean.
    add("Arm SCOPE1 single acquisition.")
    add("Wait 1 s.")
    add("Force SCOPE1 trigger.")
    add("Acquire SCOPE1 (timeout = 5 s, force_on_timeout = yes).")
    r_dc = ref("dc_mean")
    add(f"Measure SCOPE1 CH{ch1} mean voltage as {r_dc}.")
    if pg:
        r_pg = ref("pg_mean")
        add(f"Measure SCOPE1 CH{ch2} mean voltage as {r_pg}.")
    add(f"Save SCOPE1 screenshot as \"{stem}_dc.jpg\" "
        f"labeled \"{rail} DC level\" for {r_dc}.")

    # Ripple: CH1 AC coupling, auto-scale, acquire, RMS.
    add(f"Configure SCOPE1 CH{ch1}: coupling=AC.")
    add(f"Auto-scale SCOPE1 CH{ch1}.")
    add("Arm SCOPE1 single acquisition.")
    add("Wait 1 s.")
    add("Force SCOPE1 trigger.")
    add("Acquire SCOPE1 (timeout = 5 s, force_on_timeout = yes).")
    r_ripple = ref("ripple_rms")
    add(f"Measure SCOPE1 CH{ch1} RMS voltage as {r_ripple}.")
    add(f"Save SCOPE1 screenshot as \"{stem}_ripple.jpg\" "
        f"labeled \"{rail} ripple\" for {r_ripple}.")

    # Finish with the PSU output OFF.
    add("Set PSU1 CH1 output = OFF.")

    # ---- Expected -----------------------------------------------------------
    exp: list[str] = []
    if controllable_en:
        exp.append(f"{{{refs['enable_off']}}} < 100 mV")
    exp.append(f"{{{refs['rise_time']}}} <= {_num(p.rise_time_limit_ms)} ms")
    if pg:
        exp.append(
            f"{{{refs['pg_delay']}}} <= {_num(p.power_good.delay_limit_ms)} ms")
    exp.append(
        f"{{{refs['dc_mean']}}} = {_num(p.vout_nominal_v)} V "
        f"+/- {_num(p.dc_tolerance_pct)} %")
    if pg:
        exp.append(
            f"{{{refs['pg_mean']}}} = {_num(p.power_good.nominal_v)} V "
            f"+/- {_num(p.power_good.tolerance_pct)} %")
    exp.append(
        f"{{{refs['ripple_rms']}}} <= {_num(_ripple_limit_mv(p))} mV")

    # ---- Assemble -----------------------------------------------------------
    out = []
    out.append("## Equipment")
    out.extend(equip)
    out.append("")
    out.append("## Steps")
    out.extend(steps)
    out.append("")
    out.append("## Expected")
    out.extend(exp)
    return "\n".join(out)


if __name__ == "__main__":
    demo = DcDcTestParams(
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
    print(generate_dcdc_test(demo))
