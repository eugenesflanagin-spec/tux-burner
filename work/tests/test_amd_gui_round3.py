"""Local AMD port, round 3: `@amd` event parsing + plot traces, run records and
the history table, the AMD Restore Defaults dialog, the Profiles-page LACT
gate, the CLI restore dry run and manual-profile visibility."""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from auto_uv import amd_sweep
from drivers.amdgpu.plan import AmdTuningPlan, plan_profile_payload
from drivers.amdgpu.sysfs import LactStatus, discover_amd_gpus
from ui.features.tuning.amd_activity import AmdActivity, traces_from_run_record

from amd_fixtures import make_fake_card

EVENTS = "\n".join(
    [
        'AMD Auto-UV ladder log: /tmp/x/debug-logs/tux_burner-amd-ladder-1.log',
        '@amd {"event":"ladder_start","rungs":[0,-10,-20],"rung_seconds":30,"floor_mv":-150}',
        '@amd {"event":"rung_start","offset_mv":0,"index":1,"total":3,"seconds":30}',
        '@amd {"event":"sample","offset_mv":0,"t_s":0.5,"vddgfx_mv":1050,"sclk_mhz":3100,"power_w":330.0,"junction_c":80.0}',
        '@amd {"event":"sample","offset_mv":0,"t_s":1.0,"vddgfx_mv":1060,"sclk_mhz":3150,"power_w":335.0,"junction_c":82.0}',
        '@amd {"event":"rung_result","offset_mv":0,"result":"stable","failure_type":"","detail":"","seconds":30.2}',
        '@amd {"event":"rung_start","offset_mv":-10,"index":2,"total":3,"seconds":30}',
        '@amd {"event":"sample","offset_mv":-10,"t_s":0.5,"vddgfx_mv":1040,"sclk_mhz":3160,"power_w":328.0,"junction_c":81.0}',
        '@amd {"event":"rung_result","offset_mv":-10,"result":"unstable","failure_type":"opencl-mismatch","detail":"int.x=12","seconds":9.0}',
        '@amd {"event":"ladder_end","deepest_stable_mv":0,"first_unstable_mv":-10,"stopped_reason":"unstable","summary":"deepest tested stable VDDGFX offset +0 mV; first unstable rung -10 mV (OpenCL result mismatch)"}',
        "AMD Auto-UV: run record /tmp/x/amd-runs/amd-run-1-amd-ladder.json",
        "",
    ]
)


def test_activity_parses_amd_events_into_traces() -> None:
    activity = AmdActivity()
    activity.start("amd-ladder", now=0.0)
    activity.feed(EVENTS, now=10.0)
    assert activity.rungs == [0, -10, -20]
    assert activity.log_path.endswith("tux_burner-amd-ladder-1.log")
    assert activity.record_path.endswith("amd-run-1-amd-ladder.json")
    traces = {trace.offset_mv: trace for trace in activity.plot_traces()}
    assert traces[0].samples == [(1050.0, 3100.0), (1060.0, 3150.0)]
    assert traces[0].result == "stable"
    assert traces[-10].result == "unstable" and traces[-10].failure_type == "opencl-mismatch"
    assert activity.stable == [0] and activity.unstable_mv == -10
    assert activity.plot_version > 0
    text = activity.candidate_text(now=12.0)
    assert text.startswith("rung -10 mV (2/3), 30 s stress") and "now 3160 MHz @ 1040 mV" in text
    result = activity.finish(1)
    assert "stable down to +0 mV" in result
    assert "unstable at -10 mV (opencl-mismatch: int.x=12)" in result
    assert result.endswith("- log /tmp/x/debug-logs/tux_burner-amd-ladder-1.log")


def _state(tmp_path: Path):
    make_fake_card(tmp_path)
    return discover_amd_gpus(tmp_path)[0].read_state()


def test_run_records_round_trip_and_summarize(tmp_path) -> None:
    state = _state(tmp_path / "sys")
    sample = amd_sweep.TelemetrySample(0.5, 1040, 3160, 330.0, 81.0)
    result = amd_sweep.LadderResult(
        rungs=(
            amd_sweep.Rung(0, 0, 30.0, "", "", "", (sample,)),
            amd_sweep.Rung(-10, None, 0.0, "x", amd_sweep.FAILURE_APPLY_REFUSED_FLOOR, "below floor"),
        ),
        deepest_stable_mv=0,
        first_unstable_mv=None,
        refused_by_floor_mv=-10,
    )
    record = amd_sweep.run_record(
        kind="amd-ladder",
        state=state,
        settings=amd_sweep.LadderSettings(rung_seconds=30),
        result=result,
        started_at="2026-10-14T13:00:00-04:00",
        finished_at="2026-10-14T13:05:00-04:00",
        app_floor_mv=-150,
        daemon_floor_mv=-150,
        saved_tiers={"balanced": "amd-ladder-balanced"},
        log_path="/tmp/l.log",
    )
    path = amd_sweep.save_run_record(record, directory=tmp_path / "runs")
    loaded = amd_sweep.load_run_records(tmp_path / "runs")
    assert len(loaded) == 1 and loaded[0]["path"] == str(path)
    assert loaded[0]["stopped_reason"] == "daemon-safety-floor"
    assert loaded[0]["rungs"][1]["failure_type"] == "apply-refused-floor"
    assert loaded[0]["rungs"][0]["samples"][0]["sclk_mhz"] == 3160
    row = amd_sweep.record_summary_row(loaded[0])
    assert row["kind"] == "AMD ladder" and row["deepest"] == "+0 mV" and row["rungs"] == "1/2 stable"
    assert "REFUSED by the daemon's safety floor" in row["result"]
    traces, rungs = traces_from_run_record(loaded[0])
    assert rungs == [0, -10] and traces[0].samples == [(1040.0, 3160.0)] and traces[1].result == "refused"


def test_classify_stress_exit_types() -> None:
    assert amd_sweep.classify_stress_exit(0, "") == ("", "")
    assert amd_sweep.classify_stress_exit(3, "opencl-bruteforce: UNSTABLE redundant verification mismatch int.x=3, int.y=1 stage=100%")[0] == "opencl-mismatch"
    assert amd_sweep.classify_stress_exit(3, "opencl-bruteforce: UNSTABLE gpu fault under load: clFinish(stress) failed rc=-36")[0] == "driver-error"
    assert amd_sweep.classify_stress_exit(4, "opencl-bruteforce: HANG launch timeout: no progress for 8.0s")[0] == "hang-timeout"
    assert amd_sweep.classify_stress_exit(-9, "")[0] == "process-crash"
    assert amd_sweep.classify_stress_exit(1, "opencl-bruteforce: FAILED cannot load the OpenCL ICD loader")[0] == "setup-error"
    assert amd_sweep.classify_apply_error("VDDGFX offset -160 mV is below the Tux Burner safety floor -150 mV") == "apply-refused-floor"
    assert amd_sweep.classify_apply_error("VDDGFX offset -250 mV is outside the driver OD_RANGE -200..0 mV") == "apply-refused-range"


def test_manual_profile_is_listed_and_labelled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path / "home"))
    from common.atomic_write import atomic_write_json
    from profiles.uv.profile_store import auto_uv_profiles_dir, profile_display_name, read_auto_uv_profile_summaries
    from profiles.uv.profile_tiers import save_profile_tier_assignment
    from ui.features.profiles.profiles import profile_can_apply

    state = _state(tmp_path / "sys")
    payload = plan_profile_payload(AmdTuningPlan(voltage_offset_mv=-110, sclk_offset_mhz=300, mclk_max_mhz=1375, power_cap_w=374), state, name="Performance", tier="performance")
    assert payload["final_verified"] is True and payload["verification_status"] == "manual"
    assert payload["display_name"].endswith("(manual)")
    atomic_write_json(auto_uv_profiles_dir() / f"auto-uv-profile-{payload['profile_id']}.json", payload)
    save_profile_tier_assignment(payload["profile_id"], "performance", gpu_uuid=state.uuid)
    summaries = read_auto_uv_profile_summaries()
    assert [s["profile_id"] for s in summaries] == ["amd-Performance"]
    assert summaries[0]["profile_tier_key"] == "performance"
    assert profile_display_name(summaries[0]) == "VDDGFX -110 mV, SCLK +300 MHz, MCLK 1375 MHz, 374 W"
    assert profile_can_apply(summaries[0])


def test_cli_restore_dry_run_and_validation(tmp_path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    from cli import amd_commands

    state = _state(tmp_path / "sys")
    lact = LactStatus(True, True, True, "/etc/lact/config.yaml", {"voltage_offset": -110})
    args = SimpleNamespace(amd_restore="stock", dry_run=True, amd_dry_run=True, override_lact=False)
    amd_commands._restore(args, state, lact, print_fn=lambda text, **kw: print(text))
    out = capsys.readouterr().out
    assert "dry run: --amd-restore stock" in out and "VDDGFX -110 mV" in out
    # Without --override-lact a stock restore is refused while LACT runs.
    from common.tux_burner_errors import NvmlError

    args = SimpleNamespace(amd_restore="stock", dry_run=False, amd_dry_run=False, override_lact=False)
    with pytest.raises(NvmlError) as excinfo:
        amd_commands._restore(args, state, lact, print_fn=print)
    assert "LACT (lactd) is running" in str(excinfo.value)
    # LACT re-apply uses `lact cli profile set <current>`; no restart anywhere.
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "Default\n"
        stderr = ""

    monkeypatch.setattr(amd_commands.subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or _Done())
    monkeypatch.setattr(amd_commands, "select_amd_gpu", lambda index: type("G", (), {"read_state": lambda self: state})())
    args = SimpleNamespace(amd_restore="lact", dry_run=False, amd_dry_run=False, override_lact=False)
    amd_commands._restore(args, state, lact, print_fn=lambda text, **kw: print(text))
    assert calls == [["lact", "cli", "profile", "get"], ["lact", "cli", "profile", "set", "Default"]]
    assert not any("restart" in " ".join(c) or "systemctl" in c for c in calls)


@pytest.fixture
def amd_window(qapp, tmp_path, monkeypatch):
    import ui.window as window_mod
    from drivers.amdgpu import sysfs as sysfs_mod
    from ui.components import amd_panel as panel_mod
    from ui.qt import import_qt

    make_fake_card(tmp_path / "sys")
    monkeypatch.setenv(sysfs_mod.SYSFS_ROOT_ENV, str(tmp_path / "sys"))
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(panel_mod, "lact_status", lambda _id="": LactStatus(True, True, True, "/etc/lact/config.yaml", {"voltage_offset": -110}))
    monkeypatch.setattr(panel_mod.AmdPanel, "_query_daemon_async", lambda self: None)
    monkeypatch.setattr(window_mod, "load_profile_summaries", lambda: [])
    monkeypatch.setattr(window_mod, "systemd_autostart_profile_info", lambda: {"selector": "", "silent_fan_curve": False})
    monkeypatch.setattr(window_mod, "running_auto_uv_profile_info", lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False})
    monkeypatch.setattr(window_mod, "tux_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    qt = import_qt()
    if qt[3] is None:
        pytest.skip("pyqtgraph not available")
    monkeypatch.setattr(qt[2].QDialog, "exec", lambda self: 0)
    # A run record on disk before the window opens -> history row.
    runs = tmp_path / "home" / ".config" / "TuxBurner" / "amd-runs"
    runs.mkdir(parents=True)
    (runs / "amd-run-2026-10-14T13-12-00-amd-ladder.json").write_text(json.dumps({
        "format_version": 1, "kind": "amd-ladder", "started_at": "2026-10-14T13:12:00-04:00", "finished_at": "2026-10-14T13:24:00-04:00",
        "reconstructed": True, "notes": "test", "gpu": {}, "settings": {"start_mv": 0, "step_mv": 10, "app_floor_mv": -110, "daemon_floor_mv": -110,
        "live_sclk_offset_mhz": 300, "live_mclk_max_mhz": 1375, "live_power_cap_w": 374, "sclk_offset_mhz": 0}, "log_path": "",
        "saved_tiers": {"balanced": "amd-ladder-balanced"}, "deepest_stable_mv": -110, "stopped_reason": "completed", "summary": "all passed",
        "rungs": [{"offset_mv": 0, "result": "stable", "failure_type": "", "detail": "", "samples": [{"vddgfx_mv": 1050, "sclk_mhz": 3100}]},
                  {"offset_mv": -110, "result": "stable", "failure_type": "", "detail": "", "samples": [{"vddgfx_mv": 940, "sclk_mhz": 3150}]}],
    }))
    window = window_mod.MainWindow(qt)
    yield window
    window.window.close()


def test_history_table_and_plot_from_record(amd_window) -> None:
    win = amd_window
    panel = win.amd_panel
    assert panel.history_table.rowCount() == 1
    assert panel.history_table.item(0, 1).text() == "AMD ladder (reconstructed)"
    panel.history_table.selectRow(0)
    assert "RECONSTRUCTED" in panel.output.toPlainText()
    assert "-110 mV: stable" in panel.output.toPlainText()
    panel._history_plot()
    assert win.tabs.currentIndex() == win.auto_uv_tab_index
    assert len(win.vf_plot.amd_rung_items) == 2
    assert win.runs_table.widget.rowCount() == 2
    assert win.header.stage() == "AMD run (history)"


def test_live_ladder_output_feeds_plot_runs_table_and_log(amd_window, monkeypatch) -> None:
    win = amd_window
    started: list[list[str]] = []
    monkeypatch.setattr(win.command_controller, "start", lambda kind, command, fail_text="": started.append(command) or True)
    assert win._run_amd_cli("amd-ladder", ["--gpu-index", "0", "--amd-auto-uv"])
    win._command_output(EVENTS)
    assert len(win.vf_plot.amd_rung_items) >= 2  # two rung clouds (+ current-rung label while active)
    assert win.runs_table.widget.rowCount() == 2
    assert "@amd" not in win.amd_panel.output.toPlainText()
    assert win.amd_panel.last_log_path.endswith("tux_burner-amd-ladder-1.log")
    win._finish_amd_activity("amd-ladder", 1)
    assert win.header.stage() == "Idle"
    assert "unstable at -10 mV (opencl-mismatch: int.x=12)" in win.header.candidate_label.text()
    assert win.header.candidate_label.text().endswith("tux_burner-amd-ladder-1.log")


def test_restore_defaults_on_amd_offers_lact_or_stock(amd_window, monkeypatch) -> None:
    win = amd_window
    QtWidgets = win.QtWidgets
    clicked: dict[str, str] = {}

    class _Box:
        instances: list = []

        def __init__(self, parent=None):
            self.buttons = {}
            _Box.instances.append(self)

        def setIcon(self, *_): pass
        def setWindowTitle(self, *_): pass
        def setText(self, text): self.text = text
        def addButton(self, label, role=None):
            button = type("B", (), {"label": label, "enabled": True, "setEnabled": lambda s, v: setattr(s, "enabled", v)})()
            self.buttons[label] = button
            return button
        def setDefaultButton(self, *_): pass
        def exec(self): return 0
        def clickedButton(self): return self.buttons.get(clicked.get("label"))

    monkeypatch.setattr(QtWidgets, "QMessageBox", type("QMB", (), {"__new__": lambda cls, *a, **k: _Box(*a, **k), "Question": 0, "Warning": 1, "AcceptRole": 0, "DestructiveRole": 1, "Cancel": 2, "Yes": 3}))
    runs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(win, "_run_amd_cli", lambda kind, args: runs.append((kind, args)) or True)
    clicked["label"] = "Return to LACT's settings"
    win._restore_gpu_defaults()
    assert runs[-1] == ("amd-restore", ["--gpu-index", "0", "--amd-restore", "lact"])
    clicked["label"] = "Driver stock defaults"
    win._restore_gpu_defaults()
    assert runs[-1] == ("amd-restore", ["--gpu-index", "0", "--amd-restore", "stock", "--override-lact"])
    assert "removes your undervolt" in _Box.instances[-1].text
    clicked["label"] = "nope"
    win._restore_gpu_defaults()
    assert len(runs) == 2


def test_profiles_page_apply_gate_for_amd_profiles(amd_window, monkeypatch) -> None:
    win = amd_window
    profile = {"profile_id": "amd-x", "amd": {"voltage_offset_mv": -100, "sclk_offset_mhz": 0}, "power_limit_w": 374, "final_verified": True}
    choices = iter(["", "export", "override"])
    monkeypatch.setattr(win.amd_panel, "_lact_choice", lambda action: next(choices))
    exported: list[dict] = []
    monkeypatch.setattr(win, "_export_lact_profile", lambda p: exported.append(p))
    assert win._amd_profile_lact_gate(profile) is False  # cancel
    assert win._amd_profile_lact_gate(profile) is False and exported == [profile]  # export
    monkeypatch.setattr(win.QtWidgets.QMessageBox, "exec", lambda self: win.QtWidgets.QMessageBox.Yes)
    assert win._amd_profile_lact_gate(profile) is True  # override + confirmed
    assert win._amd_profile_lact_gate({"profile_id": "nvidia", "plan": [1]}) is True
