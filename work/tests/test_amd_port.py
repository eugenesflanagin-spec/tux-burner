"""Local AMD port: sysfs parsing, plan validation, dry-run rendering, profile
round trip and the VDDGFX ladder plan - all against a fake sysfs tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_uv.amd_sweep import LadderSettings, describe_ladder, plan_ladder, run_ladder, tier_plans
from drivers.amdgpu import plan as amd_plan
from drivers.amdgpu.plan import (
    AmdPlanError,
    AmdTuningPlan,
    current_plan,
    parse_plan_assignments,
    plan_profile_payload,
    render_dry_run,
    sysfs_writes,
    validate_plan,
)
from drivers.amdgpu.sysfs import (
    discover_amd_gpus,
    lact_gpu_config_id,
    parse_dpm_levels,
    parse_fan_curve,
    parse_od_table,
)
from integrations.lact.export import LactExportError, build_lact_amd_config

OD_TEXT = (
    "OD_SCLK_OFFSET:\n300Mhz\nOD_MCLK:\n0: 676Mhz\n1: 1375MHz\nOD_RANGE:\n"
    "SCLK_OFFSET:    -500Mhz       1000Mhz\nMCLK:      97Mhz       1500Mhz\n"
    "VDDGFX_OFFSET:    -200mv          0mv\nOD_VDDGFX_OFFSET:\n-110mV\n"
)
FAN_TEXT = (
    "OD_FAN_CURVE:\n0: 25C 15%\n1: 39C 19%\n2: 56C 60%\n3: 70C 85%\n4: 80C 100%\n"
    "OD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\nFAN_CURVE(fan speed): 15% 100%\n"
)


@pytest.fixture
def fake_card(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv(amd_plan.VOLTAGE_FLOOR_ENV, raising=False)
    device = tmp_path / "card0" / "device"
    hwmon = device / "hwmon" / "hwmon11"
    hwmon.mkdir(parents=True)
    (device / "gpu_od" / "fan_ctrl").mkdir(parents=True)
    (tmp_path / "card0-DP-1" / "device").mkdir(parents=True)
    other = tmp_path / "card1" / "device"
    other.mkdir(parents=True)
    (other / "vendor").write_text("0x10de\n")
    (other / "pp_od_clk_voltage").write_text("")
    files = {
        "vendor": "0x1002\n",
        "device": "0x7550\n",
        "subsystem_vendor": "0x1eae\n",
        "subsystem_device": "0x8811\n",
        "uevent": "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n",
        "unique_id": "0123456789abcdef\n",
        "pp_od_clk_voltage": OD_TEXT,
        "gpu_od/fan_ctrl/fan_curve": FAN_TEXT,
        "gpu_od/fan_ctrl/fan_zero_rpm_enable": "FAN_ZERO_RPM_ENABLE:\n0\nOD_RANGE:\nZERO_RPM_ENABLE: 0 1\n",
        "gpu_od/fan_ctrl/fan_minimum_pwm": "FAN_MINIMUM_PWM:\n15\nOD_RANGE:\nMINIMUM_PWM: 15 100\n",
        "pp_dpm_sclk": "S: 0Mhz *\n1: 500Mhz \n2: 2400Mhz \n",
        "pp_dpm_mclk": "0: 96Mhz \n1: 456Mhz \n2: 772Mhz *\n3: 875Mhz \n4: 1124Mhz \n5: 1258Mhz \n",
        "gpu_busy_percent": "6\n",
        "power_dpm_force_performance_level": "manual\n",
        "mem_info_vram_total": "17095983104\n",
        "mem_info_vram_used": "1000000000\n",
    }
    for name, text in files.items():
        (device / name).write_text(text)
    hw = {
        "power1_cap": "374000000\n",
        "power1_cap_min": "212000000\n",
        "power1_cap_max": "374000000\n",
        "power1_cap_default": "304000000\n",
        "power1_average": "33000000\n",
        "temp1_label": "edge\n",
        "temp1_input": "27000\n",
        "temp2_label": "junction\n",
        "temp2_input": "31000\n",
        "in0_input": "700\n",
        "freq1_input": "63000000\n",
        "freq2_input": "772000000\n",
        "fan1_input": "589\n",
        "pwm1": "40\n",
        "pwm1_max": "255\n",
    }
    for name, text in hw.items():
        (hwmon / name).write_text(text)
    return tmp_path


def test_parsers_match_live_rx_9070_xt_text() -> None:
    od = parse_od_table(OD_TEXT)
    assert od.sclk_offset_mhz == 300
    assert (od.mclk_min_mhz, od.mclk_max_mhz) == (676, 1375)
    assert od.vddgfx_offset_mv == -110
    assert od.range_sclk_offset_mhz == (-500, 1000)
    assert od.range_mclk_mhz == (97, 1500)
    assert od.range_vddgfx_offset_mv == (-200, 0)
    fan = parse_fan_curve(FAN_TEXT)
    assert fan.points == ((25, 15), (39, 19), (56, 60), (70, 85), (80, 100))
    assert fan.temp_range_c == (25, 100)
    assert fan.speed_range_pct == (15, 100)
    assert parse_dpm_levels("S: 0Mhz *\n1: 500Mhz \n2: 2400Mhz \n") == (500, 2400)
    assert lact_gpu_config_id(0x1002, 0x7550, (0x1EAE, 0x8811), "0000:03:00.0") == "1002:7550-1EAE:8811-0000:03:00.0"


def test_discovery_and_state(fake_card: Path) -> None:
    gpus = discover_amd_gpus(fake_card)
    assert [(g.index, g.card, g.pci_addr) for g in gpus] == [(0, "card0", "0000:03:00.0")]
    state = gpus[0].read_state()
    assert state.uuid == "AMD-0123456789abcdef"
    assert state.pci_device_id == "0x75501002"
    assert state.stock_mclk_max_mhz == 1258
    assert state.power_cap_w == 374 and state.power_cap_default_w == 304
    assert state.temps_c["junction"] == 31.0
    assert state.fan_pwm_pct == 16
    assert current_plan(state).describe().startswith("VDDGFX -110 mV, SCLK +300 MHz, MCLK max 1375 MHz, power 374 W")


def test_plan_parsing_validation_and_dry_run(fake_card: Path) -> None:
    state = discover_amd_gpus(fake_card)[0].read_state()
    plan = parse_plan_assignments(["vo=-100", "sclk=+250", "mclk=1300", "power=123%"], state=state)
    assert plan == AmdTuningPlan(voltage_offset_mv=-100, sclk_offset_mhz=250, mclk_max_mhz=1300, power_cap_w=374)
    assert validate_plan(plan, state) == []
    writes = sysfs_writes(plan, state)
    od = str(state.od_path)
    assert writes == [
        (od, "m 1 1300"),
        (od, "s 250"),
        (od, "vo -100"),
        (od, "c"),
        (str(state.power_cap_path), "374000000"),
    ]
    text = render_dry_run(plan, state)
    assert "nothing was written" in text and "echo 'vo -100'" in text
    # Unchanged plan -> no writes.
    from dataclasses import replace

    assert sysfs_writes(replace(current_plan(state), fan_curve=None, zero_rpm=None), state) == []

    bad = parse_plan_assignments(["vo=-250", "sclk=1500", "power=400", "fan=25:10,20:50"], state=state)
    problems = validate_plan(bad, state)
    assert any("outside OD_RANGE -200..0 mV" in p for p in problems)
    assert any("SCLK offset 1500 MHz is outside" in p for p in problems)
    assert any("power cap 400 W is outside" in p for p in problems)
    assert any("fan point 10% is outside" in p for p in problems)
    assert any("strictly increasing" in p for p in problems)
    with pytest.raises(AmdPlanError):
        parse_plan_assignments(["bogus=1"], state=state)
    with pytest.raises(AmdPlanError):
        parse_plan_assignments(["vo=abc"], state=state)


def test_voltage_floor_defaults_to_minus_110_and_is_configurable(fake_card: Path, monkeypatch) -> None:
    state = discover_amd_gpus(fake_card)[0].read_state()
    deep = AmdTuningPlan(voltage_offset_mv=-160)
    assert any("below the safety floor -150 mV" in p for p in validate_plan(deep, state))
    assert validate_plan(AmdTuningPlan(voltage_offset_mv=-110), state) == []
    assert validate_plan(AmdTuningPlan(voltage_offset_mv=-150), state) == []
    assert validate_plan(deep, state, voltage_floor_mv=-170) == []
    monkeypatch.setenv(amd_plan.VOLTAGE_FLOOR_ENV, "-170")
    assert validate_plan(deep, state) == []
    assert any("overvolt" in p for p in validate_plan(AmdTuningPlan(voltage_offset_mv=5), state))


def test_ladder_plan_respects_floor_and_marker(fake_card: Path, monkeypatch) -> None:
    state = discover_amd_gpus(fake_card)[0].read_state()
    offsets = plan_ladder(state, LadderSettings())
    # Default: explore down to the configured floor (-150) with no flags.
    assert offsets == list(range(0, -151, -10))
    # Requesting -200 cannot go below the safety floor.
    assert plan_ladder(state, LadderSettings(floor_mv=-200)) == offsets
    assert plan_ladder(state, LadderSettings(), unsafe_from_marker=-80) == list(range(0, -71, -10))
    monkeypatch.setenv(amd_plan.VOLTAGE_FLOOR_ENV, "-170")
    assert plan_ladder(state, LadderSettings())[-1] == -170
    monkeypatch.delenv(amd_plan.VOLTAGE_FLOOR_ENV)
    text = describe_ladder(state, LadderSettings(), offsets)
    assert "safety floor" in text and "range: +0 -> -150 mV" in text
    tiers = tier_plans(-60, state, LadderSettings(sclk_offset_mhz=200, margin_mv=20))
    assert tiers["efficiency"] == AmdTuningPlan(voltage_offset_mv=-40, power_cap_w=304)
    assert tiers["balanced"] == AmdTuningPlan(voltage_offset_mv=-40, power_cap_w=374)
    assert tiers["performance"] == AmdTuningPlan(voltage_offset_mv=-40, sclk_offset_mhz=200, power_cap_w=374)
    # Suggested tiers never go below -110 even when the ladder passed -150.
    assert tier_plans(-150, state, LadderSettings(margin_mv=20))["balanced"].voltage_offset_mv == -110


def test_ladder_run_stops_at_first_unstable_rung(fake_card: Path, tmp_path: Path) -> None:
    state = discover_amd_gpus(fake_card)[0].read_state()
    applied: list[int] = []
    outcomes = {0: 0, -10: 0, -20: 3}
    events: list[str] = []
    result = run_ladder(
        state,
        LadderSettings(rung_seconds=1),
        apply_offset=applied.append,
        run_stress=lambda _seconds: (outcomes[applied[-1]], "opencl-bruteforce: UNSTABLE redundant verification mismatch int.x=7" if outcomes[applied[-1]] == 3 else "", []),
        log=events.append,
        marker_path=tmp_path / "marker.json",
    )
    assert applied == [0, -10, -20]
    assert result.deepest_stable_mv == -10
    assert result.first_unstable_mv == -20
    assert result.rungs[-1].failure_type == "opencl-mismatch"
    assert "int.x=7" in result.rungs[-1].detail
    assert not result.aborted
    assert not (tmp_path / "marker.json").exists()
    assert any(line.startswith("@amd ") and '"event":"rung_result"' in line for line in events)
    assert any('"event":"ladder_end"' in line for line in events)


def test_profile_payload_round_trips_through_loader_and_runtime_spec(fake_card: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path / "home"))
    from common.atomic_write import atomic_write_json
    from profiles.uv.profile_store import auto_uv_profiles_dir, profile_display_name, read_auto_uv_profile_summaries
    from profiles.uv.runtime_auto_uv_profile import load_auto_uv_final_curve
    from runtime.runtime_spec import _profile_spec

    state = discover_amd_gpus(fake_card)[0].read_state()
    plan = AmdTuningPlan(voltage_offset_mv=-90, sclk_offset_mhz=100, mclk_max_mhz=1300, power_cap_w=340)
    payload = plan_profile_payload(plan, state, name="quiet", tier="balanced", verified=True, verification={"method": "test"})
    assert payload["amd"] == {"voltage_offset_mv": -90, "sclk_offset_mhz": 100, "mclk_max_mhz": 1300}
    assert payload["plan"] == [] and payload["candidate_voltage_mv"] == 0
    atomic_write_json(auto_uv_profiles_dir() / f"auto-uv-profile-{payload['profile_id']}.json", payload)
    summaries = read_auto_uv_profile_summaries()
    assert [s["profile_id"] for s in summaries] == ["amd-quiet"]
    assert profile_display_name(summaries[0]) == "VDDGFX -90 mV, SCLK +100 MHz, MCLK 1300 MHz, 340 W"
    curve = load_auto_uv_final_curve("amd-quiet")
    assert curve["amd"] == payload["amd"] and curve["plan"] == []
    spec = _profile_spec(curve)
    # The daemon's RuntimeProfile is deny_unknown_fields: exactly these keys.
    assert set(spec) == {
        "path", "profile_id", "profile_tier", "profile_tier_key", "plan", "lock_clock_mhz",
        "candidate_voltage_mv", "memory_offset_mhz", "power_limit_w", "amd", "flatten_target",
    }
    assert set(spec["flatten_target"]) == {
        "source", "lock_clock_mhz", "lock_voltage_mv", "end_voltage_mv", "tail_point_count",
        "ceiling_clock_mhz", "tail_rise_bins",
    }
    json.dumps(spec)


def test_lact_amd_export_enforces_curve_limits(fake_card: Path) -> None:
    plan = AmdTuningPlan(
        voltage_offset_mv=-110,
        sclk_offset_mhz=300,
        mclk_max_mhz=1375,
        power_cap_w=374,
        fan_curve=((25, 15), (39, 19), (56, 60), (70, 85), (80, 100)),
    )
    text, warnings = build_lact_amd_config(gpu_id="1002:7550-1EAE:8811-0000:03:00.0", plan=plan, source_label="test")
    assert "voltage_offset: -110" in text and "max_memory_clock: 1375" in text and "power_cap: 374.0" in text
    assert "temperature_key: junction" in text and "        80: 1\n" in text and warnings == []
    low = AmdTuningPlan(fan_curve=((25, 5), (80, 100)))
    _text, warnings = build_lact_amd_config(gpu_id="x", plan=low, source_label="test")
    assert warnings and "firmware minimum 0.15" in warnings[0]
    six = AmdTuningPlan(fan_curve=tuple((25 + 10 * i, 20 + 10 * i) for i in range(6)))
    with pytest.raises(LactExportError):
        build_lact_amd_config(gpu_id="x", plan=six, source_label="test")


def test_cuda_companion_dispatches_to_opencl_on_amd(fake_card: Path, monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNERD_AMDGPU_SYSFS_ROOT", str(fake_card))
    from stability.q2rtx.cuda_companion import cuda_bruteforce_companion_command

    command = cuda_bruteforce_companion_command(gpu_index=0, duration_s=30)
    assert command[1].endswith("opencl_bruteforce.py")
    from stability import cuda_bruteforce, opencl_bruteforce

    assert opencl_bruteforce.OPENCL_INSTABILITY_EXIT_CODES == cuda_bruteforce.CUDA_INSTABILITY_EXIT_CODES
    # The OpenCL kernel's reference chain must stay bit-identical to CUDA's.
    assert opencl_bruteforce._cpu_reference(7, 256, 1, 2) == cuda_bruteforce._cpu_reference(7, 256, 1, 2)
