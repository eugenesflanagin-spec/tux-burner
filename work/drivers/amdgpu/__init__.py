"""Low-level amdgpu (AMD Radeon) sysfs access.

Local AMD port. Read-side facts come straight from sysfs (world-readable);
every write goes through the root daemon's socket API, never from here.
"""
