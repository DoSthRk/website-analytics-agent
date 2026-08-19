"""Narrow Windows Credential Manager access for local analytics credentials."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_LPBYTE = ctypes.POINTER(ctypes.c_byte)


class WindowsCredentialError(RuntimeError):
    """Raised when a local Windows generic credential cannot be used safely."""


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", _LPBYTE),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def load_windows_generic_credential(target: str) -> str:
    """Return a UTF-16 generic secret from the current user's credential vault."""
    _validate_target(target)
    functions = _credential_functions()
    credential_pointer = ctypes.POINTER(_Credential)()
    if not functions.read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            raise WindowsCredentialError("local credential was not found")
        raise WindowsCredentialError("local credential could not be read") from ctypes.WinError(code)
    try:
        credential = credential_pointer.contents
        if credential.CredentialBlobSize == 0 or not credential.CredentialBlob:
            raise WindowsCredentialError("local credential has no value")
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            value = raw.decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise WindowsCredentialError("local credential has an invalid encoding") from error
        if not value:
            raise WindowsCredentialError("local credential has no value")
        return value
    finally:
        functions.free(credential_pointer)


def store_windows_generic_credential(
    target: str, value: str, *, username: str = "website-analytics-agent"
) -> None:
    """Store a local generic secret; only provisioning code should call this."""
    _validate_target(target)
    if not isinstance(value, str) or not value:
        raise WindowsCredentialError("local credential value is invalid")
    if not isinstance(username, str) or not username:
        raise WindowsCredentialError("local credential username is invalid")
    encoded = value.encode("utf-16-le")
    blob = (ctypes.c_byte * len(encoded)).from_buffer_copy(encoded)
    credential = _Credential(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment=None,
        LastWritten=_FileTime(),
        CredentialBlobSize=len(encoded),
        CredentialBlob=ctypes.cast(blob, _LPBYTE),
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username,
    )
    functions = _credential_functions()
    if not functions.write(ctypes.byref(credential), 0):
        raise WindowsCredentialError("local credential could not be stored") from ctypes.WinError(
            ctypes.get_last_error()
        )


def delete_windows_generic_credential(target: str) -> None:
    """Remove a local generic secret when a provisioning transaction rolls back."""
    _validate_target(target)
    functions = _credential_functions()
    if functions.delete(target, _CRED_TYPE_GENERIC, 0):
        return
    code = ctypes.get_last_error()
    if code != _ERROR_NOT_FOUND:
        raise WindowsCredentialError("local credential could not be removed") from ctypes.WinError(
            code
        )


def _validate_target(target: str) -> None:
    if not isinstance(target, str) or not target.startswith("WebsiteAnalytics/"):
        raise WindowsCredentialError("local credential target is invalid")


class _CredentialFunctions:
    def __init__(self, read: object, write: object, delete: object, free: object) -> None:
        self.read = read
        self.write = write
        self.delete = delete
        self.free = free


def _credential_functions() -> _CredentialFunctions:
    if sys.platform != "win32":
        raise WindowsCredentialError("Windows Credential Manager is unavailable on this platform")
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    read = library.CredReadW
    read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    read.restype = wintypes.BOOL
    write = library.CredWriteW
    write.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    write.restype = wintypes.BOOL
    delete = library.CredDeleteW
    delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    delete.restype = wintypes.BOOL
    free = library.CredFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    return _CredentialFunctions(read, write, delete, free)
