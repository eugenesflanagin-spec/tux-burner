# Changelog

Tux Burner is a fork of PenguinBurner. Changes are Copyright (C) 2026 by Eugene Flanagin.
Licensed under GPL-3.0-or-later. Entries older than 0.99 are upstream PenguinBurner releases
(see `docs/release-notes-0.*.md`).

## 0.99 - 2026-10-27

First Tux Burner release, forked from PenguinBurner 0.8.3 (by jpietek).

### Renamed

- The product is now **Tux Burner**. Package `tux-burner` (was `penguin-burner-amd`), commands
  `tux-burner` / `tburn` (GUI) and `tux-burner-cli` / `tburn-cli` (was `pburn-cli`), daemon
  `tux-burnerd` and `tux-burnerd.service`, config `~/.config/TuxBurner/`, system files under
  `/etc/tux-burner/`, `/var/lib/tux-burner/`, `/var/opt/tux-burner/`, `/run/tux-burner/`,
  environment variables `TUX_BURNER_*` / `TUX_BURNERD_*`, desktop entry
  `io.github.eugenesflanagin_spec.TuxBurner`.
- Migration: the package install script copies `/etc/penguin-burner` and `/var/lib/penguin-burner`
  to the new names (the old ones are left in place); the app copies `~/.config/PenguinBurner` to
  `~/.config/TuxBurner` on first use (saved profiles, run history, settings); the daemon also reads
  the old `/etc/penguin-burner/amd-vddgfx-floor-mv` if the new floor file does not exist.
- `provides=(penguin-burner penguin-burner-amd)`, `conflicts=` the same, `replaces=(penguin-burner-amd)`.

### What the fork adds (compared with PenguinBurner 0.8.3)

- **AMD amdgpu backend** in the Rust root daemon: reads and writes the amdgpu OverDrive sysfs
  interface (global VDDGFX offset, SCLK offset, memory clock max, power cap, 5-point PMFW fan curve),
  with the power cap restored after every OverDrive commit. Tested on a Radeon RX 9070 XT (gfx1201).
- **OpenCL stress workload** (`stability/opencl_bruteforce.py`): the CUDA brute-force workload ported
  to OpenCL, same exit codes.
- **`--amd-*` command line**: `--amd-status`, `--amd-plan`, `--amd-dry-run`, `--amd-apply`,
  `--amd-save-profile`, `--amd-profile-tier`, `--amd-export-lact`, `--amd-stress`, `--amd-auto-uv`
  (+ `-start/-step/-floor/-rung-seconds/-margin`), `--amd-voltage-floor`, `--allow-below-floor`,
  `--override-lact`, `--amd-set-daemon-floor`.
- **Configurable VDDGFX safety floor** (default -150 mV; suggested tiers never deeper than -110 mV),
  enforced separately in the CLI/GUI and in the daemon, synchronised from the GUI.
- **AMD Tuning tab** in the GUI: live status, plan editor, dry-run preview, apply with confirmation,
  LACT handling, Auto-UV ladder with live Clock-vs-Voltage plot and run history, safety-floor controls.
- **Profiles page works with AMD profiles** (list, apply, rename, AMD columns, restore defaults with
  "return to LACT's settings").
- **LACT coexistence**: detection, `--override-lact`, export of a LACT `gpus:` block.
- New: `--version`; man pages; user guide (Markdown, HTML, PDF); release packaging and CI workflow.
- Cargo crate licence field is now `GPL-3.0-or-later`; About dialog and `--version` carry the fork notice.
- The `.gitignore` no longer hides `work/burnerd/src` (it was matched by the makepkg `src/` rule).
