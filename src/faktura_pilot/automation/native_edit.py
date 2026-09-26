"""Read the text of a verified native Win32 Edit control."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any


class _Win32EditReader:
    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._is_window = user32.IsWindow
        self._is_window.argtypes = (wintypes.HWND,)
        self._is_window.restype = wintypes.BOOL
        self._get_class_name = user32.GetClassNameW
        self._get_class_name.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self._get_class_name.restype = ctypes.c_int

    def is_window(self, handle: int) -> bool:
        return bool(self._is_window(handle))

    def class_name(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        length = self._get_class_name(handle, buffer, len(buffer))
        return buffer.value if length else ""

    @staticmethod
    def window_text(handle: int) -> str:
        # Fakturama's UIA value can be blank while the Win32 Edit holds text.
        from pywinauto import Desktop

        return Desktop(backend="win32").window(handle=handle).wrapper_object().window_text()


def native_edit_text(handle: int | None, *, native: Any = None) -> str | None:
    """Return exact Edit text, including '', or None when a safe read is unavailable."""
    max_handle = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
    if (
        isinstance(handle, bool)
        or not isinstance(handle, int)
        or handle <= 0
        or handle > max_handle
    ):
        return None
    try:
        if native is None:
            if os.name != "nt":
                return None
            native = _Win32EditReader()
        if not native.is_window(handle):
            return None
        control_class = native.class_name(handle)
        if not isinstance(control_class, str) or control_class.casefold() != "edit":
            return None
        value = native.window_text(handle)
        return value if isinstance(value, str) else None
    except Exception:
        return None

