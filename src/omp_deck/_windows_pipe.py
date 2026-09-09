from __future__ import annotations

import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, c_size_t, c_void_p, wintypes

from ._windows_security import process_owned_by_current_user

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_READMODE_BYTE = 0
_ERROR_FILE_NOT_FOUND = 2
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_BUSY = 231
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class _Overlapped(Structure):
    _fields_ = [
        ("internal", c_size_t),
        ("internal_high", c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


_KERNEL32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
_KERNEL32.WaitNamedPipeW.restype = wintypes.BOOL
_KERNEL32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_KERNEL32.CreateFileW.restype = wintypes.HANDLE
_KERNEL32.GetNamedPipeServerProcessId.argtypes = (wintypes.HANDLE, POINTER(wintypes.ULONG))
_KERNEL32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
_KERNEL32.SetNamedPipeHandleState.argtypes = (
    wintypes.HANDLE,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
)
_KERNEL32.SetNamedPipeHandleState.restype = wintypes.BOOL
_KERNEL32.CreateEventW.argtypes = (c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_KERNEL32.CreateEventW.restype = wintypes.HANDLE
_KERNEL32.ReadFile.argtypes = (wintypes.HANDLE, c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD), POINTER(_Overlapped))
_KERNEL32.ReadFile.restype = wintypes.BOOL
_KERNEL32.WriteFile.argtypes = (
    wintypes.HANDLE,
    c_void_p,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
    POINTER(_Overlapped),
)
_KERNEL32.WriteFile.restype = wintypes.BOOL
_KERNEL32.GetOverlappedResult.argtypes = (
    wintypes.HANDLE,
    POINTER(_Overlapped),
    POINTER(wintypes.DWORD),
    wintypes.BOOL,
)
_KERNEL32.GetOverlappedResult.restype = wintypes.BOOL
_KERNEL32.CancelIoEx.argtypes = (wintypes.HANDLE, POINTER(_Overlapped))
_KERNEL32.CancelIoEx.restype = wintypes.BOOL
_KERNEL32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
_KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
_KERNEL32.CloseHandle.restype = wintypes.BOOL


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


class WindowsPipe:
    def __init__(self, handle: wintypes.HANDLE) -> None:
        self._handle = handle
        self._timeout: float | None = None
        self._close_lock = threading.Lock()

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        self._timeout = timeout

    def recv(self, size: int) -> bytes:
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        transferred = self._io(_KERNEL32.ReadFile, buffer, size, allow_eof=True)
        return buffer.raw[:transferred]

    def sendall(self, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            chunk = view[offset : offset + 65536]
            buffer = (ctypes.c_char * len(chunk)).from_buffer_copy(chunk)
            written = self._io(_KERNEL32.WriteFile, buffer, len(chunk), allow_eof=False)
            if written <= 0:
                raise BrokenPipeError("named pipe closed during write")
            offset += written

    def shutdown(self, _how: int) -> None:
        handle = self._handle
        if handle not in (None, _INVALID_HANDLE_VALUE):
            _KERNEL32.CancelIoEx(handle, None)

    def close(self) -> None:
        with self._close_lock:
            handle, self._handle = self._handle, None
        if handle not in (None, _INVALID_HANDLE_VALUE):
            _KERNEL32.CancelIoEx(handle, None)
            _KERNEL32.CloseHandle(handle)

    def _io(self, function, buffer, size: int, *, allow_eof: bool) -> int:
        handle = self._handle
        if handle in (None, _INVALID_HANDLE_VALUE):
            raise OSError("named pipe is closed")
        event = _KERNEL32.CreateEventW(None, True, False, None)
        if not event:
            raise _winerror()
        overlapped = _Overlapped(event=event)
        transferred = wintypes.DWORD()
        try:
            if function(handle, buffer, size, byref(transferred), byref(overlapped)):
                return transferred.value
            error = ctypes.get_last_error()
            if allow_eof and error == _ERROR_BROKEN_PIPE:
                return 0
            if error != _ERROR_IO_PENDING:
                raise ctypes.WinError(error)
            milliseconds = _INFINITE if self._timeout is None else max(0, min(_INFINITE - 1, int(self._timeout * 1000)))
            result = _KERNEL32.WaitForSingleObject(event, milliseconds)
            if result == _WAIT_TIMEOUT:
                _KERNEL32.CancelIoEx(handle, byref(overlapped))
                _KERNEL32.WaitForSingleObject(event, _INFINITE)
                raise TimeoutError("named pipe operation timed out")
            if result != _WAIT_OBJECT_0:
                raise OSError(f"waiting for named pipe I/O failed: {result}")
            if not _KERNEL32.GetOverlappedResult(handle, byref(overlapped), byref(transferred), False):
                error = ctypes.get_last_error()
                if allow_eof and error == _ERROR_BROKEN_PIPE:
                    return 0
                raise ctypes.WinError(error)
            return transferred.value
        finally:
            _KERNEL32.CloseHandle(event)


def connect_named_pipe(path: str, *, timeout: float = 5.0) -> WindowsPipe:
    if not isinstance(path, str) or not path.startswith("\\\\.\\pipe\\") or "\x00" in path or len(path) > 256:
        raise ValueError("invalid OMP Deck named pipe")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("named pipe connection timed out")
        if not _KERNEL32.WaitNamedPipeW(path, max(1, int(remaining * 1000))):
            error = ctypes.get_last_error()
            if error not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY):
                raise ctypes.WinError(error)
            if error == _ERROR_FILE_NOT_FOUND:
                time.sleep(min(0.01, remaining))
            continue
        handle = _KERNEL32.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle != _INVALID_HANDLE_VALUE:
            process_id = wintypes.ULONG()
            if not _KERNEL32.GetNamedPipeServerProcessId(handle, byref(process_id)):
                error = _winerror()
                _KERNEL32.CloseHandle(handle)
                raise error
            try:
                owned = process_owned_by_current_user(process_id.value)
            except OSError:
                _KERNEL32.CloseHandle(handle)
                raise
            if not owned:
                _KERNEL32.CloseHandle(handle)
                raise PermissionError("OMP Deck named-pipe server is owned by another user")
            mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
            if not _KERNEL32.SetNamedPipeHandleState(handle, byref(mode), None, None):
                error = _winerror()
                _KERNEL32.CloseHandle(handle)
                raise error
            return WindowsPipe(handle)
        error = ctypes.get_last_error()
        if error not in (_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY):
            raise ctypes.WinError(error)
        if error == _ERROR_FILE_NOT_FOUND:
            time.sleep(min(0.01, remaining))
