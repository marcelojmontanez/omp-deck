from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import Structure, byref, c_size_t, wintypes
from dataclasses import dataclass

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CREATE_SUSPENDED = 0x00000004
_STARTF_USESTDHANDLES = 0x00000100
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_INFINITE = 0xFFFFFFFF
_WAIT_FAILED = 0xFFFFFFFF
_STILL_ACTIVE = 259


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


class _StartupInfo(Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved2_size", wintypes.WORD),
        ("reserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _ProcessInformation(Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


@dataclass(frozen=True)
class _SuspendedProcess:
    process: wintypes.HANDLE
    thread: wintypes.HANDLE
    process_id: int


_KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
_KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
_KERNEL32.SetInformationJobObject.argtypes = (wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD)
_KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
_KERNEL32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
_KERNEL32.GetStdHandle.argtypes = (wintypes.DWORD,)
_KERNEL32.GetStdHandle.restype = wintypes.HANDLE
_KERNEL32.CreateProcessW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(_StartupInfo),
    ctypes.POINTER(_ProcessInformation),
)
_KERNEL32.CreateProcessW.restype = wintypes.BOOL
_KERNEL32.ResumeThread.argtypes = (wintypes.HANDLE,)
_KERNEL32.ResumeThread.restype = wintypes.DWORD
_KERNEL32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_KERNEL32.TerminateProcess.restype = wintypes.BOOL
_KERNEL32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
_KERNEL32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
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


def _create_suspended(command: list[str]) -> _SuspendedProcess:
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    standard_handles = (
        _KERNEL32.GetStdHandle(_STD_INPUT_HANDLE),
        _KERNEL32.GetStdHandle(_STD_OUTPUT_HANDLE),
        _KERNEL32.GetStdHandle(_STD_ERROR_HANDLE),
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if all(handle and handle != invalid_handle for handle in standard_handles):
        startup.flags = _STARTF_USESTDHANDLES
        startup.stdin, startup.stdout, startup.stderr = standard_handles
    information = _ProcessInformation()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if not _KERNEL32.CreateProcessW(
        None,
        command_line,
        None,
        None,
        True,
        _CREATE_SUSPENDED,
        None,
        None,
        byref(startup),
        byref(information),
    ):
        raise _winerror()
    return _SuspendedProcess(information.process, information.thread, int(information.process_id))


def _assign(job: wintypes.HANDLE, process: wintypes.HANDLE) -> None:
    if not _KERNEL32.AssignProcessToJobObject(job, process):
        raise _winerror()


def _resume(process: _SuspendedProcess) -> None:
    if _KERNEL32.ResumeThread(process.thread) == _WAIT_FAILED:
        raise _winerror()


def _wait(process: _SuspendedProcess) -> int:
    if _KERNEL32.WaitForSingleObject(process.process, _INFINITE) == _WAIT_FAILED:
        raise _winerror()
    exit_code = wintypes.DWORD(_STILL_ACTIVE)
    if not _KERNEL32.GetExitCodeProcess(process.process, byref(exit_code)):
        raise _winerror()
    return int(exit_code.value)


def _terminate_and_reap(process: _SuspendedProcess) -> None:
    if not _KERNEL32.TerminateProcess(process.process, 1):
        error = ctypes.get_last_error()
        exit_code = wintypes.DWORD()
        if not _KERNEL32.GetExitCodeProcess(process.process, byref(exit_code)) or exit_code.value == _STILL_ACTIVE:
            raise ctypes.WinError(error)
    if _KERNEL32.WaitForSingleObject(process.process, _INFINITE) == _WAIT_FAILED:
        raise _winerror()


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise SystemExit("missing RPC command")
    job = _create_job()
    process: _SuspendedProcess | None = None
    try:
        process = _create_suspended(command)
        try:
            _assign(job, process.process)
            _resume(process)
        except BaseException:
            _terminate_and_reap(process)
            raise
        return _wait(process)
    finally:
        _KERNEL32.CloseHandle(job)
        if process is not None:
            _KERNEL32.CloseHandle(process.thread)
            _KERNEL32.CloseHandle(process.process)


if __name__ == "__main__":
    raise SystemExit(main())
