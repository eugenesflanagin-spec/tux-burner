#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# Release helpers. Usage: release-info.sh version | check TAG | notes TAG | title TAG
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
ver=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/work/pyproject.toml" | head -1)
case "${1:-}" in
  version) echo "$ver" ;;
  check) [ "${2:-}" = "v$ver" ] || { echo "tag ${2:-} does not match version v$ver" >&2; exit 1; } ;;
  notes) cat "$root/packaging/release-notes/${2:?}.md" ;;
  title) sed -n '1s/^# //p' "$root/packaging/release-notes/${2:?}.md" ;;
  *) echo "usage: $0 version|check TAG|notes TAG|title TAG" >&2; exit 2 ;;
esac
