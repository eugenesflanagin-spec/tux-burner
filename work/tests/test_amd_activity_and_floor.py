"""Local AMD port: header activity tracking, ladder floor-refusal semantics and
the CLI's daemon-floor pre-checks (pure, no Qt, no GPU)."""

from __future__ import annotations

import pytest

from auto_uv import amd_sweep
from auto_uv.amd_sweep import LadderSettings, run_ladder
from ui.features.tuning.amd_activity import AmdActivity

LADDER_OUTPUT = """AMD Auto-UV ladder for AMD Navi 48 (0000:03:00.0)
  rungs (12): +0, -10, -20, -30, -40, -50, -60, -70, -80, -90, -100, -110 mV
  per rung: apply VDDGFX offset via tux-burnerd, then 60s OpenCL brute-force
AMD Auto-UV rung +0 mV: stressing for 60s ...
opencl-bruteforce: starting gpu-index=0 duration=60.0s stress-elements=4194304
opencl-bruteforce: stage occupancy=12% grid=32 elements=524288 seconds=1.0
AMD Auto-UV rung +0 mV: stable (61s)
AMD Auto-UV rung -10 mV: stressing for 60s ...
opencl-bruteforce: stage occupancy=100% grid=256 elements=4194304 seconds=57.0
"""


def test_activity_tracks_ladder_rungs_and_time() -> None:
    activity = AmdActivity()
    activity.start("amd-ladder", now=100.0)
    assert activity.stage_text() == "AMD Auto-UV ladder"
    assert activity.candidate_text(now=100.0) == "starting the ladder"
    activity.feed(LADDER_OUTPUT, now=130.0)
    assert activity.rungs == [0, -10, -20, -30, -40, -50, -60, -70, -80, -90, -100, -110]
    assert activity.current_offset_mv == -10
    assert activity.stable == [0]
    text = activity.candidate_text(now=153.0)
    assert text == "rung -10 mV (2/12), 60 s stress, 37 s left, load 100%"
    # Partial lines are buffered until the newline arrives.
    activity.feed("AMD Auto-UV rung -10 mV: UN", now=160.0)
    assert activity.unstable_mv is None
    activity.feed("STABLE (exit 3); ladder ends here\n", now=160.0)
    assert activity.unstable_mv == -10
    result = activity.finish(1)
    assert result == "Ladder finished: stable down to +0 mV, unstable at -10 mV"
    assert not activity.active and activity.stage_text() == "Idle"
    assert activity.candidate_text() == result


def test_activity_reports_floor_refusals_as_configuration_not_failure() -> None:
    activity = AmdActivity()
    activity.start("amd-ladder", now=0.0)
    activity.feed(
        "  rungs (3): -100, -110, -120 mV\n"
        "AMD Auto-UV rung -100 mV: stressing for 30s ...\n"
        "AMD Auto-UV rung -100 mV: stable (30s)\n"
        "AMD Auto-UV rung -110 mV: stressing for 30s ...\n"
        "AMD Auto-UV rung -110 mV: stable (30s)\n"
        "AMD Auto-UV rung -120 mV: refused by the daemon's safety floor - this is a configuration limit\n",
        now=1.0,
    )
    result = activity.finish(1)
    assert "stable down to -110 mV" in result
    assert "refused by the daemon floor at -120 mV (not a stability failure)" in result
    assert "failed" not in result.lower()


def test_activity_stress_and_apply_texts() -> None:
    stress = AmdActivity()
    stress.start("amd-stress", now=0.0)
    assert stress.candidate_text(now=0.0) == "starting the OpenCL workload"
    stress.feed("opencl-bruteforce: starting gpu-index=0 duration=30.0s stress-elements=1\n", now=5.0)
    assert stress.candidate_text(now=15.0) == "30 s stress, 20 s left"
    assert stress.finish(0) == "Stress test passed"
    assert stress.finish(3).startswith("Stress test exit code 3")
    apply = AmdActivity()
    apply.start("amd-apply")
    apply.feed("Applied through tux-burnerd: VDDGFX -100 mV, SCLK +300 MHz\n")
    assert apply.candidate_text() == "VDDGFX -100 mV, SCLK +300 MHz"
    assert apply.finish(0) == "Applied: VDDGFX -100 mV, SCLK +300 MHz"


def _fake_state(tmp_path):
    from amd_fixtures import make_fake_card
    from drivers.amdgpu.sysfs import discover_amd_gpus

    make_fake_card(tmp_path)
    return discover_amd_gpus(tmp_path)[0].read_state()


def test_ladder_distinguishes_daemon_floor_refusal_from_instability(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TUX_BURNER_AMD_VDDGFX_FLOOR_MV", "-180")
    state = _fake_state(tmp_path / "sys")
    applied: list[int] = []

    def apply_offset(offset_mv: int) -> None:
        if offset_mv < -110:
            raise RuntimeError(
                f"VDDGFX offset {offset_mv} mV is below the Tux Burner safety floor -110 mV (lower it deliberately ...)"
            )
        applied.append(offset_mv)

    result = run_ladder(
        state,
        LadderSettings(rung_seconds=1, start_mv=-100),
        apply_offset=apply_offset,
        run_stress=lambda _s: 0,
        log=lambda _m: None,
        marker_path=tmp_path / "marker.json",
    )
    assert applied == [-100, -110]
    assert result.deepest_stable_mv == -110
    assert result.first_unstable_mv is None
    assert result.refused_by_floor_mv == -120
    assert not result.aborted
    assert result.stopped_reason == "daemon-safety-floor"
    assert "REFUSED by the daemon's safety floor" in result.summary()
    assert "not a stability failure" in result.summary()
    assert "refused by the daemon safety floor" in result.rungs[-1].note
    assert result.rungs[-1].failure_type == amd_sweep.FAILURE_APPLY_REFUSED_FLOOR
    assert amd_sweep.is_safety_floor_refusal("... below the Tux Burner safety floor -110 mV")
    assert not amd_sweep.is_safety_floor_refusal("write failed: EINVAL")


def test_cli_refuses_to_start_when_app_floor_is_below_daemon_floor(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from cli import amd_commands
    from common.tux_burner_errors import NvmlError

    state = _fake_state(tmp_path / "sys")
    monkeypatch.setenv("TUX_BURNER_AMD_VDDGFX_FLOOR_MV", "-180")
    monkeypatch.setattr(amd_commands, "daemon_vddgfx_floor", lambda _state: (-150, "default"))
    with pytest.raises(NvmlError) as excinfo:
        amd_commands._require_daemon_floor_covers(SimpleNamespace(), state, action="--amd-auto-uv")
    assert "daemon's safety floor is -150 mV" in str(excinfo.value)
    assert "--amd-set-daemon-floor -180 --allow-below-floor" in str(excinfo.value)
    monkeypatch.setattr(amd_commands, "daemon_vddgfx_floor", lambda _state: (-180, "file"))
    amd_commands._require_daemon_floor_covers(SimpleNamespace(), state, action="--amd-auto-uv")
    monkeypatch.setattr(amd_commands, "daemon_vddgfx_floor", lambda _state: (None, ""))
    with pytest.raises(NvmlError):
        amd_commands._require_daemon_floor_covers(SimpleNamespace(), state, action="--amd-apply")
    # Within the default floor nothing is queried.
    monkeypatch.setenv("TUX_BURNER_AMD_VDDGFX_FLOOR_MV", "-150")
    amd_commands._require_daemon_floor_covers(SimpleNamespace(), state, action="--amd-apply")
