#!/usr/bin/env python3
"""Take the user-guide screenshots of the Tux Burner GUI.

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.

Safe by construction: the GUI is started offscreen against a FAKE amdgpu sysfs tree
(tests/amd_fixtures.make_fake_card, values copied from a Radeon RX 9070 XT but with
a made-up unique id), with a throw-away HOME, and the daemon/CLI are never called.
Nothing touches a real GPU. The Apply / Run buttons are not pressed; the only action
taken is the dry-run preview, which writes nothing.

Usage: PYTHONPATH=. QT_QPA_PLATFORM=offscreen python scripts/guide_screenshots.py OUTDIR
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

out_dir = Path(sys.argv[1]).resolve()
out_dir.mkdir(parents=True, exist_ok=True)
scratch = Path(tempfile.mkdtemp(prefix="tux-burner-shots-"))
home = scratch / "home"
home.mkdir()
os.environ["HOME"] = str(home)
os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
os.environ["XDG_DATA_HOME"] = str(home / ".local" / "share")
os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
os.environ["TUX_BURNER_HOME"] = str(home)
sysfs = scratch / "sys"
sysfs.mkdir()

from amd_fixtures import make_fake_card  # noqa: E402

make_fake_card(sysfs)
# A made-up id, so no real serial appears in any picture.
(sysfs / "card0" / "device" / "unique_id").write_text("0123456789abcdef\n")
os.environ["TUX_BURNERD_AMDGPU_SYSFS_ROOT"] = str(sysfs)
os.environ.pop("TUX_BURNER_AMD_VDDGFX_FLOOR_MV", None)

import ui.window as window_mod  # noqa: E402
from drivers.amdgpu.sysfs import LactStatus  # noqa: E402
from ui.components import amd_panel as panel_mod  # noqa: E402
from ui.qt import import_qt  # noqa: E402


def lact(active: bool) -> LactStatus:
    return LactStatus(
        unit_active=active,
        socket_present=active,
        process_running=active,
        config_path="/etc/lact/config.yaml",
        gpu_config={"voltage_offset": -110, "power_cap": 374.0} if active else None,
    )


state = {"lact": False}
panel_mod.lact_status = lambda _id="": lact(state["lact"])  # type: ignore[assignment]
panel_mod.AmdPanel._query_daemon_async = lambda self: None  # type: ignore[assignment]
window_mod.load_profile_summaries = lambda: []
window_mod.systemd_autostart_profile_info = lambda: {"selector": "", "silent_fan_curve": False}
window_mod.running_auto_uv_profile_info = lambda: {
    "selector": "",
    "silent_fan_curve": False,
    "adaptive_auto_uv": False,
}
window_mod.tux_burner_runtime_is_active = lambda: False
window_mod.silent_fan_curve_from_runtime_config = lambda: False
window_mod.silent_fan_curve_to_runtime_config = lambda v: v

qt = import_qt()
QtCore, QtGui, QtWidgets, _pg = qt
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: 0)  # type: ignore[assignment]

win = window_mod.MainWindow(qt)
main = win.window
main.resize(1180, 820)
main.show()


def pump(n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


def shot(name: str, widget=None) -> None:
    pump()
    (widget or main).grab().save(str(out_dir / f"{name}.png"))
    print("saved", name)


panel = win.amd_panel
panel._timer.stop()
panel.refresh() if hasattr(panel, "refresh") else None
win.tabs.setCurrentIndex(win.amd_tab_index)
shot("amd-tuning")

# Dry-run preview: shows the exact sysfs writes, writes nothing.
panel.vo_spin.setValue(-100)
panel.preview()
shot("amd-preview")

# Safety floor: the "allow below" box and its warning (nothing is applied).
panel.allow_below_check.setChecked(True)
shot("amd-below-floor")
panel.allow_below_check.setChecked(False)

# LACT running: Apply would offer Export / Override / Cancel.
state["lact"] = True
panel.refresh() if hasattr(panel, "refresh") else None
shot("amd-lact-running")
state["lact"] = False
panel.refresh() if hasattr(panel, "refresh") else None

for index in range(win.tabs.count()):
    label = win.tabs.tabText(index).strip().lower().replace(" ", "-").replace("/", "-")
    if index == win.amd_tab_index:
        continue
    win.tabs.setCurrentIndex(index)
    shot(f"tab-{label}")

win.tabs.setCurrentIndex(win.amd_tab_index)

# About dialog.
from ui.dialogs import about as about_mod  # noqa: E402

about_mod.energy_savings_lines = lambda status=None: ""
captured: list = []
QtWidgets.QDialog.exec = lambda self: (captured.append(self), self.setMinimumWidth(640), self.show(), self.resize(640, 640), pump(), self.grab().save(str(out_dir / "about.png")), 0)[-1]  # type: ignore[assignment]
about_mod.show_about_dialog(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, parent=main)
print("saved about")
main.close()
print("scratch dir (throw-away):", scratch)
