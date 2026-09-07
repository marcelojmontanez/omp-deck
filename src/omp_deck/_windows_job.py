from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import Structure, byref, c_size_t, wintypes

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IoCounters(Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", c_size_t),
        ("maximum_working_set_size", c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(Structure):
    _fields_ = [
        ("basic_limit_information", _BasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", c_size_t),
        ("job_memory_limit", c_size_t),
        ("peak_process_memory_used", c_size_t),
        ("peak_job_memory_used", c_size_t),
    ]


_KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
_KERNEL32.SetInformationJobObject.argtypes = (wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD)
_KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
_KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_KERNEL32.OpenProcess.restype = wintypes.HANDLE
_KERNEL32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
_KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
_KERNEL32.CloseHandle.restype = wintypes.BOOL


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _create_job() -> wintypes.HANDLE:
    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        raise _winerror()
    limits = _ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _KERNEL32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        byref(limits),
        ctypes.sizeof(limits),
    ):
        error = _winerror()
        _KERNEL32.CloseHandle(job)
        raise error
    return job


def _assign(job: wintypes.HANDLE, process_id: int) -> None:
    process = _KERNEL32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, process_id)
    if not process:
        raise _winerror()
    try:
        if not _KERNEL32.AssignProcessToJobObject(job, process):
            raise _winerror()
    finally:
        _KERNEL32.CloseHandle(process)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise SystemExit("missing RPC command")
    job = _create_job()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command)
        try:
            _assign(job, process.pid)
        except BaseException:
            process.kill()
            process.wait()
            raise
        return process.wait()
    finally:
        _KERNEL32.CloseHandle(job)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
