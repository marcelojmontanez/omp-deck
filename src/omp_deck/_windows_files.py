from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_void_p, cast, create_string_buffer, wintypes
from pathlib import Path

from ._windows_security import current_user_sid_string

_ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GENERIC_READ = 0x80000000
_READ_CONTROL = 0x00020000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_ACCESS_ALLOWED_ACE_TYPE = 0
_UNSUPPORTED_ALLOW_ACE_TYPES = {4, 5, 9, 11}
_ACL_SIZE_INFORMATION = 2
_UNTRUSTED_WRITE = (
    0x00000002
    | 0x00000004
    | 0x00000010
    | 0x00000040
    | 0x00000100
    | 0x00010000
    | 0x00040000
    | 0x00080000
    | 0x10000000
    | 0x40000000
)
_TRUSTED_SIDS = ("S-1-5-18", "S-1-5-32-544")


class _FileTime(Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _FileInformation(Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", _FileTime),
        ("last_access_time", _FileTime),
        ("last_write_time", _FileTime),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _AclSizeInformation(Structure):
    _fields_ = [
        ("ace_count", wintypes.DWORD),
        ("acl_bytes_in_use", wintypes.DWORD),
        ("acl_bytes_free", wintypes.DWORD),
    ]


class _AceHeader(Structure):
    _fields_ = [("ace_type", wintypes.BYTE), ("ace_flags", wintypes.BYTE), ("ace_size", wintypes.WORD)]


class _AccessAllowedAce(Structure):
    _fields_ = [("header", _AceHeader), ("mask", wintypes.DWORD), ("sid_start", wintypes.DWORD)]


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
_KERNEL32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, POINTER(_FileInformation))
_KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
_KERNEL32.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
_KERNEL32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
_KERNEL32.ReadFile.argtypes = (wintypes.HANDLE, c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD), c_void_p)
_KERNEL32.ReadFile.restype = wintypes.BOOL
_KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
_KERNEL32.CloseHandle.restype = wintypes.BOOL
_KERNEL32.LocalFree.argtypes = (c_void_p,)
_KERNEL32.LocalFree.restype = c_void_p
_ADVAPI32.GetSecurityInfo.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
)
_ADVAPI32.GetSecurityInfo.restype = wintypes.DWORD
_ADVAPI32.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, POINTER(c_void_p))
_ADVAPI32.ConvertStringSidToSidW.restype = wintypes.BOOL
_ADVAPI32.EqualSid.argtypes = (c_void_p, c_void_p)
_ADVAPI32.EqualSid.restype = wintypes.BOOL
_ADVAPI32.GetAclInformation.argtypes = (c_void_p, c_void_p, wintypes.DWORD, wintypes.DWORD)
_ADVAPI32.GetAclInformation.restype = wintypes.BOOL
_ADVAPI32.GetAce.argtypes = (c_void_p, wintypes.DWORD, POINTER(c_void_p))
_ADVAPI32.GetAce.restype = wintypes.BOOL


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _open(path: Path, *, directory: bool) -> wintypes.HANDLE:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (_FILE_FLAG_BACKUP_SEMANTICS if directory else 0)
    handle = _KERNEL32.CreateFileW(
        str(path),
        _GENERIC_READ | _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _winerror()
    return handle


def _information(handle: wintypes.HANDLE) -> _FileInformation:
    info = _FileInformation()
    if not _KERNEL32.GetFileInformationByHandle(handle, byref(info)):
        raise _winerror()
    return info


def _sid(value: str) -> c_void_p:
    sid = c_void_p()
    if not _ADVAPI32.ConvertStringSidToSidW(value, byref(sid)):
        raise _winerror()
    return sid


def _trusted_sid(candidate: c_void_p, trusted: tuple[c_void_p, ...]) -> bool:
    return any(bool(_ADVAPI32.EqualSid(candidate, value)) for value in trusted)


def _safe_security(handle: wintypes.HANDLE) -> bool:
    owner = c_void_p()
    dacl = c_void_p()
    descriptor = c_void_p()
    status = _ADVAPI32.GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        byref(owner),
        None,
        byref(dacl),
        None,
        byref(descriptor),
    )
    if status:
        raise OSError(status, "reading blob security failed")
    trusted = tuple(_sid(value) for value in (current_user_sid_string(), *_TRUSTED_SIDS))
    try:
        if not owner or not dacl or not _ADVAPI32.EqualSid(owner, trusted[0]):
            return False
        details = _AclSizeInformation()
        if not _ADVAPI32.GetAclInformation(dacl, byref(details), ctypes.sizeof(details), _ACL_SIZE_INFORMATION):
            raise _winerror()
        for index in range(details.ace_count):
            raw_ace = c_void_p()
            if not _ADVAPI32.GetAce(dacl, index, byref(raw_ace)):
                raise _winerror()
            header = cast(raw_ace, POINTER(_AceHeader)).contents
            if header.ace_type in _UNSUPPORTED_ALLOW_ACE_TYPES:
                return False
            if header.ace_type != _ACCESS_ALLOWED_ACE_TYPE:
                continue
            ace = cast(raw_ace, POINTER(_AccessAllowedAce)).contents
            ace_sid = c_void_p(raw_ace.value + _AccessAllowedAce.sid_start.offset)
            if ace.mask & _UNTRUSTED_WRITE and not _trusted_sid(ace_sid, trusted):
                return False
        return True
    finally:
        for value in trusted:
            _KERNEL32.LocalFree(value)
        _KERNEL32.LocalFree(descriptor)


def _final_path(handle: wintypes.HANDLE) -> str:
    required = _KERNEL32.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not required:
        raise _winerror()
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = _KERNEL32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise _winerror()
    return buffer.value.rstrip("\\").casefold()


def read_user_owned_file(root: Path, candidate: Path, max_bytes: int) -> bytes | None:
    root_handle = file_handle = None
    try:
        root_handle = _open(root, directory=True)
        root_info = _information(root_handle)
        if root_info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not root_info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
            return None
        if not _safe_security(root_handle):
            return None
        file_handle = _open(candidate, directory=False)
        file_info = _information(file_handle)
        size = (file_info.file_size_high << 32) | file_info.file_size_low
        if (
            file_info.attributes & (_FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY)
            or not 0 < size <= max_bytes
            or not _safe_security(file_handle)
        ):
            return None
        expected = _final_path(root_handle) + "\\" + candidate.name.casefold()
        if _final_path(file_handle) != expected:
            return None
        buffer = create_string_buffer(size)
        read = wintypes.DWORD()
        if not _KERNEL32.ReadFile(file_handle, buffer, size, byref(read), None) or read.value != size:
            return None
        return buffer.raw
    except OSError:
        return None
    finally:
        if file_handle not in (None, _INVALID_HANDLE_VALUE):
            _KERNEL32.CloseHandle(file_handle)
        if root_handle not in (None, _INVALID_HANDLE_VALUE):
            _KERNEL32.CloseHandle(root_handle)
