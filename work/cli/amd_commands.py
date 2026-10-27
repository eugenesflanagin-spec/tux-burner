"""AMD (amdgpu) CLI commands: status, dry-run plans, apply, profiles, LACT export,
the OpenCL stress workload and the VDDGFX ladder.

Local AMD port. Reads come from sysfs directly; every GPU write goes through
the root daemon's socket API (``DaemonGpuClient``), exactly like the NVIDIA
paths. When LACT is running it owns this GPU's tuning: writes are refused
unless ``--override-lact`` is given, and even then LACT re-applies its own
config on its next apply (boot, profile switch, GUI change).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from common.atomic_write import atomic_write_json
from common.tux_burner_errors import NvmlError
from drivers.amdgpu.plan import (
    AmdPlanError,
    AmdTuningPlan,
    current_plan,
    parse_plan_assignments,
    plan_profile_payload,
    render_dry_run,
    sysfs_writes,
    validate_plan,
)
from drivers.amdgpu.sysfs import (
    AmdGpuState,
    discover_amd_gpus,
    format_state,
    lact_status,
    select_amd_gpu,
)

AMD_FLAGS = (
    "amd_restore",
    "amd_set_daemon_floor",
    "amd_status",
    "amd_plan",
    "amd_apply",
    "amd_save_profile",
    "amd_export_lact",
    "amd_stress",
    "amd_auto_uv",
)


def amd_command_requested(args) -> bool:
    return any(getattr(args, flag, None) not in (None, False, []) for flag in AMD_FLAGS)


def route_amd_command(args, *, deps) -> bool:
    """Handle the ``--amd-*`` flags. Returns True when one was handled."""
    if not amd_command_requested(args):
        return False
    print_fn = deps.print_fn
    gpu_index = int(args.gpu_index) if getattr(args, "gpu_index", None) is not None else 0
    gpu = select_amd_gpu(gpu_index)
    state = gpu.read_state()
    lact = lact_status(state.lact_gpu_id)
    json_output = bool(getattr(args, "json_events", False))

    if getattr(args, "amd_stress", None) is not None:
        return _run_stress(args, state, print_fn=print_fn)

    if getattr(args, "amd_restore", None):
        _restore(args, state, lact, print_fn=print_fn)
        return True

    if getattr(args, "amd_set_daemon_floor", None) is not None:
        _set_daemon_floor(args, state, print_fn=print_fn)
        if not (args.amd_plan or args.amd_apply or args.amd_auto_uv):
            return True

    if getattr(args, "amd_status", False):
        _print_status(state, lact, json_output=json_output, print_fn=print_fn)
        if not (args.amd_plan or args.amd_apply or args.amd_save_profile or args.amd_export_lact or args.amd_auto_uv):
            return True

    if getattr(args, "amd_auto_uv", False):
        return _run_ladder(args, state, lact, print_fn=print_fn, log=deps.log)

    _apply_voltage_floor_args(args)
    plan = _plan_from_args(args, state)
    problems = validate_plan(plan, state)
    if problems:
        raise NvmlError("AMD plan rejected:\n  - " + "\n  - ".join(problems))

    handled = False
    if args.amd_plan and not (args.amd_apply or args.amd_save_profile or args.amd_export_lact):
        # A bare plan is a preview.
        _print_plan(plan, state, json_output=json_output, print_fn=print_fn)
        handled = True
    if getattr(args, "dry_run", False) and (args.amd_apply or args.amd_save_profile):
        _print_plan(plan, state, json_output=json_output, print_fn=print_fn)
        if args.amd_save_profile:
            payload = plan_profile_payload(plan, state, name=str(args.amd_save_profile), tier=_tier(args))
            print_fn("# profile that --amd-save-profile would write:", flush=True)
            print_fn(json.dumps(payload, indent=2), flush=True)
        return True
    if args.amd_export_lact is not None:
        _export_lact(args, plan, state, print_fn=print_fn)
        handled = True
    if args.amd_save_profile:
        _save_profile(args, plan, state, print_fn=print_fn)
        handled = True
    if args.amd_apply:
        _apply_plan(args, plan, state, lact, print_fn=print_fn)
        handled = True
    return handled or True


# ---------------------------------------------------------------------------


def _apply_voltage_floor_args(args) -> None:
    """--amd-voltage-floor below the default needs --allow-below-floor."""
    import os

    from drivers.amdgpu.plan import (
        DAEMON_VOLTAGE_FLOOR_FILE,
        DEFAULT_VOLTAGE_OFFSET_FLOOR_MV,
        VOLTAGE_FLOOR_ENV,
    )

    requested = getattr(args, "amd_voltage_floor", None)
    if requested is None:
        return
    requested = int(requested)
    if requested < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
        if not getattr(args, "allow_below_floor", False):
            raise NvmlError(
                f"--amd-voltage-floor {requested} is deeper than the default safety floor "
                f"{DEFAULT_VOLTAGE_OFFSET_FLOOR_MV} mV; add --allow-below-floor to confirm you want "
                "Tux Burner to plan/write/sweep that deep"
            )
        print(
            f"WARNING: safety floor lowered to {requested} mV for this command. The daemon keeps its own "
            f"floor: write the same value to {DAEMON_VOLTAGE_FLOOR_FILE} (root) before applying, e.g. "
            f"`echo {requested} | sudo tee {DAEMON_VOLTAGE_FLOOR_FILE}`.",
            file=sys.stderr,
            flush=True,
        )
    os.environ[VOLTAGE_FLOOR_ENV] = str(requested)


def _tier(args) -> str:
    return str(getattr(args, "amd_profile_tier", "") or "").strip().lower()


def _plan_from_args(args, state: AmdGpuState) -> AmdTuningPlan:
    base = AmdTuningPlan(
        voltage_offset_mv=int(state.od.vddgfx_offset_mv or 0),
        sclk_offset_mhz=int(state.od.sclk_offset_mhz or 0),
    )
    assignments = list(getattr(args, "amd_plan", None) or [])
    try:
        return parse_plan_assignments(assignments, state=state, base=base)
    except AmdPlanError as exc:
        raise NvmlError(str(exc)) from exc


def _print_status(state: AmdGpuState, lact, *, json_output: bool, print_fn) -> None:
    opencl_lines: list[str] = []
    try:
        from stability.opencl_bruteforce import describe_devices

        opencl_lines = describe_devices()
    except Exception as exc:  # noqa: BLE001 - OpenCL is optional for status
        opencl_lines = [f"unavailable: {exc}"]
    daemon_line = _daemon_view(state)
    if json_output:
        payload = {
            "gpu": {
                "index": state.index,
                "name": state.name,
                "pci_addr": state.pci_addr,
                "uuid": state.uuid,
                "lact_gpu_id": state.lact_gpu_id,
                "device_dir": str(state.device_dir),
                "performance_level": state.performance_level,
            },
            "overdrive": asdict(state.od),
            "fan_curve": asdict(state.fan_curve),
            "stock_mclk_max_mhz": state.stock_mclk_max_mhz,
            "power_cap_w": {
                "current": state.power_cap_w,
                "min": state.power_cap_min_w,
                "max": state.power_cap_max_w,
                "default": state.power_cap_default_w,
            },
            "live": {
                "sclk_mhz": state.sclk_mhz,
                "mclk_mhz": state.mclk_mhz,
                "vddgfx_mv": state.vddgfx_mv,
                "power_w": state.power_avg_w,
                "busy_pct": state.busy_pct,
                "temps_c": state.temps_c,
                "fan_rpm": state.fan_rpm,
                "fan_pwm_pct": state.fan_pwm_pct,
            },
            "current_plan": asdict(current_plan(state)),
            "lact": {
                "active": lact.active,
                "unit_active": lact.unit_active,
                "socket_present": lact.socket_present,
                "process_running": lact.process_running,
                "gpu_config": lact.gpu_config,
            },
            "opencl_devices": opencl_lines,
            "daemon": daemon_line,
        }
        print_fn(json.dumps(payload, indent=2, default=str), flush=True)
        return
    print_fn(format_state(state, lact), flush=True)
    print_fn("  OpenCL (stress workload) devices: " + ("; ".join(opencl_lines) or "none"), flush=True)
    print_fn("  tux-burnerd: " + daemon_line, flush=True)


def _daemon_view(state: AmdGpuState) -> str:
    try:
        from drivers.nvidia.daemon_gpu import DaemonGpuClient

        caps = DaemonGpuClient(state.index).capabilities()
    except Exception as exc:  # noqa: BLE001
        return f"not reachable ({exc}); start/install it with the GUI or `sudo tux-burner-cli --install-systemd-service`"
    return (
        f"vendor={caps.vendor} name={caps.identity.name!r} uuid={caps.identity.uuid} "
        f"voltage_offset={caps.voltage_offset_mv} mV (range {caps.voltage_offset_range_mv}) "
        f"gpc_offset={caps.clock_offsets.gpc_mhz} mem_offset={caps.clock_offsets.memory_mhz} "
        f"power={caps.power.current_w} W locked_clock_control={caps.locked_clock_control} "
        f"daemon_vddgfx_floor={caps.vddgfx_floor_mv} mV ({caps.vddgfx_floor_source or 'unknown'})"
    )


def daemon_vddgfx_floor(state: AmdGpuState) -> tuple[int | None, str]:
    """(floor_mv, source) as the daemon reports it; (None, '') when unreachable
    or when the daemon predates the floor RPC."""
    try:
        from drivers.nvidia.daemon_gpu import DaemonGpuClient

        caps = DaemonGpuClient(state.index).capabilities(refresh=True)
    except Exception:  # noqa: BLE001
        return None, ""
    return caps.vddgfx_floor_mv, caps.vddgfx_floor_source


def _require_daemon_floor_covers(args, state: AmdGpuState, *, action: str) -> None:
    """Refuse to start a write/ladder whose app floor is deeper than the daemon's
    floor: every rung below the daemon floor would be refused and look like a
    failure. Keeps both floors in sync before anything runs."""
    from drivers.amdgpu.plan import DEFAULT_VOLTAGE_OFFSET_FLOOR_MV, voltage_offset_floor_mv

    app_floor = voltage_offset_floor_mv()
    if app_floor >= DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
        return
    daemon_floor, source = daemon_vddgfx_floor(state)
    if daemon_floor is None:
        raise NvmlError(
            f"{action} refused: the app floor is {app_floor} mV but tux-burnerd did not report its own "
            "safety floor (daemon unreachable or older than the AMD floor RPC). Reinstall/restart the "
            "daemon and retry."
        )
    if app_floor < daemon_floor:
        raise NvmlError(
            f"{action} refused: the app floor is {app_floor} mV but the daemon's safety floor is {daemon_floor} mV "
            f"({source}); writes below {daemon_floor} mV would be refused and look like failures. Lower the daemon "
            f"floor first: `tburn-cli --amd-set-daemon-floor {app_floor} --allow-below-floor` (or tick the GUI floor box)."
        )


def _set_daemon_floor(args, state: AmdGpuState, *, print_fn) -> None:
    from drivers.amdgpu.plan import DEFAULT_VOLTAGE_OFFSET_FLOOR_MV

    raw = str(args.amd_set_daemon_floor).strip().lower()
    if raw in {"default", "reset", "none"}:
        floor = None
    else:
        try:
            floor = int(raw)
        except ValueError as exc:
            raise NvmlError(f"--amd-set-daemon-floor expects an integer mV value or 'default', got {raw!r}") from exc
        if floor > 0:
            raise NvmlError("the VDDGFX floor must be <= 0 mV (undervolt only)")
        rng = state.od.range_vddgfx_offset_mv
        if rng and floor < rng[0]:
            raise NvmlError(f"the VDDGFX floor {floor} mV is below the driver OD_RANGE minimum {rng[0]} mV")
        if floor < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV and not getattr(args, "allow_below_floor", False):
            raise NvmlError(
                f"--amd-set-daemon-floor {floor} is deeper than the default {DEFAULT_VOLTAGE_OFFSET_FLOOR_MV} mV; "
                "add --allow-below-floor to confirm"
            )
    client, _caps = _client_for(state)
    result = client.set_vddgfx_floor_mv(floor)
    print_fn(
        "tux-burnerd VDDGFX safety floor: "
        f"{result.get('floor_mv')} mV ({result.get('source')}; file {result.get('file')})",
        flush=True,
    )


def _print_plan(plan: AmdTuningPlan, state: AmdGpuState, *, json_output: bool, print_fn) -> None:
    if json_output:
        print_fn(
            json.dumps(
                {
                    "plan": asdict(plan),
                    "current": asdict(current_plan(state)),
                    "writes": [{"path": path, "value": value} for path, value in sysfs_writes(plan, state)],
                },
                indent=2,
            ),
            flush=True,
        )
        return
    print_fn(render_dry_run(plan, state), flush=True)


def _require_lact_override(args, state: AmdGpuState, lact, *, action: str) -> None:
    if not lact.active:
        return
    if getattr(args, "override_lact", False):
        print(
            f"WARNING: LACT is running and owns {state.name}. {action} will go ahead because "
            "--override-lact was given, but lactd re-applies its own config (voltage/clock offsets, "
            "power cap, fan curve) on its next apply: boot, profile switch, or any LACT GUI change. "
            "Prefer `--amd-export-lact` and let LACT carry the settings.",
            file=sys.stderr,
            flush=True,
        )
        return
    configured = ""
    if lact.gpu_config:
        configured = " LACT currently configures: " + ", ".join(
            f"{key}={lact.gpu_config[key]}"
            for key in ("voltage_offset", "gpu_clock_offsets", "max_memory_clock", "power_cap")
            if key in lact.gpu_config
        ) + "."
    raise NvmlError(
        f"{action} refused: LACT (lactd) is running and owns GPU tuning on this machine, so Tux Burner "
        "defers to it.%s Options: review the plan with --amd-dry-run, hand it to LACT with --amd-export-lact, "
        "or pass --override-lact to write anyway (LACT will re-apply its own settings later; never "
        "restart lactd on a live desktop to pick up changes)." % configured
    )


def _client_for(state: AmdGpuState):
    from drivers.nvidia.daemon_gpu import DaemonGpuClient

    client = DaemonGpuClient(state.index)
    caps = client.capabilities(refresh=True)
    if caps.vendor != "amd":
        raise NvmlError(
            f"tux-burnerd reports GPU {state.index} as vendor {caps.vendor!r}; the daemon binary "
            "predates the AMD port - reinstall tux-burner-amd and reinstall the service"
        )
    if caps.identity.uuid != state.uuid:
        raise NvmlError(
            f"tux-burnerd GPU {state.index} is {caps.identity.uuid}, sysfs says {state.uuid}; refusing to write"
        )
    return client, caps


def _apply_plan(args, plan: AmdTuningPlan, state: AmdGpuState, lact, *, print_fn) -> None:
    _require_lact_override(args, state, lact, action="--amd-apply")
    _require_daemon_floor_covers(args, state, action="--amd-apply")
    client, caps = _client_for(state)
    results: dict[str, object] = {}
    mem_offset = None
    if plan.mclk_max_mhz is not None:
        stock = caps.memory_clock_stock_max_mhz or state.stock_mclk_max_mhz
        if not stock:
            raise NvmlError("cannot apply a memory clock: the stock memory clock is unknown")
        mem_offset = int(plan.mclk_max_mhz) - int(stock)
    gpc_offset = int(plan.sclk_offset_mhz) if state.od.supports_sclk_offset else None
    if gpc_offset is not None or mem_offset is not None:
        results["clock_offsets"] = client.apply_clock_offsets(
            gpc_clk_vf_offset_mhz=gpc_offset,
            mem_clk_vf_offset_mhz=mem_offset,
        )
    results["voltage_offset"] = client.apply_voltage_offset_mv(int(plan.voltage_offset_mv))
    if plan.power_cap_w is not None:
        results["power_limit_w"] = client.apply_power_limit_w(int(plan.power_cap_w))
    if plan.fan_curve is not None or plan.zero_rpm is not None:
        results["fan"] = (
            "not applied: the fan curve is applied by the daemon's runtime engine (silent fan curve) "
            "or by LACT; use --amd-export-lact to hand it to LACT"
        )
    after = select_amd_gpu(state.index).read_state()
    print_fn(f"Applied through tux-burnerd: {plan.describe()}", flush=True)
    print_fn("Readback: " + current_plan(after).describe(), flush=True)
    print_fn(json.dumps(results, indent=2, default=str), flush=True)


def _save_profile(args, plan: AmdTuningPlan, state: AmdGpuState, *, print_fn) -> None:
    from profiles.uv.profile_store import auto_uv_profiles_dir
    from profiles.uv.profile_tiers import save_profile_tier_assignment

    name = str(args.amd_save_profile)
    tier = _tier(args)
    payload = plan_profile_payload(plan, state, name=name, tier=tier)
    path = atomic_write_json(auto_uv_profiles_dir() / f"auto-uv-profile-{payload['profile_id']}.json", payload)
    if tier:
        save_profile_tier_assignment(payload["profile_id"], tier, gpu_uuid=state.uuid)
    print_fn(f"Saved AMD profile {payload['profile_id']} -> {path}", flush=True)
    print_fn(
        "It is saved UNVERIFIED (like a user-edited profile). Verify it with the GUI's Profiles > Verify "
        f"or `tux-burner-cli --stability-test --auto-uv-profile {payload['profile_id']}` "
        "(Q2RTX + OpenCL load), then apply it from the GUI or with "
        f"`tux-burner-cli --daemonize --auto-uv-profile {payload['profile_id']}`.",
        flush=True,
    )


def _export_lact(args, plan: AmdTuningPlan, state: AmdGpuState, *, print_fn) -> None:
    from integrations.lact.export import build_lact_amd_config

    text, warnings = build_lact_amd_config(
        gpu_id=state.lact_gpu_id,
        plan=plan,
        source_label="tux-burner-cli --amd-plan " + " ".join(getattr(args, "amd_plan", None) or []),
    )
    target = str(args.amd_export_lact or "-")
    if target == "-":
        print_fn(text, flush=True)
    else:
        Path(target).expanduser().write_text(text, encoding="utf-8")
        print_fn(f"LACT config fragment written to {target}", flush=True)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    print(
        "Merge the `gpus:` entry into /etc/lact/config.yaml by hand and let LACT apply it at the next "
        "boot (do NOT `systemctl restart lactd` on a live desktop: a hot re-apply has frozen this machine).",
        file=sys.stderr,
        flush=True,
    )


def _opencl_args(state: AmdGpuState, duration_s: float) -> list[str]:
    bus = state.pci_addr.split(":")[1] if state.pci_addr.count(":") >= 2 else ""
    extra = ["--pci-bus", bus] if bus else []
    return ["--gpu-index", str(state.index), "--duration-seconds", str(float(duration_s)), *extra]


class _RunLog:
    """Tee of everything a ladder/stress run prints, under debug-logs/."""

    def __init__(self, kind: str, explicit_path: str | None = None) -> None:
        from datetime import datetime

        from common.tux_burner_paths import default_user_config_dir

        if explicit_path:
            self.path = Path(explicit_path).expanduser()
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = default_user_config_dir() / "debug-logs" / f"tux_burner-amd-{kind}-{stamp}.log"
        self._handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
            self._handle.write(f"# Tux Burner AMD {kind} log, started {datetime.now().astimezone().isoformat()}\n")
            self._handle.flush()
        except OSError:
            self._handle = None

    def write(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(str(text).rstrip("\n") + "\n")
            self._handle.flush()
        except OSError:
            pass

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def _sample_telemetry(gpu, t0: float):
    """One hwmon operating point (read-only): vddgfx mV, sclk MHz, power W, junction C."""
    import time

    from auto_uv.amd_sweep import TelemetrySample

    try:
        st = gpu.read_state()
    except Exception:  # noqa: BLE001
        return None
    return TelemetrySample(
        t_s=round(time.monotonic() - t0, 2),
        vddgfx_mv=st.vddgfx_mv,
        sclk_mhz=st.sclk_mhz,
        power_w=None if st.power_avg_w is None else round(st.power_avg_w, 1),
        junction_c=st.temps_c.get("junction"),
    )


def run_opencl_sampled(
    state: AmdGpuState,
    seconds: float,
    *,
    log,
    offset_mv: int | None = None,
    sample_interval_s: float = 0.5,
):
    """Run the OpenCL workload as a child, stream its output through ``log``,
    and sample the live operating point every ``sample_interval_s`` while it
    runs (emitted as `@amd sample` events). Returns (exit_code, output, samples)."""
    import selectors
    import time

    from auto_uv.amd_sweep import emit_event
    from drivers.amdgpu.sysfs import select_amd_gpu
    from stability import opencl_bruteforce

    gpu = select_amd_gpu(state.index)
    command = [sys.executable, str(Path(opencl_bruteforce.__file__).resolve()), *_opencl_args(state, seconds)]
    env = dict(__import__("os").environ)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    output: list[str] = []
    samples = []
    t0 = time.monotonic()
    next_sample = t0
    eof = False
    while True:
        now = time.monotonic()
        if now >= next_sample:
            sample = _sample_telemetry(gpu, t0)
            if sample is not None:
                samples.append(sample)
                emit_event(
                    log,
                    "sample",
                    offset_mv=offset_mv,
                    t_s=sample.t_s,
                    vddgfx_mv=sample.vddgfx_mv,
                    sclk_mhz=sample.sclk_mhz,
                    power_w=sample.power_w,
                    junction_c=sample.junction_c,
                )
            next_sample = now + sample_interval_s
        if not eof:
            for _key, _mask in selector.select(timeout=max(0.0, min(0.25, next_sample - time.monotonic()))):
                line = process.stdout.readline()
                if line == "":
                    eof = True
                    break
                output.append(line)
                log(line.rstrip("\n"))
        if eof and process.poll() is not None:
            break
        if eof:
            time.sleep(0.05)
    selector.close()
    return process.returncode, "".join(output), samples


def _run_stress(args, state: AmdGpuState, *, print_fn) -> bool:
    from auto_uv.amd_sweep import FAILURE_LABELS, classify_stress_exit
    from stability import opencl_bruteforce

    duration = float(args.amd_stress)
    if getattr(args, "amd_dry_run", False) or getattr(args, "dry_run", False):
        print_fn(
            f"# dry run: would run {opencl_bruteforce.__file__} {' '.join(_opencl_args(state, duration))} "
            "(sustained GPU compute load; no tuning writes)",
            flush=True,
        )
        return True
    run_log = _RunLog("stress", getattr(args, "amd_run_log", None))

    def log(text: str) -> None:
        print_fn(text, flush=True)
        run_log.write(text)

    log(f"AMD stress test: {duration:.0f}s OpenCL brute-force on {state.name}; log {run_log.path}")
    code, output, samples = run_opencl_sampled(state, duration, log=log, offset_mv=state.od.vddgfx_offset_mv)
    failure, detail = classify_stress_exit(code, output)
    verdict = "stable" if code == 0 else f"{FAILURE_LABELS.get(failure, failure)}{': ' + detail if detail else ''}"
    log(f"opencl-bruteforce exit code {code}: {verdict} ({len(samples)} telemetry samples); log {run_log.path}")
    run_log.close()
    if code != 0:
        raise SystemExit(code)
    return True


def _run_ladder(args, state: AmdGpuState, lact, *, print_fn, log) -> bool:
    import os
    from datetime import datetime

    from auto_uv import amd_sweep
    from drivers.amdgpu.plan import VOLTAGE_FLOOR_ENV, voltage_offset_floor_mv
    from stability import opencl_bruteforce

    _apply_voltage_floor_args(args)
    plan_for_tiers = _plan_from_args(args, state) if getattr(args, "amd_plan", None) else None
    settings = amd_sweep.LadderSettings(
        start_mv=int(getattr(args, "amd_auto_uv_start", 0) or 0),
        step_mv=int(getattr(args, "amd_auto_uv_step", amd_sweep.DEFAULT_STEP_MV) or amd_sweep.DEFAULT_STEP_MV),
        floor_mv=getattr(args, "amd_auto_uv_floor", None),
        rung_seconds=int(getattr(args, "amd_auto_uv_rung_seconds", amd_sweep.DEFAULT_RUNG_SECONDS)),
        margin_mv=int(getattr(args, "amd_auto_uv_margin", amd_sweep.DEFAULT_MARGIN_MV)),
        sclk_offset_mhz=plan_for_tiers.sclk_offset_mhz if plan_for_tiers else 0,
        mclk_max_mhz=plan_for_tiers.mclk_max_mhz if plan_for_tiers else None,
    )
    offsets = amd_sweep.plan_ladder(state, settings, unsafe_from_marker=amd_sweep.read_marker())
    print_fn(amd_sweep.describe_ladder(state, settings, offsets), flush=True)
    if getattr(args, "amd_dry_run", False) or getattr(args, "dry_run", False):
        print_fn("# dry run: nothing applied, no load generated", flush=True)
        for tier, plan in amd_sweep.tier_plans(offsets[-1] if offsets else 0, state, settings).items():
            print_fn(f"# example tier if the whole ladder passed - {tier}: {plan.describe()}", flush=True)
        return True
    _require_lact_override(args, state, lact, action="--amd-auto-uv")
    _require_daemon_floor_covers(args, state, action="--amd-auto-uv")
    client, _caps = _client_for(state)
    daemon_floor, _source = daemon_vddgfx_floor(state)
    original_offset = int(state.od.vddgfx_offset_mv or 0)
    run_log = _RunLog("ladder", getattr(args, "amd_run_log", None))
    started_at = datetime.now().astimezone().isoformat()

    def tee(text: str) -> None:
        log(text)
        run_log.write(text)

    tee(f"AMD Auto-UV ladder log: {run_log.path}")

    def apply_offset(offset_mv: int) -> None:
        client.apply_voltage_offset_mv(int(offset_mv))

    current = {"offset": None}

    def run_stress(seconds: int):
        return run_opencl_sampled(state, seconds, log=tee, offset_mv=current["offset"])

    original_apply = apply_offset

    def apply_and_track(offset_mv: int) -> None:
        original_apply(offset_mv)
        current["offset"] = int(offset_mv)

    result = amd_sweep.run_ladder(state, settings, apply_offset=apply_and_track, run_stress=run_stress, log=tee)
    try:
        client.apply_voltage_offset_mv(original_offset)
        tee(f"AMD Auto-UV: restored the pre-scan VDDGFX offset {original_offset:+d} mV")
    except Exception as exc:  # noqa: BLE001
        tee(f"AMD Auto-UV: WARNING could not restore the pre-scan offset: {exc}")
    for rung in result.rungs:
        tee("  " + rung.describe())
    tee("AMD Auto-UV: " + result.summary())
    saved_tiers: dict[str, str] = {}
    record_kwargs = dict(
        kind="amd-ladder",
        state=state,
        settings=settings,
        result=result,
        started_at=started_at,
        finished_at=datetime.now().astimezone().isoformat(),
        app_floor_mv=voltage_offset_floor_mv(),
        daemon_floor_mv=daemon_floor,
        log_path=str(run_log.path),
        override_lact=bool(getattr(args, "override_lact", False)),
    )
    if result.aborted or result.deepest_stable_mv is None:
        path = amd_sweep.save_run_record(amd_sweep.run_record(saved_tiers=saved_tiers, **record_kwargs))
        tee(f"AMD Auto-UV: run record {path}")
        run_log.close()
        if result.aborted:
            raise NvmlError(f"AMD Auto-UV aborted: {result.aborted}")
        if result.refused_by_floor_mv is not None:
            raise NvmlError(
                "AMD Auto-UV: the daemon's safety floor refused the very first rung; no stability result. "
                "Lower the daemon floor (`tburn-cli --amd-set-daemon-floor <mV> --allow-below-floor`) and retry."
            )
        raise NvmlError("AMD Auto-UV: even the first rung failed; the card is not stable at the starting offset")
    plans = amd_sweep.tier_plans(result.deepest_stable_mv, state, settings)
    from profiles.uv.profile_store import auto_uv_profiles_dir
    from profiles.uv.profile_tiers import save_profile_tier_assignment

    verification = {
        "method": "amd-vddgfx-ladder",
        "deepest_stable_mv": result.deepest_stable_mv,
        "first_unstable_mv": result.first_unstable_mv,
        "stopped_reason": result.stopped_reason,
        "refused_by_daemon_floor_mv": result.refused_by_floor_mv,
        "rungs": [rung.describe() for rung in result.rungs],
        "margin_mv": settings.margin_mv,
        "rung_seconds": settings.rung_seconds,
        "workload": "opencl-bruteforce",
        "log_path": str(run_log.path),
    }
    for tier, plan in plans.items():
        payload = plan_profile_payload(plan, state, name=f"ladder-{tier}", tier=tier, verified=True, verification=verification, manual=False)
        path = atomic_write_json(auto_uv_profiles_dir() / f"auto-uv-profile-{payload['profile_id']}.json", payload)
        save_profile_tier_assignment(payload["profile_id"], tier, gpu_uuid=state.uuid)
        saved_tiers[tier] = payload["profile_id"]
        tee(f"Saved {tier}: {plan.describe()} -> {path}")
    record_path = amd_sweep.save_run_record(amd_sweep.run_record(saved_tiers=saved_tiers, **record_kwargs))
    tee(f"AMD Auto-UV: run record {record_path}")
    tee(
        f"Deepest stable VDDGFX offset {result.deepest_stable_mv:+d} mV; tiers use {result.deepest_stable_mv + settings.margin_mv:+d} mV. "
        "Apply one from the GUI (Profiles) or `tux-burner-cli --daemonize --auto-uv-profile amd-ladder-balanced`."
    )
    run_log.close()
    return True


def _restore(args, state: AmdGpuState, lact, *, print_fn) -> None:
    """--amd-restore lact|stock with before/after readback."""
    before = current_plan(state).describe()
    mode = str(args.amd_restore)
    if getattr(args, "amd_dry_run", False) or getattr(args, "dry_run", False):
        print_fn(f"# dry run: --amd-restore {mode}; current: {before}", flush=True)
        return
    if mode == "lact":
        if not lact.active:
            raise NvmlError("--amd-restore lact: lactd is not running; start it from LACT or reboot, or use --amd-restore stock")
        try:
            current = subprocess.run(["lact", "cli", "profile", "get"], check=False, capture_output=True, text=True, timeout=10)
            profile = (current.stdout or "").strip().splitlines()[-1].strip() if current.stdout.strip() else "default"
        except (OSError, subprocess.SubprocessError, IndexError):
            profile = "default"
        print_fn(f"Asking lactd to re-apply its profile {profile!r} (lact cli profile set; no restart, no config edit) ...", flush=True)
        result = subprocess.run(["lact", "cli", "profile", "set", profile], check=False, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and profile != "default":
            result = subprocess.run(["lact", "cli", "profile", "set", "default"], check=False, capture_output=True, text=True, timeout=30)
        print_fn((result.stdout or "") + (result.stderr or ""), flush=True)
        if result.returncode != 0:
            raise NvmlError(
                "lact cli profile set failed; LACT exposes no other re-apply interface. Use LACT's GUI Apply or reboot "
                "(LACT re-applies its config at boot). Never restart lactd on a live desktop."
            )
    else:
        _require_lact_override(args, state, lact, action="--amd-restore stock")
        client, _caps = _client_for(state)
        from runtime.daemon_client import gpu_reset_defaults, gpu_reset_fans

        print_fn("Resetting OverDrive offsets, memory clock and the power cap to driver defaults through tux-burnerd ...", flush=True)
        reset = gpu_reset_defaults(state.index)
        print_fn(json.dumps({k: v for k, v in reset.items() if k != "points"}, indent=2, default=str), flush=True)
        if lact.active:
            print_fn("PMFW fan curve left alone: LACT is running and owns it.", flush=True)
        else:
            try:
                fans = gpu_reset_fans(state.index)
                print_fn(f"PMFW fan curve reset to firmware auto: {fans}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print_fn(f"PMFW fan curve reset skipped: {exc}", flush=True)
        print_fn(
            "Note: this removed the undervolt/overclock; LACT re-applies its own settings at its next apply "
            "(boot, profile switch, LACT GUI Apply).",
            flush=True,
        )
    after = select_amd_gpu(state.index).read_state()
    print_fn(f"Before: {before}", flush=True)
    print_fn(f"After:  {current_plan(after).describe()}", flush=True)


def amd_gpus_summary() -> list[dict]:
    return [
        {"index": gpu.index, "card": gpu.card, "pci_addr": gpu.pci_addr, "device_dir": str(gpu.device_dir)}
        for gpu in discover_amd_gpus()
    ]
