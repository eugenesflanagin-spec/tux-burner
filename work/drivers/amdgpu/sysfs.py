"""Read amdgpu OverDrive, hwmon and fan-control state from sysfs.

Pure reads. The parsers mirror ``burnerd/src/gpu/amdgpu.rs`` so the Python
preview and the Rust daemon agree on ranges and units:

* ``pp_od_clk_voltage`` - ``OD_SCLK_OFFSET`` (MHz), ``OD_MCLK`` min/max (MHz),
  ``OD_VDDGFX_OFFSET`` (mV, negative = undervolt) and ``OD_RANGE``;
* ``hwmon/hwmon*/power1_cap{,_min,_max,_default}`` - power limit in uW;
* ``gpu_od/fan_ctrl/*`` - the PMFW fan curve (5 points, hotspot keyed).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

AMD_VENDOR_ID = 0x1002
UUID_PREFIX = "AMD-"
SYSFS_ROOT_ENV = "TUX_BURNERD_AMDGPU_SYSFS_ROOT"
LACT_SOCKET = Path("/run/lactd.sock")
LACT_CONFIG = Path("/etc/lact/config.yaml")
_INT_RE = re.compile(r"^[+-]?\d+")


def sysfs_root() -> Path:
    override = os.environ.get(SYSFS_ROOT_ENV, "").strip()
    return Path(override) if override else Path("/sys/class/drm")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_hex(path: Path) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def leading_int(token: str) -> int | None:
    match = _INT_RE.match(str(token).strip())
    return int(match.group(0)) if match else None


def _range_pair(rest: str) -> tuple[int, int] | None:
    values = [value for value in (leading_int(tok) for tok in rest.split()) if value is not None]
    if len(values) < 2:
        return None
    return values[0], values[1]


@dataclass(frozen=True, slots=True)
class OdTable:
    sclk_offset_mhz: int | None = None
    sclk_min_mhz: int | None = None
    sclk_max_mhz: int | None = None
    mclk_min_mhz: int | None = None
    mclk_max_mhz: int | None = None
    vddgfx_offset_mv: int | None = None
    range_sclk_offset_mhz: tuple[int, int] | None = None
    range_sclk_mhz: tuple[int, int] | None = None
    range_mclk_mhz: tuple[int, int] | None = None
    range_vddgfx_offset_mv: tuple[int, int] | None = None

    @property
    def supports_sclk_offset(self) -> bool:
        return self.range_sclk_offset_mhz is not None


def parse_od_table(text: str) -> OdTable:
    values: dict[str, object] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":") and " " not in line:
            section = {
                "OD_SCLK_OFFSET:": "sclk_offset",
                "OD_SCLK:": "sclk",
                "OD_MCLK:": "mclk",
                "OD_VDDGFX_OFFSET:": "vddgfx",
                "OD_RANGE:": "range",
            }.get(line, "other")
            continue
        if section == "sclk_offset":
            values["sclk_offset_mhz"] = leading_int(line)
        elif section == "vddgfx":
            values["vddgfx_offset_mv"] = leading_int(line)
        elif section in {"sclk", "mclk"}:
            level, _, rest = line.partition(":")
            level = level.strip()
            if level in {"0", "1"}:
                bound = "min" if level == "0" else "max"
                values[f"{section}_{bound}_mhz"] = leading_int(rest)
        elif section == "range":
            label, _, rest = line.partition(":")
            key = {
                "SCLK_OFFSET": "range_sclk_offset_mhz",
                "SCLK": "range_sclk_mhz",
                "MCLK": "range_mclk_mhz",
                "VDDGFX_OFFSET": "range_vddgfx_offset_mv",
            }.get(label.strip())
            if key:
                values[key] = _range_pair(rest)
    return OdTable(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FanCurveTable:
    points: tuple[tuple[int, int], ...] = ()
    temp_range_c: tuple[int, int] | None = None
    speed_range_pct: tuple[int, int] | None = None


def parse_fan_curve(text: str) -> FanCurveTable:
    points: list[tuple[int, int]] = []
    temp_range = speed_range = None
    in_range = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "OD_FAN_CURVE:":
            in_range = False
            continue
        if line == "OD_RANGE:":
            in_range = True
            continue
        label, sep, rest = line.partition(":")
        if not sep:
            continue
        if in_range:
            if label.startswith("FAN_CURVE(hotspot temp)"):
                temp_range = _range_pair(rest)
            elif label.startswith("FAN_CURVE(fan speed)"):
                speed_range = _range_pair(rest)
            continue
        if label.strip().isdigit():
            pair = _range_pair(rest)
            if pair is not None:
                points.append(pair)
    return FanCurveTable(tuple(points), temp_range, speed_range)


def parse_dpm_levels(text: str) -> tuple[int, ...]:
    levels: set[int] = set()
    for raw in text.splitlines():
        level, sep, rest = raw.strip().partition(":")
        if not sep or not level.strip().isdigit():
            continue
        value = leading_int(rest)
        if value is not None and value > 0:
            levels.add(value)
    return tuple(sorted(levels))


def _parse_single_od(text: str | None, label: str) -> tuple[int | None, tuple[int, int] | None]:
    """``FAN_MINIMUM_PWM:\\n15\\nOD_RANGE:\\nMINIMUM_PWM: 15 100`` style files."""
    if text is None:
        return None, None
    current = None
    current_range = None
    in_range = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "OD_RANGE:":
            in_range = True
            continue
        if line.endswith(":"):
            continue
        if in_range:
            name, _, rest = line.partition(":")
            if name.strip() == label:
                current_range = _range_pair(rest)
        elif current is None:
            current = leading_int(line)
    return current, current_range


@dataclass(frozen=True, slots=True)
class LactStatus:
    unit_active: bool
    socket_present: bool
    process_running: bool
    config_path: str
    gpu_config: dict | None

    @property
    def active(self) -> bool:
        return self.unit_active or (self.socket_present and self.process_running)

    def describe(self) -> str:
        if not self.active:
            return "LACT: not running"
        parts = ["LACT: RUNNING (lactd owns GPU tuning and the GPU fan curve on this machine)"]
        if self.gpu_config:
            applied = []
            for key in (
                "voltage_offset",
                "gpu_clock_offsets",
                "max_memory_clock",
                "min_memory_clock",
                "power_cap",
                "performance_level",
                "power_profile_mode_index",
                "fan_control_enabled",
            ):
                if key in self.gpu_config:
                    applied.append(f"{key}={self.gpu_config[key]}")
            if applied:
                parts.append("  configured in " + self.config_path + ": " + ", ".join(applied))
        return "\n".join(parts)


def lact_gpu_config_id(vendor_id: int, device_id: int, subsystem: tuple[int, int] | None, pci_addr: str) -> str:
    """LACT keys its per-GPU config as ``1002:7550-1EAE:8811-0000:03:00.0``."""
    sub = f"{subsystem[0]:04X}:{subsystem[1]:04X}" if subsystem else "0000:0000"
    return f"{vendor_id:04X}:{device_id:04X}-{sub}-{pci_addr}"


def _process_named_lact() -> bool:
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            comm = _read(Path(entry.path) / "comm")
            if comm and (comm == "lact" or comm.startswith("lactd")):
                return True
    except OSError:
        return False
    return False


def lact_status(gpu_config_id: str = "") -> LactStatus:
    unit_active = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "lactd.service"],
            check=False,
            capture_output=True,
            timeout=3,
        )
        unit_active = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        unit_active = False
    gpu_config = None
    if gpu_config_id:
        try:
            import yaml

            payload = yaml.safe_load(LACT_CONFIG.read_text(encoding="utf-8"))
            gpus = payload.get("gpus", {}) if isinstance(payload, dict) else {}
            entry = gpus.get(gpu_config_id) if isinstance(gpus, dict) else None
            if isinstance(entry, dict):
                gpu_config = entry
        except Exception:  # noqa: BLE001 - LACT config is advisory
            gpu_config = None
    return LactStatus(
        unit_active=unit_active,
        socket_present=LACT_SOCKET.exists(),
        process_running=_process_named_lact(),
        config_path=str(LACT_CONFIG),
        gpu_config=gpu_config,
    )


@dataclass(frozen=True, slots=True)
class AmdGpuState:
    index: int
    card: str
    device_dir: Path
    hwmon_dir: Path | None
    pci_addr: str
    vendor_id: int
    device_id: int
    subsystem: tuple[int, int] | None
    unique_id: str
    name: str
    od: OdTable
    fan_curve: FanCurveTable
    stock_mclk_max_mhz: int | None
    dpm_sclk_mhz: tuple[int, ...]
    dpm_mclk_mhz: tuple[int, ...]
    power_cap_w: int | None
    power_cap_min_w: int | None
    power_cap_max_w: int | None
    power_cap_default_w: int | None
    power_avg_w: float | None
    temps_c: dict[str, float] = field(default_factory=dict)
    fan_rpm: int | None = None
    fan_pwm_pct: int | None = None
    vddgfx_mv: int | None = None
    sclk_mhz: int | None = None
    mclk_mhz: int | None = None
    busy_pct: int | None = None
    performance_level: str = ""
    fan_zero_rpm: int | None = None
    fan_minimum_pwm: int | None = None
    fan_minimum_pwm_range: tuple[int, int] | None = None
    fan_target_temp_c: int | None = None
    fan_acoustic_limit_rpm: int | None = None
    fan_acoustic_target_rpm: int | None = None

    @property
    def uuid(self) -> str:
        return f"{UUID_PREFIX}{self.unique_id}" if self.unique_id else f"{UUID_PREFIX}PCI-{self.pci_addr}"

    @property
    def pci_device_id(self) -> str:
        return f"0x{self.device_id:04X}{self.vendor_id:04X}"

    @property
    def lact_gpu_id(self) -> str:
        return lact_gpu_config_id(self.vendor_id, self.device_id, self.subsystem, self.pci_addr)

    @property
    def od_path(self) -> Path:
        return self.device_dir / "pp_od_clk_voltage"

    @property
    def fan_curve_path(self) -> Path:
        return self.device_dir / "gpu_od" / "fan_ctrl" / "fan_curve"

    @property
    def power_cap_path(self) -> Path | None:
        return None if self.hwmon_dir is None else self.hwmon_dir / "power1_cap"

    @property
    def stock_sclk_max_mhz(self) -> int | None:
        return self.dpm_sclk_mhz[-1] if self.dpm_sclk_mhz else None


def _pci_ids_name(vendor: int, device: int, subsystem: tuple[int, int] | None) -> str | None:
    for candidate in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
        try:
            text = Path(candidate).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        vendor_tag = f"{vendor:04x}  "
        device_tag = f"\t{device:04x}  "
        sub_tag = f"\t\t{subsystem[0]:04x} {subsystem[1]:04x}  " if subsystem else None
        in_vendor = in_device = False
        device_name = None
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            if not line.startswith("\t"):
                if in_vendor:
                    break
                in_vendor = line.startswith(vendor_tag)
                continue
            if not in_vendor:
                continue
            if not line.startswith("\t\t"):
                if in_device:
                    break
                if line.startswith(device_tag):
                    in_device = True
                    device_name = line[len(device_tag) :].strip()
                continue
            if in_device and sub_tag and line.startswith(sub_tag):
                return line[len(sub_tag) :].strip()
        if device_name:
            return device_name
    return None


def _pci_address(device_dir: Path) -> str:
    uevent = _read(device_dir / "uevent") or ""
    for line in uevent.splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            return line.split("=", 1)[1].strip()
    try:
        return device_dir.resolve().name
    except OSError:
        return device_dir.name


class AmdGpuSysfs:
    """One OverDrive-capable AMD card."""

    def __init__(self, index: int, card: str, device_dir: Path) -> None:
        self.index = int(index)
        self.card = card
        self.device_dir = device_dir
        hwmon_root = device_dir / "hwmon"
        hwmon_dirs = sorted(hwmon_root.glob("hwmon*")) if hwmon_root.is_dir() else []
        self.hwmon_dir: Path | None = hwmon_dirs[0] if hwmon_dirs else None
        self.pci_addr = _pci_address(device_dir)

    def _hwmon_int(self, name: str) -> int | None:
        return None if self.hwmon_dir is None else _read_int(self.hwmon_dir / name)

    def _temps(self) -> dict[str, float]:
        temps: dict[str, float] = {}
        if self.hwmon_dir is None:
            return temps
        for index in range(1, 9):
            label = _read(self.hwmon_dir / f"temp{index}_label")
            value = _read_int(self.hwmon_dir / f"temp{index}_input")
            if label is None or value is None:
                continue
            temps[label] = value / 1000.0
        return temps

    def read_state(self) -> AmdGpuState:
        d = self.device_dir
        vendor_id = _read_hex(d / "vendor") or AMD_VENDOR_ID
        device_id = _read_hex(d / "device") or 0
        sub_vendor = _read_hex(d / "subsystem_vendor")
        sub_device = _read_hex(d / "subsystem_device")
        subsystem = (sub_vendor, sub_device) if sub_vendor is not None and sub_device is not None else None
        product_name = _read(d / "product_name") or ""
        name = product_name or _pci_ids_name(vendor_id, device_id, subsystem) or f"GPU {vendor_id:04x}:{device_id:04x}"
        if not name.startswith(("AMD", "Radeon")):
            name = f"AMD {name}"
        od = parse_od_table(_read(d / "pp_od_clk_voltage") or "")
        fan_curve = parse_fan_curve(_read(d / "gpu_od" / "fan_ctrl" / "fan_curve") or "")
        dpm_sclk = parse_dpm_levels(_read(d / "pp_dpm_sclk") or "")
        dpm_mclk = parse_dpm_levels(_read(d / "pp_dpm_mclk") or "")

        def watts(name: str) -> int | None:
            value = self._hwmon_int(name)
            return None if value is None else (value + 500_000) // 1_000_000

        power_avg = self._hwmon_int("power1_average")
        if power_avg is None:
            power_avg = self._hwmon_int("power1_input")
        pwm = self._hwmon_int("pwm1")
        pwm_max = self._hwmon_int("pwm1_max") or 255
        sclk_hz = self._hwmon_int("freq1_input")
        mclk_hz = self._hwmon_int("freq2_input")
        zero_rpm, _ = _parse_single_od(_read(d / "gpu_od/fan_ctrl/fan_zero_rpm_enable"), "ZERO_RPM_ENABLE")
        minimum_pwm, minimum_pwm_range = _parse_single_od(
            _read(d / "gpu_od/fan_ctrl/fan_minimum_pwm"), "MINIMUM_PWM"
        )
        target_temp, _ = _parse_single_od(_read(d / "gpu_od/fan_ctrl/fan_target_temperature"), "TARGET_TEMPERATURE")
        acoustic_limit, _ = _parse_single_od(_read(d / "gpu_od/fan_ctrl/acoustic_limit_rpm_threshold"), "ACOUSTIC_LIMIT")
        acoustic_target, _ = _parse_single_od(_read(d / "gpu_od/fan_ctrl/acoustic_target_rpm_threshold"), "ACOUSTIC_TARGET")
        return AmdGpuState(
            index=self.index,
            card=self.card,
            device_dir=d,
            hwmon_dir=self.hwmon_dir,
            pci_addr=self.pci_addr,
            vendor_id=vendor_id,
            device_id=device_id,
            subsystem=subsystem,
            unique_id=_read(d / "unique_id") or "",
            name=name,
            od=od,
            fan_curve=fan_curve,
            stock_mclk_max_mhz=dpm_mclk[-1] if dpm_mclk else None,
            dpm_sclk_mhz=dpm_sclk,
            dpm_mclk_mhz=dpm_mclk,
            power_cap_w=watts("power1_cap"),
            power_cap_min_w=watts("power1_cap_min"),
            power_cap_max_w=watts("power1_cap_max"),
            power_cap_default_w=watts("power1_cap_default"),
            power_avg_w=None if power_avg is None else power_avg / 1_000_000.0,
            temps_c=self._temps(),
            fan_rpm=self._hwmon_int("fan1_input"),
            fan_pwm_pct=None if pwm is None else round(pwm * 100 / pwm_max),
            vddgfx_mv=self._hwmon_int("in0_input"),
            sclk_mhz=None if sclk_hz is None else sclk_hz // 1_000_000,
            mclk_mhz=None if mclk_hz is None else mclk_hz // 1_000_000,
            busy_pct=_read_int(d / "gpu_busy_percent"),
            performance_level=_read(d / "power_dpm_force_performance_level") or "",
            fan_zero_rpm=zero_rpm,
            fan_minimum_pwm=minimum_pwm,
            fan_minimum_pwm_range=minimum_pwm_range,
            fan_target_temp_c=target_temp,
            fan_acoustic_limit_rpm=acoustic_limit,
            fan_acoustic_target_rpm=acoustic_target,
        )


def discover_amd_gpus(root: Path | None = None) -> list[AmdGpuSysfs]:
    """OverDrive-capable AMD cards sorted by PCI address (daemon index order)."""
    root = sysfs_root() if root is None else root
    found: list[tuple[str, str, Path]] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        suffix = entry.name[4:] if entry.name.startswith("card") else ""
        if not suffix.isdigit():
            continue
        device_dir = Path(entry.path) / "device"
        if _read_hex(device_dir / "vendor") != AMD_VENDOR_ID:
            continue
        if not (device_dir / "pp_od_clk_voltage").exists():
            continue
        found.append((_pci_address(device_dir), entry.name, device_dir))
    found.sort()
    return [AmdGpuSysfs(index, card, device_dir) for index, (_addr, card, device_dir) in enumerate(found)]


def amd_gpu_present(root: Path | None = None) -> bool:
    return bool(discover_amd_gpus(root))


def select_amd_gpu(gpu_index: int = 0, root: Path | None = None) -> AmdGpuSysfs:
    gpus = discover_amd_gpus(root)
    if not gpus:
        raise RuntimeError(
            "no OverDrive-capable AMD GPU found: amdgpu must be loaded with "
            "pp_od_clk_voltage exposed (kernel parameter amdgpu.ppfeaturemask=0xffffffff "
            "or the distribution's OverDrive default)"
        )
    for gpu in gpus:
        if gpu.index == int(gpu_index):
            return gpu
    raise RuntimeError(f"AMD GPU index {int(gpu_index)} not found; detected {len(gpus)} AMD GPU(s)")


def format_state(state: AmdGpuState, lact: LactStatus | None = None) -> str:
    od = state.od
    lines = [
        f"GPU {state.index}: {state.name}",
        f"  sysfs: {state.device_dir}  (hwmon: {state.hwmon_dir})",
        f"  pci: {state.pci_addr}  id: {state.vendor_id:04x}:{state.device_id:04x}"
        + (f"  subsystem: {state.subsystem[0]:04x}:{state.subsystem[1]:04x}" if state.subsystem else "")
        + f"  uuid: {state.uuid}",
        f"  LACT config id: {state.lact_gpu_id}",
        f"  performance level: {state.performance_level or 'n/a'}",
        "  OverDrive (pp_od_clk_voltage):",
    ]
    if od.supports_sclk_offset:
        lo, hi = od.range_sclk_offset_mhz or (None, None)
        lines.append(f"    SCLK offset:   {od.sclk_offset_mhz:+d} MHz   (range {lo}..{hi} MHz)")
    elif od.sclk_max_mhz is not None:
        lines.append(
            f"    SCLK min/max:  {od.sclk_min_mhz}/{od.sclk_max_mhz} MHz   (range {od.range_sclk_mhz})"
        )
    lines.append(
        f"    MCLK min/max:  {od.mclk_min_mhz}/{od.mclk_max_mhz} MHz   (range {od.range_mclk_mhz}; "
        f"stock max {state.stock_mclk_max_mhz} MHz from pp_dpm_mclk)"
    )
    lines.append(
        f"    VDDGFX offset: {od.vddgfx_offset_mv} mV   (range {od.range_vddgfx_offset_mv} mV; negative = undervolt)"
    )
    lines.append(
        f"  power cap: {state.power_cap_w} W   (min {state.power_cap_min_w}, max {state.power_cap_max_w}, "
        f"default {state.power_cap_default_w} W)"
    )
    temps = ", ".join(f"{label} {value:.0f}C" for label, value in state.temps_c.items())
    lines.append(
        f"  live: {state.sclk_mhz} MHz sclk, {state.mclk_mhz} MHz mclk, {state.vddgfx_mv} mV, "
        f"{state.power_avg_w if state.power_avg_w is None else round(state.power_avg_w)} W, "
        f"busy {state.busy_pct}%, {temps}"
    )
    lines.append(
        f"  fan: {state.fan_rpm} rpm ({state.fan_pwm_pct}% pwm); PMFW curve "
        + " ".join(f"{t}C:{p}%" for t, p in state.fan_curve.points)
        + f"  [temp {state.fan_curve.temp_range_c}, speed {state.fan_curve.speed_range_pct}, "
        f"zero_rpm={state.fan_zero_rpm}, min_pwm={state.fan_minimum_pwm}, target_temp={state.fan_target_temp_c}C]"
    )
    lines.append(f"  DPM sclk levels: {state.dpm_sclk_mhz}  mclk levels: {state.dpm_mclk_mhz}")
    if lact is not None:
        lines.append("  " + lact.describe().replace("\n", "\n  "))
    return "\n".join(lines)
