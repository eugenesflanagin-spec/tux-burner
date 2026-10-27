#!/usr/bin/env bash
# Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
# The repository root README (the GitHub front page) is work/README.md with its relative links
# moved under work/. Run from anywhere; commit both files.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
python3 - "$here/README.md" "$here/../README.md" <<'PY'
import re, sys
src, dst = sys.argv[1:3]
t = open(src, encoding="utf-8").read()
def fix(m):
    target = m.group(2)
    if re.match(r"([a-z]+:|#|mailto:)", target):
        return m.group(0)
    return f"{m.group(1)}work/{target}"
t = re.sub(r'(\]\()([^)\s]+)', lambda m: fix(m) if True else m.group(0), t)
t = re.sub(r'((?:src|href)=")([^"]+)', fix, t)
open(dst, "w", encoding="utf-8").write(t)
PY
