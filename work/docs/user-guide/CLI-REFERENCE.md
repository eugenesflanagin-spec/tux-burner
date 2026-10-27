# Tux Burner - Command-line reference

Version 0.99. Programs: `tburn-cli` and `tux-burner-cli` (the same program). `tux-burner` and `tburn` start the window.

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later. It is a fork of PenguinBurner by jpietek (GPL-3.0-or-later). Not endorsed by or affiliated with the author of PenguinBurner.

Plain words first: the commands that **only read** are `--version`, `--help`, `--amd-status`, `--daemon-status`, `--list-auto-uv-profiles`, and any `--amd-plan ...` **without** `--amd-apply` (that one prints what would be written). The commands that **write to the GPU** are `--amd-apply`, `--amd-auto-uv`, `--restore-stock`, `--amd-restore stock`, `--daemonize` and the upstream Auto-UV scan. `--amd-stress` and `--amd-auto-uv` also put heavy sustained load on the GPU. Write commands go through the root service `tux-burnerd`, and are refused while LACT runs unless you add `--override-lact`.

## The AMD options in short

| Option | What it does |
| --- | --- |
| `--amd-status` | Show ranges, live values, fan curve and LACT state. Read-only. |
| `--amd-plan KEY=VALUE ...` | Describe a plan: `vo=<mV>` `sclk=<MHz>` `mclk=<MHz>` `power=<W>` or `<NN%>` `fan=T:P,T:P,...` (up to 5 points) `zero_rpm=on|off`. Alone it only previews. |
| `--amd-dry-run` | With `--amd-apply`, `--amd-save-profile`, `--amd-auto-uv` or `--amd-stress`: show, change nothing. |
| `--amd-apply` | Write the plan now (checked against the live driver ranges and both floors). |
| `--amd-save-profile NAME` | Save the plan as a Tux Burner profile (marked unverified). |
| `--amd-profile-tier T` | Tier for the saved profile: `efficiency`, `balanced`, `performance`. |
| `--amd-export-lact [PATH]` | Write the plan as a LACT config fragment (to the screen without PATH). |
| `--amd-stress SECONDS` | OpenCL stress workload. Heavy load. No tuning writes. |
| `--amd-auto-uv` | The ladder. Heavy load. Options: `--amd-auto-uv-start MV` (0), `--amd-auto-uv-step MV` (10), `--amd-auto-uv-floor MV` (driver minimum, never below the app floor), `--amd-auto-uv-rung-seconds S` (60), `--amd-auto-uv-margin MV` (20). |
| `--amd-voltage-floor MV` | The app's floor (default -150). Deeper needs `--allow-below-floor`. |
| `--allow-below-floor` | Confirms a floor deeper than -150 mV. |
| `--amd-set-daemon-floor MV\|default` | Set the root service's own floor. |
| `--amd-restore lact\|stock` | Give the GPU back to LACT, or reset to driver defaults. |
| `--override-lact` | Write although LACT is running. |

## Full `--help` output

This is the output of `tburn-cli --help`, kept here so the guide matches the program. The upstream sections (Auto-UV, Steam overlay setup, Runtime and daemon, Runtime tuning, Advanced/debug) describe the NVIDIA-era features; many of them are not used on AMD.

```text
usage: tburn-cli [options]

Tux Burner: GPU undervolting and overclocking for Linux (AMD amdgpu
and NVIDIA). A fork of PenguinBurner by jpietek (GPL-3.0-or-later).
Run Auto-UV scans, manage the saved Efficiency/Balanced/Performance
profiles, drive the adaptive daemon runtime, and set up the Steam
launch wrapper and in-game overlay.

options:
  -h, --help            show this help message and exit
  --version             Print the Tux Burner version and exit

Auto-UV:
  --auto-uv-voltage-scan
                        Discover a stable fixed-clock undervolt from the
                        live/default NVIDIA V/F curve, step the lock voltage
                        down through real editable VF bins, and verify
                        candidates with Q2RTX plus CUDA load. Resume an
                        unfinished scan automatically when the GPU and
                        settings match
  --list-auto-uv-profiles
                        List saved Auto-UV profiles and exit; use a shown
                        profile id or candidate id with --auto-uv-profile.
  --assign-auto-uv-tier PROFILE TIER
                        Assign a saved verified Auto-UV profile to an adaptive
                        tier and exit. PROFILE accepts the same selectors as
                        --auto-uv-profile; TIER is efficiency, balanced,
                        performance, or none.
  --auto-uv-mode MODE   Auto-UV preset path. efficiency uses one 2-bin tail
                        sweep to the voltage floor; balanced uses the 2-bin
                        tail sweep; performance uses the 2-bin tail sweep plus
                        Auto-OC.
  --auto-uv-q2rtx-resolution {auto,1080p,1440p,4k}
                        Q2RTX resolution for the complete Auto-UV scan and
                        final verification. Default: auto (4K on desktop RTX
                        4080/Super, 4090, 5070 Ti, 5080 and 5090, plus RTX
                        4090/5090 Laptop; 1080p on all others, including
                        unknown GPUs). Use 1080p to compare a lighter
                        rendering workload. CUDA is unchanged.
  --auto-uv-retry-after-freeze
                        CLI only; off by default. On an explicit scan resume
                        after a hard freeze, allow retrying the interrupted
                        clock climb at higher voltage. May require further
                        reboots. Keeps the failed-point blacklist, predicted
                        stability margins, floor caution and voltage headroom.
  --auto-uv-min-voltage-mv mV
                        Lowest voltage for the selected undervolt point.
                        Overrides the detected GPU table floor and the
                        percentage-drop fallback. Lower-voltage curve
                        transitions are checked separately at their lower
                        clocks.
  --auto-uv-memory-offset-mhz MHz
                        Memory clock V/F offset to apply during Auto-UV and
                        save with the final profile. NVML offsets are in
                        transfer-rate units (MT/s): the realized memory clock
                        rises by half the value. Clamped to the driver-
                        reported limit for the GPU (fallback 0..2000 when NVML
                        exposes no range).
  --auto-uv-power-limit-w W
                        Power limit in watts to apply during Auto-UV and save
                        with the final profile. The UI clamps this to the
                        selected GPU's NVML minimum and maximum power-limit
                        range.
  --auto-uv-boost {standard,aggressive}
                        CLI-only boost headroom for every scanned tier:
                        Standard Boost (default) uses 2 rising tail bins (+30
                        MHz nominal); Aggressive Boost uses 4 (+60 MHz
                        nominal). Does not enable freeze retries.
  --auto-uv-tail-rise-bins N
                        How many voltage bins can the voltage curve rise above
                        the locked undervolt point. Defaults to 2 for every
                        tier; 0 makes the tail flat.
  --auto-uv-final-verification-s SECONDS
                        Final verification soak per tier for this scan (every
                        stage of an adaptive 3-in-1 run uses the same
                        duration). Default 300 seconds; equivalent to
                        TUX_BURNER_AUTO_UV_FINAL_SECONDS.
  --auto-uv-efficiency-power-limit-w W
                        Full-scan override: the Efficiency tier's power limit
                        in watts, applied for that tier's baseline, descent,
                        final verification, and saved profile. Only meaningful
                        with --auto-uv-mode adaptive.
  --auto-uv-efficiency-memory-offset-mhz MHz
                        Full-scan override: the Efficiency tier's memory V/F
                        offset (NVML transfer-rate units, like --auto-uv-
                        memory-offset-mhz), applied for that tier's descent
                        and saved with its profile. Only meaningful with
                        --auto-uv-mode adaptive.
  --auto-uv-efficiency-target-voltage-mv mV
                        Custom Efficiency voltage target for a single-tier or
                        full scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-uv-efficiency-target-clock-mhz MHz
                        Custom Efficiency clock target for a single-tier or
                        full scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-uv-balanced-power-limit-w W
                        Full-scan override: the Balanced tier's power limit in
                        watts, applied for that tier's baseline, descent,
                        final verification, and saved profile. Only meaningful
                        with --auto-uv-mode adaptive.
  --auto-uv-balanced-memory-offset-mhz MHz
                        Full-scan override: the Balanced tier's memory V/F
                        offset (NVML transfer-rate units, like --auto-uv-
                        memory-offset-mhz), applied for that tier's descent
                        and saved with its profile. Only meaningful with
                        --auto-uv-mode adaptive.
  --auto-uv-balanced-target-voltage-mv mV
                        Custom Balanced voltage target for a single-tier or
                        full scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-uv-balanced-target-clock-mhz MHz
                        Custom Balanced clock target for a single-tier or full
                        scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-uv-performance-power-limit-w W
                        Full-scan override: the Performance tier's power limit
                        in watts, applied for that tier's baseline, descent,
                        final verification, and saved profile. Only meaningful
                        with --auto-uv-mode adaptive.
  --auto-uv-performance-memory-offset-mhz MHz
                        Full-scan override: the Performance tier's memory V/F
                        offset (NVML transfer-rate units, like --auto-uv-
                        memory-offset-mhz), applied for that tier's descent
                        and saved with its profile. Only meaningful with
                        --auto-uv-mode adaptive.
  --auto-uv-performance-target-voltage-mv mV
                        Custom Performance voltage target for a single-tier or
                        full scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-uv-performance-target-clock-mhz MHz
                        Custom Performance clock target for a single-tier or
                        full scan. Defaults use the GPU table. Clock ranges:
                        Efficiency -15% to Balanced; Balanced from Efficiency
                        to Performance; Performance from Balanced to
                        Performance +5%. Change only if you understand GPU
                        tuning.
  --auto-oc-target-voltage-mv mV
                        Performance-mode Auto-OC voltage cap in mV. Overrides
                        the detected GPU table target. Defaults are optimized
                        for most GPUs; change only if you understand
                        voltage/frequency tuning and instability risks.
  --auto-oc-target-clock-mhz MHz
                        Performance-mode Auto-OC core clock cap in MHz.
                        Overrides the detected GPU table target. Defaults are
                        optimized for most GPUs; change only if you understand
                        voltage/frequency tuning and instability risks.

Steam overlay setup:
  --set-steam-overlay-launch APPID
                        Write the Tux Burner native-overlay launch option for
                        a Steam app id and exit. The write is verified so
                        running Steam rewrites are reported instead of treated
                        as success.

Runtime and daemon essentials:
  --silent-fan-curve    Runtime/daemon only: opt in to Tux Burner manual fan-
                        curve control; by default fan control is left to the
                        GPU driver. Auto-UV scans write a suggested fan curve
                        automatically when safe.
  --daemonize           Apply normal runtime through the running tux-burnerd
                        service after an Auto-UV final curve exists. Auto-UV
                        scans remain foreground-only.
  --install-systemd-service
                        Install or update the Tux Burner daemon service:
                        refresh the daemon binary at /var/opt/tux-
                        burner/libexec, rewrite the unit, and restart it. With
                        profile options, also set the persistent boot profile;
                        without them, an existing boot profile is kept. Asks
                        for authorization (pkexec/sudo) when not run as root.
  --uninstall-systemd-service, --deinstall-systemd-service
                        Stop and remove the persistent Tux Burner systemd
                        service. Asks for authorization (pkexec/sudo) when not
                        run as root.
  --migrate-to-daemon-service
                        Install tux-burnerd.service and migrate an existing
                        legacy TuxBurner.service when possible. Asks for
                        authorization (pkexec/sudo) when not run as root.
  --daemon-status       Print Tux Burner hardware daemon status and exit.
  --set-main-gpu GPU-UUID
                        On a multi-NVIDIA-GPU system, make a saved startup GPU
                        the daemon's main monitored GPU after boot.
  --clear-main-gpu      Clear the explicit Main GPU and restore last-saved-GPU
                        behavior.
  --restore-stock       Recovery: reset the GPU to stock now and make stock
                        the boot state through the running tux-burnerd
                        service. Saved profiles are kept. Works without the
                        GUI.
  --auto-uv-profile AUTO_UV_PROFILE
                        Use an Auto-UV profile by profile id, candidate id,
                        JSON path, 'active', or 'latest' for daemon runtime,
                        systemd service runtime, and internal final
                        verification.
  --adaptive-auto-uv    Runtime/daemon only: allow Tux Burner to adapt between
                        saved Auto-UV profile tiers from base present-frame
                        p95 pacing. Requires at least one saved verified
                        profile tier. Target defaults to 60 FPS; override the
                        service env TUX_BURNER_ADAPTIVE_TARGET_FPS for 30, 50,
                        60, 120, etc.
  --journal-hours N     Hours of systemd journal history to suggest after
                        daemonizing; default 4.

Runtime tuning:
  --config CONFIG       Runtime config path to read defaults from
  --gpu-index GPU_INDEX
                        Override the configured GPU index

Advanced/debug:
  --debug-log           Write a verbose diagnostic log next to the selected
                        config file under debug-logs/; with the default config
                        this is ~/.config/TuxBurner/debug-logs

AMD (amdgpu OverDrive) tuning:
  Local AMD port. Reads come from sysfs; writes go through tux-burnerd. While LACT (lactd) runs it owns the GPU: writes need --override-lact.

  --amd-status          Print the AMD GPU's OverDrive ranges, live values, fan
                        curve and LACT state (read-only)
  --amd-plan KEY=VALUE [KEY=VALUE ...]
                        Tuning plan: vo=<mV> (VDDGFX offset, negative =
                        undervolt), sclk=<MHz> offset, mclk=<MHz> max memory
                        clock, power=<W>|<NN%> cap, fan=T:P,T:P,... (max 5
                        points), zero_rpm=on|off. Alone it previews the exact
                        sysfs writes (dry run)
  --amd-dry-run         With --amd-apply/--amd-save-profile/--amd-auto-
                        uv/--amd-stress: show what would happen, change
                        nothing
  --amd-apply           Apply --amd-plan now through tux-burnerd (validated
                        against the live OD_RANGE)
  --amd-save-profile NAME
                        Save --amd-plan as an (unverified) Tux Burner profile
                        the GUI and daemon can apply
  --amd-profile-tier {efficiency,balanced,performance}
                        Tier to assign the saved AMD profile to
  --amd-export-lact [PATH]
                        Write --amd-plan as a LACT config fragment (stdout
                        when PATH is omitted)
  --amd-stress SECONDS  Run the OpenCL brute-force stability workload
                        (sustained GPU load; no tuning writes)
  --amd-auto-uv         AMD Auto-UV: step the VDDGFX offset down rung by rung,
                        stress each rung, save three tiers
  --amd-auto-uv-start MV
                        First rung (default 0 = stock)
  --amd-auto-uv-step MV
                        Rung size in mV (default 10)
  --amd-auto-uv-floor MV
                        Lowest offset to try (default: driver minimum)
  --amd-auto-uv-rung-seconds S
                        Stress seconds per rung (default 60)
  --amd-auto-uv-margin MV
                        Safety margin added to the deepest stable rung
                        (default 20)
  --amd-voltage-floor MV
                        Deepest VDDGFX offset Tux Burner will plan, write or
                        sweep to (default -150 mV on this machine). Going
                        deeper also needs --allow-below-floor; the daemon
                        enforces its own floor (/etc/tux-burner/amd-vddgfx-
                        floor-mv)
  --allow-below-floor   Confirm an --amd-voltage-floor deeper than the default
                        -150 mV
  --amd-set-daemon-floor MV|default
                        Set the DAEMON's VDDGFX safety floor (/etc/tux-
                        burner/amd-vddgfx-floor-mv, written by tux-burnerd) or
                        'default' to restore -150 mV. Values deeper than -150
                        also need --allow-below-floor. Without this the daemon
                        refuses deeper writes even if --amd-voltage-floor
                        allows them
  --amd-restore {lact,stock}
                        Restore the AMD GPU: 'lact' asks lactd to re-apply its
                        current profile (lact cli profile set); 'stock' resets
                        OverDrive (sclk/mclk/voltage offsets), the power cap
                        and - unless LACT runs - the PMFW fan curve to driver
                        defaults through tux-burnerd, with readback
  --override-lact       Write even though LACT is running (LACT re-applies its
                        own settings on its next apply)
```

## Exit status

`0` success. Non-zero: a refused or failed operation (the message says why). The OpenCL stress workload itself exits with 0 = passed, 1 = setup error (no device, build failed), 3 = instability (a wrong result), 4 = hang (the GPU stopped answering); the ladder reads these to decide a rung's result.
