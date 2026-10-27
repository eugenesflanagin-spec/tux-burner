# Install Tux Burner

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later. It is a fork of
PenguinBurner by jpietek, and not endorsed by or affiliated with its author.

**AMD (the main target of this fork):** use the in-kernel amdgpu driver with OverDrive enabled. Tux Burner
reads and writes `pp_od_clk_voltage`, `power1_cap` and `gpu_od/fan_ctrl/fan_curve`. The stress workload needs an
OpenCL driver (`rocm-opencl-runtime`, or Mesa rusticl). See the [user guide](user-guide/USER-GUIDE.md#3-install).

**NVIDIA (upstream backend, less tested in this fork):** install the NVIDIA proprietary driver and CUDA first.
Tux Burner supports RTX 30 (Ampere), RTX 40 (Ada), and RTX 50 (Blackwell) cards. The driver must provide
`libnvidia-ml.so.1` for NVML telemetry/GPU discovery, `libnvidia-api.so.1` for NVIDIA V/F curve control, the
Vulkan runtime for Q2RTX, and `libcuda.so.1` for the CUDA companion test.

## Release packages (recommended)

Each release on the repository's Releases page has an Arch package (`.pkg.tar.zst`), a `.deb`, a `.rpm`, the
source tarball, `SHA256SUMS` and the guide as a PDF.

```bash
sha256sum -c SHA256SUMS --ignore-missing
sudo pacman -U tux-burner-0.99-1-x86_64.pkg.tar.zst          # Arch / CachyOS
sudo apt install ./tux-burner_0.99-1_amd64.deb                 # Debian / Ubuntu (less tested)
sudo dnf install ./tux-burner-0.99-1.x86_64.rpm                # Fedora (less tested)
```

The Arch package `provides` and `conflicts` with `penguin-burner` and `penguin-burner-amd` and `replaces`
`penguin-burner-amd`. Then install the root service once: `tburn-cli --install-systemd-service`.

## Build the Arch package from a checkout

```bash
packaging/build-arch-local.sh      # builds in a scratch directory, installs nothing
```

## Flatpak, pip, COPR and PPA (not published for Tux Burner)

## Flatpak

The Flatpak recipe is kept from PenguinBurner (see the [Flatpak guide](flatpak.md)); when a host Steam installation
is detected, the first GUI launch silently installs and verifies the host wrappers. Tux Burner is not published as a
Flatpak yet.

Tux Burner is not published on PyPI, Flathub, COPR, a PPA or the AUR. The Flatpak files under `packaging/flatpak/`
and the sections of the upstream guide below that mention them are kept from PenguinBurner for reference; the
`penguinburner` flatpak remote and the upstream COPR/PPA install **PenguinBurner**, not Tux Burner.
A local wheel can be installed from a checkout (see "Local wheel" below).

## Entry points

- GUI: `tux-burner` (alias `tburn`)
- CLI: `tux-burner-cli` (alias `tburn-cli`)

The pip and Flatpak-wrapper installs also add desktop-friendly command entries.
If the commands are not found, make sure `~/.local/bin` is on your `PATH`.

## Local wheel (from a checkout)

```bash
python -m pip install --user --no-index --no-deps --find-links dist --upgrade tux-burner
```

## Root daemon (from a checkout)

The privileged root daemon (`tux-burnerd`) is a compiled Rust binary. The
native COPR/AUR/PPA packages build it for you and ship a package-owned source
at `/usr/libexec/tux-burnerd`. The PyPI/local wheel also bundles a compiled
source copy (built with `cargo` at wheel-build time) inside the package at
`runtime/daemon_bin/tux-burnerd`; building the wheel from source therefore
needs a Rust toolchain (`cargo`) just as it needs `cmake` for the Vulkan layer
and MinGW for the NVAPI shim. From a checkout you can instead build the daemon
once with:

```bash
scripts/build-daemon.sh   # cargo build --release --locked in burnerd/
```

The systemd unit always executes the root-owned
`/var/opt/tux-burner/libexec/tux-burnerd`; it never points into a
user-writable wheel, checkout, or Flatpak deployment. During the one elevated
service setup, the installer atomically copies the current wheel-bundled
`runtime/daemon_bin/tux-burnerd`, the dev build at
`burnerd/target/release/tux-burnerd`, or a native package's safe
`/usr/libexec/tux-burnerd` source into that fixed path. Failed startup or
socket readiness restores the previous binary, unit, and service state.

## Recovery: getting back to stock

Applied tuning persists across reboots only while **Apply on startup** is
ticked for the selected GPU (off by default). Unticking it clears that GPU's
saved boot entry immediately without removing other GPUs' entries. The daemon
replays saved GPUs serially by stable UUID and leaves one GPU actively
monitored; see [Profile Management](features/profile-management.md).

If the GPU is in a bad state (or a boot profile misbehaves), reset it to
stock — now and at boot — without the GUI:

```bash
tux-burner-cli --restore-stock
```

This asks the running root daemon to clear core/memory offsets, release locked
clocks, restore the factory V/F curve and default power limit, and makes stock
the boot state. Saved profiles are kept and can be re-applied later.
You can then start a GUI or CLI Auto-UV scan normally: scan startup releases
the active stock runtime profile without disabling the service or changing
the stock boot setting.

If a boot profile ever makes the desktop unusable before you can run that
command, boot once into systemd rescue mode (hold the boot menu, select the
rescue/recovery entry, or add `systemd.unit=rescue.target` to the kernel
command line) and run:

```bash
systemctl disable --now tux-burnerd.service
```

That stops the profile from applying at the next normal boot. (The
`--restore-stock` command needs the daemon service running, so in rescue mode
either disable the unit as above, or `systemctl start tux-burnerd.service`
first and then run `tux-burner-cli --restore-stock`.)
