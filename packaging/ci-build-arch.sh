#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Build the Arch package inside an archlinux container (run as root there).
# Usage: packaging/ci-build-arch.sh SOURCE_TARBALL OUTDIR
set -euo pipefail
tarball=$(readlink -f "${1:?usage: ci-build-arch.sh SOURCE_TARBALL OUTDIR}")
out=$(readlink -m "${2:?}")
root=$(cd "$(dirname "$0")/.." && pwd)
# lib32-* makedepends live in [multilib].
if ! grep -q '^\[multilib\]' /etc/pacman.conf; then
  printf '\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n' >> /etc/pacman.conf
fi
pacman -Syu --noconfirm --needed base-devel git
useradd -m builder 2>/dev/null || true
work=/home/builder/build
rm -rf "$work"; mkdir -p "$work" "$out"
cp "$tarball" "$root/work/packaging/arch/PKGBUILD" "$root/work/packaging/arch/tux-burner.install" "$work/"
chown -R builder: "$work"
# Install dependencies as root (makepkg -s would need sudo).
# shellcheck disable=SC1091
deps=$(bash -c 'source "$1"; printf "%s\n" "${depends[@]}" "${makedepends[@]}"' _ "$work/PKGBUILD")
# shellcheck disable=SC2086
pacman -S --noconfirm --needed $(printf '%s\n' "$deps" | sed "s/'//g")
su builder -c "cd '$work' && makepkg -f --nodeps --nocheck --noconfirm"
cp "$work"/*.pkg.tar.zst "$out"/
ls -l "$out"
