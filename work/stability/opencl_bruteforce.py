"""Deterministic OpenCL stability workload (AMD port of ``cuda_bruteforce``).

Same algorithm, same guarantees, same exit codes as the CUDA workload so the
probe logic in ``auto_uv/probes`` is untouched:

* the integer LCG/xorshift chain from ``brute_force_kernel`` (bit-identical to
  ``cuda_bruteforce._cpu_reference``) runs twice per seed and the two copies
  are compared element for element on the device;
* the load ramps through four occupancy stages;
* a host watchdog turns a stalled GPU into an exit code.

Why OpenCL and not HIP or Vulkan compute: the CUDA original is a driver-API
program that loads PTX *source* at runtime through ``ctypes`` - no compiler
step, no per-architecture binary. OpenCL C is the same model on AMD: the
kernel source is compiled by the ICD at runtime (ROCm's ``amdocl64`` or Mesa's
``rusticl``), the host side is a plain C API reachable through ``ctypes``, and
no ROCm SDK, hipcc, SPIR-V toolchain or Python binding is required.

Exit codes: 0 stable, 1 setup or driver error, 3 instability (mismatch or GPU
fault during the stress), 4 the GPU stopped answering.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass

KERNEL_SOURCE = rb"""
__kernel void brute_force_kernel(__global uint *out_x,
                                 __global uint *out_y,
                                 uint n,
                                 uint rounds,
                                 uint seed0,
                                 uint seed1)
{
    uint stride = (uint)get_global_size(0);
    for (uint i = (uint)get_global_id(0); i < n; i += stride) {
        uint x = seed0 ^ i;
        uint y = i * 17u + seed1;
        for (uint r = 0u; r < rounds; ++r) {
            x = x * 1664525u + 1013904223u;
            x = x + y;
            y = y ^ (x >> 13);
            y = y * 22695477u + 1u;
            x = x ^ (y << 7);
            y = y ^ (x >> 17);
        }
        out_x[i] = x;
        out_y[i] = y;
    }
}

__kernel void compare_u32_kernel(__global const uint *lhs,
                                 __global const uint *rhs,
                                 uint n,
                                 __global uint *counter)
{
    uint stride = (uint)get_global_size(0);
    for (uint i = (uint)get_global_id(0); i < n; i += stride) {
        if (lhs[i] != rhs[i]) {
            atomic_inc(counter);
        }
    }
}
"""

DEFAULT_STRESS_ELEMENTS = 4 * 1024 * 1024
DEFAULT_VERIFY_ELEMENTS = 4096
DEFAULT_STRESS_ROUNDS = 8192
DEFAULT_VERIFY_ROUNDS = 256
DEFAULT_STRESS_BATCH_LAUNCHES = 8
MAX_VERIFY_INTERVAL_S = 5.0
FULL_GRID_BLOCKS = 256
BLOCK_THREADS = 256
RAMP_FRACTIONS = (0.125, 0.25, 0.5, 1.0)
RAMP_STAGE_MIN_S = 0.3
RAMP_STAGE_MAX_S = 1.0
HANG_TIMEOUT_S = 8.0

EXIT_OK = 0
EXIT_SETUP_ERROR = 1
EXIT_INSTABILITY = 3
EXIT_HANG = 4
OPENCL_INSTABILITY_EXIT_CODES = frozenset({EXIT_INSTABILITY, EXIT_HANG})

PLATFORM_ENV = "TUX_BURNER_OPENCL_PLATFORM"
AMD_VENDOR_ID = 0x1002

CL_SUCCESS = 0
CL_DEVICE_TYPE_GPU = 1 << 2
CL_PLATFORM_NAME = 0x0902
CL_DEVICE_VENDOR_ID = 0x1001
CL_DEVICE_NAME = 0x102B
CL_DRIVER_VERSION = 0x102D
CL_DEVICE_VERSION = 0x102F
CL_PROGRAM_BUILD_LOG = 0x1183
CL_MEM_READ_WRITE = 1
CL_DEVICE_TOPOLOGY_AMD = 0x4037
CL_DEVICE_PCI_BUS_INFO_KHR = 0x410F


@dataclass(frozen=True, slots=True)
class RampStage:
    fraction: float
    seconds: float

    @property
    def grid_blocks(self) -> int:
        return max(1, round(FULL_GRID_BLOCKS * float(self.fraction)))

    @property
    def elements(self) -> int:
        return max(DEFAULT_VERIFY_ELEMENTS, int(DEFAULT_STRESS_ELEMENTS * float(self.fraction)))


def ramp_schedule(duration_seconds: float) -> list[RampStage]:
    total = max(1.0, float(duration_seconds))
    partial_s = max(RAMP_STAGE_MIN_S, min(RAMP_STAGE_MAX_S, total / 10.0))
    stages: list[RampStage] = []
    remaining = total
    for fraction in RAMP_FRACTIONS[:-1]:
        seconds = min(partial_s, max(0.0, remaining - RAMP_STAGE_MIN_S))
        if seconds < RAMP_STAGE_MIN_S:
            break
        stages.append(RampStage(fraction, seconds))
        remaining -= seconds
    stages.append(RampStage(RAMP_FRACTIONS[-1], max(RAMP_STAGE_MIN_S, remaining)))
    return stages


class _Watchdog:
    def __init__(self, timeout_s: float, on_hang, clock=time.monotonic, poll_s: float = 0.25) -> None:
        self.timeout_s = float(timeout_s)
        self.on_hang = on_hang
        self.clock = clock
        self.poll_s = float(poll_s)
        self._last_beat = clock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="opencl-watchdog", daemon=True)

    def beat(self) -> None:
        self._last_beat = self.clock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            stalled_s = self.clock() - self._last_beat
            if stalled_s > self.timeout_s:
                self.on_hang(stalled_s)
                return


def _exit_on_hang(stalled_s: float) -> None:
    _log(
        f"HANG launch timeout: no progress for {stalled_s:.1f}s; "
        "the GPU stopped answering (treated as instability)"
    )
    os._exit(EXIT_HANG)


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _cpu_reference(index: int, rounds: int, seed0: int, seed1: int) -> tuple[int, int]:
    x = _u32(seed0 ^ index)
    y = _u32(index * 17 + seed1)
    for _ in range(int(rounds)):
        x = _u32(x * 1664525 + 1013904223)
        x = _u32(x + y)
        y = _u32(y ^ (x >> 13))
        y = _u32(y * 22695477 + 1)
        x = _u32(x ^ ((y << 7) & 0xFFFFFFFF))
        y = _u32(y ^ (x >> 17))
    return x, y


def _stress_seed(base_seed: int, launch_index: int, stream_seed: int) -> int:
    return _u32(int(base_seed) + int(launch_index) * 0x9E3779B9 + int(stream_seed))


def _verification_sample_indices(element_count: int) -> list[int]:
    count = int(element_count)
    candidates = [0, 1, 2, 7, 31, 255, 1023, 4095, count // 4, count // 2, (count * 3) // 4, count - 1]
    seen: set[int] = set()
    indices = []
    for index in candidates:
        if index < 0 or index >= count or index in seen:
            continue
        seen.add(index)
        indices.append(index)
    return indices


def _verify_interval_s(duration_seconds: float) -> float:
    duration_s = max(1.0, float(duration_seconds))
    return max(1.0, min(MAX_VERIFY_INTERVAL_S, duration_s / 3.0))


class OpenClDriverError(RuntimeError):
    pass


class OpenClInstabilityError(OpenClDriverError):
    """A wrong result or GPU fault while under load: the candidate is unsafe."""


def _log(message: str) -> None:
    print(f"opencl-bruteforce: {message}", flush=True)


class _OpenCl:
    """Minimal ctypes binding over the OpenCL ICD loader (libOpenCL.so.1)."""

    def __init__(self, lib=None) -> None:
        if lib is None:
            name = ctypes.util.find_library("OpenCL") or "libOpenCL.so.1"
            try:
                lib = ctypes.CDLL(name)
            except OSError as exc:
                raise OpenClDriverError(
                    f"cannot load the OpenCL ICD loader ({name}): {exc}; install ocl-icd "
                    "plus an AMD OpenCL ICD (rocm-opencl-runtime or Mesa rusticl)"
                ) from exc
        self.lib = lib
        self._bind()

    def _bind(self) -> None:
        c = ctypes
        lib = self.lib
        lib.clGetPlatformIDs.argtypes = [c.c_uint, c.POINTER(c.c_void_p), c.POINTER(c.c_uint)]
        lib.clGetPlatformIDs.restype = c.c_int
        lib.clGetPlatformInfo.argtypes = [c.c_void_p, c.c_uint, c.c_size_t, c.c_void_p, c.POINTER(c.c_size_t)]
        lib.clGetPlatformInfo.restype = c.c_int
        lib.clGetDeviceIDs.argtypes = [c.c_void_p, c.c_ulonglong, c.c_uint, c.POINTER(c.c_void_p), c.POINTER(c.c_uint)]
        lib.clGetDeviceIDs.restype = c.c_int
        lib.clGetDeviceInfo.argtypes = [c.c_void_p, c.c_uint, c.c_size_t, c.c_void_p, c.POINTER(c.c_size_t)]
        lib.clGetDeviceInfo.restype = c.c_int
        lib.clCreateContext.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_void_p), c.c_void_p, c.c_void_p, c.POINTER(c.c_int)]
        lib.clCreateContext.restype = c.c_void_p
        lib.clCreateCommandQueue.argtypes = [c.c_void_p, c.c_void_p, c.c_ulonglong, c.POINTER(c.c_int)]
        lib.clCreateCommandQueue.restype = c.c_void_p
        lib.clCreateProgramWithSource.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_char_p), c.POINTER(c.c_size_t), c.POINTER(c.c_int)]
        lib.clCreateProgramWithSource.restype = c.c_void_p
        lib.clBuildProgram.argtypes = [c.c_void_p, c.c_uint, c.POINTER(c.c_void_p), c.c_char_p, c.c_void_p, c.c_void_p]
        lib.clBuildProgram.restype = c.c_int
        lib.clGetProgramBuildInfo.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.c_size_t, c.c_void_p, c.POINTER(c.c_size_t)]
        lib.clGetProgramBuildInfo.restype = c.c_int
        lib.clCreateKernel.argtypes = [c.c_void_p, c.c_char_p, c.POINTER(c.c_int)]
        lib.clCreateKernel.restype = c.c_void_p
        lib.clCreateBuffer.argtypes = [c.c_void_p, c.c_ulonglong, c.c_size_t, c.c_void_p, c.POINTER(c.c_int)]
        lib.clCreateBuffer.restype = c.c_void_p
        lib.clSetKernelArg.argtypes = [c.c_void_p, c.c_uint, c.c_size_t, c.c_void_p]
        lib.clSetKernelArg.restype = c.c_int
        lib.clEnqueueNDRangeKernel.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.POINTER(c.c_size_t), c.POINTER(c.c_size_t), c.POINTER(c.c_size_t), c.c_uint, c.c_void_p, c.c_void_p]
        lib.clEnqueueNDRangeKernel.restype = c.c_int
        lib.clFinish.argtypes = [c.c_void_p]
        lib.clFinish.restype = c.c_int
        lib.clEnqueueReadBuffer.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.c_size_t, c.c_size_t, c.c_void_p, c.c_uint, c.c_void_p, c.c_void_p]
        lib.clEnqueueReadBuffer.restype = c.c_int
        lib.clEnqueueWriteBuffer.argtypes = [c.c_void_p, c.c_void_p, c.c_uint, c.c_size_t, c.c_size_t, c.c_void_p, c.c_uint, c.c_void_p, c.c_void_p]
        lib.clEnqueueWriteBuffer.restype = c.c_int
        for name in ("clReleaseMemObject", "clReleaseKernel", "clReleaseProgram", "clReleaseCommandQueue", "clReleaseContext"):
            getattr(lib, name).argtypes = [c.c_void_p]
            getattr(lib, name).restype = c.c_int

    def check(self, rc: int, context: str) -> None:
        if int(rc) == CL_SUCCESS:
            return
        raise OpenClDriverError(f"{context} failed rc={int(rc)}")

    # -- discovery -------------------------------------------------------
    def platforms(self) -> list[ctypes.c_void_p]:
        count = ctypes.c_uint()
        rc = self.lib.clGetPlatformIDs(0, None, ctypes.byref(count))
        if rc != CL_SUCCESS or count.value == 0:
            return []
        ids = (ctypes.c_void_p * count.value)()
        self.check(self.lib.clGetPlatformIDs(count.value, ids, None), "clGetPlatformIDs")
        return [ctypes.c_void_p(value) for value in ids]

    def platform_name(self, platform) -> str:
        buf = ctypes.create_string_buffer(256)
        rc = self.lib.clGetPlatformInfo(platform, CL_PLATFORM_NAME, len(buf), buf, None)
        return buf.value.decode(errors="replace") if rc == CL_SUCCESS else ""

    def devices(self, platform) -> list[ctypes.c_void_p]:
        count = ctypes.c_uint()
        rc = self.lib.clGetDeviceIDs(platform, CL_DEVICE_TYPE_GPU, 0, None, ctypes.byref(count))
        if rc != CL_SUCCESS or count.value == 0:
            return []
        ids = (ctypes.c_void_p * count.value)()
        self.check(self.lib.clGetDeviceIDs(platform, CL_DEVICE_TYPE_GPU, count.value, ids, None), "clGetDeviceIDs")
        return [ctypes.c_void_p(value) for value in ids]

    def device_string(self, device, param: int) -> str:
        buf = ctypes.create_string_buffer(512)
        rc = self.lib.clGetDeviceInfo(device, param, len(buf), buf, None)
        return buf.value.decode(errors="replace") if rc == CL_SUCCESS else ""

    def device_vendor_id(self, device) -> int:
        value = ctypes.c_uint()
        rc = self.lib.clGetDeviceInfo(device, CL_DEVICE_VENDOR_ID, ctypes.sizeof(value), ctypes.byref(value), None)
        return int(value.value) if rc == CL_SUCCESS else 0

    def device_pci_bus(self, device) -> int | None:
        """PCI bus number via cl_khr_pci_bus_info or the AMD topology query."""
        khr = (ctypes.c_uint * 4)()
        rc = self.lib.clGetDeviceInfo(device, CL_DEVICE_PCI_BUS_INFO_KHR, ctypes.sizeof(khr), khr, None)
        if rc == CL_SUCCESS:
            return int(khr[1])
        topology = (ctypes.c_ubyte * 24)()
        rc = self.lib.clGetDeviceInfo(device, CL_DEVICE_TOPOLOGY_AMD, ctypes.sizeof(topology), topology, None)
        if rc == CL_SUCCESS and int.from_bytes(bytes(topology[:4]), "little") == 1:
            return int(topology[21])
        return None


@dataclass(frozen=True, slots=True)
class SelectedDevice:
    platform_name: str
    device_name: str
    driver_version: str
    device_version: str
    pci_bus: int | None


def select_device(cl: _OpenCl, *, pci_bus: int | None = None, gpu_index: int = 0):
    """Pick the AMD GPU: by PCI bus when known, else the ``gpu_index``-th AMD
    device. Platforms whose name mentions AMD (ROCm) are preferred over
    rusticl; ``TUX_BURNER_OPENCL_PLATFORM=<substring>`` forces one."""
    wanted = os.environ.get(PLATFORM_ENV, "").strip().lower()
    candidates: list[tuple[int, str, object, object]] = []
    for platform in cl.platforms():
        platform_name = cl.platform_name(platform)
        lowered = platform_name.lower()
        if wanted and wanted not in lowered:
            continue
        rank = 0 if "amd" in lowered or "rocm" in lowered else 1
        for device in cl.devices(platform):
            if cl.device_vendor_id(device) != AMD_VENDOR_ID:
                continue
            candidates.append((rank, platform_name, platform, device))
    if not candidates:
        raise OpenClDriverError(
            "no AMD OpenCL GPU device found (checked the ROCm and rusticl ICDs); "
            "install rocm-opencl-runtime or enable rusticl (RUSTICL_ENABLE=radeonsi)"
        )
    candidates.sort(key=lambda item: item[0])
    chosen = None
    if pci_bus is not None:
        for item in candidates:
            if cl.device_pci_bus(item[3]) == pci_bus:
                chosen = item
                break
    if chosen is None:
        same_rank = [item for item in candidates if item[0] == candidates[0][0]]
        chosen = same_rank[min(max(0, int(gpu_index)), len(same_rank) - 1)]
    _rank, platform_name, platform, device = chosen
    info = SelectedDevice(
        platform_name=platform_name,
        device_name=cl.device_string(device, CL_DEVICE_NAME),
        driver_version=cl.device_string(device, CL_DRIVER_VERSION),
        device_version=cl.device_string(device, CL_DEVICE_VERSION),
        pci_bus=cl.device_pci_bus(device),
    )
    return platform, device, info


def describe_devices() -> list[str]:
    """Read-only listing used by ``--amd-status`` (no context is created)."""
    cl = _OpenCl()
    lines = []
    for platform in cl.platforms():
        name = cl.platform_name(platform)
        for device in cl.devices(platform):
            if cl.device_vendor_id(device) != AMD_VENDOR_ID:
                continue
            bus = cl.device_pci_bus(device)
            lines.append(
                f"{name}: {cl.device_string(device, CL_DEVICE_NAME)} "
                f"({cl.device_string(device, CL_DEVICE_VERSION)}, driver {cl.device_string(device, CL_DRIVER_VERSION)}"
                + (f", pci bus {bus:02x}" if bus is not None else "")
                + ")"
            )
    return lines


class _Session:
    def __init__(self, cl: _OpenCl, platform, device) -> None:
        self.cl = cl
        self.device = device
        err = ctypes.c_int()
        devices = (ctypes.c_void_p * 1)(device)
        self.context = cl.lib.clCreateContext(None, 1, devices, None, None, ctypes.byref(err))
        cl.check(err.value, "clCreateContext")
        self.queue = cl.lib.clCreateCommandQueue(self.context, device, 0, ctypes.byref(err))
        cl.check(err.value, "clCreateCommandQueue")
        source = ctypes.c_char_p(KERNEL_SOURCE)
        sources = (ctypes.c_char_p * 1)(source)
        lengths = (ctypes.c_size_t * 1)(len(KERNEL_SOURCE))
        self.program = cl.lib.clCreateProgramWithSource(self.context, 1, sources, lengths, ctypes.byref(err))
        cl.check(err.value, "clCreateProgramWithSource")
        rc = cl.lib.clBuildProgram(self.program, 1, devices, b"-cl-std=CL1.2", None, None)
        if rc != CL_SUCCESS:
            size = ctypes.c_size_t()
            cl.lib.clGetProgramBuildInfo(self.program, device, CL_PROGRAM_BUILD_LOG, 0, None, ctypes.byref(size))
            log = ctypes.create_string_buffer(max(1, size.value))
            cl.lib.clGetProgramBuildInfo(self.program, device, CL_PROGRAM_BUILD_LOG, len(log), log, None)
            raise OpenClDriverError(
                f"clBuildProgram failed rc={rc}: {log.value.decode(errors='replace').strip()}"
            )
        self.kernels: dict[str, ctypes.c_void_p] = {}
        for name in ("brute_force_kernel", "compare_u32_kernel"):
            kernel = cl.lib.clCreateKernel(self.program, name.encode(), ctypes.byref(err))
            cl.check(err.value, f"clCreateKernel({name})")
            self.kernels[name] = kernel
        self.buffers: dict[str, ctypes.c_void_p] = {}
        sizes = {name: DEFAULT_STRESS_ELEMENTS * 4 for name in ("int_x_a", "int_y_a", "int_x_b", "int_y_b")}
        sizes.update(verify_x=DEFAULT_VERIFY_ELEMENTS * 4, verify_y=DEFAULT_VERIFY_ELEMENTS * 4, counter=4)
        for name, size in sizes.items():
            buffer = cl.lib.clCreateBuffer(self.context, CL_MEM_READ_WRITE, size, None, ctypes.byref(err))
            cl.check(err.value, f"clCreateBuffer({name})")
            self.buffers[name] = buffer

    def release(self) -> None:
        lib = self.cl.lib
        for buffer in self.buffers.values():
            lib.clReleaseMemObject(buffer)
        for kernel in self.kernels.values():
            lib.clReleaseKernel(kernel)
        if getattr(self, "program", None):
            lib.clReleaseProgram(self.program)
        if getattr(self, "queue", None):
            lib.clReleaseCommandQueue(self.queue)
        if getattr(self, "context", None):
            lib.clReleaseContext(self.context)

    # -- helpers ---------------------------------------------------------
    def _set_args(self, kernel, args: list[tuple[object, int]]) -> None:
        for index, (value, size) in enumerate(args):
            self.cl.check(
                self.cl.lib.clSetKernelArg(kernel, index, size, ctypes.byref(value)),
                f"clSetKernelArg({index})",
            )

    def _launch(self, kernel, grid_blocks: int) -> None:
        global_size = (ctypes.c_size_t * 1)(int(grid_blocks) * BLOCK_THREADS)
        local_size = (ctypes.c_size_t * 1)(BLOCK_THREADS)
        rc = self.cl.lib.clEnqueueNDRangeKernel(self.queue, kernel, 1, None, global_size, local_size, 0, None, None)
        if rc != CL_SUCCESS:
            raise OpenClDriverError(f"kernel launch failed: clEnqueueNDRangeKernel rc={rc}")

    def finish(self, context: str) -> None:
        self.cl.check(self.cl.lib.clFinish(self.queue), f"clFinish({context})")

    def launch_brute_force(self, *, out_x: str, out_y: str, element_count: int, rounds: int, seed0: int, seed1: int, grid_blocks: int) -> None:
        args = [
            (ctypes.c_void_p(self.buffers[out_x]), ctypes.sizeof(ctypes.c_void_p)),
            (ctypes.c_void_p(self.buffers[out_y]), ctypes.sizeof(ctypes.c_void_p)),
            (ctypes.c_uint(int(element_count)), 4),
            (ctypes.c_uint(int(rounds)), 4),
            (ctypes.c_uint(int(seed0)), 4),
            (ctypes.c_uint(int(seed1)), 4),
        ]
        self._set_args(self.kernels["brute_force_kernel"], args)
        self._launch(self.kernels["brute_force_kernel"], grid_blocks)

    def read_u32(self, buffer: str, index: int) -> int:
        value = ctypes.c_uint()
        self.cl.check(
            self.cl.lib.clEnqueueReadBuffer(self.queue, self.buffers[buffer], 1, int(index) * 4, 4, ctypes.byref(value), 0, None, None),
            f"clEnqueueReadBuffer({buffer}[{index}])",
        )
        return int(value.value)

    def count_mismatches(self, pairs: list[tuple[str, str, str, int]]) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        zero = ctypes.c_uint(0)
        for label, lhs, rhs, element_count in pairs:
            self.cl.check(
                self.cl.lib.clEnqueueWriteBuffer(self.queue, self.buffers["counter"], 1, 0, 4, ctypes.byref(zero), 0, None, None),
                "clEnqueueWriteBuffer(counter)",
            )
            args = [
                (ctypes.c_void_p(self.buffers[lhs]), ctypes.sizeof(ctypes.c_void_p)),
                (ctypes.c_void_p(self.buffers[rhs]), ctypes.sizeof(ctypes.c_void_p)),
                (ctypes.c_uint(int(element_count)), 4),
                (ctypes.c_void_p(self.buffers["counter"]), ctypes.sizeof(ctypes.c_void_p)),
            ]
            self._set_args(self.kernels["compare_u32_kernel"], args)
            self._launch(self.kernels["compare_u32_kernel"], FULL_GRID_BLOCKS)
            self.finish(f"compare {label}")
            count = self.read_u32("counter", 0)
            if count:
                found.append((label, count))
        return found


def _verify_outputs(session: _Session, buffer_x: str, buffer_y: str, *, element_count: int, rounds: int, seed0: int, seed1: int, label: str) -> None:
    checked = []
    for index in _verification_sample_indices(int(element_count)):
        actual_x = session.read_u32(buffer_x, index)
        actual_y = session.read_u32(buffer_y, index)
        expected_x, expected_y = _cpu_reference(index, int(rounds), int(seed0), int(seed1))
        if actual_x != expected_x or actual_y != expected_y:
            raise OpenClDriverError(
                f"{label} verification mismatch idx={index} x={actual_x} expected_x={expected_x} "
                f"y={actual_y} expected_y={expected_y}"
            )
        checked.append(index)
    _log(f"verified {label} indices={checked}")


def compile_check(*, gpu_index: int = 0, pci_bus: int | None = None) -> SelectedDevice:
    """Build the kernels and run one 4096-element sanity launch (microseconds
    of GPU time): proves the ICD, compiler and result path without load."""
    cl = _OpenCl()
    platform, device, info = select_device(cl, pci_bus=pci_bus, gpu_index=gpu_index)
    session = _Session(cl, platform, device)
    try:
        session.launch_brute_force(
            out_x="verify_x", out_y="verify_y", element_count=DEFAULT_VERIFY_ELEMENTS,
            rounds=DEFAULT_VERIFY_ROUNDS, seed0=0x13579BDF, seed1=0x2468ACE1, grid_blocks=FULL_GRID_BLOCKS,
        )
        session.finish("sanity")
        _verify_outputs(
            session, "verify_x", "verify_y", element_count=DEFAULT_VERIFY_ELEMENTS,
            rounds=DEFAULT_VERIFY_ROUNDS, seed0=0x13579BDF, seed1=0x2468ACE1, label="sanity",
        )
    finally:
        session.release()
    return info


def run_opencl_bruteforce_test(
    *,
    gpu_index: int,
    duration_seconds: float,
    pci_bus: int | None = None,
    hang_timeout_s: float = HANG_TIMEOUT_S,
    on_hang=_exit_on_hang,
) -> None:
    cl = _OpenCl()
    platform, device, info = select_device(cl, pci_bus=pci_bus, gpu_index=gpu_index)
    _log(
        f"device platform={info.platform_name!r} name={info.device_name!r} "
        f"driver={info.driver_version!r} version={info.device_version!r}"
    )
    session = _Session(cl, platform, device)
    seed0 = 0x13579BDF
    seed1 = 0x2468ACE1
    launches = compares = verification_passes = 0
    watchdog = _Watchdog(hang_timeout_s, on_hang)
    try:
        schedule = ramp_schedule(float(duration_seconds))
        _log(
            f"starting gpu-index={int(gpu_index)} duration={float(duration_seconds):.1f}s "
            f"stress-elements={DEFAULT_STRESS_ELEMENTS} stress-rounds={DEFAULT_STRESS_ROUNDS} "
            "redundant-copies=2 ramp="
            + "/".join(f"{int(stage.fraction * 100)}%:{stage.seconds:.1f}s" for stage in schedule)
        )

        def sanity_check() -> None:
            session.launch_brute_force(
                out_x="verify_x", out_y="verify_y", element_count=DEFAULT_VERIFY_ELEMENTS,
                rounds=DEFAULT_VERIFY_ROUNDS, seed0=seed0, seed1=seed1, grid_blocks=FULL_GRID_BLOCKS,
            )
            session.finish("sanity")
            _verify_outputs(
                session, "verify_x", "verify_y", element_count=DEFAULT_VERIFY_ELEMENTS,
                rounds=DEFAULT_VERIFY_ROUNDS, seed0=seed0, seed1=seed1, label="sanity",
            )

        watchdog.start()
        sanity_check()
        verification_passes += 1
        watchdog.beat()

        start_monotonic = time.monotonic()
        verify_interval_s = _verify_interval_s(float(duration_seconds))
        next_verify_monotonic = start_monotonic + verify_interval_s
        last_seed0, last_seed1, last_elements = seed0, seed1, DEFAULT_VERIFY_ELEMENTS
        try:
            for stage in schedule:
                stage_deadline = time.monotonic() + float(stage.seconds)
                grid = stage.grid_blocks
                elements = stage.elements
                _log(f"stage occupancy={int(stage.fraction * 100)}% grid={grid} elements={elements} seconds={stage.seconds:.1f}")
                while time.monotonic() < stage_deadline:
                    for _ in range(DEFAULT_STRESS_BATCH_LAUNCHES):
                        launch_seed0 = _stress_seed(seed0, launches, 0xA5A5A5A5)
                        launch_seed1 = _stress_seed(seed1, launches, 0x5A5A5A5A)
                        for out_x, out_y in (("int_x_a", "int_y_a"), ("int_x_b", "int_y_b")):
                            session.launch_brute_force(
                                out_x=out_x, out_y=out_y, element_count=elements, rounds=DEFAULT_STRESS_ROUNDS,
                                seed0=launch_seed0, seed1=launch_seed1, grid_blocks=grid,
                            )
                        last_seed0, last_seed1, last_elements = launch_seed0, launch_seed1, elements
                        launches += 1
                    session.finish("stress")
                    watchdog.beat()
                    mismatches = session.count_mismatches(
                        [("int.x", "int_x_a", "int_x_b", elements), ("int.y", "int_y_a", "int_y_b", elements)]
                    )
                    compares += 1
                    watchdog.beat()
                    if mismatches:
                        detail = ", ".join(f"{label}={count}" for label, count in mismatches)
                        raise OpenClInstabilityError(
                            f"redundant verification mismatch {detail} stage={int(stage.fraction * 100)}% launch={launches}"
                        )
                    now = time.monotonic()
                    if now >= next_verify_monotonic:
                        _verify_outputs(
                            session, "int_x_a", "int_y_a", element_count=last_elements, rounds=DEFAULT_STRESS_ROUNDS,
                            seed0=last_seed0, seed1=last_seed1, label="stress",
                        )
                        verification_passes += 1
                        sanity_check()
                        verification_passes += 1
                        watchdog.beat()
                        next_verify_monotonic = now + verify_interval_s
            if launches > 0:
                _verify_outputs(
                    session, "int_x_a", "int_y_a", element_count=last_elements, rounds=DEFAULT_STRESS_ROUNDS,
                    seed0=last_seed0, seed1=last_seed1, label="stress",
                )
                verification_passes += 1
            sanity_check()
            verification_passes += 1
        except OpenClInstabilityError:
            raise
        except OpenClDriverError as exc:
            raise OpenClInstabilityError(f"gpu fault under load: {exc}") from exc
        finally:
            watchdog.stop()
        _log(
            f"completed launches={launches} compares={compares} verifications={verification_passes} "
            f"elapsed={time.monotonic() - start_monotonic:.1f}s"
        )
    finally:
        watchdog.stop()
        session.release()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a deterministic OpenCL brute-force stability workload on an AMD GPU.")
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--duration-seconds", type=float, default=90.0)
    parser.add_argument("--pci-bus", type=lambda text: int(text, 16), default=None, help="PCI bus number (hex) to pin the device")
    parser.add_argument("--compile-check", action="store_true", help="build the kernels and run one tiny sanity launch, then exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.compile_check:
            info = compile_check(gpu_index=int(args.gpu_index), pci_bus=args.pci_bus)
            _log(f"compile-check ok platform={info.platform_name!r} device={info.device_name!r} driver={info.driver_version!r}")
            return EXIT_OK
        run_opencl_bruteforce_test(
            gpu_index=int(args.gpu_index),
            duration_seconds=float(args.duration_seconds),
            pci_bus=args.pci_bus,
        )
    except KeyboardInterrupt:
        _log("Interrupted by user.")
        return 130
    except OpenClInstabilityError as exc:
        _log(f"UNSTABLE {exc}")
        return EXIT_INSTABILITY
    except Exception as exc:  # noqa: BLE001
        _log(f"FAILED {exc}")
        return EXIT_SETUP_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
