"""AMD Auto-UV: a one-dimensional VDDGFX-offset ladder.

RDNA3/RDNA4 have no per-point V/F curve, so the NVIDIA search (voltage bins,
flattening, clock locks) does not apply. The AMD equivalent is deliberately
simple and transparent:

1. start at ``start_mv`` (0 = stock) and step the global voltage offset down
   by ``step_mv`` until ``floor_mv`` (never below the configurable safety
   floor, default -110 mV, nor the driver's ``OD_RANGE`` minimum);
2. at every rung apply the offset through the root daemon, then run the
   OpenCL brute-force workload for ``rung_seconds`` while sampling the live
   operating point (VDDGFX mV, sclk MHz, power, junction) from hwmon;
3. the ladder stops at the first rung that is unstable, that the daemon
   refuses (safety floor / LACT / range - a configuration limit, NOT a
   stability verdict) or that errors out; the deepest passing rung minus a
   safety ``margin_mv`` becomes the three tiers:

   * Efficiency: offset + margin, stock power cap;
   * Balanced:   offset + margin, the power cap in force when the scan began;
   * Performance: offset + margin, the user's SCLK offset, maximum power cap.

Every rung ends with a classified ``Rung`` (``failure_type``), the whole run
is persisted as a run record (``amd_runs``) that the GUI's run history and
the Clock-vs-Voltage plot read back.

``plan_ladder`` is pure; ``run_ladder`` performs the writes and the stress.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from common.atomic_write import atomic_write_json
from common.tux_burner_paths import default_user_config_dir
from drivers.amdgpu.plan import SUGGESTION_FLOOR_MV, AmdTuningPlan, voltage_offset_floor_mv
from drivers.amdgpu.sysfs import AmdGpuState
from stability.opencl_bruteforce import EXIT_HANG, EXIT_INSTABILITY, OPENCL_INSTABILITY_EXIT_CODES

DEFAULT_STEP_MV = 10
DEFAULT_RUNG_SECONDS = 60
DEFAULT_MARGIN_MV = 20
SAFETY_FLOOR_REFUSAL_MARKER = "safety floor"
RUN_RECORD_FORMAT_VERSION = 1

# Failure types a rung can end with. "" means the rung passed.
FAILURE_STABLE = ""
FAILURE_OPENCL_MISMATCH = "opencl-mismatch"
FAILURE_DRIVER_ERROR = "driver-error"
FAILURE_HANG_TIMEOUT = "hang-timeout"
FAILURE_PROCESS_CRASH = "process-crash"
FAILURE_SETUP_ERROR = "setup-error"
FAILURE_APPLY_REFUSED_FLOOR = "apply-refused-floor"
FAILURE_APPLY_REFUSED_RANGE = "apply-refused-range"
FAILURE_APPLY_REFUSED_LACT = "apply-refused-lact"
FAILURE_APPLY_FAILED = "apply-failed"
APPLY_REFUSALS = {FAILURE_APPLY_REFUSED_FLOOR, FAILURE_APPLY_REFUSED_RANGE, FAILURE_APPLY_REFUSED_LACT}
FAILURE_LABELS = {
    FAILURE_STABLE: "stable",
    FAILURE_OPENCL_MISMATCH: "OpenCL result mismatch",
    FAILURE_DRIVER_ERROR: "kernel/driver error",
    FAILURE_HANG_TIMEOUT: "GPU stopped answering (hang timeout)",
    FAILURE_PROCESS_CRASH: "stress process crashed",
    FAILURE_SETUP_ERROR: "stress workload setup error",
    FAILURE_APPLY_REFUSED_FLOOR: "apply refused by the daemon safety floor (configuration, not instability)",
    FAILURE_APPLY_REFUSED_RANGE: "apply refused: outside the driver OD_RANGE",
    FAILURE_APPLY_REFUSED_LACT: "apply refused: LACT owns the GPU",
    FAILURE_APPLY_FAILED: "apply failed",
}


def is_safety_floor_refusal(error: object) -> bool:
    """True when a write was refused by the daemon's VDDGFX floor, which is a
    configuration limit - never a stability verdict."""
    return SAFETY_FLOOR_REFUSAL_MARKER in str(error).lower()


def classify_apply_error(error: object) -> str:
    text = str(error).lower()
    if SAFETY_FLOOR_REFUSAL_MARKER in text:
        return FAILURE_APPLY_REFUSED_FLOOR
    if "od_range" in text or "outside" in text:
        return FAILURE_APPLY_REFUSED_RANGE
    if "lact" in text:
        return FAILURE_APPLY_REFUSED_LACT
    return FAILURE_APPLY_FAILED


def classify_stress_exit(exit_code: int | None, output: str = "") -> tuple[str, str]:
    """(failure_type, detail) for a finished stress child."""
    text = str(output or "")
    lowered = text.lower()
    detail = ""
    for line in reversed(text.splitlines()):
        if "opencl-bruteforce:" in line and any(key in line for key in ("UNSTABLE", "FAILED", "HANG", "mismatch")):
            detail = line.split("opencl-bruteforce:", 1)[1].strip()
            break
    if exit_code == 0:
        return FAILURE_STABLE, ""
    if exit_code is None or (isinstance(exit_code, int) and exit_code < 0):
        return FAILURE_PROCESS_CRASH, detail or f"terminated by signal {-exit_code if exit_code else '?'}"
    if exit_code == EXIT_HANG or "hang launch timeout" in lowered:
        return FAILURE_HANG_TIMEOUT, detail
    if exit_code == EXIT_INSTABILITY:
        if "mismatch" in lowered:
            return FAILURE_OPENCL_MISMATCH, detail
        return FAILURE_DRIVER_ERROR, detail or "gpu fault under load"
    return FAILURE_SETUP_ERROR, detail or f"exit code {exit_code}"


@dataclass(frozen=True, slots=True)
class LadderSettings:
    start_mv: int = 0
    step_mv: int = DEFAULT_STEP_MV
    floor_mv: int | None = None
    rung_seconds: int = DEFAULT_RUNG_SECONDS
    margin_mv: int = DEFAULT_MARGIN_MV
    sclk_offset_mhz: int = 0
    mclk_max_mhz: int | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One live operating point during a rung (hwmon reads)."""

    t_s: float
    vddgfx_mv: int | None
    sclk_mhz: int | None
    power_w: float | None
    junction_c: float | None


@dataclass(frozen=True, slots=True)
class Rung:
    offset_mv: int
    exit_code: int | None
    seconds: float
    note: str = ""
    failure_type: str = FAILURE_STABLE
    detail: str = ""
    samples: tuple[TelemetrySample, ...] = ()

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.failure_type

    @property
    def result(self) -> str:
        if self.passed:
            return "stable"
        if self.failure_type in APPLY_REFUSALS:
            return "refused"
        if self.failure_type in (FAILURE_OPENCL_MISMATCH, FAILURE_DRIVER_ERROR, FAILURE_HANG_TIMEOUT):
            return "unstable"
        return "error"

    def describe(self) -> str:
        label = FAILURE_LABELS.get(self.failure_type, self.failure_type or "stable")
        text = f"{self.offset_mv:+d} mV: {label}"
        if self.detail:
            text += f" - {self.detail}"
        if self.seconds:
            text += f" ({self.seconds:.0f}s)"
        return text


@dataclass(frozen=True, slots=True)
class LadderResult:
    rungs: tuple[Rung, ...]
    deepest_stable_mv: int | None
    first_unstable_mv: int | None
    aborted: str = ""
    # The rung the daemon refused because of its safety floor (not instability).
    refused_by_floor_mv: int | None = None

    @property
    def stopped_reason(self) -> str:
        if self.aborted:
            return "aborted"
        if self.refused_by_floor_mv is not None:
            return "daemon-safety-floor"
        if self.first_unstable_mv is not None:
            return "unstable"
        return "completed"

    def summary(self) -> str:
        if self.aborted:
            return f"Ladder aborted: {self.aborted}"
        parts = []
        if self.deepest_stable_mv is not None:
            parts.append(f"deepest tested stable VDDGFX offset {self.deepest_stable_mv:+d} mV")
        else:
            parts.append("no rung passed")
        if self.refused_by_floor_mv is not None:
            parts.append(
                f"stopped at {self.refused_by_floor_mv:+d} mV: REFUSED by the daemon's safety floor "
                "(a configuration limit, not a stability failure; lower the daemon floor with "
                "`tburn-cli --amd-set-daemon-floor <mV> --allow-below-floor` or the GUI floor checkbox to test deeper)"
            )
        elif self.first_unstable_mv is not None:
            failing = next((r for r in self.rungs if r.offset_mv == self.first_unstable_mv), None)
            label = FAILURE_LABELS.get(failing.failure_type, "unstable") if failing else "unstable"
            parts.append(f"first unstable rung {self.first_unstable_mv:+d} mV ({label})")
        else:
            parts.append("every planned rung passed")
        return "; ".join(parts)


def ladder_marker_path() -> Path:
    return default_user_config_dir() / "amd-auto-uv-rung-in-progress.json"


def amd_runs_dir() -> Path:
    return default_user_config_dir() / "amd-runs"


def effective_floor_mv(state: AmdGpuState, settings: LadderSettings) -> int:
    """Never below the driver minimum and never below the configurable safety
    floor (default -110 mV); an explicit ladder floor can only be shallower."""
    rng = state.od.range_vddgfx_offset_mv
    driver_min = rng[0] if rng else -200
    floor = max(driver_min, voltage_offset_floor_mv())
    if settings.floor_mv is None:
        return floor
    return max(floor, int(settings.floor_mv))


def plan_ladder(state: AmdGpuState, settings: LadderSettings, *, unsafe_from_marker: int | None = None) -> list[int]:
    """Offsets to test, most conservative first. Pure."""
    step = max(1, abs(int(settings.step_mv)))
    floor = effective_floor_mv(state, settings)
    if unsafe_from_marker is not None:
        floor = max(floor, int(unsafe_from_marker) + step)
    start = min(0, int(settings.start_mv))
    offsets = []
    current = start
    while current >= floor:
        offsets.append(current)
        current -= step
    return offsets


def read_marker(path: Path | None = None) -> int | None:
    path = ladder_marker_path() if path is None else path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return int(payload.get("offset_mv"))
    except (TypeError, ValueError, AttributeError):
        return None


def tier_plans(deepest_stable_mv: int, state: AmdGpuState, settings: LadderSettings) -> dict[str, AmdTuningPlan]:
    # Suggested tiers never go below the validated -110 mV even when the
    # ladder explored deeper: the user applies deeper values deliberately.
    safe = max(SUGGESTION_FLOOR_MV, min(0, int(deepest_stable_mv) + abs(int(settings.margin_mv))))
    stock_cap = state.power_cap_default_w
    current_cap = state.power_cap_w
    max_cap = state.power_cap_max_w
    base = AmdTuningPlan(voltage_offset_mv=safe, sclk_offset_mhz=0, mclk_max_mhz=settings.mclk_max_mhz)
    return {
        "efficiency": replace(base, power_cap_w=stock_cap),
        "balanced": replace(base, power_cap_w=current_cap or stock_cap),
        "performance": replace(
            base,
            sclk_offset_mhz=int(settings.sclk_offset_mhz),
            power_cap_w=max_cap or current_cap or stock_cap,
        ),
    }


def describe_ladder(state: AmdGpuState, settings: LadderSettings, offsets: list[int]) -> str:
    lines = [
        f"AMD Auto-UV ladder for {state.name} ({state.pci_addr})",
        f"  range: {offsets[0] if offsets else 0:+d} -> {offsets[-1] if offsets else 0:+d} mV, stop at the first failing rung",
        f"  rungs ({len(offsets)}): " + ", ".join(f"{mv:+d}" for mv in offsets) + " mV",
        f"  per rung: apply VDDGFX offset via tux-burnerd, then {settings.rung_seconds}s OpenCL brute-force",
        f"  stop at first failure; tiers = deepest stable + {settings.margin_mv} mV margin",
        f"  safety floor: {effective_floor_mv(state, settings)} mV (default -150; --amd-voltage-floor / the GUI checkbox change it)",
        f"  suggested tiers are capped at {SUGGESTION_FLOOR_MV} mV (+ margin) whatever passes",
        f"  live card: SCLK {state.od.sclk_offset_mhz:+d} MHz, MCLK max {state.od.mclk_max_mhz} MHz, "
        f"power cap {state.power_cap_w} W stay as they are during the rungs",
        f"  Performance tier adds SCLK {settings.sclk_offset_mhz:+d} MHz and the max power cap "
        f"({state.power_cap_max_w} W); Efficiency uses the stock cap ({state.power_cap_default_w} W)",
        f"  worst case {len(offsets) * settings.rung_seconds}s of sustained GPU load",
    ]
    return "\n".join(lines)


def emit_event(log: Callable[[str], None], event: str, **fields) -> None:
    """Machine-readable progress line (`@amd {...}`) beside the human log; the
    GUI's header, plot and run history parse these."""
    payload = {"event": str(event), **fields}
    log("@amd " + json.dumps(payload, separators=(",", ":"), default=str))


def run_ladder(
    state: AmdGpuState,
    settings: LadderSettings,
    *,
    apply_offset: Callable[[int], None],
    run_stress: Callable[[int], object],
    log: Callable[[str], None] = print,
    marker_path: Path | None = None,
) -> LadderResult:
    """Drive the ladder. ``run_stress(seconds)`` returns an exit code, or a
    ``(exit_code, output_text, samples)`` tuple when the caller streams the
    workload and samples telemetry."""
    marker_path = ladder_marker_path() if marker_path is None else marker_path
    unsafe = read_marker(marker_path)
    if unsafe is not None:
        log(
            f"AMD Auto-UV: a previous rung at {unsafe:+d} mV never finished (system reset?); "
            "treating it as unsafe and starting above it."
        )
    offsets = plan_ladder(state, settings, unsafe_from_marker=unsafe)
    emit_event(log, "ladder_start", rungs=offsets, rung_seconds=settings.rung_seconds, floor_mv=effective_floor_mv(state, settings))
    rungs: list[Rung] = []
    deepest = None
    first_unstable = unsafe
    aborted = ""
    refused_by_floor = None
    for index, offset in enumerate(offsets):
        started = time.monotonic()
        atomic_write_json(
            marker_path,
            {"offset_mv": int(offset), "started_at": datetime.now().astimezone().isoformat()},
        )
        emit_event(log, "rung_start", offset_mv=int(offset), index=index + 1, total=len(offsets), seconds=settings.rung_seconds)
        try:
            apply_offset(int(offset))
        except Exception as exc:  # noqa: BLE001 - a rejected write ends the ladder
            failure = classify_apply_error(exc)
            rung = Rung(offset, None, 0.0, FAILURE_LABELS[failure], failure, str(exc).splitlines()[0] if str(exc) else "")
            rungs.append(rung)
            if failure == FAILURE_APPLY_REFUSED_FLOOR:
                refused_by_floor = int(offset)
                log(
                    f"AMD Auto-UV rung {offset:+d} mV: refused by the daemon's safety floor - "
                    "this is a configuration limit, not a stability failure; the ladder stops here."
                )
            else:
                aborted = f"applying {offset:+d} mV failed: {exc}"
                log(f"AMD Auto-UV rung {offset:+d} mV: {rung.describe()}")
            emit_event(log, "rung_result", offset_mv=int(offset), result=rung.result, failure_type=failure, detail=rung.detail)
            break
        log(f"AMD Auto-UV rung {offset:+d} mV: stressing for {settings.rung_seconds}s ...")
        try:
            outcome = run_stress(int(settings.rung_seconds))
        except Exception as exc:  # noqa: BLE001
            aborted = f"stress workload failed to run at {offset:+d} mV: {exc}"
            rungs.append(Rung(offset, None, time.monotonic() - started, aborted, FAILURE_SETUP_ERROR, str(exc)))
            emit_event(log, "rung_result", offset_mv=int(offset), result="error", failure_type=FAILURE_SETUP_ERROR, detail=str(exc))
            break
        if isinstance(outcome, tuple):
            code, output, samples = (list(outcome) + [None, "", ()])[:3]
        else:
            code, output, samples = outcome, "", ()
        code = None if code is None else int(code)
        elapsed = time.monotonic() - started
        failure, detail = classify_stress_exit(code, str(output or ""))
        rung = Rung(offset, code, elapsed, FAILURE_LABELS[failure] if failure else "", failure, detail, tuple(samples or ()))
        rungs.append(rung)
        emit_event(log, "rung_result", offset_mv=int(offset), result=rung.result, failure_type=failure, detail=detail, seconds=round(elapsed, 1))
        if rung.passed:
            deepest = offset
            log(f"AMD Auto-UV rung {offset:+d} mV: stable ({elapsed:.0f}s)")
            continue
        if code in OPENCL_INSTABILITY_EXIT_CODES or failure in (FAILURE_OPENCL_MISMATCH, FAILURE_DRIVER_ERROR, FAILURE_HANG_TIMEOUT):
            first_unstable = offset
            log(f"AMD Auto-UV rung {offset:+d} mV: UNSTABLE (exit {code}; {FAILURE_LABELS[failure]}{': ' + detail if detail else ''}); ladder ends here")
            break
        aborted = f"{FAILURE_LABELS[failure]} at {offset:+d} mV (exit {code}){': ' + detail if detail else ''}"
        log(f"AMD Auto-UV rung {offset:+d} mV: {aborted}")
        break
    try:
        marker_path.unlink()
    except OSError:
        pass
    result = LadderResult(tuple(rungs), deepest, first_unstable, aborted, refused_by_floor)
    emit_event(
        log,
        "ladder_end",
        deepest_stable_mv=deepest,
        first_unstable_mv=first_unstable,
        refused_by_floor_mv=refused_by_floor,
        stopped_reason=result.stopped_reason,
        summary=result.summary(),
    )
    return result


# --------------------------------------------------------------------------
# Run records (the GUI's run history + plot read these)
# --------------------------------------------------------------------------


def run_record(
    *,
    kind: str,
    state: AmdGpuState,
    settings: LadderSettings | None,
    result: LadderResult | None,
    started_at: str,
    finished_at: str,
    app_floor_mv: int | None,
    daemon_floor_mv: int | None,
    saved_tiers: dict[str, str] | None = None,
    log_path: str = "",
    reconstructed: bool = False,
    notes: str = "",
    override_lact: bool = False,
) -> dict:
    record = {
        "format_version": RUN_RECORD_FORMAT_VERSION,
        "kind": kind,
        "started_at": started_at,
        "finished_at": finished_at,
        "reconstructed": bool(reconstructed),
        "notes": notes,
        "gpu": {"name": state.name, "uuid": state.uuid, "pci_addr": state.pci_addr},
        "settings": {
            **(asdict(settings) if settings is not None else {}),
            "app_floor_mv": app_floor_mv,
            "daemon_floor_mv": daemon_floor_mv,
            "override_lact": bool(override_lact),
            "live_sclk_offset_mhz": state.od.sclk_offset_mhz,
            "live_mclk_max_mhz": state.od.mclk_max_mhz,
            "live_power_cap_w": state.power_cap_w,
        },
        "log_path": log_path,
        "saved_tiers": dict(saved_tiers or {}),
    }
    if result is not None:
        record.update(
            {
                "deepest_stable_mv": result.deepest_stable_mv,
                "first_unstable_mv": result.first_unstable_mv,
                "refused_by_floor_mv": result.refused_by_floor_mv,
                "stopped_reason": result.stopped_reason,
                "aborted": result.aborted,
                "summary": result.summary(),
                "rungs": [
                    {
                        "offset_mv": rung.offset_mv,
                        "result": rung.result,
                        "failure_type": rung.failure_type,
                        "detail": rung.detail,
                        "exit_code": rung.exit_code,
                        "seconds": round(rung.seconds, 1),
                        "samples": [asdict(sample) for sample in rung.samples],
                    }
                    for rung in result.rungs
                ],
            }
        )
    return record


def save_run_record(record: dict, *, directory: Path | None = None) -> Path:
    directory = amd_runs_dir() if directory is None else directory
    stamp = str(record.get("started_at") or datetime.now().astimezone().isoformat())
    safe = "".join(ch if ch.isalnum() else "-" for ch in stamp[:19])
    return atomic_write_json(directory / f"amd-run-{safe}-{record.get('kind', 'run')}.json", record)


def load_run_records(directory: Path | None = None) -> list[dict]:
    directory = amd_runs_dir() if directory is None else directory
    records = []
    try:
        paths = sorted(directory.glob("amd-run-*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["path"] = str(path)
            records.append(payload)
    records.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return records


def record_summary_row(record: dict) -> dict[str, str]:
    """Flat strings for a history table row."""
    settings = record.get("settings") or {}
    rungs = record.get("rungs") or []
    stable = [r for r in rungs if r.get("result") == "stable"]
    tiers = record.get("saved_tiers") or {}
    return {
        "started": str(record.get("started_at") or "")[:19].replace("T", " "),
        "kind": ("AMD ladder" if record.get("kind") == "amd-ladder" else str(record.get("kind") or "")) + (" (reconstructed)" if record.get("reconstructed") else ""),
        "floor": f"app {settings.get('app_floor_mv')} / daemon {settings.get('daemon_floor_mv')} mV",
        "plan": (
            f"start {settings.get('start_mv', 0)} step {settings.get('step_mv', '')} | "
            f"SCLK {settings.get('live_sclk_offset_mhz')} MCLK {settings.get('live_mclk_max_mhz')} "
            f"cap {settings.get('live_power_cap_w')} W | tier SCLK {settings.get('sclk_offset_mhz', 0)}"
        ),
        "result": str(record.get("summary") or record.get("stopped_reason") or ""),
        "deepest": "" if record.get("deepest_stable_mv") is None else f"{record['deepest_stable_mv']:+d} mV",
        "rungs": f"{len(stable)}/{len(rungs)} stable",
        "tiers": ", ".join(f"{tier}={pid}" for tier, pid in tiers.items()) or "-",
        "log": str(record.get("log_path") or ""),
    }
