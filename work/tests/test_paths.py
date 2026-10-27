from __future__ import annotations

import os
from pathlib import Path
import pwd

import common.tux_burner_paths as paths_mod
from common.tux_burner_paths import (
    _migrate_legacy_config_dir,
    claim_desktop_user_ownership,
    default_user_config_dir,
    effective_desktop_user_ids,
)


def test_config_dir_uses_tux_burner_home_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path))

    assert default_user_config_dir() == tmp_path / ".config" / "TuxBurner"


def test_config_dir_uses_pkexec_desktop_user_when_sudo_user_is_absent(
    monkeypatch,
) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    home = Path(pwd.getpwnam(user).pw_dir)
    monkeypatch.delenv("TUX_BURNER_HOME", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("TUX_BURNER_Q2RTX_USER", user)
    # Never migrate the developer's real ~/.config/PenguinBurner from a test.
    monkeypatch.setattr(paths_mod, "_migrate_legacy_config_dir", lambda *a: None)

    assert default_user_config_dir() == home / ".config" / "TuxBurner"


def test_legacy_config_dir_is_copied_once_and_kept(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_HOME", str(tmp_path))
    old = tmp_path / ".config" / "PenguinBurner"
    (old / "auto-uv-profiles").mkdir(parents=True)
    (old / "auto-uv-profiles" / "p.json").write_text('{"x": 1}')
    (old / "penguin_burner.toml").write_text("[gpu]\nindex = 0\n")

    new = default_user_config_dir()

    assert new == tmp_path / ".config" / "TuxBurner"
    assert (new / "auto-uv-profiles" / "p.json").read_text() == '{"x": 1}'
    assert (new / "tux_burner.toml").read_text() == "[gpu]\nindex = 0\n"
    assert not (new / "penguin_burner.toml").exists()
    assert (old / "penguin_burner.toml").exists()  # the old directory is untouched
    # A second call never overwrites what the user changed since.
    (new / "tux_burner.toml").write_text("[gpu]\nindex = 1\n")
    _migrate_legacy_config_dir(tmp_path / ".config", new)
    assert default_user_config_dir() == new
    assert (new / "tux_burner.toml").read_text() == "[gpu]\nindex = 1\n"


def test_effective_desktop_user_ids_prefers_pkexec_identity(monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_Q2RTX_UID", "1234")
    monkeypatch.setenv("TUX_BURNER_Q2RTX_GID", "5678")
    monkeypatch.setenv("SUDO_UID", "1")
    monkeypatch.setenv("SUDO_GID", "2")

    assert effective_desktop_user_ids() == (1234, 5678)


def test_effective_desktop_user_ids_ignores_root_target(monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_Q2RTX_UID", "0")
    monkeypatch.setenv("TUX_BURNER_Q2RTX_GID", "0")
    monkeypatch.delenv("TUX_BURNER_Q2RTX_USER", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)

    assert effective_desktop_user_ids() is None


def test_claim_desktop_user_ownership_allows_runtime_dir(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("SUDO_USER", "desktop")
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    monkeypatch.setattr("common.tux_burner_paths.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "common.tux_burner_paths.pwd.getpwnam",
        lambda user: type(
            "Entry",
            (),
            {"pw_uid": 1234, "pw_gid": 5678, "pw_dir": "/home/desktop"},
        )(),
    )
    monkeypatch.setattr(
        "common.tux_burner_paths.os.lchown",
        lambda path, uid, gid: calls.append((Path(path), uid, gid)),
    )

    claim_desktop_user_ownership(Path("/run/user/1234/tux-burner"))

    assert calls == [(Path("/run/user/1234/tux-burner"), 1234, 5678)]
