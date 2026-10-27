# <img src="work/docs/assets/tux-burner-logo.png" alt="Tux Burner logo" width="38" align="top"> Tux Burner

**GPU undervolting, overclocking and per-game tuning for Linux - AMD Radeon (amdgpu) and NVIDIA.**

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.

Tux Burner is a fork of PenguinBurner. Changes are Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.

> **Based on PenguinBurner by jpietek** (<https://github.com/jpietek/PenguinBurner>), GPL-3.0-or-later.
> Tux Burner is an independent fork. It is not endorsed by or affiliated with the author of PenguinBurner.
> Please report Tux Burner problems here, not upstream. Details: [CREDITS.md](work/CREDITS.md).

<p align="center">
  <img alt="Tux Burner, AMD Tuning page" src="work/docs/assets/hero.png" width="860">
</p>

<p align="center"><sub>The AMD Tuning page, in a dry-run preview. The screenshot is taken against a fake sysfs tree: nothing was written to a GPU.</sub></p>

> **Tested on one PC.** The AMD backend was developed and tested only on a Ryzen 7 7800X3D with a Radeon RX 9070 XT.
> Other AMD generations and the NVIDIA backend in this fork may have bugs. Please open an issue with your hardware
> and the report: <https://github.com/eugenesflanagin-spec/tux-burner/issues>.

## What it does

Lower the voltage of your graphics card so it uses less power, runs cooler and quieter at the same speed, or push it
a little faster, then save the result as a profile.

- **AMD amdgpu OverDrive backend** (the main addition of this fork): global VDDGFX voltage offset, core clock
  offset, memory clock ceiling and power cap, written by a small root service. The 5-point fan curve is read and shown; Apply
  never writes it (the silent-fan engine in the daemon can run one, and fan control is refused while LACT runs).
- **Auto-UV ladder for AMD**: steps the voltage down one rung at a time, stress-tests each rung with an OpenCL
  workload, and saves Efficiency / Balanced / Performance profiles from the deepest stable rung plus a margin.
- **Safety floors**: the voltage offset cannot go deeper than -150 mV by default (suggested settings stay at -110 mV
  or shallower). The app and the root service each enforce their own floor.
- **LACT friendly**: if LACT owns the GPU, Tux Burner asks first (`--override-lact`) and can export your plan as a
  LACT configuration block.
- **Profiles** work with AMD profiles: list, apply, rename, tiers, restore defaults ("return to LACT's settings").
- **Command line** (`tburn-cli`) for everything: `--amd-status`, `--amd-plan`, `--amd-apply`, `--amd-auto-uv`, ...
- From PenguinBurner (upstream): the Qt window, the NVIDIA Auto-UV scan, adaptive per-game switching, the
  Steam, Lutris, Heroic and Faugus **Game Library** and the in-game Vulkan overlay. These were written for NVIDIA and
  have had less testing on AMD.

## Install

**Arch Linux / CachyOS** (release package, see the Releases page):

```bash
sudo pacman -U tux-burner-0.99-1-x86_64.pkg.tar.zst
```

It `provides` and `conflicts` with `penguin-burner` and `penguin-burner-amd` and `replaces` `penguin-burner-amd`.
Or build it from this repository:

```bash
git clone https://github.com/eugenesflanagin-spec/tux-burner
cd tux-burner && packaging/build-arch-local.sh
```

**Other distributions:** each release also carries a `.deb`, a `.rpm`, `SHA256SUMS` and the guide as a PDF.
Check downloads with `sha256sum -c SHA256SUMS --ignore-missing`. These packages are built by the release workflow
and have had less testing than the Arch package.

Then install the root service once, as your normal user:

```bash
tburn-cli --install-systemd-service
tburn-cli --amd-status
tux-burner
```

Coming from PenguinBurner or `penguin-burner-amd`? Your settings are copied, not moved; the steps are in the
[guide](work/docs/user-guide/USER-GUIDE.md#12-moving-from-penguinburner-or-penguin-burner-amd).

## Documentation

- [User guide](work/docs/user-guide/USER-GUIDE.md) ([PDF](work/docs/user-guide/Tux-Burner-User-Guide.pdf), [HTML](work/docs/user-guide/Tux-Burner-User-Guide.html)) - plain words, then detail, options and a glossary.
- [Command-line reference](work/docs/user-guide/CLI-REFERENCE.md) and `man tburn-cli`.
- [AMD design notes](work/docs/amd.md) - what maps to which sysfs file.
- [Glossary](work/docs/user-guide/GLOSSARY.md), [CHANGELOG.md](work/CHANGELOG.md), [CREDITS.md](work/CREDITS.md), [SECURITY.md](work/SECURITY.md).
- Upstream's NVIDIA-era feature guides are kept under [docs/features](work/docs/features/README.md); they still describe PenguinBurner's NVIDIA behaviour.

## Safety

Tux Burner changes real GPU voltage, clocks, power and fans, and runs sustained heavy loads. A bad setting can hang
the GPU, freeze the PC or force a reboot. Nothing is written until you press a button and confirm; dry runs write
nothing. You use it at your own risk. Test on a PC you can reboot.

## Licence and credits

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later (see [LICENSE](work/LICENSE)).

PenguinBurner is by jpietek, also GPL-3.0-or-later, and all its copyright and licence notices are kept. Other
credits (LACT, the amdgpu driver, libraries) are in [CREDITS.md](work/CREDITS.md). Tux Burner is not endorsed by or
affiliated with the author of PenguinBurner.

Bug reports: <https://github.com/eugenesflanagin-spec/tux-burner/issues> (include `tburn-cli --version`,
your hardware and the report; remove your host name, IP addresses and GPU serial first).
