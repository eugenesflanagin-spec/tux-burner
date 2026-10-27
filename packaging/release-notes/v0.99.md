# Tux Burner 0.99 - first release, forked from PenguinBurner 0.8.3

Tux Burner is a fork of PenguinBurner. Changes are Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.

Tux Burner is based on [PenguinBurner](https://github.com/jpietek/PenguinBurner) by jpietek (version 0.8.3), GPL-3.0-or-later. Tux Burner is an independent fork, not endorsed by or affiliated with the author of PenguinBurner. Other credits (LACT, the amdgpu driver, libraries) are in `CREDITS.md`.

## What it is

GPU undervolting, overclocking and per-game tuning for Linux. This release adds **AMD Radeon (amdgpu) support** to PenguinBurner and renames the program.

## What's new compared with PenguinBurner 0.8.3

- AMD amdgpu OverDrive backend in the root daemon: VDDGFX offset, core clock offset, memory clock ceiling, power cap, 5-point fan curve.
- OpenCL stress workload and an AMD Auto-UV **ladder** (0 mV, -10, -20 ... each rung stress-tested).
- `--amd-*` command line, `--version`, man pages, and a user guide (Markdown, HTML, PDF).
- A configurable VDDGFX safety floor (default -150 mV; suggestions stay at -110 mV or shallower), enforced separately by the app and by the daemon.
- An AMD Tuning tab, AMD-aware Profiles page, LACT coexistence (`--override-lact`, export to LACT).
- Renamed: package `tux-burner`, commands `tux-burner`/`tburn` and `tux-burner-cli`/`tburn-cli`, service `tux-burnerd.service`, config `~/.config/TuxBurner/`.

## Upgrading from PenguinBurner or penguin-burner-amd

Your settings are copied to the new names, never moved. The old service is left alone; switch once:

```bash
sudo pacman -U tux-burner-0.99-1-x86_64.pkg.tar.zst
sudo systemctl disable --now penguin-burnerd.service
sudo rm -f /etc/systemd/system/penguin-burnerd.service
sudo tburn-cli --install-systemd-service
```

## Downloads

Arch package (`.pkg.tar.zst`), `.deb`, `.rpm`, source tarball, `SHA256SUMS`, and `Tux-Burner-User-Guide.pdf`. Check with `sha256sum -c SHA256SUMS --ignore-missing`.

## Tested on one PC (and what that means)

I build and test Tux Burner on a single machine: a Ryzen 7 7800X3D with a Radeon RX 9070 XT. Anything that needs other hardware - other AMD generations (RDNA 2 and older, RDNA 3 cards), NVIDIA GPUs through the upstream backend, Intel graphics, multi-GPU PCs, ARM - may have bugs I have not met. The `.deb` and `.rpm` packages were not installed and tried on real Debian, Ubuntu or Fedora systems before release. If you find a problem, please open an issue at <https://github.com/eugenesflanagin-spec/tux-burner/issues> with your hardware and the report (`tburn-cli --version`, `tburn-cli --amd-status`), with your host name, IP addresses and GPU serial removed.

Tuning changes real hardware: a bad voltage offset can freeze the PC. Nothing is written until you press a button and confirm.
