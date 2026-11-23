"""A secret kept on disk only in a form one Windows account can read back.

Windows DPAPI (CryptProtectData) encrypts with a key tied to the signed-in
account on this machine: the same file copied to another machine, or read by
another account, cannot be decrypted. Called through ctypes, so no package is
needed. Off Windows, `available()` is False and callers keep their own
fallback (an owner-only file).
"""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes

# What the encrypted value says it is: shown in the file, never secret.
_DESCRIPTION = "SDM saved connection"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    """Whether DPAPI can be used here (Windows only)."""
    return os.name == "nt"


def _libraries():
    """crypt32 and kernel32 with their signatures declared, so pointers are
    passed whole on 64-bit Windows and the error code is the call's own."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signature = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                 ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt32.CryptProtectData.argtypes = signature
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob),
                                           ctypes.POINTER(wintypes.LPWSTR)] + signature[2:]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _taken(blob: _Blob, kernel32) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def protect(secret: str) -> str:
    """The secret encrypted for this account on this machine, as base64 text."""
    if not available():
        raise OSError("DPAPI is only available on Windows.")
    crypt32, kernel32 = _libraries()
    source, _keep = _blob(secret.encode("utf-8"))
    out = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN (0x1): never show a prompt on a server.
    if not crypt32.CryptProtectData(
            ctypes.byref(source), _DESCRIPTION, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError(f"Windows could not encrypt the secret (error {ctypes.get_last_error()}).")
    return base64.b64encode(_taken(out, kernel32)).decode("ascii")


def unprotect(text: str) -> str:
    """A secret `protect` wrote. Raises ValueError when this account on this
    machine cannot decrypt it (another account, another machine, or damage)."""
    if not available():
        raise ValueError("The saved secret was encrypted on Windows and cannot be read here.")
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("The saved secret is damaged.") from exc
    crypt32, kernel32 = _libraries()
    source, _keep = _blob(data)
    out = _Blob()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(out)):
        raise ValueError("The saved secret cannot be decrypted by this Windows account on "
                         "this machine.")
    return _taken(out, kernel32).decode("utf-8")
