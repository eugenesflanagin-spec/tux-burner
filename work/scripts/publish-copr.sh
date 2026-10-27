#!/usr/bin/env bash

set -euo pipefail
# Tux Burner change: this script is upstream's publishing flow and is disabled here; Tux Burner
# releases come from .github/workflows/release.yml. Set TUX_BURNER_ALLOW_UPSTREAM_SCRIPTS=1 to run it anyway.
if [ "${TUX_BURNER_ALLOW_UPSTREAM_SCRIPTS:-}" != 1 ] && [ "${1:-}" != "--help" ] && [ "${1:-}" != "-h" ]; then
    echo "$0: disabled in Tux Burner (releases are made by .github/workflows/release.yml)." >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Smoke-build the RPM in the containerized scenarios for the supported
# Fedora releases first so a broken build fails here, not in COPR after the
# push. Deliberately skips the rawhide scenario: rawhide breakage must not
# block releases for stable Fedora users.
# TUX_BURNER_SKIP_PACKAGE_SMOKE=1 skips the gate.
if [ "${TUX_BURNER_SKIP_PACKAGE_SMOKE:-0}" != 1 ]; then
    "$script_dir/check-fedora-package-build.sh" fedora-43 fedora-44
fi

project="${COPR_PROJECT:-tux-burner}"
srpm="${1:-}"
config_file="${COPR_CONFIG_FILE:-}"
temp_config=""

if [ -z "$srpm" ]; then
    srpm="$(find dist/rpm -maxdepth 1 -name '*.src.rpm' -print -quit 2>/dev/null || true)"
fi

if [ -z "$srpm" ] || [ ! -f "$srpm" ]; then
    echo "usage: $0 PATH_TO_SRPM" >&2
    exit 2
fi

if [ -z "$config_file" ] && [ -n "${COPR_CONFIG:-}" ]; then
    temp_config="$(mktemp)"
    printf '%s\n' "$COPR_CONFIG" > "$temp_config"
    config_file="$temp_config"
fi

cleanup() {
    if [ -n "$temp_config" ]; then
        rm -f "$temp_config"
    fi
}
trap cleanup EXIT

args=()
if [ -n "$config_file" ]; then
    args+=(--config "$config_file")
fi

copr-cli "${args[@]}" build "$project" "$srpm"
