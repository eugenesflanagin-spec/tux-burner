#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Build the .rpm inside a fedora container (run as root there). Network is allowed
# (cargo fetches crates). NOT tested outside CI.
# Usage: packaging/ci-build-rpm.sh SOURCE_TARBALL OUTDIR
set -euo pipefail
tarball=$(readlink -f "${1:?usage: ci-build-rpm.sh SOURCE_TARBALL OUTDIR}")
out=$(readlink -m "${2:?}")
dnf install -y rpm-build dnf-plugins-core tar gzip
top=$(mktemp -d)
mkdir -p "$top"/{SOURCES,SPECS,RPMS,BUILD}
cp "$tarball" "$top/SOURCES/"
tar -xzOf "$tarball" --wildcards '*/packaging/rpm/tux-burner.spec' > "$top/SPECS/tux-burner.spec"
dnf builddep -y "$top/SPECS/tux-burner.spec"
rpmbuild -bb --define "_topdir $top" "$top/SPECS/tux-burner.spec"
mkdir -p "$out"
cp "$top"/RPMS/*/*.rpm "$out"/
ls -l "$out"
