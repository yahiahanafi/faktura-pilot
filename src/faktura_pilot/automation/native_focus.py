"""Guarded Win32 foreground activation for a known top-level window."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

SW_RESTORE = 9
GA_ROOT = 2


class _Win32Foreground:
    def __init__(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._is_window = user32.IsWindow
        self._is_window.argtypes = (wintypes.HWND,)
        self._is_window.restype = wintypes.BOOL
        self._get_ancestor = user32.GetAncestor
        self._get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self._get_ancestor.restype = wintypes.HWND
        self._is_enabled = user32.IsWindowEnabled
        self._is_enabled.argtypes = (wintypes.HWND,)
        self._is_enabled.restype = wintypes.BOOL
        self._is_iconic = user32.IsIconic
        self._is_iconic.argtypes = (wintypes.HWND,)
        self._is_iconic.restype = wintypes.BOOL
        self._show_window_async = user32.ShowWindowAsync
        self._show_window_async.argtypes = (wintypes.HWND, ctypes.c_int)
        self._show_window_async.restype = wintypes.BOOL
        self._get_foreground = user32.GetForegroundWindow
        self._get_foreground.argtypes = ()
        self._get_foreground.restype = wintypes.HWND
        self._set_foreground = user32.SetForegroundWindow
        self._set_foreground.argtypes = (wintypes.HWND,)
        self._set_foreground.restype = wintypes.BOOL
        self._bring_to_top = user32.BringWindowToTop
        self._bring_to_top.argtypes = (wintypes.HWND,)
        self._bring_to_top.restype = wintypes.BOOL
        self._window_thread = user32.GetWindowThreadProcessId
        self._window_thread.argtypes = (wintypes.HWND, ctypes.c_void_p)
        self._window_thread.restype = wintypes.DWORD
        self._attach_input = user32.AttachThreadInput
        self._attach_input.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        self._attach_input.restype = wintypes.BOOL
        self._current_thread = kernel32.GetCurrentThreadId
        self._current_thread.argtypes = ()
        self._current_thread.restype = wintypes.DWORD

    def is_window(self, handle: int) -> bool:
        return bool(self._is_window(handle))

    def root_ancestor(self, handle: int) -> int:
        return int(self._get_ancestor(handle, GA_ROOT) or 0)

    def is_enabled(self, handle: int) -> bool:
        return bool(self._is_enabled(handle))

    def is_iconic(self, handle: int) -> bool:
        return bool(self._is_iconic(handle))

    def show_window_async(self, handle: int) -> bool:
        return bool(self._show_window_async(handle, SW_RESTORE))

    def foreground(self) -> int:
        return int(self._get_foreground() or 0)

    def set_foreground(self, handle: int) -> bool:
        return bool(self._set_foreground(handle))

    def bring_to_top(self, handle: int) -> bool:
        return bool(self._bring_to_top(handle))

    def window_thread(self, handle: int) -> int:
        return int(self._window_thread(handle, None))

    def current_thread(self) -> int:
        return int(self._current_thread())

    def attach_input(self, source: int, target: int, attach: bool) -> bool:
        return bool(self._attach_input(source, target, attach))


def top_level_handle(handle: int, *, native: Any = None) -> int | None:
    """Return the valid GA_ROOT HWND for a window or child control."""
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        return None
    try:
        if native is None:
            if os.name != "nt":
                return None
            native = _Win32Foreground()
        if not native.is_window(handle):
            return None
        root = native.root_ancestor(handle)
        if not root or not native.is_window(root):
            return None
        return root
    except Exception:
        return None


def _until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def ensure_foreground(handle: int, *, native: Any = None, timeout: float = 0.4) -> bool:
    """Activate a top-level *handle* only when that exact HWND is foreground.

    Input queues are attached only for one fallback attempt and always detached.
    A blocked activation, stale HWND, or native API failure returns ``False``.
    """
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        return False
    try:
        if native is None:
            if os.name != "nt":
                return False
            native = _Win32Foreground()

        if not native.is_window(handle) or not native.is_enabled(handle):
            return False
        if native.foreground() == handle:
            return True

        if native.is_iconic(handle):
            if not native.show_window_async(handle):
                return False
            if not _until(lambda: not native.is_iconic(handle), timeout):
                return False

        native.set_foreground(handle)
        if _until(lambda: native.foreground() == handle, timeout):
            return True

        foreground = native.foreground()
        current_thread = native.current_thread()
        foreground_thread = native.window_thread(foreground) if foreground else 0
        target_thread = native.window_thread(handle)
        if not current_thread or not foreground_thread or not target_thread:
            return False

        attached: list[int] = []
        activated = False
        detached = True
        try:
            for thread in (foreground_thread, target_thread):
                if thread != current_thread and thread not in attached:
                    if not native.attach_input(current_thread, thread, True):
                        return False
                    attached.append(thread)
            native.bring_to_top(handle)
            native.set_foreground(handle)
            activated = _until(lambda: native.foreground() == handle, timeout)
        finally:
            for thread in reversed(attached):
                try:
                    detached = native.attach_input(current_thread, thread, False) and detached
                except Exception:
                    detached = False
        return activated and detached and native.foreground() == handle
    except Exception:
        return False
