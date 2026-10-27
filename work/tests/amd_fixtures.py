"""Shared fake-sysfs helpers for the local AMD port tests."""

from __future__ import annotations

from pathlib import Path

OD_TEXT = (
    "OD_SCLK_OFFSET:\n300Mhz\nOD_MCLK:\n0: 676Mhz\n1: 1375MHz\nOD_RANGE:\n"
    "SCLK_OFFSET:    -500Mhz       1000Mhz\nMCLK:      97Mhz       1500Mhz\n"
    "VDDGFX_OFFSET:    -200mv          0mv\nOD_VDDGFX_OFFSET:\n-110mV\n"
)
FAN_TEXT = (
    "OD_FAN_CURVE:\n0: 25C 15%\n1: 39C 19%\n2: 56C 60%\n3: 70C 85%\n4: 80C 100%\n"
    "OD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\nFAN_CURVE(fan speed): 15% 100%\n"
)


def make_fake_card(root: Path) -> Path:
    device = root / "card0" / "device"
    hwmon = device / "hwmon" / "hwmon11"
    hwmon.mkdir(parents=True)
    (device / "gpu_od" / "fan_ctrl").mkdir(parents=True)
    files = {
        "vendor": "0x1002\n",
        "device": "0x7550\n",
        "subsystem_vendor": "0x1eae\n",
        "subsystem_device": "0x8811\n",
        "uevent": "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n",
        "unique_id": "0123456789abcdef\n",
        "pp_od_clk_voltage": OD_TEXT,
        "gpu_od/fan_ctrl/fan_curve": FAN_TEXT,
        "pp_dpm_sclk": "S: 0Mhz *\n1: 500Mhz \n2: 2400Mhz \n",
        "pp_dpm_mclk": "0: 96Mhz \n5: 1258Mhz \n",
        "gpu_busy_percent": "6\n",
        "power_dpm_force_performance_level": "manual\n",
    }
    for name, text in files.items():
        (device / name).write_text(text)
    for name, text in {
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
    }.items():
        (hwmon / name).write_text(text)
    return root


