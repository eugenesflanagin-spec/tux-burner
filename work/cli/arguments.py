"""Command-line arguments for the Tux Burner CLI.

This module only defines flags and defaults; command execution stays in the runtime entrypoint.
"""

from __future__ import annotations

import argparse

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode.auto_uv_mode import ADAPTIVE_TIER_MODES, AUTO_UV_MODES
from common.tux_burner_paths import default_runtime_config_path
from integrations.afterburner.policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from runtime.support.runtime_service import DEFAULT_JOURNAL_HOURS
from stability.q2rtx.resolution import Q2RTX_SCAN_RESOLUTIONS

DEFAULT_AUTO_UV_FINAL_DURATION_S = AUTO_UV_DEFAULTS.final_duration_s


def default_cli_config_path() -> str:
    return str(default_runtime_config_path())


def _version_text() -> str:
    try:
        from ui.assets import application_version

        version = application_version()
    except Exception:  # noqa: BLE001 - --version must never fail
        version = "unknown"
    return (
        f"Tux Burner {version}\n"
        "Fork of PenguinBurner by jpietek (https://github.com/jpietek/PenguinBurner).\n"
        "Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later."
    )


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="tux_burner.py",
        usage="tux_burner.py [options]",
        description=(
            "Tux Burner: GPU undervolting and overclocking for Linux (AMD amdgpu\n"
            "and NVIDIA). A fork of PenguinBurner by jpietek (GPL-3.0-or-later).\n"
            "Run Auto-UV scans, manage the saved Efficiency/Balanced/Performance\n"
            "profiles, drive the adaptive daemon runtime, and set up the Steam\n"
            "launch wrapper and in-game overlay."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=_version_text(),
        help="Print the Tux Burner version and exit",
    )

    auto_uv_group = parser.add_argument_group("Auto-UV")
    steam_group = parser.add_argument_group("Steam overlay setup")
    daemon_group = parser.add_argument_group("Runtime and daemon essentials")
    runtime_group = parser.add_argument_group("Runtime tuning")
    advanced_group = parser.add_argument_group("Advanced/debug")
    amd_group = parser.add_argument_group(
        "AMD (amdgpu OverDrive) tuning",
        "Local AMD port. Reads come from sysfs; writes go through tux-burnerd. "
        "While LACT (lactd) runs it owns the GPU: writes need --override-lact.",
    )
    amd_group.add_argument(
        "--amd-status",
        action="store_true",
        help="Print the AMD GPU's OverDrive ranges, live values, fan curve and LACT state (read-only)",
    )
    amd_group.add_argument(
        "--amd-plan",
        nargs="+",
        metavar="KEY=VALUE",
        default=None,
        help=(
            "Tuning plan: vo=<mV> (VDDGFX offset, negative = undervolt), sclk=<MHz> offset, "
            "mclk=<MHz> max memory clock, power=<W>|<NN%%> cap, fan=T:P,T:P,... (max 5 points), "
            "zero_rpm=on|off. Alone it previews the exact sysfs writes (dry run)"
        ),
    )
    amd_group.add_argument(
        "--amd-dry-run",
        dest="dry_run",
        action="store_true",
        help="With --amd-apply/--amd-save-profile/--amd-auto-uv/--amd-stress: show what would happen, change nothing",
    )
    amd_group.add_argument(
        "--amd-apply",
        action="store_true",
        help="Apply --amd-plan now through tux-burnerd (validated against the live OD_RANGE)",
    )
    amd_group.add_argument(
        "--amd-save-profile",
        metavar="NAME",
        default=None,
        help="Save --amd-plan as an (unverified) Tux Burner profile the GUI and daemon can apply",
    )
    amd_group.add_argument(
        "--amd-profile-tier",
        choices=("efficiency", "balanced", "performance"),
        default="",
        help="Tier to assign the saved AMD profile to",
    )
    amd_group.add_argument(
        "--amd-export-lact",
        nargs="?",
        const="-",
        metavar="PATH",
        default=None,
        help="Write --amd-plan as a LACT config fragment (stdout when PATH is omitted)",
    )
    amd_group.add_argument(
        "--amd-stress",
        type=float,
        metavar="SECONDS",
        default=None,
        help="Run the OpenCL brute-force stability workload (sustained GPU load; no tuning writes)",
    )
    amd_group.add_argument(
        "--amd-auto-uv",
        action="store_true",
        help="AMD Auto-UV: step the VDDGFX offset down rung by rung, stress each rung, save three tiers",
    )
    amd_group.add_argument("--amd-auto-uv-start", type=int, default=0, metavar="MV", help="First rung (default 0 = stock)")
    amd_group.add_argument("--amd-auto-uv-step", type=int, default=10, metavar="MV", help="Rung size in mV (default 10)")
    amd_group.add_argument("--amd-auto-uv-floor", type=int, default=None, metavar="MV", help="Lowest offset to try (default: driver minimum)")
    amd_group.add_argument("--amd-auto-uv-rung-seconds", type=int, default=60, metavar="S", help="Stress seconds per rung (default 60)")
    amd_group.add_argument("--amd-auto-uv-margin", type=int, default=20, metavar="MV", help="Safety margin added to the deepest stable rung (default 20)")
    amd_group.add_argument(
        "--amd-voltage-floor",
        type=int,
        default=None,
        metavar="MV",
        help=(
            "Deepest VDDGFX offset Tux Burner will plan, write or sweep to "
            "(default -150 mV on this machine). Going deeper "
            "also needs --allow-below-floor; the daemon enforces its own floor "
            "(/etc/tux-burner/amd-vddgfx-floor-mv)"
        ),
    )
    amd_group.add_argument(
        "--allow-below-floor",
        action="store_true",
        help="Confirm an --amd-voltage-floor deeper than the default -150 mV",
    )
    amd_group.add_argument(
        "--amd-set-daemon-floor",
        metavar="MV|default",
        default=None,
        help=(
            "Set the DAEMON's VDDGFX safety floor (/etc/tux-burner/amd-vddgfx-floor-mv, written by "
            "tux-burnerd) or 'default' to restore -150 mV. Values deeper than -150 also need "
            "--allow-below-floor. Without this the daemon refuses deeper writes even if --amd-voltage-floor allows them"
        ),
    )
    amd_group.add_argument(
        "--amd-run-log",
        metavar="PATH",
        default=None,
        help=argparse.SUPPRESS,  # GUI: the tee log the run record should reference
    )
    amd_group.add_argument(
        "--amd-restore",
        choices=("lact", "stock"),
        default=None,
        help=(
            "Restore the AMD GPU: 'lact' asks lactd to re-apply its current profile (lact cli profile set); "
            "'stock' resets OverDrive (sclk/mclk/voltage offsets), the power cap and - unless LACT runs - "
            "the PMFW fan curve to driver defaults through tux-burnerd, with readback"
        ),
    )
    amd_group.add_argument(
        "--override-lact",
        action="store_true",
        help="Write even though LACT is running (LACT re-applies its own settings on its next apply)",
    )

    auto_uv_group.add_argument(
        "--auto-uv-voltage-scan",
        action="store_true",
        help=(
            "Discover a stable fixed-clock undervolt from the live/default "
            "NVIDIA V/F curve, step the lock voltage down through real editable "
            "VF bins, and verify candidates with Q2RTX plus CUDA load. "
            "Resume an unfinished scan automatically when the GPU and settings match"
        ),
    )
    auto_uv_group.add_argument(
        "--json-events",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-require-final-choice",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--list-auto-uv-profiles",
        action="store_true",
        help=(
            "List saved Auto-UV profiles and exit; use a shown profile id or "
            "candidate id with --auto-uv-profile."
        ),
    )
    auto_uv_group.add_argument(
        "--delete-auto-uv-profiles",
        nargs="+",
        default=[],
        metavar="PROFILE",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--assign-auto-uv-tier",
        nargs=2,
        metavar=("PROFILE", "TIER"),
        help=(
            "Assign a saved verified Auto-UV profile to an adaptive tier and "
            "exit. PROFILE accepts the same selectors as --auto-uv-profile; "
            "TIER is efficiency, balanced, performance, or none."
        ),
    )
    auto_uv_group.add_argument(
        "--fresh-auto-uv-scan",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--clear-auto-uv-state",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-mode",
        choices=AUTO_UV_MODES,
        default=None,
        metavar="MODE",
        help=(
            "Auto-UV preset path. efficiency uses one 2-bin tail sweep to the "
            "voltage floor; balanced uses the 2-bin tail sweep; "
            "performance uses the 2-bin tail sweep plus Auto-OC."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-q2rtx-resolution",
        choices=tuple(Q2RTX_SCAN_RESOLUTIONS),
        default=None,
        help=(
            "Q2RTX resolution for the complete Auto-UV scan and final verification. "
            "Default: auto (4K on desktop RTX 4080/Super, 4090, 5070 Ti, "
            "5080 and 5090, plus RTX 4090/5090 Laptop; "
            "1080p on all others, including unknown GPUs). "
            "Use 1080p to compare a lighter rendering workload. CUDA is unchanged."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-retry-after-freeze",
        action="store_true",
        help=(
            "CLI only; off by default. On an explicit scan resume after a hard "
            "freeze, allow retrying the interrupted clock climb at higher voltage. "
            "May require further reboots. Keeps the failed-point blacklist, "
            "predicted stability margins, floor caution and voltage headroom."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-min-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Lowest voltage for the selected undervolt point. Overrides the detected GPU "
            "table floor and the percentage-drop fallback. Lower-voltage curve "
            "transitions are checked separately at their lower clocks."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-memory-offset-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Memory clock V/F offset to apply during Auto-UV and save with "
            "the final profile. NVML offsets are in transfer-rate units "
            "(MT/s): the realized memory clock rises by half the value. "
            "Clamped to the driver-reported limit for the GPU (fallback "
            f"0..{MAX_AFTERBURNER_MEM_OFFSET_MHZ} when NVML exposes no range)."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-power-limit-w",
        type=int,
        default=None,
        metavar="W",
        help=(
            "Power limit in watts to apply during Auto-UV and save with the "
            "final profile. The UI clamps this to the selected GPU's NVML "
            "minimum and maximum power-limit range."
        ),
    )
    boost_group = auto_uv_group.add_mutually_exclusive_group()
    boost_group.add_argument(
        "--auto-uv-boost",
        choices=("standard", "aggressive"),
        default=None,
        help=(
            "CLI-only boost headroom for every scanned tier: Standard Boost "
            "(default) uses 2 rising tail bins (+30 MHz nominal); Aggressive "
            "Boost uses 4 (+60 MHz nominal). Does not enable freeze retries."
        ),
    )
    boost_group.add_argument(
        "--auto-uv-tail-rise-bins",
        type=int,
        default=None,
        metavar="N",
        help=(
            "How many voltage bins can the voltage curve rise above the locked "
            "undervolt point. Defaults to 2 for every tier; 0 makes the tail flat."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-final-verification-s",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Final verification soak per tier for this scan (every stage of "
            "an adaptive 3-in-1 run uses the same duration). Default 300 "
            "seconds; equivalent to TUX_BURNER_AUTO_UV_FINAL_SECONDS."
        ),
    )
    steam_group.add_argument(
        "--set-steam-overlay-launch",
        metavar="APPID",
        help=(
            "Write the Tux Burner native-overlay launch option for a Steam "
            "app id and exit. The write is verified so running Steam rewrites "
            "are reported instead of treated as success."
        ),
    )
    for tier in ADAPTIVE_TIER_MODES:
        tier_label = tier.capitalize()
        auto_uv_group.add_argument(
            f"--auto-uv-{tier}-power-limit-w",
            type=int,
            default=None,
            metavar="W",
            help=(
                f"Full-scan override: the {tier_label} tier's power limit in "
                "watts, applied for that tier's baseline, descent, final "
                "verification, and saved profile. Only meaningful with "
                "--auto-uv-mode adaptive."
            ),
        )
        auto_uv_group.add_argument(
            f"--auto-uv-{tier}-memory-offset-mhz",
            type=int,
            default=None,
            metavar="MHz",
            help=(
                f"Full-scan override: the {tier_label} tier's memory V/F "
                "offset (NVML transfer-rate units, like "
                "--auto-uv-memory-offset-mhz), applied for that tier's descent "
                "and saved with its profile. Only meaningful with "
                "--auto-uv-mode adaptive."
            ),
        )
        for field, unit in (("voltage", "mV"), ("clock", "MHz")):
            suffix = "voltage_mv" if field == "voltage" else "clock_mhz"
            auto_uv_group.add_argument(
                f"--auto-uv-{tier}-target-{suffix.replace('_', '-')}",
                type=int, default=None, metavar=unit,
                help=(
                    f"Custom {tier_label} {field} target for a single-tier or full scan. "
                    "Defaults use the GPU table. Clock ranges: Efficiency -15%% to Balanced; "
                    "Balanced from Efficiency to Performance; Performance from Balanced "
                    "to Performance +5%%. Change only if you understand GPU tuning."
                ),
            )
    auto_uv_group.add_argument(
        "--auto-oc-target-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Performance-mode Auto-OC voltage cap in mV. Overrides the detected "
            "GPU table target. Defaults are optimized for most GPUs; change only "
            "if you understand voltage/frequency tuning and instability risks."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-oc-target-clock-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Performance-mode Auto-OC core clock cap in MHz. Overrides the detected "
            "GPU table target. Defaults are optimized for most GPUs; change only "
            "if you understand voltage/frequency tuning and instability risks."
        ),
    )
    daemon_group.add_argument(
        "--silent-fan-curve",
        action="store_true",
        help=(
            "Runtime/daemon only: opt in to Tux Burner manual fan-curve "
            "control; by default fan control is left to the GPU driver. "
            "Auto-UV scans write a suggested fan curve automatically when safe."
        ),
    )
    daemon_group.add_argument(
        "--daemonize",
        action="store_true",
        help=(
            "Apply normal runtime through the running tux-burnerd service "
            "after an Auto-UV final curve exists. Auto-UV scans remain "
            "foreground-only."
        ),
    )
    daemon_group.add_argument(
        "--install-systemd-service",
        action="store_true",
        help=(
            "Install or update the Tux Burner daemon service: refresh the "
            "daemon binary at /var/opt/tux-burner/libexec, rewrite the "
            "unit, and restart it. With profile options, also set the "
            "persistent boot profile; without them, an existing boot profile "
            "is kept. Asks for authorization (pkexec/sudo) when not run as "
            "root."
        ),
    )
    daemon_group.add_argument(
        "--uninstall-systemd-service",
        "--deinstall-systemd-service",
        dest="uninstall_systemd_service",
        action="store_true",
        help=(
            "Stop and remove the persistent Tux Burner systemd service. "
            "Asks for authorization (pkexec/sudo) when not run as root."
        ),
    )
    daemon_group.add_argument(
        "--migrate-to-daemon-service",
        action="store_true",
        help=(
            "Install tux-burnerd.service and migrate an existing "
            "legacy TuxBurner.service when possible. Asks for "
            "authorization (pkexec/sudo) when not run as root."
        ),
    )
    daemon_group.add_argument(
        "--daemon-status",
        action="store_true",
        help="Print Tux Burner hardware daemon status and exit.",
    )
    daemon_group.add_argument(
        "--set-main-gpu",
        metavar="GPU-UUID",
        default="",
        help=(
            "On a multi-NVIDIA-GPU system, make a saved startup GPU the "
            "daemon's main monitored GPU after boot."
        ),
    )
    daemon_group.add_argument(
        "--clear-main-gpu",
        action="store_true",
        help="Clear the explicit Main GPU and restore last-saved-GPU behavior.",
    )
    daemon_group.add_argument(
        "--restore-stock",
        action="store_true",
        help=(
            "Recovery: reset the GPU to stock now and make stock the boot "
            "state through the running tux-burnerd service. Saved "
            "profiles are kept. Works without the GUI."
        ),
    )
    daemon_group.add_argument(
        "--auto-uv-profile",
        default="",
        help=(
            "Use an Auto-UV profile by profile id, candidate id, JSON path, "
            "'active', or 'latest' for daemon runtime, systemd service runtime, "
            "and internal final verification."
        ),
    )
    daemon_group.add_argument(
        "--adaptive-auto-uv",
        action="store_true",
        help=(
            "Runtime/daemon only: allow Tux Burner to adapt between saved "
            "Auto-UV profile tiers from base present-frame p95 pacing. Requires "
            "at least one saved verified profile tier. Target defaults to 60 FPS; "
            "override the service env TUX_BURNER_ADAPTIVE_TARGET_FPS for "
            "30, 50, 60, 120, etc."
        ),
    )
    daemon_group.add_argument(
        "--journal-hours",
        type=float,
        default=DEFAULT_JOURNAL_HOURS,
        metavar="N",
        help=(
            "Hours of systemd journal history to suggest after daemonizing; "
            f"default {DEFAULT_JOURNAL_HOURS}."
        ),
    )
    runtime_group.add_argument(
        "--config",
        default=default_cli_config_path(),
        help="Runtime config path to read defaults from",
    )
    runtime_group.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    advanced_group.add_argument(
        "--stability-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_AUTO_UV_FINAL_DURATION_S,
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-stop-request-file",
        default="",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose diagnostic log next to the selected config file "
            "under debug-logs/; with the default config this is "
            "~/.config/TuxBurner/debug-logs"
        ),
    )
    return parser.parse_args(argv)
