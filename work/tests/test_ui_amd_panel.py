"""Local AMD port: the AMD Tuning panel and the window's amdgpu gating, driven
against a fake sysfs tree under offscreen Qt."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui.window as window_mod
from drivers.amdgpu import sysfs as sysfs_mod
from drivers.amdgpu.sysfs import LactStatus
from ui.components import amd_panel as panel_mod
from ui.qt import import_qt

from amd_fixtures import FAN_TEXT, OD_TEXT, make_fake_card  # noqa: F401


def _lact(active: bool) -> LactStatus:
    return LactStatus(
        unit_active=active,
        socket_present=active,
        process_running=active,
        config_path="/etc/lact/config.yaml",
        gpu_config={"voltage_offset": -110, "power_cap": 374.0} if active else None,
    )


@pytest.fixture
def fake_amd_env(tmp_path, monkeypatch):
    make_fake_card(tmp_path)
    monkeypatch.setenv(sysfs_mod.SYSFS_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv("TUX_BURNER_AMD_VDDGFX_FLOOR_MV", raising=False)
    monkeypatch.setattr(panel_mod, "lact_status", lambda _id="": _lact(False))
    # No daemon in tests: the async query must fail quietly.
    monkeypatch.setattr(panel_mod.AmdPanel, "_query_daemon_async", lambda self: None)
    return tmp_path


@pytest.fixture
def panel(qapp, fake_amd_env):
    qt = import_qt()
    QtCore, _QtGui, QtWidgets, _pg = qt
    calls: list[tuple[str, list[str]]] = []
    log: list[str] = []
    errors: list[tuple[str, str]] = []
    parent = QtWidgets.QWidget()
    created = panel_mod.AmdPanel(
        QtCore=QtCore,
        QtWidgets=QtWidgets,
        parent=parent,
        run_cli=lambda kind, args: calls.append((kind, list(args))) or True,
        busy=lambda: False,
        log=log.append,
        show_error=lambda title, message: errors.append((title, message)),
    )
    created._timer.stop()
    created.test_calls = calls  # type: ignore[attr-defined]
    created.test_errors = errors  # type: ignore[attr-defined]
    yield created
    parent.close()


def test_panel_shows_live_values_and_ranges(panel) -> None:
    labels = {key: label.text() for key, label in panel.status_labels.items()}
    assert "AMD Navi 48" in labels["gpu"] and "0000:03:00.0" in labels["gpu"]
    assert labels["vddgfx"].startswith("-110 mV") and "(-200, 0)" in labels["vddgfx"]
    assert labels["sclk"].startswith("+300 MHz")
    assert "1375 MHz" in labels["mclk"] and "stock 1258" in labels["mclk"]
    assert labels["power"].startswith("374 W") and "default 304" in labels["power"]
    assert "junction 31 C" in labels["temps"]
    assert labels["lact"] == "not running"
    # Editor loaded the current values and the default floor.
    assert panel.vo_spin.value() == -110
    assert panel.sclk_spin.value() == 300
    assert panel.vo_spin.minimum() == -150 and panel.vo_spin.maximum() == 0
    assert panel.effective_floor_mv() == -150
    assert panel.plan().mclk_max_mhz is None and panel.plan().power_cap_w is None


def test_preview_is_a_dry_run_and_validation_blocks_bad_plans(panel) -> None:
    panel.vo_spin.setValue(-100)
    panel.power_check.setChecked(True)
    panel.power_spin.setValue(340)
    panel.preview()
    text = panel.output.toPlainText()
    assert "nothing was written" in text
    assert "echo 'vo -100'" in text and "echo 'c'" in text and "340000000" in text
    assert panel.test_calls == []
    # Spin box range already stops -250; force an out-of-range plan through
    # the validator by widening the floor and shrinking the driver range.
    panel.allow_below_check.setChecked(True)
    panel.floor_spin.setValue(-200)
    assert panel.vo_spin.minimum() == -200
    assert not panel.floor_warning.isHidden()
    panel.allow_below_check.setChecked(False)
    assert panel.vo_spin.minimum() == -150
    assert panel.floor_warning.isHidden()
    assert panel.floor_spin.value() == -150


def test_apply_requires_confirmation_and_runs_the_cli(panel, monkeypatch) -> None:
    confirmations: list[str] = []
    monkeypatch.setattr(panel, "_confirm", lambda title, text: confirmations.append(title) or False)
    panel.vo_spin.setValue(-90)
    panel.apply()
    assert confirmations == ["Apply AMD tuning"]
    assert panel.test_calls == []
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.apply()
    assert panel.test_calls == [
        ("amd-apply", ["--gpu-index", "0", "--amd-plan", "vo=-90", "sclk=300", "--amd-apply"])
    ]


def test_apply_defers_to_lact_unless_overridden(panel, monkeypatch) -> None:
    panel.lact = _lact(True)
    choices = iter(["", "export", "override"])
    monkeypatch.setattr(panel, "_lact_choice", lambda action: next(choices))
    exported: list[int] = []
    monkeypatch.setattr(panel, "export_lact", lambda: exported.append(1))
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.apply()  # cancel
    assert panel.test_calls == [] and exported == []
    panel.apply()  # export (preferred)
    assert panel.test_calls == [] and exported == [1]
    panel.apply()  # override, then confirmation
    assert panel.test_calls[-1][0] == "amd-apply"
    assert "--override-lact" in panel.test_calls[-1][1]


def test_below_floor_plans_carry_the_explicit_floor_flags(panel, monkeypatch) -> None:
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.daemon_floor_mv, panel.daemon_floor_source = -180, "file"
    panel.allow_below_check.setChecked(True)
    panel.floor_spin.setValue(-180)
    panel.vo_spin.setValue(-160)
    panel.apply()
    kind, args = panel.test_calls[-1]
    assert kind == "amd-apply"
    assert "vo=-160" in args
    assert args[-3:] == ["--amd-voltage-floor", "-180", "--allow-below-floor"]
    # Within the default floor no floor flags are passed and -130 is a plain plan value.
    panel.allow_below_check.setChecked(False)
    panel.vo_spin.setValue(-130)
    panel.apply()
    assert panel.test_calls[-1][1] == ["--gpu-index", "0", "--amd-plan", "vo=-130", "sclk=300", "--amd-apply"]


def test_apply_blocks_until_the_daemon_floor_is_synced(panel, monkeypatch) -> None:
    confirmations: list[str] = []
    monkeypatch.setattr(panel, "_confirm", lambda title, text: confirmations.append(title) or False)
    panel.daemon_floor_mv, panel.daemon_floor_source = -150, "default"
    panel.allow_below_check.setChecked(True)
    panel.floor_spin.setValue(-180)
    panel.vo_spin.setValue(-160)
    assert "OUT OF SYNC" in panel.status_labels["floors"].text()
    panel.apply()
    # The first dialog offered to lower the daemon floor; declined -> blocked.
    assert confirmations == ["Daemon safety floor"]
    assert panel.test_calls == []
    assert panel.test_errors and "daemon's floor is -150" in panel.test_errors[-1][1]
    # Accepting syncs the daemon through the RPC, then the apply proceeds.
    synced: list[int | None] = []

    class _Client:
        def __init__(self, _index):
            pass

        def set_vddgfx_floor_mv(self, floor):
            synced.append(floor)
            return {"floor_mv": floor, "source": "file", "file": "/etc/tux-burner/amd-vddgfx-floor-mv"}

    import drivers.nvidia.daemon_gpu as daemon_gpu_mod

    monkeypatch.setattr(daemon_gpu_mod, "DaemonGpuClient", _Client)
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.apply()
    assert synced == [-180]
    assert panel.daemon_floor_mv == -180
    assert "OUT OF SYNC" not in panel.status_labels["floors"].text()
    assert panel.test_calls[-1][0] == "amd-apply"
    # Unticking restores the default daemon floor (confirmed) via the RPC.
    panel.allow_below_check.setChecked(False)
    assert synced == [-180, None]


def test_group_box_titles_do_not_overlap_the_first_row(panel, qapp) -> None:
    from ui.styles import STYLESHEET

    widget = panel.widget
    widget.setStyleSheet(STYLESHEET)
    widget.resize(1200, 700)
    widget.show()
    widget.layout().activate()
    for _ in range(4):
        qapp.processEvents()
    # Rendering forces a full layout pass under the offscreen platform.
    pixmap = widget.grab()
    assert not pixmap.isNull()
    qapp.processEvents()
    QtWidgets = panel.QtWidgets
    for group in widget.findChildren(QtWidgets.QGroupBox):
        assert group.layout().contentsMargins().top() >= group.fontMetrics().height() + 8
        option = QtWidgets.QStyleOptionGroupBox()
        group.initStyleOption(option)
        title_rect = group.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_GroupBox,
            option,
            QtWidgets.QStyle.SubControl.SC_GroupBoxLabel,
            group,
        )
        layout = group.layout()
        tops = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            child = item.widget()
            if child is not None and not child.isVisible():
                continue  # hidden rows (the floor warning) are not laid out
            if item.geometry().height() > 0:
                tops.append(item.geometry().top())
        first_child_top = min(tops)
        assert first_child_top >= title_rect.bottom(), (
            f"{group.title()!r}: first row top {first_child_top} overlaps title bottom {title_rect.bottom()}"
        )
    widget.hide()


def test_ladder_previews_first_then_runs_after_confirmation(panel, monkeypatch) -> None:
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.ladder()
    text = panel.output.toPlainText()
    assert "AMD Auto-UV ladder" in text and "preview only" in text
    assert "-150 mV" in text and "-160" not in text and "range: +0 -> -150 mV" in text
    assert panel.test_calls == []
    assert panel.ladder_button.text().endswith("RUN...")
    panel.ladder()
    kind, args = panel.test_calls[-1]
    assert kind == "amd-ladder"
    assert args[:7] == ["--gpu-index", "0", "--amd-plan", "sclk=300", "--amd-auto-uv", "--amd-auto-uv-rung-seconds", "60"]
    assert args[7] == "--amd-run-log" and args[8].endswith(".log") and "tux_burner-amd-ladder-" in args[8]
    assert panel.last_log_path == args[8]


def test_stress_requires_confirmation(panel, monkeypatch) -> None:
    monkeypatch.setattr(panel, "_confirm", lambda title, text: False)
    panel.stress()
    assert panel.test_calls == []
    monkeypatch.setattr(panel, "_confirm", lambda title, text: True)
    panel.stress_spin.setValue(30)
    panel.stress()
    kind, args = panel.test_calls[-1]
    assert kind == "amd-stress"
    assert args[:4] == ["--gpu-index", "0", "--amd-stress", "30"]
    assert args[4] == "--amd-run-log" and "tux_burner-amd-stress-" in args[5]


@pytest.fixture
def amd_window(qapp, fake_amd_env, monkeypatch):
    monkeypatch.setattr(window_mod, "load_profile_summaries", lambda: [])
    monkeypatch.setattr(
        window_mod, "systemd_autostart_profile_info", lambda: {"selector": "", "silent_fan_curve": False}
    )
    monkeypatch.setattr(
        window_mod,
        "running_auto_uv_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    monkeypatch.setattr(window_mod, "tux_burner_runtime_is_active", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_from_runtime_config", lambda: False)
    monkeypatch.setattr(window_mod, "silent_fan_curve_to_runtime_config", lambda v: v)
    qt = import_qt()
    if qt[3] is None:
        pytest.skip("pyqtgraph not available")
    monkeypatch.setattr(qt[2].QDialog, "exec", lambda self: 0)
    warnings: list[tuple] = []
    monkeypatch.setattr(qt[2].QMessageBox, "warning", staticmethod(lambda *args, **kwargs: warnings.append(args)))
    window = window_mod.MainWindow(qt)
    window.test_warnings = warnings  # type: ignore[attr-defined]
    yield window
    window.window.close()


def test_window_on_amd_has_amd_tab_and_never_starts_the_nvidia_scan(amd_window, monkeypatch) -> None:
    win = amd_window
    assert win.amd_mode and win.amd_panel is not None
    assert win.tabs.tabText(win.amd_tab_index) == "AMD Tuning"
    assert win.controls.start_button.text() == "AMD Auto-UV ladder"
    assert win.controls.import_afterburner_button.isHidden()
    win._check_gpu_supported_on_startup()
    assert win.test_warnings == []
    started: list[list[str]] = []
    monkeypatch.setattr(win.scan_controller, "start", lambda command: started.append(command) or False)
    dialogs: list[int] = []
    monkeypatch.setattr(window_mod, "select_scan_tuning", lambda **kwargs: dialogs.append(1) or None)
    win.start_scan()
    assert started == [] and dialogs == []
    assert win.tabs.currentIndex() == win.amd_tab_index
    assert "preview only" in win.amd_panel.output.toPlainText()


def test_window_header_follows_amd_command_children(amd_window, monkeypatch) -> None:
    win = amd_window
    started: list[list[str]] = []
    monkeypatch.setattr(win.command_controller, "start", lambda kind, command, fail_text="": started.append(command) or True)
    assert win._run_amd_cli("amd-ladder", ["--gpu-index", "0", "--amd-auto-uv"])
    assert started and started[0][:2] == ["env", "PYTHONUNBUFFERED=1"] and "--amd-auto-uv" in started[0]
    assert win.header.stage() == "AMD Auto-UV ladder"
    assert win.header.candidate_label.text() == "starting the ladder"
    win._command_output("  rungs (3): +0, -10, -20 mV\nAMD Auto-UV rung -10 mV: stressing for 60s ...\n")
    assert win.header.candidate_label.text().startswith("rung -10 mV (2/3), 60 s stress")
    assert "AMD Auto-UV ladder: rung -10 mV" in win.controls.status_label.text()
    win._command_output("AMD Auto-UV rung -10 mV: stable (60s)\nAMD Auto-UV rung -20 mV: refused by the daemon's safety floor - x\n")
    win._finish_amd_activity("amd-ladder", 1)
    assert win.header.stage() == "Idle"
    assert win.header.candidate_label.text() == (
        "Ladder finished: stable down to -10 mV, refused by the daemon floor at -20 mV (not a stability failure)"
    )
    assert win.header.separator_label.text() == "|"


def test_spin_box_ranges_keep_minus_110_reachable(panel, qapp) -> None:
    from PySide6.QtTest import QTest

    assert (panel.vo_spin.minimum(), panel.vo_spin.maximum()) == (-150, 0)
    assert (panel.sclk_spin.minimum(), panel.sclk_spin.maximum()) == (-500, 1000)
    assert (panel.mclk_spin.minimum(), panel.mclk_spin.maximum()) == (97, 1500)
    assert (panel.power_spin.minimum(), panel.power_spin.maximum()) == (212, 374)
    assert (panel.floor_spin.minimum(), panel.floor_spin.maximum()) == (-200, -150)
    panel.widget.show()
    qapp.processEvents()
    panel.vo_spin.setFocus()
    panel.vo_spin.selectAll()
    QTest.keyClicks(panel.vo_spin, "-110")
    QTest.keyClick(panel.vo_spin, panel.QtCore.Qt.Key_Enter)
    assert panel.vo_spin.value() == -110
    # Typing a floor value never clamps the plan spin on intermediate keystrokes.
    panel.vo_spin.setValue(-100)
    panel.allow_below_check.setChecked(True)
    panel.floor_spin.setFocus()
    panel.floor_spin.selectAll()
    QTest.keyClicks(panel.floor_spin, "-180")
    assert panel.vo_spin.value() == -100
    QTest.keyClick(panel.floor_spin, panel.QtCore.Qt.Key_Enter)
    assert panel.floor_spin.value() == -180
    assert panel.vo_spin.minimum() == -180 and panel.vo_spin.value() == -100
    panel.allow_below_check.setChecked(False)
    assert panel.vo_spin.minimum() == -150
    panel.widget.hide()
