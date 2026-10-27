"""Track an AMD command child (ladder / stress / apply) from its output.

Local AMD port. The status header used to follow only the NVIDIA scan child,
so an AMD ladder started from the GUI read "Idle - No active scan". This pure
tracker turns the CLI child's lines into header text:

    AMD Auto-UV ladder | rung -40 mV (5/12), 60 s stress, 23 s left

plus the per-rung telemetry clouds the Clock-vs-Voltage plot draws live, and a
one-line result when the child exits (stable down to X / refused by the daemon
floor / unstable at Y / failed).

The CLI emits machine-readable `@amd {json}` events (``amd_sweep.emit_event``);
the human lines are parsed as a fallback so an older CLI still drives the header.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

_RUNGS_RE = re.compile(r"rungs \((\d+)\):\s*(.*?)\s*mV\s*$")
_RUNG_STRESSING_RE = re.compile(r"AMD Auto-UV rung ([+-]?\d+) mV: stressing for (\d+)s")
_RUNG_STABLE_RE = re.compile(r"AMD Auto-UV rung ([+-]?\d+) mV: stable")
_RUNG_UNSTABLE_RE = re.compile(r"AMD Auto-UV rung ([+-]?\d+) mV: UNSTABLE")
_RUNG_REFUSED_RE = re.compile(r"AMD Auto-UV rung ([+-]?\d+) mV: refused by the daemon's safety floor")
_STRESS_START_RE = re.compile(r"opencl-bruteforce: starting .*?duration=([\d.]+)s")
_STRESS_STAGE_RE = re.compile(r"opencl-bruteforce: stage occupancy=(\d+)%")
_APPLIED_RE = re.compile(r"Applied through tux-burnerd: (.*)")
_LOG_RE = re.compile(r"(?:ladder|stress test:.*?) log[: ]+(\S+\.log)")
_RECORD_RE = re.compile(r"run record (\S+\.json)")

KIND_LABELS = {
    "amd-ladder": "AMD Auto-UV ladder",
    "amd-stress": "AMD stress test",
    "amd-apply": "AMD tuning apply",
    "amd-restore": "AMD restore",
}


@dataclass
class RungTrace:
    """Live/finished telemetry of one rung for the plot."""

    offset_mv: int
    samples: list[tuple[float, float]] = field(default_factory=list)  # (vddgfx mV, sclk MHz)
    result: str = "running"  # running | stable | unstable | refused | error
    failure_type: str = ""
    detail: str = ""


@dataclass
class AmdActivity:
    kind: str = ""
    active: bool = False
    rungs: list[int] = field(default_factory=list)
    current_offset_mv: int | None = None
    rung_seconds: int | None = None
    rung_started: float | None = None
    stage_pct: int | None = None
    stable: list[int] = field(default_factory=list)
    unstable_mv: int | None = None
    refused_mv: int | None = None
    applied_text: str = ""
    result_text: str = ""
    summary_text: str = ""
    log_path: str = ""
    record_path: str = ""
    traces: dict[int, RungTrace] = field(default_factory=dict)
    # Incremented whenever the plot should redraw (new sample / rung result).
    plot_version: int = 0
    _buffer: str = ""

    def start(self, kind: str, *, now: float | None = None) -> None:
        self.__init__()  # type: ignore[misc]
        self.kind = str(kind)
        self.active = True
        self.rung_started = time.monotonic() if now is None else now

    def feed(self, text: str, *, now: float | None = None) -> None:
        if not self.active:
            return
        now = time.monotonic() if now is None else now
        self._buffer += str(text)
        *lines, self._buffer = self._buffer.split("\n")
        for raw in lines:
            self._line(raw.strip(), now)

    # ------------------------------------------------------------- events --

    def _event(self, payload: dict, now: float) -> None:
        event = str(payload.get("event", ""))
        if event == "ladder_start":
            self.rungs = [int(v) for v in payload.get("rungs", [])]
            if payload.get("rung_seconds"):
                self.rung_seconds = int(payload["rung_seconds"])
        elif event == "rung_start":
            offset = int(payload["offset_mv"])
            self.current_offset_mv = offset
            self.rung_seconds = int(payload.get("seconds") or self.rung_seconds or 0) or None
            self.rung_started = now
            self.stage_pct = None
            self.traces.setdefault(offset, RungTrace(offset))
            self.plot_version += 1
        elif event == "sample":
            offset = payload.get("offset_mv")
            vddgfx, sclk = payload.get("vddgfx_mv"), payload.get("sclk_mhz")
            if offset is None or vddgfx is None or sclk is None:
                return
            trace = self.traces.setdefault(int(offset), RungTrace(int(offset)))
            trace.samples.append((float(vddgfx), float(sclk)))
            self.plot_version += 1
        elif event == "rung_result":
            offset = int(payload["offset_mv"])
            trace = self.traces.setdefault(offset, RungTrace(offset))
            trace.result = str(payload.get("result") or "error")
            trace.failure_type = str(payload.get("failure_type") or "")
            trace.detail = str(payload.get("detail") or "")
            if trace.result == "stable":
                if offset not in self.stable:
                    self.stable.append(offset)
            elif trace.result == "unstable":
                self.unstable_mv = offset
            elif trace.result == "refused" and trace.failure_type == "apply-refused-floor":
                self.refused_mv = offset
            self.plot_version += 1
        elif event == "ladder_end":
            self.summary_text = str(payload.get("summary") or "")

    def _line(self, line: str, now: float) -> None:
        if line.startswith("@amd "):
            try:
                payload = json.loads(line[5:])
            except json.JSONDecodeError:
                return
            if isinstance(payload, dict):
                self._event(payload, now)
            return
        match = _LOG_RE.search(line)
        if match and not self.log_path:
            self.log_path = match.group(1)
        match = _RECORD_RE.search(line)
        if match:
            self.record_path = match.group(1)
        match = _RUNGS_RE.search(line)
        if match and not self.rungs:
            self.rungs = [int(tok) for tok in re.findall(r"[+-]?\d+", match.group(2))]
            return
        match = _RUNG_STRESSING_RE.search(line)
        if match:
            offset = int(match.group(1))
            if self.current_offset_mv != offset:
                self.current_offset_mv = offset
                self.rung_started = now
                self.stage_pct = None
                self.traces.setdefault(offset, RungTrace(offset))
            self.rung_seconds = int(match.group(2))
            return
        match = _RUNG_STABLE_RE.search(line)
        if match:
            offset = int(match.group(1))
            if offset not in self.stable:
                self.stable.append(offset)
            self.traces.setdefault(offset, RungTrace(offset)).result = "stable"
            self.plot_version += 1
            return
        match = _RUNG_UNSTABLE_RE.search(line)
        if match:
            offset = int(match.group(1))
            self.unstable_mv = offset
            self.traces.setdefault(offset, RungTrace(offset)).result = "unstable"
            self.plot_version += 1
            return
        match = _RUNG_REFUSED_RE.search(line)
        if match:
            offset = int(match.group(1))
            self.refused_mv = offset
            trace = self.traces.setdefault(offset, RungTrace(offset))
            trace.result, trace.failure_type = "refused", "apply-refused-floor"
            self.plot_version += 1
            return
        match = _STRESS_START_RE.search(line)
        if match:
            if self.kind == "amd-stress":
                self.rung_seconds = int(float(match.group(1)))
                self.rung_started = now
            return
        match = _STRESS_STAGE_RE.search(line)
        if match:
            self.stage_pct = int(match.group(1))
            return
        match = _APPLIED_RE.search(line)
        if match:
            self.applied_text = match.group(1).strip()

    # ------------------------------------------------------------ render --

    def stage_text(self) -> str:
        return KIND_LABELS.get(self.kind, "AMD command") if self.active else "Idle"

    def _progress_text(self, now: float) -> str:
        parts = []
        if self.current_offset_mv is not None:
            position = ""
            if self.rungs and self.current_offset_mv in self.rungs:
                position = f" ({self.rungs.index(self.current_offset_mv) + 1}/{len(self.rungs)})"
            parts.append(f"rung {self.current_offset_mv:+d} mV{position}")
        if self.rung_seconds:
            elapsed = max(0.0, now - (self.rung_started or now))
            remaining = max(0, int(round(self.rung_seconds - elapsed)))
            parts.append(f"{self.rung_seconds} s stress, {remaining} s left")
        if self.stage_pct is not None:
            parts.append(f"load {self.stage_pct}%")
        trace = self.traces.get(self.current_offset_mv) if self.current_offset_mv is not None else None
        if trace and trace.samples:
            vddgfx, sclk = trace.samples[-1]
            parts.append(f"now {sclk:.0f} MHz @ {vddgfx:.0f} mV")
        return ", ".join(parts)

    def candidate_text(self, *, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if self.active:
            if self.kind == "amd-apply":
                return self.applied_text or "writing the plan through tux-burnerd"
            progress = self._progress_text(now)
            if self.kind == "amd-ladder" and not progress:
                return "starting the ladder"
            if self.kind == "amd-stress" and not progress:
                return "starting the OpenCL workload"
            return progress
        return self.result_text or "No active scan"

    def finish(self, exit_code) -> str:
        self.active = False
        if self.kind == "amd-ladder":
            parts = []
            if self.stable:
                parts.append(f"stable down to {min(self.stable):+d} mV")
            elif self.refused_mv is None and self.unstable_mv is None:
                parts.append("no rung passed")
            if self.refused_mv is not None:
                parts.append(f"refused by the daemon floor at {self.refused_mv:+d} mV (not a stability failure)")
            if self.unstable_mv is not None:
                trace = self.traces.get(self.unstable_mv)
                label = f" ({trace.failure_type}{': ' + trace.detail if trace and trace.detail else ''})" if trace and trace.failure_type else ""
                parts.append(f"unstable at {self.unstable_mv:+d} mV{label}")
            if exit_code not in (0, None) and self.refused_mv is None and self.unstable_mv is None:
                parts.append(f"ladder exited with code {exit_code}")
            self.result_text = "Ladder finished: " + ", ".join(parts) if parts else f"Ladder exited with code {exit_code}"
        elif self.kind == "amd-stress":
            self.result_text = (
                "Stress test passed" if exit_code == 0 else f"Stress test exit code {exit_code} (3/4 = unstable)"
            )
        elif self.kind == "amd-apply":
            self.result_text = (
                f"Applied: {self.applied_text}" if exit_code == 0 and self.applied_text
                else ("Apply finished" if exit_code == 0 else f"Apply failed (exit code {exit_code})")
            )
        else:
            self.result_text = f"{KIND_LABELS.get(self.kind, self.kind)} finished (exit code {exit_code})"
        if self.log_path:
            self.result_text += f" - log {self.log_path}"
        return self.result_text

    # --------------------------------------------------------------- plot --

    def plot_traces(self) -> list[RungTrace]:
        """Rung traces in ladder order (unknown offsets last)."""
        order = {offset: index for index, offset in enumerate(self.rungs)}
        return sorted(self.traces.values(), key=lambda trace: order.get(trace.offset_mv, len(order)))


def traces_from_run_record(record: dict) -> tuple[list[RungTrace], list[int]]:
    """Rebuild plot traces from a saved run record."""
    traces: list[RungTrace] = []
    for rung in record.get("rungs") or []:
        try:
            offset = int(rung.get("offset_mv"))
        except (TypeError, ValueError):
            continue
        trace = RungTrace(offset, result=str(rung.get("result") or "error"), failure_type=str(rung.get("failure_type") or ""), detail=str(rung.get("detail") or ""))
        for sample in rung.get("samples") or []:
            vddgfx, sclk = sample.get("vddgfx_mv"), sample.get("sclk_mhz")
            if vddgfx is not None and sclk is not None:
                trace.samples.append((float(vddgfx), float(sclk)))
        traces.append(trace)
    settings = record.get("settings") or {}
    rungs = [trace.offset_mv for trace in traces]
    return traces, rungs if rungs else [int(settings.get("start_mv", 0) or 0)]
