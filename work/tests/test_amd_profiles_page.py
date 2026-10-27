"""Local AMD port, round 4: the Profiles page with amdgpu profiles - listing
(including pre-round-3 manual saves), AMD-aware cells, rename, the apply route
and the LACT gate - driven offscreen against a temp HOME."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from drivers.amdgpu.plan import AmdTuningPlan, plan_profile_payload
from drivers.amdgpu.sysfs import LactStatus, discover_amd_gpus

from amd_fixtures import make_fake_card

LEGACY_MANUAL = {
    "format_version": 1,
    "profile_id": "amd-Performance",
    "candidate_id": "amd-Performance",
    "display_name": "Performance - VDDGFX -110 mV, SCLK +300 MHz, MCLK max 1375 MHz, power 374 W",
    "profile_source": "amd-manual",
    "profile_created_at": "2026-10-14T13:43:21-04:00",
    "gpu_identity": {"name": "AMD Navi 48", "uuid": "AMD-0123456789abcdef", "pci_bus_id": "0000:03:00.0", "index_at_verification": 0},
    "lock_clock_mhz": 2700,
    "candidate_voltage_mv": 0,
    "points": [],
    "plan": [],
    "memory_offset_mhz": None,
    "power_limit_w": 374,
    "amd": {"voltage_offset_mv": -110, "sclk_offset_mhz": 300, "mclk_max_mhz": 1375},
    "flatten_target": {"source": "amdgpu", "lock_clock_mhz": 2700},
    "final_verified": False,
    "requires_verification": True,
    "verification_status": "unverified",
    "profile_tier": "performance",
}


@pytest.fixture
def profiles_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path / "home"))
    from common.atomic_write import atomic_write_json
    from profiles.uv.profile_store import auto_uv_profiles_dir
    from profiles.uv.profile_tiers import save_profile_tier_assignment

    make_fake_card(tmp_path / "sys")
    state = discover_amd_gpus(tmp_path / "sys")[0].read_state()
    atomic_write_json(auto_uv_profiles_dir() / "auto-uv-profile-amd-Performance.json", LEGACY_MANUAL)
    ladder = plan_profile_payload(
        AmdTuningPlan(voltage_offset_mv=-110, sclk_offset_mhz=0, power_cap_w=374),
        state, name="ladder-balanced", tier="balanced", verified=True, verification={"method": "amd-vddgfx-ladder"}, manual=False,
    )
    atomic_write_json(auto_uv_profiles_dir() / f"auto-uv-profile-{ladder['profile_id']}.json", ladder)
    save_profile_tier_assignment("amd-ladder-balanced", "balanced", gpu_uuid=state.uuid)
    save_profile_tier_assignment("amd-Performance", "performance", gpu_uuid=state.uuid)
    return state


def test_legacy_manual_profiles_are_listed_and_runnable(profiles_home) -> None:
    from profiles.uv.profile_store import read_auto_uv_profile_summaries, resolve_auto_uv_profile
    from profiles.uv.runtime_auto_uv_profile import load_auto_uv_final_curve
    from ui.features.profiles.profiles import profile_can_apply

    summaries = {s["profile_id"]: s for s in read_auto_uv_profile_summaries()}
    assert set(summaries) == {"amd-Performance", "amd-ladder-balanced"}
    legacy = summaries["amd-Performance"]
    assert legacy["final_verified"] is True and legacy["verification_status"] == "manual"
    assert legacy["profile_tier_key"] == "performance"
    assert profile_can_apply(legacy)
    assert resolve_auto_uv_profile("amd-Performance") is not None
    curve = load_auto_uv_final_curve("amd-Performance")
    assert curve["amd"] == {"voltage_offset_mv": -110, "sclk_offset_mhz": 300, "mclk_max_mhz": 1375}
    # The file itself is untouched (migration happens on read).
    raw = json.loads(Path(legacy["path"]).read_text())
    assert raw["final_verified"] is False
    assert summaries["amd-ladder-balanced"]["verification_status"] == "verified (OpenCL ladder only)"


def test_rename_updates_display_name_only(profiles_home) -> None:
    from profiles.uv.profile_store import read_auto_uv_profile_summaries, rename_auto_uv_profile

    path = rename_auto_uv_profile("amd-Performance", "Daily driver")
    raw = json.loads(Path(path).read_text())
    assert raw["display_name"] == "Daily driver" and raw["amd"]["voltage_offset_mv"] == -110
    assert raw["profile_id"] == "amd-Performance"
    names = {s["profile_id"]: s["display_name"] for s in read_auto_uv_profile_summaries()}
    assert names["amd-Performance"] == "Daily driver"
    with pytest.raises(ValueError):
        rename_auto_uv_profile("amd-Performance", "   ")
    with pytest.raises(FileNotFoundError):
        rename_auto_uv_profile("does-not-exist", "x")


@pytest.fixture
def profiles_window(qapp, profiles_home, tmp_path, monkeypatch):
    import ui.window as window_mod
    from drivers.amdgpu import sysfs as sysfs_mod
    from ui.components import amd_panel as panel_mod
    from ui.qt import import_qt

    monkeypatch.setenv(sysfs_mod.SYSFS_ROOT_ENV, str(tmp_path / "sys"))
    monkeypatch.setattr(panel_mod, "lact_status", lambda _id="": LactStatus(True, True, True, "/etc/lact/config.yaml", {"voltage_offset": -110}))
    monkeypatch.setattr(panel_mod.AmdPanel, "_query_daemon_async", lambda self: None)
    monkeypatch.setattr(window_mod, "systemd_autostart_profile_info", lambda: {"selector": "", "silent_fan_curve": False})
    monkeypatch.setattr(window_mod, "running_auto_uv_profile_info", lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False})
    monkeypatch.setattr(window_mod, "tux_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    # The GPU choice list normally comes from the daemon; answer from sysfs.
    from ui.features.tuning.gpu_selection import GpuChoice

    state = profiles_home
    monkeypatch.setattr(window_mod, "gpu_choices_with_fallback", lambda **kw: ([GpuChoice(0, state.name, state.pci_addr, state.uuid, state.pci_device_id, 304.0)], 0))
    qt = import_qt()
    if qt[3] is None:
        pytest.skip("pyqtgraph not available")
    monkeypatch.setattr(qt[2].QDialog, "exec", lambda self: 0)
    window = window_mod.MainWindow(qt)
    yield window
    window.window.close()


def test_profiles_page_lists_amd_profiles_with_amd_cells(profiles_window) -> None:
    win = profiles_window
    table = win.profile_list.table
    rows = {table.item(r, 1).text(): [table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())}
    assert len(rows) == 2
    legacy = next(v for k, v in rows.items() if k.startswith("Performance"))
    assert legacy[3] == "-110 off" and legacy[4] == "SCLK +300" and legacy[8] == "cap 374" and legacy[9] == "MCLK 1375"
    assert legacy[10] == "Performance" and legacy[11] == "AMD manual"
    ladder = next(v for k, v in rows.items() if k.startswith("ladder-balanced"))
    assert ladder[3] == "-110 off" and ladder[4] == "SCLK +0" and ladder[8] == "cap 374" and ladder[9] == ""
    assert ladder[10] == "Balanced" and ladder[11] == "AMD ladder (OpenCL)"
    win.profile_list.select_profile("amd-Performance")
    assert win.profile_list.daemonize_button.isEnabled()
    assert win.profile_list.delete_button.isEnabled()


def test_apply_from_profiles_page_routes_through_daemon_intent_with_lact_gate(profiles_window, monkeypatch) -> None:
    win = profiles_window
    import ui.features.profiles.profile_actions as actions_mod

    monkeypatch.setattr(actions_mod, "ensure_daemon_ready_for_privileged_action", lambda **kw: True)
    started: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(win.command_controller, "start", lambda kind, command, fail_text="": started.append((kind, command)) or True)
    win.profile_list.select_profile("amd-Performance")
    choices = iter(["", "override"])
    monkeypatch.setattr(win.amd_panel, "_lact_choice", lambda action: next(choices))
    win._run_runtime_action("daemonize")  # cancelled at the LACT gate
    assert started == []
    monkeypatch.setattr(win.QtWidgets.QMessageBox, "exec", lambda self: win.QtWidgets.QMessageBox.Yes)
    win._run_runtime_action("daemonize")
    assert started and started[-1][0] == "daemonize"
    command = started[-1][1]
    assert "apply-runtime-intent" in command
    intent = json.loads(command[-1])
    assert intent["profile_selector"] == "amd-Performance" and intent["gpu_index"] == 0
    assert "--clear-boot" in command  # Apply on startup is off: nothing persisted for boot


def test_runtime_spec_for_amd_profile_carries_the_amd_block(profiles_home, monkeypatch) -> None:
    from runtime import runtime_spec as spec_mod

    state = profiles_home
    monkeypatch.setattr(spec_mod, "require_daemon_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(spec_mod, "gpu_capabilities", lambda index, socket_path=None: {"vendor": "amd", "identity": {"uuid": state.uuid, "pci_bus_id": state.pci_addr, "name": state.name}})
    monkeypatch.setattr(spec_mod.DaemonGpuClient, "discover_identities", classmethod(lambda cls: [type("I", (), {"index": 0, "uuid": state.uuid})()]))
    spec = spec_mod.build_runtime_spec(profile_selector="amd-Performance", gpu_index=0)
    profile = spec["static_profile"]
    assert spec["mode"] == "static"
    assert profile["plan"] == [] and profile["amd"] == {"voltage_offset_mv": -110, "sclk_offset_mhz": 300, "mclk_max_mhz": 1375}
    assert profile["power_limit_w"] == 374
    assert spec["fan"]["enabled"] is False
