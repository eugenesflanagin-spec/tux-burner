"""The AMD tuning plan: what the user wants, validated against live ranges.

A plan replaces the NVIDIA per-point V/F curve with the four knobs RDNA3/RDNA4
OverDrive actually has:

================  ==============================  ===========================
NVIDIA concept    AMD knob                        sysfs write (root)
================  ==============================  ===========================
V/F curve point   global VDDGFX offset (mV)       ``vo -110`` + ``c``
core clock offset SCLK offset (MHz)               ``s 300`` + ``c``
memory offset     MCLK max (absolute MHz)         ``m 1 1375`` + ``c``
power limit %     power cap (W, min..max)         ``power1_cap`` in uW
fan curve         5-point PMFW hotspot curve      ``gpu_od/fan_ctrl/fan_curve``
================  ==============================  ===========================

``render_dry_run`` prints exactly those writes without performing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime

from .sysfs import AmdGpuState

MAX_FAN_CURVE_POINTS = 5
# Safety floor for the VDDGFX offset: the deepest undervolt Tux Burner will
# plan, write or sweep to by default (-150 mV, the user's exploration limit on
# the machine this port was built on; the validated daily value is -110 mV). It is configurable, not a hard clamp: the
# user lowers it deliberately with --amd-voltage-floor plus --allow-below-floor
# (CLI side) and /etc/tux-burner/amd-vddgfx-floor-mv (daemon side).
DEFAULT_VOLTAGE_OFFSET_FLOOR_MV = -150
# Nothing the app SUGGESTS (tiers, presets, Restore Defaults) goes below this,
# even when the ladder explored deeper: -110 mV is the validated daily value.
SUGGESTION_FLOOR_MV = -110
VOLTAGE_FLOOR_ENV = "TUX_BURNER_AMD_VDDGFX_FLOOR_MV"
DAEMON_VOLTAGE_FLOOR_FILE = "/etc/tux-burner/amd-vddgfx-floor-mv"


def voltage_offset_floor_mv(override: int | None = None) -> int:
    import os

    if override is not None:
        return min(0, int(override))
    text = os.environ.get(VOLTAGE_FLOOR_ENV, "").strip()
    try:
        return min(0, int(text)) if text else DEFAULT_VOLTAGE_OFFSET_FLOOR_MV
    except ValueError:
        return DEFAULT_VOLTAGE_OFFSET_FLOOR_MV
PLAN_KEYS = {
    "vo": "voltage_offset_mv",
    "voltage": "voltage_offset_mv",
    "voltage_offset": "voltage_offset_mv",
    "sclk": "sclk_offset_mhz",
    "core": "sclk_offset_mhz",
    "clock": "sclk_offset_mhz",
    "mclk": "mclk_max_mhz",
    "mem": "mclk_max_mhz",
    "memory": "mclk_max_mhz",
    "power": "power_cap_w",
    "pl": "power_cap_w",
    "cap": "power_cap_w",
    "fan": "fan_curve",
    "zero_rpm": "zero_rpm",
}
_PERCENT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")


class AmdPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AmdTuningPlan:
    voltage_offset_mv: int = 0
    sclk_offset_mhz: int = 0
    mclk_max_mhz: int | None = None
    power_cap_w: int | None = None
    fan_curve: tuple[tuple[int, int], ...] | None = None
    zero_rpm: bool | None = None

    def describe(self) -> str:
        parts = [f"VDDGFX {self.voltage_offset_mv:+d} mV", f"SCLK {self.sclk_offset_mhz:+d} MHz"]
        if self.mclk_max_mhz is not None:
            parts.append(f"MCLK max {self.mclk_max_mhz} MHz")
        if self.power_cap_w is not None:
            parts.append(f"power {self.power_cap_w} W")
        if self.fan_curve:
            parts.append("fan " + " ".join(f"{t}C:{p}%" for t, p in self.fan_curve))
        if self.zero_rpm is not None:
            parts.append(f"zero-rpm {'on' if self.zero_rpm else 'off'}")
        return ", ".join(parts)


def current_plan(state: AmdGpuState) -> AmdTuningPlan:
    """What the card is running right now, as a plan (for ``--amd-status``)."""
    return AmdTuningPlan(
        voltage_offset_mv=int(state.od.vddgfx_offset_mv or 0),
        sclk_offset_mhz=int(state.od.sclk_offset_mhz or 0),
        mclk_max_mhz=state.od.mclk_max_mhz,
        power_cap_w=state.power_cap_w,
        fan_curve=state.fan_curve.points or None,
        zero_rpm=None if state.fan_zero_rpm is None else bool(state.fan_zero_rpm),
    )


def parse_fan_curve_text(text: str) -> tuple[tuple[int, int], ...]:
    points: list[tuple[int, int]] = []
    for item in re.split(r"[,\s]+", str(text).strip()):
        if not item:
            continue
        temp, sep, speed = item.partition(":")
        if not sep:
            raise AmdPlanError(f"fan curve point {item!r} must be TEMP:PERCENT")
        try:
            points.append((int(temp), int(speed.rstrip("%"))))
        except ValueError as exc:
            raise AmdPlanError(f"fan curve point {item!r} must be integers") from exc
    return tuple(points)


def parse_plan_assignments(
    assignments: list[str] | tuple[str, ...],
    *,
    state: AmdGpuState | None = None,
    base: AmdTuningPlan | None = None,
) -> AmdTuningPlan:
    """``vo=-110 sclk=+300 mclk=1375 power=374`` (or ``power=110%``) → plan."""
    plan = base or AmdTuningPlan()
    for raw in assignments:
        key, sep, value = str(raw).partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not sep or key not in PLAN_KEYS:
            raise AmdPlanError(
                f"unknown plan assignment {raw!r}; use vo=, sclk=, mclk=, power=, fan=, zero_rpm="
            )
        field_name = PLAN_KEYS[key]
        if field_name == "fan_curve":
            plan = replace(plan, fan_curve=parse_fan_curve_text(value))
            continue
        if field_name == "zero_rpm":
            plan = replace(plan, zero_rpm=value.lower() in {"1", "on", "true", "yes"})
            continue
        if field_name == "power_cap_w":
            percent = _PERCENT_RE.match(value)
            if percent:
                default = state.power_cap_default_w if state is not None else None
                if not default:
                    raise AmdPlanError("power=NN% needs the card's default power cap; read it first")
                plan = replace(plan, power_cap_w=round(default * float(percent.group(1)) / 100.0))
                continue
        try:
            number = int(value.rstrip("mMvVhHzZwW%"))
        except ValueError as exc:
            raise AmdPlanError(f"{key}= expects an integer, got {value!r}") from exc
        plan = replace(plan, **{field_name: number})
    return plan


def validate_plan(
    plan: AmdTuningPlan,
    state: AmdGpuState,
    *,
    voltage_floor_mv: int | None = None,
) -> list[str]:
    """Human-readable problems; empty means the plan fits the live OD_RANGE
    and the configurable VDDGFX safety floor (default -150 mV)."""
    errors: list[str] = []
    od = state.od
    floor = voltage_offset_floor_mv(voltage_floor_mv)
    if plan.voltage_offset_mv < floor:
        errors.append(
            f"VDDGFX offset {plan.voltage_offset_mv} mV is below the safety floor {floor} mV; "
            "lower the floor deliberately with --amd-voltage-floor <mV> --allow-below-floor "
            f"(and {DAEMON_VOLTAGE_FLOOR_FILE} for the daemon)"
        )
    lo_hi = od.range_vddgfx_offset_mv
    if lo_hi is None:
        errors.append("the driver reports no VDDGFX_OFFSET range (OverDrive voltage offset unsupported)")
    elif not lo_hi[0] <= plan.voltage_offset_mv <= lo_hi[1]:
        errors.append(
            f"VDDGFX offset {plan.voltage_offset_mv} mV is outside OD_RANGE {lo_hi[0]}..{lo_hi[1]} mV"
        )
    if plan.voltage_offset_mv > 0:
        errors.append("a positive VDDGFX offset is an overvolt; Tux Burner only undervolts (<= 0 mV)")
    if plan.sclk_offset_mhz != 0 or od.supports_sclk_offset:
        rng = od.range_sclk_offset_mhz
        if rng is None:
            errors.append(
                "this GPU exposes OD_SCLK min/max instead of OD_SCLK_OFFSET; SCLK offsets are not supported here"
            )
        elif not rng[0] <= plan.sclk_offset_mhz <= rng[1]:
            errors.append(f"SCLK offset {plan.sclk_offset_mhz} MHz is outside OD_RANGE {rng[0]}..{rng[1]} MHz")
    if plan.mclk_max_mhz is not None:
        rng = od.range_mclk_mhz
        if rng is None:
            errors.append("the driver reports no MCLK range")
        elif not rng[0] <= plan.mclk_max_mhz <= rng[1]:
            errors.append(f"MCLK max {plan.mclk_max_mhz} MHz is outside OD_RANGE {rng[0]}..{rng[1]} MHz")
        if od.mclk_min_mhz is not None and plan.mclk_max_mhz < od.mclk_min_mhz:
            errors.append(f"MCLK max {plan.mclk_max_mhz} MHz is below the current MCLK min {od.mclk_min_mhz} MHz")
    if plan.power_cap_w is not None:
        lo, hi = state.power_cap_min_w, state.power_cap_max_w
        if lo is None or hi is None:
            errors.append("hwmon power1_cap_min/max are unavailable; the power cap cannot be validated")
        elif not lo <= plan.power_cap_w <= hi:
            errors.append(f"power cap {plan.power_cap_w} W is outside power1_cap_min..max {lo}..{hi} W")
    if plan.fan_curve is not None:
        errors.extend(validate_fan_curve(plan.fan_curve, state))
    return errors


def validate_fan_curve(points: tuple[tuple[int, int], ...], state: AmdGpuState) -> list[str]:
    errors: list[str] = []
    if not 2 <= len(points) <= MAX_FAN_CURVE_POINTS:
        errors.append(
            f"the PMFW fan curve takes 2..{MAX_FAN_CURVE_POINTS} points, got {len(points)} "
            "(more than 5 makes the whole OverDrive apply fail)"
        )
    temp_range = state.fan_curve.temp_range_c or (25, 100)
    speed_range = state.fan_curve.speed_range_pct or (state.fan_minimum_pwm or 0, 100)
    last_temp = last_speed = None
    for temp, speed in points:
        if not temp_range[0] <= temp <= temp_range[1]:
            errors.append(f"fan point {temp}C is outside the hotspot range {temp_range[0]}..{temp_range[1]}C")
        if not speed_range[0] <= speed <= speed_range[1]:
            errors.append(
                f"fan point {speed}% is outside the fan range {speed_range[0]}..{speed_range[1]}% "
                "(the firmware minimum is a hard floor; a lower point is rejected)"
            )
        if last_temp is not None and temp <= last_temp:
            errors.append("fan curve temperatures must be strictly increasing")
        if last_speed is not None and speed < last_speed:
            errors.append("fan curve speeds must not decrease")
        last_temp, last_speed = temp, speed
    return errors


def sysfs_writes(plan: AmdTuningPlan, state: AmdGpuState, *, include_unchanged: bool = False) -> list[tuple[str, str]]:
    """Ordered ``(path, value)`` pairs the daemon performs for this plan."""
    od_path = str(state.od_path)
    writes: list[tuple[str, str]] = []
    od = state.od
    od_dirty = False
    if plan.mclk_max_mhz is not None and (include_unchanged or plan.mclk_max_mhz != od.mclk_max_mhz):
        writes.append((od_path, f"m 1 {plan.mclk_max_mhz}"))
        od_dirty = True
    if od.supports_sclk_offset and (include_unchanged or plan.sclk_offset_mhz != (od.sclk_offset_mhz or 0)):
        writes.append((od_path, f"s {plan.sclk_offset_mhz}"))
        od_dirty = True
    if include_unchanged or plan.voltage_offset_mv != (od.vddgfx_offset_mv or 0):
        writes.append((od_path, f"vo {plan.voltage_offset_mv}"))
        od_dirty = True
    if od_dirty:
        writes.append((od_path, "c"))
    if plan.fan_curve is not None:
        fan_path = str(state.fan_curve_path)
        for index, (temp, speed) in enumerate(plan.fan_curve):
            writes.append((fan_path, f"{index} {temp} {speed}"))
        writes.append((fan_path, "c"))
    if plan.zero_rpm is not None:
        zero_path = str(state.device_dir / "gpu_od" / "fan_ctrl" / "fan_zero_rpm_enable")
        writes.append((zero_path, "1" if plan.zero_rpm else "0"))
        writes.append((zero_path, "c"))
    if plan.power_cap_w is not None and state.power_cap_path is not None:
        if include_unchanged or plan.power_cap_w != state.power_cap_w or od_dirty or plan.fan_curve is not None:
            # Last: an OverDrive commit can reset power1_cap on SMU14, so the
            # cap is (re)written after every commit, exactly as the daemon does.
            writes.append((str(state.power_cap_path), str(plan.power_cap_w * 1_000_000)))
    return writes


def render_dry_run(plan: AmdTuningPlan, state: AmdGpuState) -> str:
    lines = [
        f"# Tux Burner AMD dry run for {state.name} ({state.pci_addr}) - nothing was written",
        f"# plan: {plan.describe()}",
        "# current: " + current_plan(state).describe(),
        "# as root, the daemon would perform these sysfs writes in this order:",
    ]
    writes = sysfs_writes(plan, state)
    if not writes:
        lines.append("# (the card already runs this plan; no writes needed)")
    for path, value in writes:
        lines.append(f"echo '{value}' > {path}")
    return "\n".join(lines)


def plan_profile_payload(
    plan: AmdTuningPlan,
    state: AmdGpuState,
    *,
    name: str,
    tier: str = "",
    verified: bool = False,
    verification: dict | None = None,
    manual: bool = True,
) -> dict:
    """A saved-profile JSON the daemon can apply (``amd`` block, empty plan).

    ``verified`` marks a ladder-verified profile. A ``manual`` save (the user
    typed the numbers) is also applyable - the Profiles page lists only
    applyable or user-edited profiles - but is labelled "manual" instead of
    "verified" so nobody mistakes it for a stress-tested result.
    """
    now = datetime.now().astimezone().isoformat()
    applyable = bool(verified or manual)
    stock_sclk = state.stock_sclk_max_mhz or 0
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-") or "amd"
    profile_id = f"amd-{safe_name}"
    payload = {
        "format_version": 1,
        "profile_id": profile_id,
        "candidate_id": profile_id,
        "display_name": f"{name.strip()} - {plan.describe()}" + ("" if verified else " (manual)"),
        "profile_source": "auto-uv-final" if verified else "amd-manual",
        "profile_created_at": now,
        "gpu_identity": {
            "name": state.name,
            "uuid": state.uuid,
            "pci_bus_id": state.pci_addr,
            "pci_device_id": state.pci_device_id,
            "index_at_verification": state.index,
        },
        # Informational for the NVIDIA-shaped UI columns: the amdgpu boost
        # ceiling moves by the SCLK offset; there is no candidate voltage.
        "lock_clock_mhz": max(1, stock_sclk + plan.sclk_offset_mhz),
        "candidate_voltage_mv": 0,
        "points": [],
        "plan": [],
        "memory_offset_mhz": None,
        "power_limit_w": plan.power_cap_w,
        "amd": {
            "voltage_offset_mv": int(plan.voltage_offset_mv),
            "sclk_offset_mhz": int(plan.sclk_offset_mhz),
            **({"mclk_max_mhz": int(plan.mclk_max_mhz)} if plan.mclk_max_mhz is not None else {}),
        },
        "amd_fan_curve": [list(point) for point in plan.fan_curve] if plan.fan_curve else None,
        "flatten_target": {
            "source": "amdgpu",
            "lock_clock_mhz": max(1, stock_sclk + plan.sclk_offset_mhz),
        },
        "final_verified": applyable,
        "requires_verification": not applyable,
        "verification_status": "verified (OpenCL ladder only)" if verified else ("manual" if manual else "unverified"),
        "manual_edit": None if verified else {"source": "amd-plan", "note": "typed by the user, not stress-tested"},
    }
    if tier:
        payload["profile_tier"] = tier
        payload["auto_uv_requested_mode"] = tier
    if verification:
        payload["verification"] = dict(verification)
        payload["verified_at"] = now
    return payload
