# Credits

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.

Tux Burner is a fork of PenguinBurner. Changes are Copyright (C) 2026 by Eugene Flanagin.
Licensed under GPL-3.0-or-later.

## Based on PenguinBurner

Tux Burner is based on **PenguinBurner** by **jpietek**
(<https://github.com/jpietek/PenguinBurner>), version 0.8.3, licensed under
**GPL-3.0-or-later**. Almost everything in this program that is not listed under
"What the fork adds" in [CHANGELOG.md](CHANGELOG.md) is the work of PenguinBurner's
author and contributors: the Qt interface, the Auto-UV search and its verification,
the adaptive per-game runtime, the Game Library (Steam, Lutris, Heroic, Faugus), the
Vulkan overlay layer, the Rust root daemon's profile engine, the packaging and the
documentation that this fork builds on. All upstream copyright and licence notices
and the upstream `LICENSE` file are kept unchanged.

Tux Burner is an independent fork. It is **not endorsed by, supported by or affiliated
with** the author of PenguinBurner. Please do not send Tux Burner bug reports to the
upstream project; report them at
<https://github.com/eugenesflanagin-spec/tux-burner/issues>. Problems that also
exist in upstream PenguinBurner should be reported upstream as well.

The upstream release notes (`docs/release-notes-0.*.md`) and the web reports under
`docs/pages/` are kept as they were published upstream; they mention PenguinBurner by
its original name and its NVIDIA results.

## Other public sources

- **LACT** (<https://github.com/ilya-zlobintsev/LACT>) - the AMD GPU control application whose
  configuration format `--amd-export-lact` writes and whose coexistence rules Tux Burner follows.
- **The Linux kernel amdgpu driver** - the OverDrive sysfs interface
  (`pp_od_clk_voltage`, `gpu_od/fan_ctrl/fan_curve`, hwmon `power1_cap`) documented in the kernel's
  `Documentation/gpu/amdgpu/thermal.rst` and implemented in `smu_v14_0_2_ppt.c`.
- **Q2RTX** and the headless Q2RTX fork by jpietek, used by upstream's NVIDIA stability workload.
- **OpenCL** (Khronos) - the AMD stability workload is an OpenCL port of the upstream CUDA
  brute-force workload.
- Python libraries: PySide6 (Qt for Python), PyQtGraph, PyYAML, Colorama. Rust crates used by the
  daemon are listed in `burnerd/Cargo.lock`.

## Tested hardware

Tux Burner's AMD backend was developed and tested on **one PC**: Ryzen 7 7800X3D with a Radeon
RX 9070 XT. Other AMD generations and the NVIDIA backend may have bugs; please open an issue
with your hardware and the report.
