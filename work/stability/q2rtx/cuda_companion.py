"""Build the optional compute companion command used beside Q2RTX.

Local AMD port: on a machine with an OverDrive-capable amdgpu card the
companion is the OpenCL brute-force port (same algorithm, same exit codes),
so the probe logic that consumes the exit code is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

from stability import cuda_bruteforce, opencl_bruteforce


def _amd_gpu_present() -> bool:
    try:
        from drivers.amdgpu.sysfs import amd_gpu_present

        return amd_gpu_present()
    except Exception:  # noqa: BLE001 - sysfs detection is best effort
        return False


def cuda_bruteforce_companion_command(
    *,
    gpu_index: int,
    duration_s: int,
) -> tuple[str, ...]:
    return (
        str(sys.executable),
        str(cuda_bruteforce_script_path()),
        "--gpu-index",
        str(int(gpu_index)),
        "--duration-seconds",
        str(max(1, int(duration_s))),
    )


def cuda_bruteforce_script_path() -> Path:
    if _amd_gpu_present():
        return Path(opencl_bruteforce.__file__).resolve()
    return Path(cuda_bruteforce.__file__).resolve()
