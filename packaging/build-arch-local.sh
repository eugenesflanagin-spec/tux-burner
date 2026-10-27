#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Build the Arch package from the committed tree, in a scratch directory. Installs nothing.
# Usage: packaging/build-arch-local.sh [SCRATCHDIR]     (extra makepkg flags via MAKEPKG_FLAGS)
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
scratch=${1:-$(mktemp -d -t tux-burner-arch.XXXXXX)}
mkdir -p "$scratch"
"$root/packaging/make-source-tarball.sh" "$scratch" >/dev/null
cp "$root/work/packaging/arch/PKGBUILD" "$root/work/packaging/arch/tux-burner.install" "$scratch/"
cd "$scratch"
# The tarball is already next to the PKGBUILD, so makepkg does not try to download it.
# Share the cargo target dir if CARGO_TARGET_DIR is set; keep the load light by default.
unset CARGO_TARGET_DIR   # the wheel build expects burnerd/target
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-4}
nice -n 15 makepkg -f ${MAKEPKG_FLAGS:---nocheck}
echo "built in $scratch:"; ls -1 "$scratch"/*.pkg.tar.* 
