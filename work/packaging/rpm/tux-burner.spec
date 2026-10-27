Name:           tux-burner
Version:        0.99
Release:        1%{?dist}
Summary:        GPU undervolting, overclocking, and per-game tuning (AMD and NVIDIA)

%global debug_package %{nil}

# PyPI calls the minimal Qt wheel "PySide6-Essentials", but Fedora folds that
# distribution into python3-pyside6 and provides python3dist(pyside6), not
# python3dist(pyside6-essentials).  Keep the explicit Fedora dependency below
# and filter only the unresolvable wheel-metadata dependency generated from the
# installed .dist-info/METADATA file.
%global __requires_exclude ^python%{python3_version}dist\\(pyside6-essentials\\).*$

License:        GPL-3.0-or-later
URL:            https://github.com/eugenesflanagin-spec/tux-burner
Source0:        %{name}-%{version}.tar.gz

ExclusiveArch:  x86_64
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  cmake
BuildRequires:  gcc-c++
# Tux Burner change: the NVAPI latency shim (a Windows PE dll, NVIDIA only) is no
# longer built, so no mingw cross compiler is needed.
BuildRequires:  vulkan-headers
# The 32-bit companion Vulkan layer: only an i386 layer can be loaded into a
# 32-bit game's Vulkan instance, and setup.py builds it best effort, so without
# these the RPM silently ships 64-bit only. libatomic(x86-32) is needed on
# gcc 16 (Fedora 44+), where the link otherwise fails on libatomic.so.1 -- for
# both arches, since each is a separate package and the loader needs its own.
#
# Both arches are named explicitly. Asking only for the (x86-32) provides lets
# dnf satisfy gcc-c++'s own libstdc++-devel dependency with the i686 package
# alone, which takes /usr/include/c++/*/x86_64-redhat-linux with it and breaks
# the *64-bit* layer on bits/c++config.h -- the failure that made this
# best-effort in the first place.
BuildRequires:  glibc-devel(x86-64)
BuildRequires:  libstdc++-devel(x86-64)
BuildRequires:  libatomic(x86-64)
BuildRequires:  glibc-devel(x86-32)
BuildRequires:  libstdc++-devel(x86-32)
BuildRequires:  libgcc(x86-32)
BuildRequires:  libatomic(x86-32)
BuildRequires:  desktop-file-utils
# Root daemon (tux-burnerd) is compiled from the bundled Rust crate.
BuildRequires:  cargo
BuildRequires:  rust

Requires:       python3-pyside6 >= 6.7
Requires:       python3-colorama >= 0.4
Requires:       python3-pyqtgraph >= 0.13
Requires:       python3-pyyaml >= 6.0
Requires:       hicolor-icon-theme
Provides:       penguin-burner = %{version}
Provides:       penguin-burner-amd = %{version}
Conflicts:      penguin-burner
Conflicts:      penguin-burner-amd
Obsoletes:      penguin-burner-amd < %{version}
Recommends:     ocl-icd
Requires:       bash
Requires:       systemd

%description
Tux Burner is a fork of PenguinBurner by jpietek with AMD amdgpu support
added. On AMD Radeon cards it drives the amdgpu OverDrive interface, and an
Auto-UV ladder stress-tests each voltage step with an OpenCL load. On NVIDIA
GPUs one Auto-UV scan sweeps the card under a managed headless Q2RTX
benchmark plus a CUDA load, verifies stability and FPS per watt at single
voltage/frequency bin precision, and saves three profiles: Efficiency,
Balanced and Performance (undervolt plus overclock).

 * Adaptive undervolting switches between the saved tiers while you play to
   hold your frame-rate target, and eases off on frame caps and idle desktops.
 * The Game Library brings Steam, Lutris, Heroic and Faugus games into one list, each
   with its own GPU profile, adaptive FPS target, and overlay setting, applied
   at launch and restored on exit.
 * The in-game Vulkan overlay shows base FPS, frame-generation FPS, PC latency,
   clocks, power, temperatures, and the active tier.
 * Manual V/F and fan curve editors, an automatic silent fan curve, MSI
   Afterburner profile import, and LACT config export.
 * A Qt desktop UI and a CLI, both driving one root systemd service
   (tux-burnerd, a compiled Rust daemon) installed once with a single
   admin prompt.

Pre-optimized Auto-UV targets are included for RTX 30, 40, and 50 series cards.
GPU tuning can crash the driver or freeze the system; use it at your own risk.

On AMD the in-kernel amdgpu driver is used (OverDrive enabled). For NVIDIA, the
proprietary driver, version 580 or newer, from either Fedora's NVIDIA driver
repository or RPM Fusion is needed.

%prep
%autosetup

%build
export TUX_BURNER_REQUIRE_NATIVE_LAYER=1
export TUX_BURNER_REQUIRE_NATIVE_LAYER32=1
export TUX_BURNER_BUILD_NVAPI_SHIM=0
%pyproject_wheel

# Root daemon: compiled from the bundled Rust crate in burnerd/. --locked pins
# the committed Cargo.lock; crates.io is fetched at build time, so the COPR
# project must have internet access enabled (see packaging/rpm/README.md).
cargo build --release --locked --manifest-path burnerd/Cargo.toml

%install
%pyproject_install
%pyproject_save_files \
    auto_uv \
    cli \
    common \
    curve_editors \
    integrations \
    overlay \
    profiles \
    runtime \
    stability \
    ui \
    drivers \
    tux_burner

# Root daemon binary at the fixed discovery path (0755, root-owned).
install -D -m 0755 burnerd/target/release/tux-burnerd \
    %{buildroot}%{_libexecdir}/tux-burnerd

install -D -m 0644 packaging/man/tburn-cli.1 %{buildroot}%{_mandir}/man1/tburn-cli.1
install -D -m 0644 packaging/man/tux-burner.1 %{buildroot}%{_mandir}/man1/tux-burner.1
install -D -m 0644 packaging/man/tux-burnerd.8 %{buildroot}%{_mandir}/man8/tux-burnerd.8

desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.eugenesflanagin_spec.TuxBurner.desktop

%files -f %{pyproject_files}
%license LICENSE
%doc README.md readme-cli.md CHANGELOG.md CREDITS.md docs/amd.md docs/user-guide
%{_mandir}/man1/tburn-cli.1*
%{_mandir}/man1/tux-burner.1*
%{_mandir}/man8/tux-burnerd.8*
%{_bindir}/tux-burner
%{_bindir}/tburn
%{_bindir}/tux-burner-cli
%{_bindir}/tburn-cli
%{_bindir}/TUX_BURNER
%{_libexecdir}/tux-burnerd
%{_bindir}/tux-burner-install-wrappers
%{_datadir}/applications/io.github.eugenesflanagin_spec.TuxBurner.desktop
%{_datadir}/icons/hicolor/256x256/apps/tux-burner.png
%{_datadir}/icons/hicolor/512x512/apps/tux-burner.png
%{_datadir}/tux-burner/tux_burner.sh

%changelog
* Tue Oct 27 2026 Eugene Flanagin <eflanagin@pm.me> - 0.99-1
- First Tux Burner release, forked from PenguinBurner 0.8.3 (by jpietek): AMD amdgpu backend,
  OpenCL stress workload and Auto-UV ladder, --amd-* CLI, safety floors, rename to Tux Burner.

* Mon Oct 12 2026 PenguinBurner contributors <noreply@github.com> - 0.8.3-1
- Add Faugus support, improve Auto-UV recovery and multi-GPU selection.
- Refresh Game Library artwork, documentation, and packaging.

* Tue Sep 22 2026 PenguinBurner contributors <noreply@github.com> - 0.8.2-2
- Ship the 32-bit overlay Vulkan layer so the overlay reaches 32-bit games.

* Mon Sep 21 2026 PenguinBurner contributors <noreply@github.com> - 0.8.2-1
- Ship the 32-bit overlay layer in the published wheel so 32-bit games get
  the overlay.

* Mon Sep 21 2026 PenguinBurner contributors <noreply@github.com> - 0.8.1-1
- Add the Heroic Games Launcher integration, on par with Steam and Lutris.
- Add a per-game Wine/Proton compatibility picker and live mode, target and
  overlay changes for a running wrapped game.
- Track launcher sessions from events instead of polling, and steady Adaptive
  against alt-tab frame-time spikes.

* Sun Sep 06 2026 PenguinBurner contributors <noreply@github.com> - 0.8.0-1
- Add Auto-UV recovery, smooth curves, per-tier targets and clearer profile metrics.
- Unify the Game Library and improve adaptive switching under frame-rate caps.

* Mon Aug 17 2026 PenguinBurner contributors <noreply@github.com> - 0.7.9-1
- Add RTD3 sleep handling, resume recovery, telemetry fallback, and partial
  multi-GPU profile support.

* Sat Aug 08 2026 PenguinBurner contributors <noreply@github.com> - 0.7.8-1
- Hotfix mobile GPU power-limit detection.

* Thu Aug 06 2026 PenguinBurner contributors <noreply@github.com> - 0.7.7-1
- Fix RTX 5090-class and other power-limited Auto-UV scan scenarios.
- Skip unsupported fixed power writes on mobile GPUs.
- Improve saved-profile verification and memory-offset editing.

* Wed Jul 15 2026 PenguinBurner contributors <noreply@github.com> - 0.7.6-1
- Install or update the hardware service before the Auto-UV setup dialog
  opens, instead of showing a generic GPU with no limits.
- Name the unreachable hardware service in the setup dialog and how to fix
  it.
- Remove the legacy TuxBurner.service unit file during migration.

* Wed Jul 15 2026 PenguinBurner contributors <noreply@github.com> - 0.7.5-1
- Add the opt-in "Apply on startup" toggle; applies are session-only by
  default and a new --restore-stock recovery command resets to stock.
- Tolerate mobile GPUs that reject the power-limit setter or expose no
  controllable fans.
- Show Proton vs Native Linux per Steam game and gray the compatibility
  selector out for native titles.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.4-1
- Deduplicate in-game telemetry with per-session sample ownership.
- Keep the Vulkan layer marker fallback active when the NVAPI shim is
  deployed but never streams.
- Remove superseded latency fallbacks, dead flags, and legacy scripts.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.3-1
- Install the Flatpak Steam host integration only when host Steam is detected.
- Never block GUI startup or privileged actions on Steam integration repair.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.2-2
- Fix Fedora dependency resolution for the PySide6 runtime package.

* Sat Jun 20 2026 PenguinBurner contributors <noreply@github.com> - 0.5-1
- Remove nvidia-smi shell calls in favor of NVML API helpers.
- Use true headless Q2RTX tuned for Tux Burner benchmarking.
- Include multiple minor Auto-UV, UI, fan, profile, and cleanup fixes.

* Mon Jun 15 2026 PenguinBurner contributors <noreply@github.com> - 0.4.7-1
- Use Tux Burner's headless Q2RTX benchmark binary and shareware data.
- Remove Q2RTX OpenSSL compatibility staging, RUNPATH patching, RPM payload
  extraction, gamescope, Xvfb, and off-screen window workarounds.
- Read hot benchmark metrics from the Q2RTX event pipe instead of loop log
  parsing.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.6-1
- Add the TUX_BURNER_DUMP_LATENCY_DATA advanced diagnostic environment.
- Document advanced latency diagnostics.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.5-1
- Fix phantom frame generation reported on titles without a marker stream.
- Mark Beta development status; packaging and license metadata fixes.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.4-1
- Package Tux Burner 0.4.4.
- Build the native Vulkan latency layer.
- Add cmake, gcc-c++, and vulkan-headers build dependencies.
- Package overlay and latency telemetry modules and entry points.

* Mon Jun 01 2026 PenguinBurner contributors <noreply@github.com> - 0.2.6-1
- Package Tux Burner 0.2.6.
- Constrain Fedora Python GUI dependency versions.
- Do not hard-require distro-packaged NVIDIA drivers.

* Wed May 27 2026 PenguinBurner contributors <noreply@github.com> - 0.2-2
- Include nvml_perf_cap_reason in the RPM Python file manifest.

* Tue May 26 2026 PenguinBurner contributors <noreply@github.com> - 0.2-1
- Revise Auto-UV profiles for the 0.2 major release.
- Tune the Efficiency profile for the lowest stable voltage while retaining as much clock as possible.
- Keep Performance profiles focused on revised voltage and clock targets.
- Reapply memory OC offset consistently with runtime V/F curve reapplies, thanks to cbro33's pull request.

* Mon May 25 2026 PenguinBurner contributors <noreply@github.com> - 0.1.8-1
- Replace old Auto-UV internals with the cleaned Auto-UV package namespace.
- Add a separate Performance Auto-OC ladder after the balanced undervolt pass.
- Improve Auto-UV table progress, status, and Auto-OC reporting.
- Keep Performance final selection sorted by measured FPS.

* Wed May 13 2026 PenguinBurner contributors <noreply@github.com> - 0.1.7-1
- Honor time-based Q2RTX verification durations instead of precomputed loop counts.
- Improve PRIME laptop selected-GPU execution and diagnostics.
- Add safer interrupted-probe crash caching.
- Use a conservative Auto-UV default for RTX 3080.

* Fri May 08 2026 PenguinBurner contributors <noreply@github.com> - 0.1.6-1
- Add python3-colorama runtime dependency for pyqtgraph.
- Add borrowed GPU voltage/frequency guardrails for Auto-UV.
- Cap performance-mode Auto-OC by the GPU Performance table voltage.
- Improve final candidate sorting and stopped-scan final candidate selection.

* Mon May 04 2026 PenguinBurner contributors <noreply@github.com> - 0.1.5-1
- Initial COPR package for Fedora 42, 43, and 44.
