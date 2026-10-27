//! AMD (amdgpu) GPU backend: plain sysfs, no vendor library.
//!
//! Local AMD port (not upstream). Everything here is read from and written to
//! the amdgpu kernel driver's sysfs/hwmon nodes under
//! `/sys/class/drm/card<N>/device`, so the daemon needs no NVML, no NVAPI and
//! no ROCm to tune an AMD card.
//!
//! RDNA3/RDNA4 (SMU 13/14) OverDrive exposes no per-point V/F curve and no
//! clock lock. The NVIDIA concepts the engine was written around map onto the
//! amdgpu controls like this (verified against the running kernel's
//! `smu_v14_0_2_od_edit_dpm_table` for the exact write syntax):
//!
//! | engine concept                      | amdgpu control                                   |
//! |-------------------------------------|--------------------------------------------------|
//! | per-point V/F offsets (`vf_*`)      | none - `vf_curve_available()` is `false`         |
//! | core voltage                        | hwmon `in0_input` (VDDGFX, mV)                   |
//! | voltage undervolt                   | `pp_od_clk_voltage` `vo <mV>` (global VDDGFX     |
//! |                                     | offset, negative = undervolt, `OD_RANGE`)        |
//! | GPC clock offset (signed MHz)       | `pp_od_clk_voltage` `s <MHz>` (`OD_SCLK_OFFSET`) |
//! | memory clock offset (signed MHz)    | `pp_od_clk_voltage` `m 1 <MHz>` = stock max +    |
//! |                                     | offset (`OD_MCLK` level 1)                       |
//! | power limit (W)                     | hwmon `power1_cap` (uW), `power1_cap_min/max`    |
//! | locked clocks / clock ceiling       | none - `has_locked_clock_control()` is `false`   |
//! | fan speed %                         | `gpu_od/fan_ctrl/fan_curve` (5-point PMFW curve) |
//! | persistence mode                    | not a concept on amdgpu (no-op)                  |
//!
//! Every OverDrive edit is staged and then committed with `c`. On SMU14 a
//! committed OverDrive table can reset the power cap to its default, so every
//! commit re-reads `power1_cap` and restores the value that was in force
//! before the commit.

use std::cell::Cell;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::{
    voltage_uv_in_window, AppliedOffsets, ClockOffsets, ClockType, GpuBackend, GpuContextPids,
    GpuError, GpuIdentity, GpuMemoryInfo, GpuResult, GpuVendor, PowerLimits, RangeSnapResult,
    SnapResult, VfPoint, VfSummary,
};

/// Stable identity prefix: `AMD-<unique_id>` (amdgpu's 64-bit `unique_id`).
pub const UUID_PREFIX: &str = "AMD-";
const AMD_VENDOR_ID: u32 = 0x1002;
const SYSFS_ROOT_ENV: &str = "TUX_BURNERD_AMDGPU_SYSFS_ROOT";
const LACT_SOCKET: &str = "/run/lactd.sock";
const FAN_CURVE_POINTS: usize = 5;

pub fn default_sysfs_root() -> PathBuf {
    std::env::var_os(SYSFS_ROOT_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/sys/class/drm"))
}

/// One OverDrive-capable AMD card: `cardN` plus its `device/` directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AmdCard {
    pub card_name: String,
    pub device_dir: PathBuf,
    pub pci_addr: String,
}

/// Enumerate `card<N>` entries whose device is AMD (vendor 0x1002) and exposes
/// `pp_od_clk_voltage` (OverDrive enabled via `ppfeaturemask`). Sorted by PCI
/// address so the index space is stable across boots.
pub fn enumerate_cards(root: &Path) -> Vec<AmdCard> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut cards = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(suffix) = name.strip_prefix("card") else {
            continue;
        };
        if suffix.is_empty() || !suffix.chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        let device_dir = entry.path().join("device");
        let vendor = read_hex(&device_dir.join("vendor"));
        if vendor != Some(AMD_VENDOR_ID) || !device_dir.join("pp_od_clk_voltage").exists() {
            continue;
        }
        let pci_addr = pci_address(&device_dir).unwrap_or_else(|| name.clone());
        cards.push(AmdCard {
            card_name: name,
            device_dir,
            pci_addr,
        });
    }
    cards.sort_by(|a, b| a.pci_addr.cmp(&b.pci_addr));
    cards
}

/// `AMD-<unique_id>` → current index in the enumerated card list.
pub fn resolve_gpu_index(expected_uuid: &str, index_hint: u32) -> GpuResult<u32> {
    let cards = enumerate_cards(&default_sysfs_root());
    if let Some(card) = cards.get(index_hint as usize) {
        if card_uuid(card) == expected_uuid {
            return Ok(index_hint);
        }
    }
    cards
        .iter()
        .position(|card| card_uuid(card) == expected_uuid)
        .map(|index| index as u32)
        .ok_or_else(|| {
            GpuError::other(
                format!("runtime profile GPU {expected_uuid} is not currently detected"),
                0,
            )
        })
}

fn card_uuid(card: &AmdCard) -> String {
    match read_trimmed(&card.device_dir.join("unique_id")) {
        Some(unique) if !unique.is_empty() => format!("{UUID_PREFIX}{unique}"),
        _ => format!("{UUID_PREFIX}PCI-{}", card.pci_addr),
    }
}

/// True when the LACT daemon owns this GPU's tuning: its socket exists and a
/// `lact` process is alive. Checked on every fan query so the engine never
/// steals the PMFW fan curve from LACT.
pub fn lact_daemon_active() -> bool {
    if !Path::new(LACT_SOCKET).exists() {
        return false;
    }
    let Ok(entries) = fs::read_dir("/proc") else {
        return false;
    };
    entries.flatten().any(|entry| {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.chars().all(|c| c.is_ascii_digit()) {
            return false;
        }
        fs::read_to_string(entry.path().join("comm"))
            .map(|comm| comm.trim() == "lact" || comm.trim().starts_with("lactd"))
            .unwrap_or(false)
    })
}

// ------------------------------------------------------------------------
// Pure parsers (unit-tested with text captured from a live RX 9070 XT).
// ------------------------------------------------------------------------

/// Parsed `pp_od_clk_voltage` (RDNA3/RDNA4 shapes).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OdTable {
    /// `OD_SCLK_OFFSET` (RDNA4): signed MHz.
    pub sclk_offset_mhz: Option<i64>,
    /// `OD_SCLK` levels 0/1 (RDNA3): absolute min/max MHz.
    pub sclk_min_mhz: Option<i64>,
    pub sclk_max_mhz: Option<i64>,
    /// `OD_MCLK` levels 0/1: absolute min/max MHz.
    pub mclk_min_mhz: Option<i64>,
    pub mclk_max_mhz: Option<i64>,
    /// `OD_VDDGFX_OFFSET`: signed mV.
    pub vddgfx_offset_mv: Option<i64>,
    pub range_sclk_offset_mhz: Option<(i64, i64)>,
    pub range_sclk_mhz: Option<(i64, i64)>,
    pub range_mclk_mhz: Option<(i64, i64)>,
    pub range_vddgfx_offset_mv: Option<(i64, i64)>,
}

/// Parse the leading signed integer of a token such as `-110mV`, `1375MHz`,
/// `0: 676Mhz` (after the label was removed) or `15%`.
fn leading_int(token: &str) -> Option<i64> {
    let token = token.trim();
    let mut end = 0;
    for (index, ch) in token.char_indices() {
        if ch.is_ascii_digit() || (index == 0 && (ch == '-' || ch == '+')) {
            end = index + ch.len_utf8();
        } else {
            break;
        }
    }
    token[..end].parse().ok()
}

fn range_pair(rest: &str) -> Option<(i64, i64)> {
    let mut values = rest.split_whitespace().filter_map(leading_int);
    let low = values.next()?;
    let high = values.next()?;
    Some((low, high))
}

pub fn parse_od_table(text: &str) -> OdTable {
    let mut table = OdTable::default();
    let mut section = "";
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        if line.ends_with(':') && !line.contains(' ') {
            section = match line {
                "OD_SCLK_OFFSET:" => "sclk_offset",
                "OD_SCLK:" => "sclk",
                "OD_MCLK:" => "mclk",
                "OD_VDDGFX_OFFSET:" => "vddgfx",
                "OD_RANGE:" => "range",
                _ => "other",
            };
            continue;
        }
        match section {
            "sclk_offset" => table.sclk_offset_mhz = leading_int(line),
            "vddgfx" => table.vddgfx_offset_mv = leading_int(line),
            "sclk" | "mclk" => {
                let Some((level, value)) = line.split_once(':') else {
                    continue;
                };
                let value = leading_int(value);
                match (section, level.trim()) {
                    ("sclk", "0") => table.sclk_min_mhz = value,
                    ("sclk", "1") => table.sclk_max_mhz = value,
                    ("mclk", "0") => table.mclk_min_mhz = value,
                    ("mclk", "1") => table.mclk_max_mhz = value,
                    _ => {}
                }
            }
            "range" => {
                let Some((label, rest)) = line.split_once(':') else {
                    continue;
                };
                let pair = range_pair(rest);
                match label.trim() {
                    "SCLK_OFFSET" => table.range_sclk_offset_mhz = pair,
                    "SCLK" => table.range_sclk_mhz = pair,
                    "MCLK" => table.range_mclk_mhz = pair,
                    "VDDGFX_OFFSET" => table.range_vddgfx_offset_mv = pair,
                    _ => {}
                }
            }
            _ => {}
        }
    }
    table
}

/// Parsed `gpu_od/fan_ctrl/fan_curve`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FanCurveTable {
    /// `(temp_c, speed_pct)` per point index.
    pub points: Vec<(i64, i64)>,
    pub temp_range_c: Option<(i64, i64)>,
    pub speed_range_pct: Option<(i64, i64)>,
}

pub fn parse_fan_curve(text: &str) -> FanCurveTable {
    let mut table = FanCurveTable::default();
    let mut in_range = false;
    for raw in text.lines() {
        let line = raw.trim();
        if line == "OD_FAN_CURVE:" {
            in_range = false;
            continue;
        }
        if line == "OD_RANGE:" {
            in_range = true;
            continue;
        }
        let Some((label, rest)) = line.split_once(':') else {
            continue;
        };
        if in_range {
            if label.starts_with("FAN_CURVE(hotspot temp)") {
                table.temp_range_c = range_pair(rest);
            } else if label.starts_with("FAN_CURVE(fan speed)") {
                table.speed_range_pct = range_pair(rest);
            }
            continue;
        }
        if label.trim().chars().all(|c| c.is_ascii_digit()) {
            let mut values = rest.split_whitespace().filter_map(leading_int);
            if let (Some(temp), Some(speed)) = (values.next(), values.next()) {
                table.points.push((temp, speed));
            }
        }
    }
    table
}

/// `pp_dpm_sclk` / `pp_dpm_mclk`: `N: <MHz>Mhz [*]` lines (the `S: 0Mhz *`
/// sleep line on RDNA4 is skipped). Sorted, deduplicated MHz.
pub fn parse_dpm_levels(text: &str) -> Vec<u32> {
    let mut levels: Vec<u32> = text
        .lines()
        .filter_map(|line| {
            let (level, rest) = line.trim().split_once(':')?;
            if !level.trim().chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            let value = leading_int(rest)?;
            u32::try_from(value).ok().filter(|&mhz| mhz > 0)
        })
        .collect();
    levels.sort_unstable();
    levels.dedup();
    levels
}

/// A flat 5-point PMFW curve that holds `speed_pct` across the whole
/// supported hotspot range (the closest amdgpu equivalent of NVIDIA's
/// per-fan "set speed %").
pub fn flat_fan_curve_lines(
    speed_pct: u32,
    temp_range_c: (i64, i64),
    speed_range_pct: (i64, i64),
) -> Vec<String> {
    let speed = i64::from(speed_pct).clamp(speed_range_pct.0, speed_range_pct.1);
    let (low, high) = temp_range_c;
    let span = (high - low).max(FAN_CURVE_POINTS as i64 - 1);
    (0..FAN_CURVE_POINTS)
        .map(|index| {
            let temp = low + span * index as i64 / (FAN_CURVE_POINTS as i64 - 1);
            format!("{index} {} {speed}", temp.min(high))
        })
        .collect()
}

/// Human name for a PCI device from hwdata's `pci.ids`; the subsystem entry
/// wins when present, else the device line, else the raw ids.
pub fn pci_ids_name(
    pci_ids: &str,
    vendor: u32,
    device: u32,
    subsystem: Option<(u32, u32)>,
) -> Option<String> {
    let vendor_tag = format!("{vendor:04x}  ");
    let device_tag = format!("\t{device:04x}  ");
    let subsystem_tag = subsystem.map(|(sv, sd)| format!("\t\t{sv:04x} {sd:04x}  "));
    let mut in_vendor = false;
    let mut in_device = false;
    let mut device_name = None;
    for line in pci_ids.lines() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }
        if !line.starts_with('\t') {
            if in_vendor {
                break;
            }
            in_vendor = line.starts_with(&vendor_tag);
            continue;
        }
        if !in_vendor {
            continue;
        }
        if !line.starts_with("\t\t") {
            if in_device {
                break;
            }
            if let Some(name) = line.strip_prefix(&device_tag) {
                in_device = true;
                device_name = Some(name.trim().to_string());
            }
            continue;
        }
        if in_device {
            if let Some(tag) = &subsystem_tag {
                if let Some(name) = line.strip_prefix(tag.as_str()) {
                    return Some(name.trim().to_string());
                }
            }
        }
    }
    device_name
}

// ------------------------------------------------------------------------
// sysfs helpers
// ------------------------------------------------------------------------

fn read_trimmed(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()
        .map(|text| text.trim().to_string())
}

fn read_i64(path: &Path) -> Option<i64> {
    read_trimmed(path)?.parse().ok()
}

fn read_hex(path: &Path) -> Option<u32> {
    let text = read_trimmed(path)?;
    u32::from_str_radix(text.trim_start_matches("0x"), 16).ok()
}

fn pci_address(device_dir: &Path) -> Option<String> {
    if let Some(uevent) = read_trimmed(&device_dir.join("uevent")) {
        for line in uevent.lines() {
            if let Some(addr) = line.strip_prefix("PCI_SLOT_NAME=") {
                return Some(addr.trim().to_string());
            }
        }
    }
    fs::canonicalize(device_dir)
        .ok()?
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
}

fn write_error(path: &Path, value: &str, error: io::Error) -> GpuError {
    let hint = match error.kind() {
        io::ErrorKind::PermissionDenied => " (needs root; the daemon runs as root)",
        io::ErrorKind::InvalidInput => " (the driver rejected the value: outside OD_RANGE?)",
        _ => "",
    };
    GpuError::other(
        format!(
            "write {value:?} to {} failed: {error}{hint}",
            path.display()
        ),
        0,
    )
}

fn write_sysfs(path: &Path, value: &str) -> GpuResult<()> {
    fs::write(path, value).map_err(|error| write_error(path, value, error))
}

// ------------------------------------------------------------------------
// Backend
// ------------------------------------------------------------------------

pub struct AmdgpuBackend {
    gpu_index: u32,
    gpu_count: u32,
    card: AmdCard,
    hwmon_dir: Option<PathBuf>,
    vendor_id: u32,
    device_id: u32,
    subsystem: Option<(u32, u32)>,
    /// `pp_dpm_mclk` top level at open time: the reference the engine's
    /// signed memory offset is relative to.
    stock_mclk_max_mhz: Option<i64>,
    /// Last flat fan speed written, so the per-poll fan loop only commits an
    /// OverDrive table when the target actually changed.
    last_fan_speed_pct: Cell<Option<u32>>,
}

impl AmdgpuBackend {
    pub fn open_from(cards: Vec<AmdCard>, gpu_index: u32) -> GpuResult<Self> {
        let gpu_count = cards.len() as u32;
        let Some(card) = cards.into_iter().nth(gpu_index as usize) else {
            return Err(GpuError::other(
                format!(
                    "amdgpu GPU index {gpu_index} is out of range ({gpu_count} OverDrive-capable AMD GPU(s) detected)"
                ),
                0,
            ));
        };
        let hwmon_dir = fs::read_dir(card.device_dir.join("hwmon"))
            .ok()
            .and_then(|mut entries| entries.next())
            .and_then(Result::ok)
            .map(|entry| entry.path());
        let vendor_id = read_hex(&card.device_dir.join("vendor")).unwrap_or(AMD_VENDOR_ID);
        let device_id = read_hex(&card.device_dir.join("device")).unwrap_or(0);
        let subsystem = match (
            read_hex(&card.device_dir.join("subsystem_vendor")),
            read_hex(&card.device_dir.join("subsystem_device")),
        ) {
            (Some(sv), Some(sd)) => Some((sv, sd)),
            _ => None,
        };
        let stock_mclk_max_mhz = read_trimmed(&card.device_dir.join("pp_dpm_mclk"))
            .map(|text| parse_dpm_levels(&text))
            .and_then(|levels| levels.last().copied())
            .map(i64::from);
        Ok(Self {
            gpu_index,
            gpu_count,
            card,
            hwmon_dir,
            vendor_id,
            device_id,
            subsystem,
            stock_mclk_max_mhz,
            last_fan_speed_pct: Cell::new(None),
        })
    }

    fn device_file(&self, name: &str) -> PathBuf {
        self.card.device_dir.join(name)
    }

    fn hwmon_file(&self, name: &str) -> Option<PathBuf> {
        self.hwmon_dir.as_ref().map(|dir| dir.join(name))
    }

    fn read_hwmon_i64(&self, name: &str) -> Option<i64> {
        self.hwmon_file(name).and_then(|path| read_i64(&path))
    }

    fn od_path(&self) -> PathBuf {
        self.device_file("pp_od_clk_voltage")
    }

    fn fan_curve_path(&self) -> PathBuf {
        self.device_file("gpu_od/fan_ctrl/fan_curve")
    }

    fn fan_minimum_pwm_path(&self) -> PathBuf {
        self.device_file("gpu_od/fan_ctrl/fan_minimum_pwm")
    }

    pub fn od_table(&self) -> OdTable {
        read_trimmed(&self.od_path())
            .map(|text| parse_od_table(&text))
            .unwrap_or_default()
    }

    pub fn fan_curve_table(&self) -> FanCurveTable {
        read_trimmed(&self.fan_curve_path())
            .map(|text| parse_fan_curve(&text))
            .unwrap_or_default()
    }

    fn read_power_cap_uw(&self) -> Option<i64> {
        self.read_hwmon_i64("power1_cap")
    }

    /// Commit a staged OverDrive edit (`c` on `path`) and restore the power
    /// cap if the firmware reset it as a side effect of the commit.
    fn commit_od(&self, path: &Path) -> GpuResult<()> {
        let cap_before = self.read_power_cap_uw();
        write_sysfs(path, "c")?;
        if let Some(before) = cap_before {
            if self.read_power_cap_uw() != Some(before) {
                if let Some(cap_path) = self.hwmon_file("power1_cap") {
                    let restored = write_sysfs(&cap_path, &before.to_string());
                    crate::logging::info(&format!(
                        "amdgpu: OverDrive commit reset power1_cap; restored {} W ({})",
                        before / 1_000_000,
                        match restored {
                            Ok(()) => "ok".to_string(),
                            Err(error) => error.to_string(),
                        }
                    ));
                }
            }
        }
        Ok(())
    }

    fn write_od_and_commit(&self, command: &str) -> GpuResult<()> {
        let path = self.od_path();
        write_sysfs(&path, command)?;
        self.commit_od(&path)
    }

    fn check_range(
        label: &str,
        value: i64,
        range: Option<(i64, i64)>,
        unit: &str,
    ) -> GpuResult<()> {
        match range {
            Some((low, high)) if value < low || value > high => Err(GpuError::other(
                format!("{label} {value}{unit} is outside the driver OD_RANGE {low}..{high}{unit}"),
                0,
            )),
            Some(_) => Ok(()),
            None => Err(GpuError::unavailable(&format!(
                "{label} (amdgpu OverDrive did not report a range for it)"
            ))),
        }
    }

    fn drm_node_targets(&self) -> Vec<PathBuf> {
        fs::read_dir(self.device_file("drm"))
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.file_name().to_string_lossy().to_string())
                    .filter(|name| name.starts_with("card") || name.starts_with("renderD"))
                    .map(|name| PathBuf::from("/dev/dri").join(name))
                    .collect()
            })
            .unwrap_or_default()
    }

    fn temperature_by_label(&self, wanted: &str) -> Option<f64> {
        let dir = self.hwmon_dir.as_ref()?;
        for index in 1..=8 {
            let label = read_trimmed(&dir.join(format!("temp{index}_label")))?;
            if label == wanted {
                return read_i64(&dir.join(format!("temp{index}_input")))
                    .map(|milli| milli as f64 / 1000.0);
            }
        }
        None
    }

    fn fan_speed_range_pct(&self) -> (i64, i64) {
        let curve = self.fan_curve_table();
        if let Some(range) = curve.speed_range_pct {
            return range;
        }
        let minimum = read_trimmed(&self.fan_minimum_pwm_path())
            .and_then(|text| {
                text.lines().find_map(|line| {
                    line.trim()
                        .strip_prefix("MINIMUM_PWM:")
                        .map(str::trim)
                        .and_then(range_pair)
                })
            })
            .map(|(low, _)| low)
            .unwrap_or(0);
        (minimum, 100)
    }
}

impl GpuBackend for AmdgpuBackend {
    fn vendor(&self) -> GpuVendor {
        GpuVendor::Amd
    }

    fn has_locked_clock_control(&self) -> bool {
        false
    }

    fn gpu_index(&self) -> u32 {
        self.gpu_index
    }

    fn gpu_count(&self) -> GpuResult<u32> {
        Ok(self.gpu_count)
    }

    fn identity(&self) -> GpuIdentity {
        GpuIdentity {
            index: self.gpu_index,
            name: self.gpu_name().unwrap_or_default(),
            driver_version: read_trimmed(Path::new("/sys/module/amdgpu/version"))
                .filter(|text| !text.is_empty())
                .or_else(|| {
                    read_trimmed(Path::new("/proc/sys/kernel/osrelease"))
                        .map(|release| format!("amdgpu {release}"))
                })
                .unwrap_or_default(),
            pci_bus_id: self.card.pci_addr.clone(),
            // NVML's `0x{device}{vendor}` shape, so the Vulkan device
            // selectors derived from it keep working for Q2RTX.
            pci_device_id: format!("0x{:04X}{:04X}", self.device_id, self.vendor_id),
            uuid: card_uuid(&self.card),
        }
    }

    fn gpu_name(&self) -> Option<String> {
        if let Some(name) =
            read_trimmed(&self.device_file("product_name")).filter(|n| !n.is_empty())
        {
            return Some(name);
        }
        let pci_ids = ["/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"]
            .iter()
            .find_map(|path| fs::read_to_string(path).ok());
        let name = pci_ids
            .as_deref()
            .and_then(|text| pci_ids_name(text, self.vendor_id, self.device_id, self.subsystem))
            .unwrap_or_else(|| format!("GPU {:04x}:{:04x}", self.vendor_id, self.device_id));
        Some(if name.starts_with("AMD") || name.starts_with("Radeon") {
            name
        } else {
            format!("AMD {name}")
        })
    }

    fn memory_info(&self) -> Option<GpuMemoryInfo> {
        let total = read_i64(&self.device_file("mem_info_vram_total"))?;
        let used = read_i64(&self.device_file("mem_info_vram_used")).unwrap_or(0);
        Some(GpuMemoryInfo {
            index: self.gpu_index,
            total_bytes: total.max(0) as u64,
            free_bytes: (total - used).max(0) as u64,
            used_bytes: used.max(0) as u64,
        })
    }

    fn architecture(&self) -> Option<u32> {
        None
    }

    fn temperature_c(&self) -> GpuResult<f64> {
        // Junction (hotspot) is what the PMFW fan curve is keyed on and what
        // throttles first on RDNA4; edge runs 40 C cooler under load.
        self.temperature_by_label("junction")
            .or_else(|| self.temperature_by_label("edge"))
            .or_else(|| {
                self.read_hwmon_i64("temp1_input")
                    .map(|milli| milli as f64 / 1000.0)
            })
            .ok_or_else(|| GpuError::unavailable("amdgpu hwmon temperature"))
    }

    fn fan_count(&self) -> GpuResult<u32> {
        if lact_daemon_active() {
            return Err(GpuError::unavailable(
                "amdgpu fan control (LACT is running and owns this GPU's fan curve; stop lactd or disable fan control in Tux Burner)",
            ));
        }
        if self.fan_curve_path().exists() {
            Ok(1)
        } else {
            Err(GpuError::unavailable(
                "amdgpu fan control (gpu_od/fan_ctrl/fan_curve is not exposed by this driver)",
            ))
        }
    }

    fn fan_speed_limits(&self) -> (Option<u32>, Option<u32>) {
        let (low, high) = self.fan_speed_range_pct();
        (
            u32::try_from(low.max(0)).ok(),
            u32::try_from(high.clamp(0, 100)).ok(),
        )
    }

    fn reported_fan_speeds(&self, _fan_count: u32) -> Option<Vec<u32>> {
        let pwm = self.read_hwmon_i64("pwm1")?;
        let max = self
            .read_hwmon_i64("pwm1_max")
            .filter(|&v| v > 0)
            .unwrap_or(255);
        Some(vec![((pwm.max(0) * 100 + max / 2) / max).min(100) as u32])
    }

    fn power_draw_w(&self) -> Option<f64> {
        self.read_hwmon_i64("power1_average")
            .or_else(|| self.read_hwmon_i64("power1_input"))
            .map(|uw| uw as f64 / 1_000_000.0)
    }

    fn gpu_utilization_pct(&self) -> Option<u32> {
        read_i64(&self.device_file("gpu_busy_percent")).and_then(|pct| u32::try_from(pct).ok())
    }

    fn clock_info_mhz(&self, clock_type: ClockType) -> Option<u32> {
        let name = match clock_type {
            ClockType::Graphics | ClockType::Sm => "freq1_input",
            ClockType::Memory => "freq2_input",
            ClockType::Video => return None,
        };
        self.read_hwmon_i64(name)
            .and_then(|hz| u32::try_from(hz / 1_000_000).ok())
    }

    fn throttle_reason_mask(&self) -> Option<u64> {
        None
    }

    fn gpu_context_pids(&self) -> GpuResult<GpuContextPids> {
        let targets = self.drm_node_targets();
        let mut pids = Vec::new();
        let entries = fs::read_dir("/proc")
            .map_err(|error| GpuError::other(format!("read /proc failed: {error}"), 0))?;
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Ok(pid) = name.to_string_lossy().parse::<u32>() else {
                continue;
            };
            let Ok(fds) = fs::read_dir(entry.path().join("fd")) else {
                continue;
            };
            let holds = fds
                .flatten()
                .filter_map(|fd| fs::read_link(fd.path()).ok())
                .any(|link| targets.contains(&link));
            if holds {
                pids.push(pid);
            }
        }
        pids.sort_unstable();
        pids.dedup();
        Ok(GpuContextPids {
            graphics: pids.clone(),
            compute: pids,
        })
    }

    fn query_power_limits(&self) -> PowerLimits {
        let watts = |name: &str| {
            self.read_hwmon_i64(name)
                .map(|uw| (uw + 500_000) / 1_000_000)
        };
        let cap = watts("power1_cap");
        PowerLimits {
            power_management_enabled: Some(cap.is_some()),
            power_limit_w: cap,
            enforced_power_limit_w: cap,
            power_limit_default_w: watts("power1_cap_default"),
            power_limit_min_w: watts("power1_cap_min"),
            power_limit_max_w: watts("power1_cap_max"),
        }
    }

    fn apply_power_limit_w(&self, power_limit_w: i64) -> GpuResult<i64> {
        let path = self
            .hwmon_file("power1_cap")
            .filter(|path| path.exists())
            .ok_or_else(|| GpuError::unavailable("amdgpu hwmon power1_cap"))?;
        let limits = self.query_power_limits();
        if let (Some(low), Some(high)) = (limits.power_limit_min_w, limits.power_limit_max_w) {
            if power_limit_w < low || power_limit_w > high {
                return Err(GpuError::other(
                    format!("power limit {power_limit_w} W is outside power1_cap_min..max {low}..{high} W"),
                    0,
                ));
            }
        }
        write_sysfs(&path, &(power_limit_w * 1_000_000).to_string())?;
        Ok(power_limit_w)
    }

    fn enable_persistence_mode(&self) -> GpuResult<()> {
        // amdgpu has no persistence mode: the kernel driver is always resident.
        Ok(())
    }

    fn supported_memory_clock_steps_mhz(&self) -> Vec<u32> {
        read_trimmed(&self.device_file("pp_dpm_mclk"))
            .map(|text| parse_dpm_levels(&text))
            .unwrap_or_default()
    }

    fn supported_core_clock_steps_mhz(&self) -> Vec<u32> {
        read_trimmed(&self.device_file("pp_dpm_sclk"))
            .map(|text| parse_dpm_levels(&text))
            .unwrap_or_default()
    }

    fn apply_locked_core_clock_mhz(
        &self,
        _clock_mhz: i64,
        _prefer_not_above: bool,
        _snap_to_supported: bool,
    ) -> GpuResult<SnapResult> {
        Err(GpuError::unavailable(
            "locked core clocks (RDNA3/RDNA4 OverDrive has no SCLK lock; use the SCLK offset)",
        ))
    }

    fn apply_locked_core_clock_range_mhz(
        &self,
        _min_clock_mhz: i64,
        _max_clock_mhz: i64,
        _prefer_max_not_above: bool,
        _snap_to_supported: bool,
    ) -> GpuResult<RangeSnapResult> {
        Err(GpuError::unavailable(
            "locked core clock range (RDNA3/RDNA4 OverDrive has no SCLK lock; use the SCLK offset)",
        ))
    }

    fn reset_locked_core_clocks(&self) -> GpuResult<()> {
        // Nothing to release: this backend never creates a clock lock.
        Ok(())
    }

    fn reset_locked_memory_clocks(&self) -> GpuResult<()> {
        Ok(())
    }

    fn clock_offsets(&self) -> ClockOffsets {
        let table = self.od_table();
        ClockOffsets {
            gpc_clk_vf_offset_mhz: table.sclk_offset_mhz.and_then(|v| i32::try_from(v).ok()),
            mem_clk_vf_offset_mhz: match (table.mclk_max_mhz, self.stock_mclk_max_mhz) {
                (Some(max), Some(stock)) => i32::try_from(max - stock).ok(),
                _ => None,
            },
        }
    }

    fn memory_clock_offset_range_mhz(&self) -> Option<(i32, i32)> {
        let (low, high) = self.od_table().range_mclk_mhz?;
        let stock = self.stock_mclk_max_mhz?;
        Some((
            i32::try_from(low - stock).ok()?,
            i32::try_from(high - stock).ok()?,
        ))
    }

    fn core_clock_offset_range_mhz(&self) -> Option<(i32, i32)> {
        let (low, high) = self.od_table().range_sclk_offset_mhz?;
        Some((i32::try_from(low).ok()?, i32::try_from(high).ok()?))
    }

    fn memory_clock_stock_max_mhz(&self) -> Option<i64> {
        self.stock_mclk_max_mhz
    }

    fn apply_clock_offsets(
        &self,
        gpc_clk_vf_offset_mhz: Option<i32>,
        mem_clk_vf_offset_mhz: Option<i32>,
    ) -> GpuResult<AppliedOffsets> {
        let table = self.od_table();
        let mut applied = AppliedOffsets::default();
        if let Some(gpc) = gpc_clk_vf_offset_mhz {
            if table.range_sclk_offset_mhz.is_none() {
                return Err(GpuError::unavailable(
                    "SCLK offset (this amdgpu generation exposes OD_SCLK min/max, not OD_SCLK_OFFSET)",
                ));
            }
            Self::check_range(
                "SCLK offset",
                i64::from(gpc),
                table.range_sclk_offset_mhz,
                " MHz",
            )?;
            self.write_od_and_commit(&format!("s {gpc}"))?;
            applied.gpc_clk_vf_offset_mhz = Some(gpc);
            applied.gpc_clk_vf_offset_readback_mhz = self.clock_offsets().gpc_clk_vf_offset_mhz;
        }
        if let Some(memory) = mem_clk_vf_offset_mhz {
            let stock = self
                .stock_mclk_max_mhz
                .ok_or_else(|| GpuError::unavailable("stock memory clock (pp_dpm_mclk)"))?;
            let target = stock + i64::from(memory);
            Self::check_range("MCLK max", target, table.range_mclk_mhz, " MHz")?;
            self.write_od_and_commit(&format!("m 1 {target}"))?;
            applied.mem_clk_vf_offset_mhz = Some(memory);
            applied.mem_clk_vf_offset_readback_mhz = self.clock_offsets().mem_clk_vf_offset_mhz;
        }
        Ok(applied)
    }

    fn set_fan_speed(&self, _fan_idx: u32, speed_pct: u32) -> GpuResult<()> {
        if self.last_fan_speed_pct.get() == Some(speed_pct) {
            return Ok(());
        }
        let curve = self.fan_curve_table();
        let temp_range = curve.temp_range_c.unwrap_or((25, 100));
        let speed_range = curve
            .speed_range_pct
            .unwrap_or_else(|| self.fan_speed_range_pct());
        let path = self.fan_curve_path();
        for line in flat_fan_curve_lines(speed_pct, temp_range, speed_range) {
            write_sysfs(&path, &line)?;
        }
        self.commit_od(&path)?;
        self.last_fan_speed_pct.set(Some(speed_pct));
        Ok(())
    }

    fn set_all_fans_speed(&self, _fan_count: u32, speed_pct: u32) -> GpuResult<()> {
        self.set_fan_speed(0, speed_pct)
    }

    fn set_default_fan_speed(&self, _fan_idx: u32) -> GpuResult<()> {
        let path = self.fan_curve_path();
        write_sysfs(&path, "r")?;
        self.commit_od(&path)?;
        self.last_fan_speed_pct.set(None);
        Ok(())
    }

    fn set_all_fans_default(&self, _fan_count: u32) -> GpuResult<()> {
        self.set_default_fan_speed(0)
    }

    fn read_voltage_uv(&self) -> Option<i64> {
        let uv = self.read_hwmon_i64("in0_input")? * 1000;
        voltage_uv_in_window(uv).then_some(uv)
    }

    fn voltage_offset_mv(&self) -> Option<i64> {
        self.od_table().vddgfx_offset_mv
    }

    fn voltage_offset_range_mv(&self) -> Option<(i64, i64)> {
        self.od_table().range_vddgfx_offset_mv
    }

    fn apply_voltage_offset_mv(&self, voltage_offset_mv: i64) -> GpuResult<i64> {
        let table = self.od_table();
        Self::check_range(
            "VDDGFX offset",
            voltage_offset_mv,
            table.range_vddgfx_offset_mv,
            " mV",
        )?;
        let floor = super::amd_vddgfx_floor_mv();
        if voltage_offset_mv < floor {
            return Err(GpuError::other(
                format!(
                    "VDDGFX offset {voltage_offset_mv} mV is below the Tux Burner safety floor {floor} mV (lower it deliberately via {} or TUX_BURNERD_AMD_VDDGFX_FLOOR_MV)",
                    super::AMD_VDDGFX_FLOOR_FILE
                ),
                0,
            ));
        }
        if voltage_offset_mv > 0 {
            return Err(GpuError::other(
                format!(
                    "VDDGFX offset {voltage_offset_mv} mV would overvolt; only <= 0 mV is allowed"
                ),
                0,
            ));
        }
        self.write_od_and_commit(&format!("vo {voltage_offset_mv}"))?;
        let readback = self
            .voltage_offset_mv()
            .ok_or_else(|| GpuError::other("OD_VDDGFX_OFFSET did not read back after commit", 0))?;
        if readback != voltage_offset_mv {
            return Err(GpuError::other(
                format!(
                    "VDDGFX offset read back {readback} mV after writing {voltage_offset_mv} mV"
                ),
                0,
            ));
        }
        Ok(readback)
    }

    fn vf_curve_available(&self) -> bool {
        false
    }

    fn vf_points(&self) -> Vec<VfPoint> {
        Vec::new()
    }

    fn refresh_vf_points(&self) -> GpuResult<()> {
        Ok(())
    }

    fn editable_core_vf_points(&self) -> Vec<VfPoint> {
        Vec::new()
    }

    fn find_nearest_vf_point(&self, _core_clock_mhz: i64, _voltage_uv: i64) -> Option<VfPoint> {
        None
    }

    fn vf_summary(&self) -> VfSummary {
        VfSummary::default()
    }

    fn apply_vf_offsets_khz(&self, offsets: &[(u32, i32)]) -> GpuResult<()> {
        if offsets.is_empty() {
            return Ok(());
        }
        Err(GpuError::unavailable(
            "per-point V/F offsets (RDNA3/RDNA4 has a single global VDDGFX offset; use apply_voltage_offset_mv)",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Captured from `/sys/class/drm/card0/device/pp_od_clk_voltage` on the
    /// RX 9070 XT (kernel 7.3-rc7, LACT applied +300 MHz / 1375 MHz / -110 mV).
    const OD_TEXT: &str = "OD_SCLK_OFFSET:\n300Mhz\nOD_MCLK:\n0: 676Mhz\n1: 1375MHz\nOD_RANGE:\nSCLK_OFFSET:    -500Mhz       1000Mhz\nMCLK:      97Mhz       1500Mhz\nVDDGFX_OFFSET:    -200mv          0mv\nOD_VDDGFX_OFFSET:\n-110mV\n";
    const FAN_TEXT: &str = "OD_FAN_CURVE:\n0: 25C 15%\n1: 39C 19%\n2: 56C 60%\n3: 70C 85%\n4: 80C 100%\nOD_RANGE:\nFAN_CURVE(hotspot temp): 25C 100C\nFAN_CURVE(fan speed): 15% 100%\n";
    const SCLK_TEXT: &str = "S: 0Mhz *\n1: 500Mhz \n2: 2400Mhz \n";
    const MCLK_TEXT: &str =
        "0: 96Mhz \n1: 456Mhz \n2: 772Mhz *\n3: 875Mhz \n4: 1124Mhz \n5: 1258Mhz \n";

    #[test]
    fn parses_rdna4_od_table() {
        let table = parse_od_table(OD_TEXT);
        assert_eq!(table.sclk_offset_mhz, Some(300));
        assert_eq!(table.mclk_min_mhz, Some(676));
        assert_eq!(table.mclk_max_mhz, Some(1375));
        assert_eq!(table.vddgfx_offset_mv, Some(-110));
        assert_eq!(table.range_sclk_offset_mhz, Some((-500, 1000)));
        assert_eq!(table.range_mclk_mhz, Some((97, 1500)));
        assert_eq!(table.range_vddgfx_offset_mv, Some((-200, 0)));
        assert_eq!(table.sclk_min_mhz, None);
    }

    #[test]
    fn parses_rdna3_shape_too() {
        let text = "OD_SCLK:\n0: 500Mhz\n1: 2900Mhz\nOD_MCLK:\n0: 97Mhz\n1: 1249MHz\nOD_VDDGFX_OFFSET:\n0mV\nOD_RANGE:\nSCLK:     500Mhz       5000Mhz\nMCLK:      97Mhz       1500Mhz\nVDDGFX_OFFSET:    -450mv          0mv\n";
        let table = parse_od_table(text);
        assert_eq!(table.sclk_min_mhz, Some(500));
        assert_eq!(table.sclk_max_mhz, Some(2900));
        assert_eq!(table.range_sclk_mhz, Some((500, 5000)));
        assert_eq!(table.vddgfx_offset_mv, Some(0));
        assert_eq!(table.range_vddgfx_offset_mv, Some((-450, 0)));
    }

    #[test]
    fn parses_fan_curve_and_ranges() {
        let table = parse_fan_curve(FAN_TEXT);
        assert_eq!(
            table.points,
            vec![(25, 15), (39, 19), (56, 60), (70, 85), (80, 100)]
        );
        assert_eq!(table.temp_range_c, Some((25, 100)));
        assert_eq!(table.speed_range_pct, Some((15, 100)));
    }

    #[test]
    fn parses_dpm_levels_skipping_sleep_line() {
        assert_eq!(parse_dpm_levels(SCLK_TEXT), vec![500, 2400]);
        assert_eq!(
            parse_dpm_levels(MCLK_TEXT),
            vec![96, 456, 772, 875, 1124, 1258]
        );
    }

    #[test]
    fn flat_fan_curve_has_five_monotonic_points_within_range() {
        let lines = flat_fan_curve_lines(40, (25, 100), (15, 100));
        assert_eq!(lines.len(), 5);
        assert_eq!(lines[0], "0 25 40");
        assert_eq!(lines[4], "4 100 40");
        // Clamped to the firmware minimum: a 0 % request never reaches the driver.
        let clamped = flat_fan_curve_lines(0, (25, 100), (15, 100));
        assert!(clamped.iter().all(|line| line.ends_with(" 15")));
        let temps: Vec<i64> = lines
            .iter()
            .map(|line| line.split_whitespace().nth(1).unwrap().parse().unwrap())
            .collect();
        assert!(temps.windows(2).all(|pair| pair[0] < pair[1]));
    }

    #[test]
    fn pci_ids_lookup_prefers_subsystem_then_device() {
        let ids = "# comment\n1002  Advanced Micro Devices, Inc. [AMD/ATI]\n\t7550  Navi 48 [Radeon RX 9070/9070 XT/9070 GRE]\n\t\t1da2 e490  Navi 48 XTX [Sapphire Pulse Radeon RX 9070 XT]\n\t7551  Other\n10de  NVIDIA Corporation\n\t7550  Not this one\n";
        assert_eq!(
            pci_ids_name(ids, 0x1002, 0x7550, Some((0x1da2, 0xe490))).as_deref(),
            Some("Navi 48 XTX [Sapphire Pulse Radeon RX 9070 XT]")
        );
        assert_eq!(
            pci_ids_name(ids, 0x1002, 0x7550, Some((0x1eae, 0x8811))).as_deref(),
            Some("Navi 48 [Radeon RX 9070/9070 XT/9070 GRE]")
        );
        assert_eq!(pci_ids_name(ids, 0x1002, 0x9999, None), None);
    }

    #[test]
    fn leading_int_handles_signs_and_units() {
        assert_eq!(leading_int("-110mV"), Some(-110));
        assert_eq!(leading_int("1375MHz"), Some(1375));
        assert_eq!(leading_int("  0mv"), Some(0));
        assert_eq!(leading_int("15%"), Some(15));
        assert_eq!(leading_int("Mhz"), None);
    }

    /// Fake sysfs tree exercising enumeration, identity and the read side of
    /// the backend without a GPU.
    #[test]
    fn fake_sysfs_tree_reads_like_the_live_card() {
        let root = tempfile::tempdir().unwrap();
        let device = root.path().join("card0").join("device");
        let hwmon = device.join("hwmon").join("hwmon11");
        fs::create_dir_all(&hwmon).unwrap();
        fs::create_dir_all(device.join("gpu_od").join("fan_ctrl")).unwrap();
        fs::create_dir_all(device.join("drm").join("renderD128")).unwrap();
        // A connector entry and a non-AMD card must be ignored.
        fs::create_dir_all(root.path().join("card0-DP-1").join("device")).unwrap();
        let other = root.path().join("card1").join("device");
        fs::create_dir_all(&other).unwrap();
        fs::write(other.join("vendor"), "0x10de\n").unwrap();
        fs::write(other.join("pp_od_clk_voltage"), "").unwrap();
        let w = |name: &str, text: &str| fs::write(device.join(name), text).unwrap();
        w("vendor", "0x1002\n");
        w("device", "0x7550\n");
        w("subsystem_vendor", "0x1eae\n");
        w("subsystem_device", "0x8811\n");
        w("uevent", "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n");
        w("unique_id", "0123456789abcdef\n");
        w("pp_od_clk_voltage", OD_TEXT);
        w("gpu_od/fan_ctrl/fan_curve", FAN_TEXT);
        w("pp_dpm_sclk", SCLK_TEXT);
        w("pp_dpm_mclk", MCLK_TEXT);
        w("mem_info_vram_total", "17095983104\n");
        w("mem_info_vram_used", "1000000000\n");
        w("gpu_busy_percent", "6\n");
        let h = |name: &str, text: &str| fs::write(hwmon.join(name), text).unwrap();
        h("power1_cap", "374000000\n");
        h("power1_cap_min", "212000000\n");
        h("power1_cap_max", "374000000\n");
        h("power1_cap_default", "304000000\n");
        h("power1_average", "33000000\n");
        h("temp1_label", "edge\n");
        h("temp1_input", "27000\n");
        h("temp2_label", "junction\n");
        h("temp2_input", "31000\n");
        h("in0_input", "700\n");
        h("freq1_input", "63000000\n");
        h("freq2_input", "772000000\n");
        h("pwm1", "40\n");
        h("pwm1_max", "255\n");

        let cards = enumerate_cards(root.path());
        assert_eq!(cards.len(), 1);
        assert_eq!(cards[0].card_name, "card0");
        assert_eq!(cards[0].pci_addr, "0000:03:00.0");

        let backend = AmdgpuBackend::open_from(cards, 0).unwrap();
        assert_eq!(backend.vendor(), GpuVendor::Amd);
        assert!(!backend.has_locked_clock_control());
        let identity = backend.identity();
        assert_eq!(identity.uuid, "AMD-0123456789abcdef");
        assert_eq!(identity.pci_bus_id, "0000:03:00.0");
        assert_eq!(identity.pci_device_id, "0x75501002");
        assert_eq!(backend.temperature_c().unwrap(), 31.0);
        assert_eq!(backend.power_draw_w(), Some(33.0));
        assert_eq!(backend.gpu_utilization_pct(), Some(6));
        assert_eq!(backend.clock_info_mhz(ClockType::Graphics), Some(63));
        assert_eq!(backend.clock_info_mhz(ClockType::Memory), Some(772));
        assert_eq!(backend.read_voltage_uv(), Some(700_000));
        assert_eq!(backend.reported_fan_speeds(1), Some(vec![16]));
        assert_eq!(backend.fan_speed_limits(), (Some(15), Some(100)));
        let limits = backend.query_power_limits();
        assert_eq!(limits.power_limit_w, Some(374));
        assert_eq!(limits.power_limit_min_w, Some(212));
        assert_eq!(limits.power_limit_max_w, Some(374));
        assert_eq!(limits.power_limit_default_w, Some(304));
        assert_eq!(backend.memory_clock_stock_max_mhz(), Some(1258));
        let offsets = backend.clock_offsets();
        assert_eq!(offsets.gpc_clk_vf_offset_mhz, Some(300));
        assert_eq!(offsets.mem_clk_vf_offset_mhz, Some(1375 - 1258));
        assert_eq!(
            backend.memory_clock_offset_range_mhz(),
            Some((97 - 1258, 1500 - 1258))
        );
        assert_eq!(backend.core_clock_offset_range_mhz(), Some((-500, 1000)));
        assert_eq!(backend.voltage_offset_mv(), Some(-110));
        assert_eq!(backend.voltage_offset_range_mv(), Some((-200, 0)));
        assert_eq!(backend.supported_core_clock_steps_mhz(), vec![500, 2400]);
        assert!(!backend.vf_curve_available());
        assert!(backend.apply_vf_offsets_khz(&[]).is_ok());
        assert!(backend.apply_vf_offsets_khz(&[(0, 1)]).is_err());
        assert!(backend
            .apply_locked_core_clock_mhz(2000, true, true)
            .is_err());
        assert!(backend.reset_locked_core_clocks().is_ok());

        // Range validation happens before any write: nothing lands in the
        // fake table for an out-of-range request.
        let error = backend
            .apply_voltage_offset_mv(-250)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("outside the driver OD_RANGE -200..0 mV"),
            "{error}"
        );
        // In range for the driver but below the (default -150 mV) safety floor.
        let error = backend
            .apply_voltage_offset_mv(-170)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("below the Tux Burner safety floor -150 mV"),
            "{error}"
        );
        assert_eq!(
            fs::read_to_string(device.join("pp_od_clk_voltage")).unwrap(),
            OD_TEXT
        );
        assert_eq!(
            fs::read_to_string(device.join("pp_od_clk_voltage")).unwrap(),
            OD_TEXT
        );
        let error = backend
            .apply_clock_offsets(Some(1500), None)
            .unwrap_err()
            .to_string();
        assert!(error.contains("SCLK offset 1500 MHz is outside"), "{error}");
        let error = backend.apply_power_limit_w(400).unwrap_err().to_string();
        assert!(
            error.contains("outside power1_cap_min..max 212..374 W"),
            "{error}"
        );
        assert_eq!(
            fs::read_to_string(hwmon.join("power1_cap")).unwrap(),
            "374000000\n"
        );
    }

    #[test]
    fn resolve_index_by_uuid_in_fake_tree() {
        let root = tempfile::tempdir().unwrap();
        for (card, addr, unique) in [
            ("card2", "0000:0a:00.0", "bbbb"),
            ("card0", "0000:03:00.0", "aaaa"),
        ] {
            let device = root.path().join(card).join("device");
            fs::create_dir_all(&device).unwrap();
            fs::write(device.join("vendor"), "0x1002\n").unwrap();
            fs::write(device.join("pp_od_clk_voltage"), "").unwrap();
            fs::write(device.join("uevent"), format!("PCI_SLOT_NAME={addr}\n")).unwrap();
            fs::write(device.join("unique_id"), format!("{unique}\n")).unwrap();
        }
        let cards = enumerate_cards(root.path());
        assert_eq!(cards.len(), 2);
        // Sorted by PCI address, not by card number.
        assert_eq!(card_uuid(&cards[0]), "AMD-aaaa");
        assert_eq!(card_uuid(&cards[1]), "AMD-bbbb");
        assert_eq!(
            cards.iter().position(|c| card_uuid(c) == "AMD-bbbb"),
            Some(1)
        );
    }
}
