# Packaging and releases

Tux Burner is Copyright (C) 2026 by Eugene Flanagin. Licensed under GPL-3.0-or-later.
Fork of PenguinBurner by jpietek.

The source tree is `work/` (the repository root holds the README, credits and these scripts).

| Script | What |
| --- | --- |
| `make-source-tarball.sh OUTDIR [REF]` | `tux-burner-VERSION.tar.gz` from the committed `work/` tree (`git archive`). |
| `build-arch-local.sh [SCRATCH]` | Builds the Arch package in a scratch directory with `makepkg`. Installs nothing. |
| `ci-build-arch.sh`, `ci-build-deb.sh`, `ci-build-rpm.sh` | Run inside distro containers by the release workflow. The deb and rpm ones are untested outside CI. |
| `release-info.sh` | Version, tag check, release notes and title for the workflow. |
| `release-notes/vVERSION.md` | The release notes (first line = release title). Keep the copyright line above the third-party credits and the "tested on one PC" note. |

## Making a release

1. Set the version in `work/pyproject.toml`, `work/burnerd/Cargo.toml` (+ `Cargo.lock`), `work/packaging/arch/PKGBUILD`, `work/packaging/rpm/tux-burner.spec`, and the metainfo; add `work/docs/release-notes-VERSION.md` and `packaging/release-notes/vVERSION.md`; update `CHANGELOG.md`.
2. Rebuild the guide: `python3 -I work/docs/user-guide/build_guide.py work/docs/user-guide work/docs/user-guide/Tux-Burner-User-Guide.pdf --html work/docs/user-guide/Tux-Burner-User-Guide.html`.
3. Run "Release packages" once with "Run workflow" (no release is made) and fix what fails.
4. `git tag vVERSION && git push origin vVERSION` - the workflow builds everything and creates the GitHub release.
