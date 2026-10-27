# AMD (amdgpu / RDNA3 / RDNA4) port

This build of Tux Burner drives AMD Radeon cards through the amdgpu kernel
driver's sysfs OverDrive interface instead of NVML/NVAPI. It was ported and
tested read-only on a Radeon RX 9070 XT (Navi 48, gfx1201).

## What maps to what

| Tux Burner (NVIDIA) | AMD equivalent | sysfs |
| --- | --- | --- |
| per-point V/F curve | one global **VDDGFX voltage offset** (mV, negative = undervolt) | `pp_od_clk_voltage`: `vo -110`, `c` |
| core clock offset | **SCLK offset** (MHz) | `pp_od_clk_voltage`: `s 300`, `c` |
| memory clock offset | **MCLK max** (absolute MHz; shown as an offset from the stock `pp_dpm_mclk` top level) | `pp_od_clk_voltage`: `m 1 1375`, `c` |
| power limit | **power cap** in W between `power1_cap_min` and `power1_cap_max` | `hwmon/*/power1_cap` (microwatts) |
| fan speed % | 5-point PMFW **hotspot** fan curve | `gpu_od/fan_ctrl/fan_curve` |
| clock lock / ceiling | not available on RDNA3/RDNA4 (skipped) | - |
| GPU temperature | junction (hotspot), the sensor the firmware curve uses | `hwmon/*/temp2_input` |

All ranges are read live from `OD_RANGE` and `power1_cap_min/max`; nothing is
hard-coded - except one configurable safety floor: by default the VDDGFX
offset is never planned, written or swept below **-150 mV** (the user's chosen
exploration limit; suggested tiers/presets stay at -110 mV or shallower), even
though the driver allows -200 mV on an RX 9070 XT. To go deeper on purpose pass
`--amd-voltage-floor <mV> --allow-below-floor` (CLI) and write the same value
to `/etc/tux-burner/amd-vddgfx-floor-mv` (the daemon's independent floor;
`TUX_BURNERD_AMD_VDDGFX_FLOOR_MV` in the unit environment also works). Every OverDrive commit is followed by a power-cap readback and
restore because the firmware can reset the cap on commit.

## GUI: the AMD Tuning tab

On an amdgpu host the GUI opens on an **AMD Tuning** tab and never runs the
NVIDIA paths (no NVML check, no V/F scan, no Afterburner import):

* **Live status** (sysfs, every 2 s): VDDGFX offset, SCLK offset, MCLK max and
  power cap with their ranges, temperatures, live clocks/power, the PMFW fan
  curve, whether LACT is running (and what it configures), and what
  tux-burnerd reports.
* **Plan editor**: VDDGFX offset, SCLK offset, optional MCLK max and power cap.
  The VDDGFX spin box is clamped to the safety floor (default -150 mV); ticking
  "Allow going below the safety floor" unlocks the floor spin box and shows a
  warning (the daemon's own floor file must be lowered too).
* **Preview writes (dry run)**: the exact sysfs writes, nothing is written.
* **Apply...**: confirmation dialog. While LACT runs you get *Export to LACT*
  (preferred), *Override LACT* (warning) or *Cancel* first.
* **Save as profile...** / **Export to LACT...**, **Stress test...** (OpenCL,
  behind a confirmation) and **Auto-UV ladder** (first click previews the
  rungs, second click asks for confirmation showing the planned range, e.g.
  "+0 -> -150 mV", then runs). The ladder explores down to the configured
  floor with no flags; suggested tiers are capped at -110 mV.
* **Safety floors** status row shows the app floor and the daemon's floor
  (`/etc/tux-burner/amd-vddgfx-floor-mv`, default -150). Lowering the floor
  in the GUI asks for confirmation and writes the daemon floor through
  `tux-burnerd`; Apply/Ladder are blocked while the two are out of sync so
  no rung can be refused and look like a failure. Unticking restores the default.
* **Ladder runs (history)**: every ladder (GUI or CLI) writes
  `~/.config/TuxBurner/amd-runs/amd-run-*.json` (settings, floors, per-rung
  result with failure type - OpenCL mismatch, driver error, hang timeout,
  process crash, setup error, apply refused by floor/range/LACT - plus the
  sampled operating points) and a log under `debug-logs/`. Select a run to see
  the details, "Show run in the Auto-UV graph" to plot it.
* **Auto-UV graph**: during a ladder the Clock-vs-Voltage plot shows the live
  operating points (hwmon `in0_input` VDDGFX mV vs `freq1_input` sclk) per
  rung, coloured by result, the current rung highlighted, stock vs deepest
  stable emphasised; the "Undervolt runs" table mirrors the rungs.
* **Restore Defaults** (Profiles page) on AMD offers "Return to LACT's
  settings" (`lact cli profile set`, no restart) or "Driver stock defaults"
  through the daemon with before/after readback.

The big "AMD Auto-UV ladder" button at the top routes to the same preview.
Every write or load runs `tux-burner-cli --amd-...` as a child process so
the GUI and CLI share one validation and LACT-gating implementation.

## Commands

```bash
tburn-cli --amd-status                                   # read-only: ranges, live values, LACT state
tburn-cli --amd-plan vo=-110 sclk=300 mclk=1375 power=374   # preview the sysfs writes (dry run)
tburn-cli --amd-plan vo=-110 power=110% --amd-apply --amd-dry-run
tburn-cli --amd-plan vo=-110 sclk=300 --amd-apply        # write through tux-burnerd
tburn-cli --amd-plan vo=-110 sclk=300 mclk=1375 power=374 --amd-save-profile quiet --amd-profile-tier balanced
tburn-cli --amd-plan vo=-110 fan=25:15,56:60,80:100 --amd-export-lact ~/lact-amd.yaml
tburn-cli --amd-stress 60                                # OpenCL brute-force load, no tuning writes
tburn-cli --amd-auto-uv --amd-dry-run                        # VDDGFX ladder plan
tburn-cli --amd-auto-uv                                  # run the ladder (sustained load!)
```

`power=` accepts watts or a percentage of the default cap (`power=123%`).
`fan=` takes up to five `TEMP:PERCENT` points; the firmware minimum speed is a
hard floor.

## Auto-UV on AMD

The NVIDIA Auto-UV scan searches a per-point curve; AMD has one offset, so
`--amd-auto-uv` runs a one-dimensional ladder: 0 mV, -10, -20 ... down to the
driver minimum, each rung verified with the OpenCL brute-force workload (the
CUDA workload ported 1:1, same exit codes). The deepest passing rung plus a
20 mV margin becomes three profiles: Efficiency (stock power cap), Balanced
(the cap in force when the scan started) and Performance (your SCLK offset,
maximum cap). A rung marker file turns a hard reset during a rung into "that
offset is unsafe" on the next run.

## LACT coexistence

If `lactd` is running it owns the GPU: Tux Burner refuses `--amd-apply` and
`--amd-auto-uv` unless `--override-lact` is passed, never touches the PMFW fan
curve while LACT runs, and `--amd-export-lact` writes a LACT `gpus:` block
(voltage_offset, gpu_clock_offsets, max_memory_clock, power_cap, a 5-point
curve) so LACT can carry the tuning. Do not restart lactd on a live desktop to
pick up changes; reboot instead.

## Not ported

Afterburner import, the V/F curve editor, the NVAPI latency shim (overlay
latency falls back to the Vulkan layer), NVIDIA RTD3 handling and persistence
mode have no AMD equivalent and are inert on amdgpu.
