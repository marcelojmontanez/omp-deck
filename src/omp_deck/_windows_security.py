from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from contextlib import contextmanager
from ctypes import POINTER, Structure, byref, c_void_p, cast, wintypes
from pathlib import Path

_ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_SET_ACCESS = 2
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1
_FILE_ALL_ACCESS = 0x001F01FF
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACL_SIZE_INFORMATION = 2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x00000080


class _SidAndAttributes(Structure):
    _fields_ = [("sid", c_void_p), ("attributes", wintypes.DWORD)]


class _TokenUser(Structure):
    _fields_ = [("user", _SidAndAttributes)]


class _Trustee(Structure):
    _fields_ = [
        ("multiple_trustee", c_void_p),
        ("multiple_trustee_operation", wintypes.DWORD),
        ("trustee_form", wintypes.DWORD),
        ("trustee_type", wintypes.DWORD),
        ("name", wintypes.LPWSTR),
    ]


class _ExplicitAccess(Structure):
    _fields_ = [
        ("access_permissions", wintypes.DWORD),
        ("access_mode", wintypes.DWORD),
        ("inheritance", wintypes.DWORD),
        ("trustee", _Trustee),
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


_ADVAPI32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.HANDLE))
_ADVAPI32.OpenProcessToken.restype = wintypes.BOOL
_ADVAPI32.GetTokenInformation.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    c_void_p,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
)
_ADVAPI32.GetTokenInformation.restype = wintypes.BOOL
_ADVAPI32.GetNamedSecurityInfoW.argtypes = (
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
    POINTER(c_void_p),
)
_ADVAPI32.GetNamedSecurityInfoW.restype = wintypes.DWORD
_ADVAPI32.SetEntriesInAclW.argtypes = (
    wintypes.ULONG,
    POINTER(_ExplicitAccess),
    c_void_p,
    POINTER(c_void_p),
)
_ADVAPI32.SetEntriesInAclW.restype = wintypes.DWORD
_ADVAPI32.SetNamedSecurityInfoW.argtypes = (
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    c_void_p,
    c_void_p,
    c_void_p,
    c_void_p,
)
_ADVAPI32.SetNamedSecurityInfoW.restype = wintypes.DWORD
_ADVAPI32.EqualSid.argtypes = (c_void_p, c_void_p)
_ADVAPI32.EqualSid.restype = wintypes.BOOL
_ADVAPI32.GetAclInformation.argtypes = (c_void_p, c_void_p, wintypes.DWORD, wintypes.DWORD)
_ADVAPI32.GetAclInformation.restype = wintypes.BOOL
_ADVAPI32.GetAce.argtypes = (c_void_p, wintypes.DWORD, POINTER(c_void_p))
_ADVAPI32.GetAce.restype = wintypes.BOOL
_ADVAPI32.GetSecurityDescriptorControl.argtypes = (c_void_p, POINTER(wintypes.WORD), POINTER(wintypes.DWORD))
_ADVAPI32.GetSecurityDescriptorControl.restype = wintypes.BOOL
_KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
_KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
_KERNEL32.CloseHandle.restype = wintypes.BOOL
_KERNEL32.LocalFree.argtypes = (c_void_p,)
_KERNEL32.LocalFree.restype = c_void_p
_KERNEL32.CreateMutexW.argtypes = (c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_KERNEL32.CreateMutexW.restype = wintypes.HANDLE
_KERNEL32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
_KERNEL32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
_KERNEL32.ReleaseMutex.restype = wintypes.BOOL


def _winerror() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _check_status(status: int, operation: str) -> None:
    if status:
        raise OSError(status, f"{operation} failed")


def _current_user_sid() -> tuple[ctypes.Array[ctypes.c_char], c_void_p]:
    token = wintypes.HANDLE()
    if not _ADVAPI32.OpenProcessToken(_KERNEL32.GetCurrentProcess(), _TOKEN_QUERY, byref(token)):
        raise _winerror()
    try:
        required = wintypes.DWORD()
        _ADVAPI32.GetTokenInformation(token, _TOKEN_USER, None, 0, byref(required))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise _winerror()
        buffer = ctypes.create_string_buffer(required.value)
        if not _ADVAPI32.GetTokenInformation(token, _TOKEN_USER, buffer, required, byref(required)):
            raise _winerror()
        sid = cast(buffer, POINTER(_TokenUser)).contents.user.sid
        return buffer, sid
    finally:
        _KERNEL32.CloseHandle(token)


def _path_kind(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(f"private path is a reparse point: {path}")
    if stat.S_ISDIR(info.st_mode):
        return True
    if stat.S_ISREG(info.st_mode):
        return False
    raise OSError(f"private path has unsupported type: {path}")


def _security(path: Path) -> tuple[c_void_p, c_void_p, c_void_p]:
    owner = c_void_p()
    dacl = c_void_p()
    descriptor = c_void_p()
    status = _ADVAPI32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        byref(owner),
        None,
        byref(dacl),
        None,
        byref(descriptor),
    )
    _check_status(status, "reading private path security")
    return owner, dacl, descriptor


def owned_by_current_user(path: Path) -> bool:
    _path_kind(path)
    sid_buffer, sid = _current_user_sid()
    owner, _dacl, descriptor = _security(path)
    try:
        return bool(_ADVAPI32.EqualSid(owner, sid))
    finally:
        del sid_buffer
        _KERNEL32.LocalFree(descriptor)


def secure_path(path: Path) -> None:
    directory = _path_kind(path)
    sid_buffer, sid = _current_user_sid()
    owner, _dacl, descriptor = _security(path)
    try:
        if not _ADVAPI32.EqualSid(owner, sid):
            raise PermissionError(f"private path is not owned by the current user: {path}")
    finally:
        _KERNEL32.LocalFree(descriptor)

    trustee = _Trustee(None, 0, _TRUSTEE_IS_SID, _TRUSTEE_IS_USER, cast(sid, wintypes.LPWSTR))
    access = _ExplicitAccess(
        _FILE_ALL_ACCESS,
        _SET_ACCESS,
        (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE) if directory else 0,
        trustee,
    )
    acl = c_void_p()
    try:
        _check_status(_ADVAPI32.SetEntriesInAclW(1, byref(access), None, byref(acl)), "building private ACL")
        _check_status(
            _ADVAPI32.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            ),
            "setting private ACL",
        )
    finally:
        del sid_buffer
        if acl:
            _KERNEL32.LocalFree(acl)


def is_private_path(path: Path) -> bool:
    try:
        _path_kind(path)
        sid_buffer, sid = _current_user_sid()
        owner, dacl, descriptor = _security(path)
    except OSError:
        return False
    try:
        if not owner or not dacl or not _ADVAPI32.EqualSid(owner, sid):
            return False
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _ADVAPI32.GetSecurityDescriptorControl(descriptor, byref(control), byref(revision)):
            return False
        if not control.value & _SE_DACL_PROTECTED:
            return False
        details = _AclSizeInformation()
        if not _ADVAPI32.GetAclInformation(dacl, byref(details), ctypes.sizeof(details), _ACL_SIZE_INFORMATION):
            return False
        if details.ace_count != 1:
            return False
        raw_ace = c_void_p()
        if not _ADVAPI32.GetAce(dacl, 0, byref(raw_ace)):
            return False
        ace = cast(raw_ace, POINTER(_AccessAllowedAce)).contents
        ace_sid = c_void_p(raw_ace.value + _AccessAllowedAce.sid_start.offset)
        return (
            ace.header.ace_type == _ACCESS_ALLOWED_ACE_TYPE
            and ace.mask & _FILE_ALL_ACCESS == _FILE_ALL_ACCESS
            and bool(_ADVAPI32.EqualSid(ace_sid, sid))
        )
    finally:
        del sid_buffer
        _KERNEL32.LocalFree(descriptor)


@contextmanager
def named_mutex(path: Path):
    normalized = os.path.normcase(os.path.abspath(path))
    name = "Local\\OMPDeck-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    handle = _KERNEL32.CreateMutexW(None, False, name)
    if not handle:
        raise _winerror()
    try:
        result = _KERNEL32.WaitForSingleObject(handle, _INFINITE)
        if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            raise OSError(f"waiting for private state lock failed: {result}")
        try:
            yield
        finally:
            if not _KERNEL32.ReleaseMutex(handle):
                raise _winerror()
    finally:
        _KERNEL32.CloseHandle(handle)
