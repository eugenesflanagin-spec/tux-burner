#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Make tux-burner-VERSION.tar.gz (directory tux-burner-VERSION/) from the committed work/ tree.
# Usage: packaging/make-source-tarball.sh OUTDIR [REF]      (REF defaults to HEAD)
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:?usage: make-source-tarball.sh OUTDIR [REF]}
ref=${2:-HEAD}
ver=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/work/pyproject.toml" | head -1)
mkdir -p "$out"
git -C "$root" archive --format=tar.gz --prefix="tux-burner-$ver/" -o "$out/tux-burner-$ver.tar.gz" "$ref:work"
echo "$out/tux-burner-$ver.tar.gz"
