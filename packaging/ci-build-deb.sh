#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Build the .deb inside a debian/ubuntu container (run as root there). Network is allowed
# (cargo fetches crates). NOT tested outside CI.
# Usage: packaging/ci-build-deb.sh SOURCE_TARBALL OUTDIR
set -euo pipefail
tarball=$(readlink -f "${1:?usage: ci-build-deb.sh SOURCE_TARBALL OUTDIR}")
out=$(readlink -m "${2:?}")
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends build-essential debhelper cargo cmake g++-multilib \
  libvulkan-dev python3 python3-build python3-installer python3-setuptools python3-wheel \
  fakeroot dpkg-dev ca-certificates
tmp=$(mktemp -d)
tar -xzf "$tarball" -C "$tmp"
src=$(echo "$tmp"/tux-burner-*)
ver=$(basename "$src"); ver=${ver#tux-burner-}
cd "$src"
cp -r packaging/debian debian
cat > debian/changelog <<CHANGELOG
tux-burner ($ver-1) unstable; urgency=medium

  * Tux Burner $ver, a fork of PenguinBurner by jpietek (GPL-3.0-or-later).

 -- Eugene Flanagin <eflanagin@pm.me>  $(date -R)
CHANGELOG
CARGO_NET_OFFLINE=false CARGO_OFFLINE_FLAG= dpkg-buildpackage -b -us -uc -d
mkdir -p "$out"
cp "$tmp"/*.deb "$out"/
ls -l "$out"
