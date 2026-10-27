"""AMD tuning panel (local AMD port).

One tab that replaces the NVIDIA Auto-UV workflow on an amdgpu host:

* live status read straight from sysfs (OverDrive values + ranges, power cap,
  temperatures, fan curve) plus whether LACT owns the card;
* a plan editor for the four RDNA knobs with the configurable VDDGFX safety
  floor (default -150 mV; deeper only after ticking "allow below floor");
* Preview (dry run, in-process, read-only), Apply (confirmation; when LACT is
  running the dialog offers Export-to-LACT first and Override-LACT with a
  warning), Save as profile, Export to LACT, Stress test and the Auto-UV
  ladder (always previewed before it can be started).

Every GPU write or load goes through the CLI as a child process
(``tux-burner-cli --amd-...``) so the GUI and CLI share one validation and
LACT-gating implementation, and the output lands in the log view.
"""

from __future__ import annotations

import html
import threading
from dataclasses import replace
from pathlib import Path

from drivers.amdgpu.plan import (
    DEFAULT_VOLTAGE_OFFSET_FLOOR_MV,
    AmdTuningPlan,
    current_plan,
    render_dry_run,
    validate_plan,
)
from drivers.amdgpu.sysfs import (
    AmdGpuState,
    LactStatus,
    discover_amd_gpus,
    lact_status,
)

STATUS_REFRESH_MS = 2000


def group_content_margins(group_box) -> tuple[int, int, int, int]:
    """Content margins that keep a QGroupBox's children below its title.

    The app stylesheet pulls the frame up into the title (`margin-top`) and the
    default content rect starts at the frame top, so at small font sizes the
    first row paints over the title. Reserve the title's full height (font
    height + stylesheet padding) at the top, scaled with the current font.
    """
    metrics = group_box.fontMetrics()
    title_height = int(metrics.height() * 1.6) + 8
    return (12, title_height, 12, 12)

DAEMON_REFRESH_EVERY = 15  # status ticks between daemon capability queries
TIERS = ("efficiency", "balanced", "performance")


class AmdPanel:
    def __init__(
        self,
        *,
        QtCore,
        QtWidgets,
        parent,
        run_cli,
        busy,
        log,
        show_error,
        gpu_index: int = 0,
    ) -> None:
        self.QtCore = QtCore
        self.QtWidgets = QtWidgets
        self.parent = parent
        self.run_cli = run_cli
        self.busy = busy
        self.log = log
        self.show_error = show_error
        self.gpu_index = max(0, int(gpu_index))
        self.state: AmdGpuState | None = None
        self.lact: LactStatus | None = None
        self._daemon_line = "tux-burnerd: not queried yet"
        self._daemon_thread: threading.Thread | None = None
        self._daemon_result: str | None = None
        # The daemon's own floor as last reported (None = unknown/unreachable).
        self.daemon_floor_mv: int | None = None
        self.daemon_floor_source = ""
        self._daemon_floor_result: tuple[int | None, str] | None = None
        self._syncing_floor = False
        self._tick = 0
        self._ladder_previewed = False
        self._build()
        self.refresh()
        self._timer = QtCore.QTimer(parent)
        self._timer.setInterval(STATUS_REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ------------------------------------------------------------------ UI --

    def _build(self) -> None:
        W = self.QtWidgets
        self.widget = W.QWidget()
        outer = W.QVBoxLayout(self.widget)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)

        top = W.QHBoxLayout()
        top.setSpacing(12)
        outer.addLayout(top)

        # --- live status -------------------------------------------------
        status_group = W.QGroupBox("Live status (sysfs, read-only)")
        status_group.setObjectName("amdStatusGroup")
        status_form = W.QFormLayout(status_group)
        status_form.setHorizontalSpacing(16)
        status_form.setContentsMargins(*group_content_margins(status_group))
        self.status_labels: dict[str, object] = {}
        for key, text in (
            ("gpu", "GPU"),
            ("vddgfx", "VDDGFX offset"),
            ("sclk", "SCLK offset"),
            ("mclk", "MCLK max"),
            ("power", "Power cap"),
            ("temps", "Temperatures"),
            ("live", "Live"),
            ("fan", "Fan"),
            ("lact", "LACT"),
            ("floors", "Safety floors"),
            ("daemon", "Daemon"),
        ):
            label = W.QLabel("-")
            label.setWordWrap(True)
            label.setTextInteractionFlags(self.QtCore.Qt.TextSelectableByMouse)
            self.status_labels[key] = label
            status_form.addRow(text, label)
        top.addWidget(status_group, 3)

        # --- plan editor -------------------------------------------------
        plan_group = W.QGroupBox("Plan")
        plan_group.setObjectName("amdPlanGroup")
        plan_form = W.QFormLayout(plan_group)
        plan_form.setHorizontalSpacing(16)
        plan_form.setContentsMargins(*group_content_margins(plan_group))

        self.vo_spin = W.QSpinBox()
        self.vo_spin.setObjectName("amdVoltageOffsetSpin")
        # Default range -150..0 (the safety floor) BEFORE any sysfs read, so
        # the user's daily -110 is always typeable; the floor controls widen it.
        self.vo_spin.setRange(DEFAULT_VOLTAGE_OFFSET_FLOOR_MV, 0)
        self.vo_spin.setSingleStep(5)
        self.vo_spin.setSuffix(" mV")
        self.vo_spin.setKeyboardTracking(False)
        self.vo_spin.setCorrectionMode(W.QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        plan_form.addRow("VDDGFX offset", self.vo_spin)

        self.sclk_spin = W.QSpinBox()
        self.sclk_spin.setObjectName("amdSclkOffsetSpin")
        self.sclk_spin.setRange(-500, 1000)
        self.sclk_spin.setSingleStep(25)
        self.sclk_spin.setSuffix(" MHz")
        self.sclk_spin.setKeyboardTracking(False)
        self.sclk_spin.setCorrectionMode(W.QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        plan_form.addRow("SCLK offset", self.sclk_spin)

        mclk_row = W.QHBoxLayout()
        self.mclk_check = W.QCheckBox("set")
        self.mclk_check.setObjectName("amdMclkCheck")
        self.mclk_spin = W.QSpinBox()
        self.mclk_spin.setObjectName("amdMclkSpin")
        self.mclk_spin.setRange(97, 1500)
        self.mclk_spin.setSingleStep(1)
        self.mclk_spin.setSuffix(" MHz")
        self.mclk_spin.setKeyboardTracking(False)
        self.mclk_spin.setCorrectionMode(W.QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        self.mclk_spin.setEnabled(False)
        self.mclk_check.toggled.connect(self.mclk_spin.setEnabled)
        mclk_row.addWidget(self.mclk_check)
        mclk_row.addWidget(self.mclk_spin)
        plan_form.addRow("MCLK max", mclk_row)

        power_row = W.QHBoxLayout()
        self.power_check = W.QCheckBox("set")
        self.power_check.setObjectName("amdPowerCheck")
        self.power_spin = W.QSpinBox()
        self.power_spin.setObjectName("amdPowerSpin")
        self.power_spin.setRange(50, 1000)
        self.power_spin.setSuffix(" W")
        self.power_spin.setKeyboardTracking(False)
        self.power_spin.setCorrectionMode(W.QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        self.power_spin.setEnabled(False)
        self.power_check.toggled.connect(self.power_spin.setEnabled)
        power_row.addWidget(self.power_check)
        power_row.addWidget(self.power_spin)
        plan_form.addRow("Power cap", power_row)

        floor_row = W.QHBoxLayout()
        self.floor_spin = W.QSpinBox()
        self.floor_spin.setObjectName("amdVoltageFloorSpin")
        # The floor can only be LOWERED below the default: its maximum is the
        # default, and it never clamps the plan value on intermediate keystrokes.
        self.floor_spin.setRange(-500, DEFAULT_VOLTAGE_OFFSET_FLOOR_MV)
        self.floor_spin.setValue(DEFAULT_VOLTAGE_OFFSET_FLOOR_MV)
        self.floor_spin.setSuffix(" mV")
        self.floor_spin.setSingleStep(10)
        self.floor_spin.setKeyboardTracking(False)
        self.floor_spin.setCorrectionMode(W.QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        self.floor_spin.setEnabled(False)
        self.allow_below_check = W.QCheckBox(
            f"Allow going below the {DEFAULT_VOLTAGE_OFFSET_FLOOR_MV} mV safety floor"
        )
        self.allow_below_check.setObjectName("amdAllowBelowFloorCheck")
        self.allow_below_check.toggled.connect(self._allow_below_toggled)
        self.floor_spin.valueChanged.connect(self._floor_changed)
        self.floor_spin.editingFinished.connect(self._floor_editing_finished)
        floor_row.addWidget(self.floor_spin)
        floor_row.addWidget(self.allow_below_check)
        plan_form.addRow("Voltage floor", floor_row)
        self.floor_warning = W.QLabel("")
        self.floor_warning.setObjectName("amdFloorWarning")
        self.floor_warning.setWordWrap(True)
        self.floor_warning.setStyleSheet("color: #ff9f43;")
        self.floor_warning.hide()
        plan_form.addRow("", self.floor_warning)

        self.ladder_rung_spin = W.QSpinBox()
        self.ladder_rung_spin.setObjectName("amdLadderRungSpin")
        self.ladder_rung_spin.setRange(10, 600)
        self.ladder_rung_spin.setValue(60)
        self.ladder_rung_spin.setSuffix(" s / rung")
        self.stress_spin = W.QSpinBox()
        self.stress_spin.setObjectName("amdStressSpin")
        self.stress_spin.setRange(5, 3600)
        self.stress_spin.setValue(60)
        self.stress_spin.setSuffix(" s")
        durations = W.QHBoxLayout()
        durations.addWidget(W.QLabel("Stress"))
        durations.addWidget(self.stress_spin)
        durations.addWidget(W.QLabel("Ladder"))
        durations.addWidget(self.ladder_rung_spin)
        plan_form.addRow("Durations", durations)

        buttons = W.QGridLayout()
        self.preview_button = W.QPushButton("Preview writes (dry run)")
        self.preview_button.setObjectName("amdPreviewButton")
        self.apply_button = W.QPushButton("Apply...")
        self.apply_button.setObjectName("amdApplyButton")
        self.save_button = W.QPushButton("Save as profile...")
        self.save_button.setObjectName("amdSaveProfileButton")
        self.export_button = W.QPushButton("Export to LACT...")
        self.export_button.setObjectName("amdExportLactButton")
        self.stress_button = W.QPushButton("Stress test...")
        self.stress_button.setObjectName("amdStressButton")
        self.ladder_button = W.QPushButton("Auto-UV ladder: preview")
        self.ladder_button.setObjectName("amdLadderButton")
        self.reset_button = W.QPushButton("Load current values")
        self.reset_button.setObjectName("amdLoadCurrentButton")
        for row, col, button in (
            (0, 0, self.preview_button),
            (0, 1, self.apply_button),
            (1, 0, self.save_button),
            (1, 1, self.export_button),
            (2, 0, self.stress_button),
            (2, 1, self.ladder_button),
            (3, 0, self.reset_button),
        ):
            buttons.addWidget(button, row, col)
        plan_form.addRow(buttons)
        top.addWidget(plan_group, 2)

        self.preview_button.clicked.connect(self.preview)
        self.apply_button.clicked.connect(self.apply)
        self.save_button.clicked.connect(self.save_profile)
        self.export_button.clicked.connect(self.export_lact)
        self.stress_button.clicked.connect(self.stress)
        self.ladder_button.clicked.connect(self.ladder)
        self.reset_button.clicked.connect(self.load_current_values)

        history_group = W.QGroupBox("Ladder runs (history)")
        history_group.setObjectName("amdHistoryGroup")
        history_layout = W.QVBoxLayout(history_group)
        history_layout.setContentsMargins(*group_content_margins(history_group))
        self.history_table = W.QTableWidget(0, 8)
        self.history_table.setObjectName("amdHistoryTable")
        self.history_table.setHorizontalHeaderLabels(["Started", "Kind", "Floors", "Plan", "Result", "Deepest", "Rungs", "Tiers"])
        self.history_table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setMaximumHeight(170)
        self.history_table.itemSelectionChanged.connect(self._history_selected)
        history_buttons = W.QHBoxLayout()
        self.history_plot_button = W.QPushButton("Show run in the Auto-UV graph")
        self.history_plot_button.setObjectName("amdHistoryPlotButton")
        self.history_plot_button.clicked.connect(self._history_plot)
        self.history_log_button = W.QPushButton("Open log")
        self.history_log_button.setObjectName("amdHistoryLogButton")
        self.history_log_button.clicked.connect(self._history_open_log)
        history_buttons.addWidget(self.history_plot_button)
        history_buttons.addWidget(self.history_log_button)
        history_buttons.addStretch(1)
        history_layout.addWidget(self.history_table)
        history_layout.addLayout(history_buttons)
        outer.addWidget(history_group)
        self.history_records: list[dict] = []
        self.last_log_path = ""
        self.reload_history()

        self.output = W.QPlainTextEdit()
        self.output.setObjectName("amdOutput")
        self.output.setReadOnly(True)
        self.output.setPlaceholderText(
            "Preview, validation and command output appear here. Nothing is written to "
            "the GPU until you confirm Apply."
        )
        outer.addWidget(self.output, 1)

    # ------------------------------------------------------------- status --

    def _on_tick(self) -> None:
        self._tick += 1
        self.refresh(query_daemon=(self._tick % DAEMON_REFRESH_EVERY == 1))

    def _read_state(self) -> AmdGpuState | None:
        gpus = discover_amd_gpus()
        for gpu in gpus:
            if gpu.index == self.gpu_index:
                return gpu.read_state()
        return gpus[0].read_state() if gpus else None

    def refresh(self, *, query_daemon: bool = True) -> None:
        try:
            state = self._read_state()
        except Exception as exc:  # noqa: BLE001 - keep the panel alive on read errors
            self.status_labels["gpu"].setText(f"amdgpu sysfs read failed: {exc}")
            return
        if state is None:
            self.status_labels["gpu"].setText("No OverDrive-capable AMD GPU found")
            return
        first = self.state is None
        self.state = state
        try:
            self.lact = lact_status(state.lact_gpu_id)
        except Exception:  # noqa: BLE001
            self.lact = None
        self._render_status(state, self.lact)
        if first:
            self._apply_ranges(state)
            self.load_current_values()
        if query_daemon:
            self._query_daemon_async()

    def _render_status(self, state: AmdGpuState, lact: LactStatus | None) -> None:
        od = state.od
        labels = self.status_labels
        labels["gpu"].setText(f"{state.name}  ({state.pci_addr}, {state.uuid}, level {state.performance_level or 'n/a'})")
        if od.supports_sclk_offset:
            labels["sclk"].setText(f"{od.sclk_offset_mhz:+d} MHz   range {od.range_sclk_offset_mhz}")
        else:
            labels["sclk"].setText(f"min/max {od.sclk_min_mhz}/{od.sclk_max_mhz} MHz (no SCLK offset on this GPU)")
        labels["vddgfx"].setText(f"{od.vddgfx_offset_mv} mV   range {od.range_vddgfx_offset_mv} (negative = undervolt)")
        labels["mclk"].setText(
            f"{od.mclk_max_mhz} MHz (min {od.mclk_min_mhz})   range {od.range_mclk_mhz}, stock {state.stock_mclk_max_mhz} MHz"
        )
        labels["power"].setText(
            f"{state.power_cap_w} W   min {state.power_cap_min_w} / max {state.power_cap_max_w} / default {state.power_cap_default_w} W"
        )
        labels["temps"].setText(", ".join(f"{k} {v:.0f} C" for k, v in state.temps_c.items()) or "-")
        power = "-" if state.power_avg_w is None else f"{state.power_avg_w:.0f} W"
        labels["live"].setText(
            f"{state.sclk_mhz} MHz sclk, {state.mclk_mhz} MHz mclk, {state.vddgfx_mv} mV, {power}, busy {state.busy_pct}%"
        )
        curve = " ".join(f"{t}C:{p}%" for t, p in state.fan_curve.points)
        labels["fan"].setText(f"{state.fan_rpm} rpm ({state.fan_pwm_pct}%)   PMFW curve {curve or '-'}   zero-rpm {state.fan_zero_rpm}")
        if lact is None:
            labels["lact"].setText("unknown")
        elif lact.active:
            configured = ""
            if lact.gpu_config:
                configured = "; configured: " + ", ".join(
                    f"{key}={lact.gpu_config[key]}"
                    for key in ("voltage_offset", "gpu_clock_offsets", "max_memory_clock", "power_cap")
                    if key in lact.gpu_config
                )
            labels["lact"].setText(f"RUNNING - lactd owns GPU tuning and the fan curve{configured}")
            labels["lact"].setStyleSheet("color: #ff9f43;")
        else:
            labels["lact"].setText("not running")
            labels["lact"].setStyleSheet("")
        labels["daemon"].setText(self._daemon_line)
        self._render_floors()

    def _render_floors(self) -> None:
        app_floor = self.effective_floor_mv()
        if self.daemon_floor_mv is None:
            daemon = "daemon floor unknown (tux-burnerd not reachable or too old)"
        else:
            daemon = f"daemon floor {self.daemon_floor_mv} mV ({self.daemon_floor_source or 'default'})"
        text = f"app floor {app_floor} mV, {daemon}"
        label = self.status_labels["floors"]
        if self.daemon_floor_mv is not None and app_floor < self.daemon_floor_mv:
            text += "  -  OUT OF SYNC: writes below the daemon floor are refused; lower the daemon floor first"
            label.setStyleSheet("color: #ff6b6b;")
        else:
            label.setStyleSheet("")
        label.setText(text)

    def _apply_ranges(self, state: AmdGpuState) -> None:
        od = state.od
        if od.range_vddgfx_offset_mv:
            self.floor_spin.setRange(od.range_vddgfx_offset_mv[0], DEFAULT_VOLTAGE_OFFSET_FLOOR_MV)
        self._floor_changed(self.floor_spin.value())
        if od.range_sclk_offset_mhz:
            self.sclk_spin.setRange(*od.range_sclk_offset_mhz)
        self.sclk_spin.setEnabled(od.supports_sclk_offset)
        if od.range_mclk_mhz:
            self.mclk_spin.setRange(*od.range_mclk_mhz)
        if state.power_cap_min_w is not None and state.power_cap_max_w is not None:
            self.power_spin.setRange(state.power_cap_min_w, state.power_cap_max_w)

    def _query_daemon_async(self) -> None:
        if self._daemon_thread is not None and self._daemon_thread.is_alive():
            return
        index = self.gpu_index
        expected_uuid = self.state.uuid if self.state else ""

        def run() -> None:
            try:
                from drivers.nvidia.daemon_gpu import DaemonGpuClient

                caps = DaemonGpuClient(index).capabilities(refresh=True)
                self._daemon_floor_result = (caps.vddgfx_floor_mv, caps.vddgfx_floor_source)
                mismatch = "" if caps.identity.uuid == expected_uuid else "  (UUID differs from sysfs!)"
                self._daemon_result = (
                    f"tux-burnerd: vendor={caps.vendor}, VDDGFX {caps.voltage_offset_mv} mV, "
                    f"SCLK {caps.clock_offsets.gpc_mhz:+d} MHz, mem offset {caps.clock_offsets.memory_mhz:+d} MHz, "
                    f"{caps.power.current_w:.0f} W, clock lock {'yes' if caps.locked_clock_control else 'no'}{mismatch}"
                )
            except Exception as exc:  # noqa: BLE001
                self._daemon_floor_result = (None, "")
                self._daemon_result = f"tux-burnerd: not reachable ({str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__})"

        self._daemon_thread = threading.Thread(target=run, daemon=True)
        self._daemon_thread.start()
        self.QtCore.QTimer.singleShot(150, self._collect_daemon)

    def _collect_daemon(self) -> None:
        thread = self._daemon_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(150, self._collect_daemon)
            return
        if self._daemon_result:
            self._daemon_line = self._daemon_result
            self.status_labels["daemon"].setText(self._daemon_line)
        if self._daemon_floor_result is not None:
            self.daemon_floor_mv, self.daemon_floor_source = self._daemon_floor_result
            self._render_floors()

    # ------------------------------------------------------------- floor ---

    def effective_floor_mv(self) -> int:
        if self.allow_below_check.isChecked():
            return min(0, int(self.floor_spin.value()))
        return DEFAULT_VOLTAGE_OFFSET_FLOOR_MV

    def _allow_below_toggled(self, checked: bool) -> None:
        self.floor_spin.setEnabled(bool(checked))
        if not checked:
            self.floor_spin.setValue(DEFAULT_VOLTAGE_OFFSET_FLOOR_MV)
            self._floor_changed(self.floor_spin.value())
            # Unticking restores the daemon default too (with confirmation).
            if self.daemon_floor_mv is not None and self.daemon_floor_mv < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
                self._sync_daemon_floor(None)
            return
        self._floor_changed(self.floor_spin.value())

    def _floor_editing_finished(self) -> None:
        """The user finished typing/spinning a floor: offer to sync the daemon."""
        if not self.allow_below_check.isChecked():
            return
        floor = self.effective_floor_mv()
        if floor < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV and self.daemon_floor_mv is not None and floor != self.daemon_floor_mv:
            self._sync_daemon_floor(floor)

    def floors_in_sync(self) -> bool:
        return self.daemon_floor_mv is not None and self.effective_floor_mv() >= self.daemon_floor_mv

    def _sync_daemon_floor(self, floor: int | None) -> bool:
        """Write (or reset) the daemon's floor file through the tux-burnerd
        RPC after an explicit confirmation. Returns True when it is in sync."""
        if self._syncing_floor or self.state is None:
            return False
        if floor is not None:
            rng = self.state.od.range_vddgfx_offset_mv
            if floor > 0 or (rng and floor < rng[0]):
                self.show_error("Safety floor", f"The floor must be between {rng[0] if rng else -500} and 0 mV.")
                return False
        if floor is None:
            question = (
                f"Restore tux-burnerd's default safety floor ({DEFAULT_VOLTAGE_OFFSET_FLOOR_MV} mV)?\n\n"
                "This removes /etc/tux-burner/amd-vddgfx-floor-mv through the daemon; writes below "
                f"{DEFAULT_VOLTAGE_OFFSET_FLOOR_MV} mV will be refused again."
            )
        else:
            question = (
                f"Lower tux-burnerd's safety floor to {floor} mV?\n\n"
                "The daemon writes /etc/tux-burner/amd-vddgfx-floor-mv (root) and will from now on accept "
                f"VDDGFX offsets down to {floor} mV from any Tux Burner client. Deeper undervolts can hang the "
                "GPU or corrupt results. Untick the floor box to restore the default."
            )
        if not self._confirm("Daemon safety floor", question):
            return False
        self._syncing_floor = True
        try:
            from drivers.nvidia.daemon_gpu import DaemonGpuClient

            result = DaemonGpuClient(self.gpu_index).set_vddgfx_floor_mv(floor)
        except Exception as exc:  # noqa: BLE001
            self.show_error(
                "Daemon safety floor",
                f"tux-burnerd did not accept the floor change: {exc}\n\n"
                "If the daemon predates this feature, reinstall tux-burner-amd and restart the service.",
            )
            return False
        finally:
            self._syncing_floor = False
        self.daemon_floor_mv = result.get("floor_mv")
        self.daemon_floor_source = str(result.get("source") or "")
        message = f"tux-burnerd safety floor is now {self.daemon_floor_mv} mV ({self.daemon_floor_source}; {result.get('file')})"
        self.output.appendPlainText(message)
        self.log("\n" + message + "\n")
        self._render_floors()
        return self.floors_in_sync()

    def _require_floors_in_sync(self, action: str) -> bool:
        if self.effective_floor_mv() >= DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
            return True
        if self.daemon_floor_mv is None:
            self.show_error(
                "Safety floor",
                f"{action} refused: the app floor is {self.effective_floor_mv()} mV but tux-burnerd did not report "
                "its own floor (not reachable or older than this feature). Reinstall/restart the daemon.",
            )
            return False
        if self.effective_floor_mv() < self.daemon_floor_mv:
            if self._sync_daemon_floor(self.effective_floor_mv()):
                return True
            self.show_error(
                "Safety floor",
                f"{action} refused: the app floor is {self.effective_floor_mv()} mV but the daemon's floor is "
                f"{self.daemon_floor_mv} mV; every write below {self.daemon_floor_mv} mV would be refused and look like "
                "a failure. Lower the daemon floor first (confirm the dialog) or raise the app floor.",
            )
            return False
        return True

    def _floor_changed(self, _value: int) -> None:
        self._render_floors()
        floor = self.effective_floor_mv()
        driver_min = -500
        if self.state and self.state.od.range_vddgfx_offset_mv:
            driver_min = self.state.od.range_vddgfx_offset_mv[0]
        self.vo_spin.setRange(max(driver_min, floor), 0)
        if floor < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
            self.floor_warning.setText(
                f"WARNING: the safety floor is lowered to {floor} mV. Deeper undervolts can hang the GPU "
                "or corrupt results; the daemon enforces its own floor "
                "(/etc/tux-burner/amd-vddgfx-floor-mv, default -150 mV) and will refuse deeper writes "
                "until that file is lowered too."
            )
            self.floor_warning.show()
        else:
            self.floor_warning.hide()

    def _floor_args(self) -> list[str]:
        floor = self.effective_floor_mv()
        if floor < DEFAULT_VOLTAGE_OFFSET_FLOOR_MV:
            return ["--amd-voltage-floor", str(floor), "--allow-below-floor"]
        return []

    # -------------------------------------------------------------- plan ---

    def load_current_values(self) -> None:
        if self.state is None:
            return
        plan = current_plan(self.state)
        if plan.voltage_offset_mv < self.vo_spin.minimum():
            # The card runs deeper than the floor (LACT applied it): show it
            # but keep the floor; the user must widen the floor explicitly.
            self.output.setPlainText(
                f"The card currently runs VDDGFX {plan.voltage_offset_mv} mV, below the "
                f"{self.effective_floor_mv()} mV safety floor; the editor is clamped to the floor."
            )
        self.vo_spin.setValue(max(self.vo_spin.minimum(), plan.voltage_offset_mv))
        self.sclk_spin.setValue(plan.sclk_offset_mhz)
        if plan.mclk_max_mhz is not None:
            self.mclk_spin.setValue(plan.mclk_max_mhz)
        if plan.power_cap_w is not None:
            self.power_spin.setValue(plan.power_cap_w)
        self.mclk_check.setChecked(False)
        self.power_check.setChecked(False)

    def plan(self) -> AmdTuningPlan:
        return AmdTuningPlan(
            voltage_offset_mv=int(self.vo_spin.value()),
            sclk_offset_mhz=int(self.sclk_spin.value()) if self.sclk_spin.isEnabled() else 0,
            mclk_max_mhz=int(self.mclk_spin.value()) if self.mclk_check.isChecked() else None,
            power_cap_w=int(self.power_spin.value()) if self.power_check.isChecked() else None,
        )

    def plan_args(self) -> list[str]:
        plan = self.plan()
        args = [f"vo={plan.voltage_offset_mv}", f"sclk={plan.sclk_offset_mhz}"]
        if plan.mclk_max_mhz is not None:
            args.append(f"mclk={plan.mclk_max_mhz}")
        if plan.power_cap_w is not None:
            args.append(f"power={plan.power_cap_w}")
        return args

    def _validated_plan(self) -> AmdTuningPlan | None:
        if self.state is None:
            self.show_error("AMD tuning", "No AMD GPU state is available.")
            return None
        plan = self.plan()
        problems = validate_plan(plan, self.state, voltage_floor_mv=self.effective_floor_mv())
        if problems:
            self.output.setPlainText("Plan rejected:\n- " + "\n- ".join(problems))
            return None
        return plan

    def preview(self) -> None:
        self.refresh(query_daemon=False)
        plan = self._validated_plan()
        if plan is None or self.state is None:
            return
        self.output.setPlainText(render_dry_run(plan, self.state))
        self.log("\n" + render_dry_run(plan, self.state) + "\n")

    # ------------------------------------------------------------- actions --

    def _confirm(self, title: str, text: str) -> bool:
        box = self.QtWidgets.QMessageBox(self.parent)
        box.setIcon(self.QtWidgets.QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(self.QtWidgets.QMessageBox.Yes | self.QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(self.QtWidgets.QMessageBox.Cancel)
        return box.exec() == self.QtWidgets.QMessageBox.Yes

    def _lact_choice(self, action: str) -> str:
        """'export' (preferred), 'override' or '' (cancel) while LACT runs."""
        box = self.QtWidgets.QMessageBox(self.parent)
        box.setIcon(self.QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("LACT owns this GPU")
        configured = ""
        if self.lact and self.lact.gpu_config:
            configured = "\n\nLACT currently configures: " + ", ".join(
                f"{key}={self.lact.gpu_config[key]}"
                for key in ("voltage_offset", "gpu_clock_offsets", "max_memory_clock", "power_cap")
                if key in self.lact.gpu_config
            )
        box.setText(
            f"lactd is running and owns GPU tuning and the fan curve on this machine.{configured}\n\n"
            f"Preferred: export the plan to a LACT config fragment and let LACT apply it at the next boot.\n"
            f"Override: {action} anyway through tux-burnerd. LACT re-applies its own settings at its "
            "next apply (boot, profile switch, LACT GUI change). Never restart lactd on a live desktop."
        )
        export_button = box.addButton("Export to LACT...", self.QtWidgets.QMessageBox.AcceptRole)
        override_button = box.addButton("Override LACT", self.QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(self.QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(export_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked == export_button:
            return "export"
        if clicked == override_button:
            return "override"
        return ""

    def _start(self, kind: str, args: list[str]) -> None:
        if self.busy():
            self.show_error("AMD tuning", "Another Tux Burner workflow is still running.")
            return
        command = ["--gpu-index", str(self.gpu_index), *args]
        if kind in {"amd-ladder", "amd-stress"}:
            from datetime import datetime

            from common.tux_burner_paths import default_user_config_dir

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            short = kind.split("-", 1)[1]
            log_path = default_user_config_dir() / "debug-logs" / f"tux_burner-amd-{short}-{stamp}.log"
            self.last_log_path = str(log_path)
            command += ["--amd-run-log", str(log_path)]
        self.output.appendPlainText("$ tux-burner-cli " + " ".join(command))
        if not self.run_cli(kind, command):
            self.show_error("AMD tuning", "Could not start the Tux Burner CLI child process.")

    def apply(self) -> None:
        plan = self._validated_plan()
        if plan is None or self.state is None:
            return
        preview = render_dry_run(plan, self.state)
        self.output.setPlainText(preview)
        override = []
        if self.lact is not None and self.lact.active:
            choice = self._lact_choice("apply the plan")
            if choice == "export":
                self.export_lact()
                return
            if choice != "override":
                return
            override = ["--override-lact"]
        if not self._require_floors_in_sync("Apply"):
            return
        if not self._confirm(
            "Apply AMD tuning",
            f"Apply {plan.describe()} to {self.state.name} through tux-burnerd now?\n\n"
            "The exact sysfs writes are shown in the panel output. The GPU can hang if the "
            "undervolt is too deep; reboot restores the previous (LACT) settings.",
        ):
            return
        self._start("amd-apply", ["--amd-plan", *self.plan_args(), "--amd-apply", *override, *self._floor_args()])

    def save_profile(self) -> None:
        plan = self._validated_plan()
        if plan is None or self.state is None:
            return
        name, ok = self.QtWidgets.QInputDialog.getText(self.parent, "Save AMD profile", "Profile name:")
        if not ok or not str(name).strip():
            return
        tier, ok = self.QtWidgets.QInputDialog.getItem(
            self.parent, "Save AMD profile", "Tier:", ["none", *TIERS], 2, False
        )
        if not ok:
            return
        from common.atomic_write import atomic_write_json
        from drivers.amdgpu.plan import plan_profile_payload
        from profiles.uv.profile_store import auto_uv_profiles_dir
        from profiles.uv.profile_tiers import save_profile_tier_assignment

        tier_key = "" if tier == "none" else str(tier)
        payload = plan_profile_payload(plan, self.state, name=str(name), tier=tier_key)
        try:
            path = atomic_write_json(
                auto_uv_profiles_dir() / f"auto-uv-profile-{payload['profile_id']}.json", payload
            )
            if tier_key:
                save_profile_tier_assignment(payload["profile_id"], tier_key, gpu_uuid=self.state.uuid)
        except Exception as exc:  # noqa: BLE001
            self.show_error("Save AMD profile", f"Could not save the profile: {exc}")
            return
        message = (
            f"Saved {payload['profile_id']} -> {path}\n"
            "It is UNVERIFIED (like a user-edited profile): verify it from Profiles > Verify "
            "(Q2RTX + OpenCL load) before applying it or using it in adaptive mode."
        )
        self.output.setPlainText(message)
        self.log("\n" + message + "\n")
        self.on_profiles_changed()

    def on_profiles_changed(self) -> None:  # replaced by the window
        return

    def show_run(self, record: dict) -> None:  # replaced by the window
        return

    # ----------------------------------------------------------- history --

    def reload_history(self) -> None:
        from auto_uv.amd_sweep import load_run_records, record_summary_row

        try:
            self.history_records = load_run_records()
        except Exception:  # noqa: BLE001
            self.history_records = []
        table = self.history_table
        table.setRowCount(0)
        for record in self.history_records:
            row = record_summary_row(record)
            index = table.rowCount()
            table.insertRow(index)
            for column, key in enumerate(("started", "kind", "floor", "plan", "result", "deepest", "rungs", "tiers")):
                item = self.QtWidgets.QTableWidgetItem(row[key])
                item.setToolTip(row[key])
                table.setItem(index, column, item)
        table.resizeColumnsToContents()

    def selected_history_record(self) -> dict | None:
        rows = {index.row() for index in self.history_table.selectedIndexes()}
        if not rows:
            return None
        row = min(rows)
        return self.history_records[row] if 0 <= row < len(self.history_records) else None

    def _history_selected(self) -> None:
        record = self.selected_history_record()
        if record is None:
            return
        lines = [f"Run {record.get('started_at', '')} -> {record.get('finished_at', '')} ({record.get('kind')})"]
        if record.get("reconstructed"):
            lines.append("RECONSTRUCTED after the fact: " + str(record.get("notes") or ""))
        lines.append("Summary: " + str(record.get("summary") or ""))
        settings = record.get("settings") or {}
        lines.append(
            f"Floors: app {settings.get('app_floor_mv')} mV, daemon {settings.get('daemon_floor_mv')} mV; "
            f"live SCLK {settings.get('live_sclk_offset_mhz')} MHz, MCLK {settings.get('live_mclk_max_mhz')} MHz, "
            f"cap {settings.get('live_power_cap_w')} W; override_lact={settings.get('override_lact')}"
        )
        for rung in record.get("rungs") or []:
            samples = rung.get("samples") or []
            avg = ""
            if samples:
                clocks = [s.get("sclk_mhz") for s in samples if s.get("sclk_mhz") is not None]
                volts = [s.get("vddgfx_mv") for s in samples if s.get("vddgfx_mv") is not None]
                if clocks and volts:
                    avg = f"  avg {sum(clocks) / len(clocks):.0f} MHz @ {sum(volts) / len(volts):.0f} mV ({len(samples)} samples)"
            detail = f" - {rung.get('detail')}" if rung.get("detail") else ""
            lines.append(f"  {int(rung.get('offset_mv', 0)):+d} mV: {rung.get('result')} {rung.get('failure_type') or ''}{detail}{avg}")
        if record.get("saved_tiers"):
            lines.append("Saved tiers: " + ", ".join(f"{k}={v}" for k, v in record["saved_tiers"].items()))
        if record.get("log_path"):
            lines.append("Log: " + str(record["log_path"]))
        self.output.setPlainText("\n".join(lines))

    def _history_plot(self) -> None:
        record = self.selected_history_record()
        if record is None:
            self.show_error("Ladder runs", "Select a run first.")
            return
        self.show_run(record)

    def _history_open_log(self) -> None:
        record = self.selected_history_record()
        path = str((record or {}).get("log_path") or self.last_log_path or "")
        if not path or not Path(path).is_file():
            self.show_error("Ladder runs", "This run has no log file on disk.")
            return
        try:
            self.output.setPlainText(Path(path).read_text(encoding="utf-8", errors="replace")[-20000:])
        except OSError as exc:
            self.show_error("Ladder runs", f"Could not read {path}: {exc}")

    def export_lact(self) -> None:
        plan = self._validated_plan()
        if plan is None or self.state is None:
            return
        from integrations.lact.export import build_lact_amd_config

        fan_plan = replace(plan, fan_curve=self.state.fan_curve.points or None)
        try:
            text, warnings = build_lact_amd_config(
                gpu_id=self.state.lact_gpu_id, plan=fan_plan, source_label="Tux Burner AMD panel"
            )
        except Exception as exc:  # noqa: BLE001
            self.show_error("Export to LACT", str(exc))
            return
        path, _filter = self.QtWidgets.QFileDialog.getSaveFileName(
            self.parent,
            "Export plan as LACT config fragment",
            str(Path.home() / "tux-burner-lact-amd.yaml"),
            "YAML (*.yaml *.yml)",
        )
        if not path:
            self.output.setPlainText(text)
            return
        try:
            Path(path).expanduser().write_text(text, encoding="utf-8")
        except OSError as exc:
            self.show_error("Export to LACT", f"Could not write {path}: {exc}")
            return
        message = (
            f"LACT fragment written to {path}\n\nMerge the gpus: entry into /etc/lact/config.yaml by hand and let "
            "LACT apply it at the next boot (do NOT restart lactd on a live desktop)."
        )
        if warnings:
            message += "\n\nWarnings:\n- " + "\n- ".join(warnings)
        self.output.setPlainText(text + "\n" + message)
        self.log("\n" + message + "\n")

    def stress(self) -> None:
        if self.state is None:
            return
        seconds = int(self.stress_spin.value())
        if not self._confirm(
            "Run the OpenCL stress test",
            f"Run the OpenCL brute-force workload on {self.state.name} for {seconds} s?\n\n"
            "This is sustained full GPU compute load (no tuning is written). A marginal undervolt "
            "shows up as a result mismatch, a GPU fault or a hang.",
        ):
            return
        self._start("amd-stress", ["--amd-stress", str(seconds)])

    def ladder(self) -> None:
        if self.state is None:
            return
        from auto_uv import amd_sweep

        settings = amd_sweep.LadderSettings(
            rung_seconds=int(self.ladder_rung_spin.value()),
            sclk_offset_mhz=int(self.sclk_spin.value()) if self.sclk_spin.isEnabled() else 0,
            mclk_max_mhz=int(self.mclk_spin.value()) if self.mclk_check.isChecked() else None,
        )
        import os

        from drivers.amdgpu.plan import VOLTAGE_FLOOR_ENV

        previous = os.environ.get(VOLTAGE_FLOOR_ENV)
        os.environ[VOLTAGE_FLOOR_ENV] = str(self.effective_floor_mv())
        try:
            offsets = amd_sweep.plan_ladder(self.state, settings, unsafe_from_marker=amd_sweep.read_marker())
            preview = amd_sweep.describe_ladder(self.state, settings, offsets)
            tiers = amd_sweep.tier_plans(offsets[-1] if offsets else 0, self.state, settings)
        finally:
            if previous is None:
                os.environ.pop(VOLTAGE_FLOOR_ENV, None)
            else:
                os.environ[VOLTAGE_FLOOR_ENV] = previous
        preview += "\n" + "\n".join(f"  example tier if every rung passes - {tier}: {plan.describe()}" for tier, plan in tiers.items())
        if not self._ladder_previewed:
            self.output.setPlainText(preview + "\n\n(preview only - click the button again to run it)")
            self.ladder_button.setText("Auto-UV ladder: RUN...")
            self._ladder_previewed = True
            return
        self._ladder_previewed = False
        self.ladder_button.setText("Auto-UV ladder: preview")
        if not self._require_floors_in_sync("The ladder"):
            return
        override = []
        if self.lact is not None and self.lact.active:
            choice = self._lact_choice("run the ladder")
            if choice != "override":
                if choice == "export":
                    self.export_lact()
                return
            override = ["--override-lact"]
        span = f"{offsets[0]:+d} -> {offsets[-1]:+d} mV" if offsets else "no rungs"
        if not self._confirm(
            "Run the AMD Auto-UV ladder",
            f"Planned range: {span} ({len(offsets)} rungs of {settings.rung_seconds} s, stops at the first failing rung).\n\n"
            + preview + "\n\nEvery rung applies a VDDGFX offset through tux-burnerd and then runs the OpenCL "
            "stress workload. Start it now?",
        ):
            return
        args = ["--amd-auto-uv", "--amd-auto-uv-rung-seconds", str(settings.rung_seconds), *override, *self._floor_args()]
        plan_args = [f"sclk={settings.sclk_offset_mhz}"]
        if settings.mclk_max_mhz is not None:
            plan_args.append(f"mclk={settings.mclk_max_mhz}")
        self._start("amd-ladder", ["--amd-plan", *plan_args, *args])

    def command_finished(self, kind: str, exit_code) -> None:
        self.output.appendPlainText(f"[{kind} finished: exit code {exit_code}]")
        self.refresh()

    def set_enabled(self, enabled: bool) -> None:
        for button in (
            self.apply_button,
            self.stress_button,
            self.ladder_button,
            self.save_button,
        ):
            button.setEnabled(bool(enabled))

    @staticmethod
    def html_escape(text: str) -> str:
        return html.escape(text)
