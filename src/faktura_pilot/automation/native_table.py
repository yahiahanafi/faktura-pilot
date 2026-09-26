"""Read-only proof that a uniquely identified Windows selector is empty.

Native ListView tables provide a row count. Fakturama's SWT table instead
requires verified column headers, a native-scoped results canvas, and two
unchanged blank captures of its visible, non-scrollable body. Missing UIA rows
or OCR text alone never means an empty result.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import Any, Protocol


class _TableReader(Protocol):
    def is_window(self, handle: int) -> bool: ...
    def is_visible(self, handle: int) -> bool: ...
    def is_enabled(self, handle: int) -> bool: ...
    def process_id(self, handle: int) -> int: ...
    def child_windows(self, handle: int) -> list[int]: ...
    def class_name(self, handle: int) -> str: ...
    def item_count(self, handle: int) -> int | None: ...
    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None: ...
    def client_rect(self, handle: int) -> tuple[int, int, int, int] | None: ...
    def no_vertical_scroll(self, handle: int) -> bool | None: ...


class _Win32TableReader:
    """Use bounded Win32 messages; no cross-process memory or UI actions."""

    _LVM_GETITEMCOUNT = 0x1004
    _SMTO_ABORTIFHUNG = 0x0002

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.EnumChildWindows.argtypes = [
            wintypes.HWND,
            self._callback_type,
            wintypes.LPARAM,
        ]
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.c_void_p]
        self.user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.user32.GetScrollInfo.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

    def is_window(self, handle: int) -> bool:
        return bool(self.user32.IsWindow(handle))

    def is_visible(self, handle: int) -> bool:
        return bool(self.user32.IsWindowVisible(handle))

    def is_enabled(self, handle: int) -> bool:
        return bool(self.user32.IsWindowEnabled(handle))

    def process_id(self, handle: int) -> int:
        owner = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(handle, self.ctypes.byref(owner))
        return int(owner.value)

    def child_windows(self, handle: int) -> list[int]:
        found: list[int] = []

        def collect(child: int, _context: int) -> bool:
            found.append(int(child))
            return True

        callback = self._callback_type(collect)
        self.user32.EnumChildWindows(handle, callback, 0)
        return found

    def class_name(self, handle: int) -> str:
        buffer = self.ctypes.create_unicode_buffer(256)
        length = self.user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value if length else ""

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        class Rect(self.ctypes.Structure):
            _fields_ = [
                ("left", self.wintypes.LONG),
                ("top", self.wintypes.LONG),
                ("right", self.wintypes.LONG),
                ("bottom", self.wintypes.LONG),
            ]

        rect = Rect()
        if not self.user32.GetWindowRect(handle, self.ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def client_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        class Rect(self.ctypes.Structure):
            _fields_ = [
                ("left", self.wintypes.LONG),
                ("top", self.wintypes.LONG),
                ("right", self.wintypes.LONG),
                ("bottom", self.wintypes.LONG),
            ]

        class Point(self.ctypes.Structure):
            _fields_ = [("x", self.wintypes.LONG), ("y", self.wintypes.LONG)]

        rect = Rect()
        if not self.user32.GetClientRect(handle, self.ctypes.byref(rect)):
            return None
        top_left = Point(rect.left, rect.top)
        bottom_right = Point(rect.right, rect.bottom)
        if not self.user32.ClientToScreen(handle, self.ctypes.byref(top_left)):
            return None
        if not self.user32.ClientToScreen(handle, self.ctypes.byref(bottom_right)):
            return None
        return top_left.x, top_left.y, bottom_right.x, bottom_right.y

    def no_vertical_scroll(self, handle: int) -> bool | None:
        # A body with rows above/below the viewport is never proof of an
        # empty selector, even when the visible pixels happen to be blank.
        style = int(self.user32.GetWindowLongPtrW(handle, -16))
        if not style & 0x00200000:  # WS_VSCROLL
            return not any(
                self.is_visible(child) and self.class_name(child).casefold() == "scrollbar"
                for child in self.child_windows(handle)
            )

        class ScrollInfo(self.ctypes.Structure):
            _fields_ = [
                ("cbSize", self.wintypes.UINT),
                ("fMask", self.wintypes.UINT),
                ("nMin", self.ctypes.c_int),
                ("nMax", self.ctypes.c_int),
                ("nPage", self.wintypes.UINT),
                ("nPos", self.ctypes.c_int),
                ("nTrackPos", self.ctypes.c_int),
            ]

        info = ScrollInfo()
        info.cbSize = self.ctypes.sizeof(ScrollInfo)
        info.fMask = 0x17  # SIF_ALL
        if not self.user32.GetScrollInfo(handle, 1, self.ctypes.byref(info)):
            return None
        extent = info.nMax - info.nMin + 1
        return info.nPos == info.nMin and (extent <= 1 or info.nPage >= extent)

    def item_count(self, handle: int) -> int | None:
        # LVM_GETITEMCOUNT uses no pointers in wParam/lParam. The timeout keeps
        # a hung desktop process from hanging the automation itself.
        result = self.ctypes.c_size_t()
        status = self.user32.SendMessageTimeoutW(
            handle,
            self._LVM_GETITEMCOUNT,
            0,
            0,
            self._SMTO_ABORTIFHUNG,
            1000,
            self.ctypes.byref(result),
        )
        return int(result.value) if status else None


def unique_table_row_count(dialog: Any, *, native: _TableReader | None = None) -> int | None:
    """Count one native selector table, or return None when identity is uncertain.

    The caller must first identify the intended selector window uniquely. This
    function stays inside that window's HWND and never scans the desktop.
    """

    try:
        raw_handle = getattr(dialog, "handle", None)
        if callable(raw_handle):
            raw_handle = raw_handle()
        handle = int(raw_handle)
        if handle <= 0:
            return None
        if native is None:
            if os.name != "nt":
                return None
            native = _Win32TableReader()
        if not (
            native.is_window(handle) and native.is_visible(handle) and native.is_enabled(handle)
        ):
            return None
        selector_pid = native.process_id(handle)
        if selector_pid <= 0:
            return None
        tables = [
            child
            for child in native.child_windows(handle)
            if native.is_window(child)
            and native.is_visible(child)
            and native.is_enabled(child)
            and native.process_id(child) == selector_pid
            and native.class_name(child).casefold() == "syslistview32"
        ]
        if len(tables) != 1:
            return None
        count = native.item_count(tables[0])
        return count if count is not None and count >= 0 else None
    except Exception:
        # Missing handles, access restrictions, and stale wrappers are unknown.
        return None


def verified_empty_selector(
    dialog: Any,
    image: Any,
    headers: Sequence[Any],
    *,
    search_verified: bool,
    native: _TableReader | None = None,
    allow_grid_lines: bool = False,
) -> bool:
    """Prove no visible rows in a known selector; all uncertainty returns False.

    Headers are the already-confirmed OCR header bounds in dialog screenshot
    coordinates. The SWT fallback uses the unique native table canvas, its
    client area (excluding scrollbars), and two unchanged blank body captures.
    An embedded grid may opt in to uniform full-width/height grid lines. Pass
    its visible parent window, scoped header bounds, and verified search value.
    """

    if not search_verified or len(headers) < 3:
        return False
    if native is None:
        if os.name != "nt":
            return False
        try:
            native = _Win32TableReader()
        except Exception:
            return False
    if not allow_grid_lines:
        count = unique_table_row_count(dialog, native=native)
        if count is not None:
            return count == 0
    try:
        raw_handle = getattr(dialog, "handle", None)
        handle = int(raw_handle() if callable(raw_handle) else raw_handle)
        if not (
            handle > 0
            and native.is_window(handle)
            and native.is_visible(handle)
            and native.is_enabled(handle)
            and native.process_id(handle) > 0
        ):
            return False
        selector_rect = native.window_rect(handle)
        if selector_rect is None:
            return False
        left, top, right, bottom = selector_rect
        if right <= left or bottom <= top:
            return False
        width, height = image.size
        if width < 100 or height < 100:
            return False
        scale_x = width / (right - left)
        scale_y = height / (bottom - top)

        def image_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
            x1, y1, x2, y2 = rect
            return (
                round((x1 - left) * scale_x),
                round((y1 - top) * scale_y),
                round((x2 - left) * scale_x),
                round((y2 - top) * scale_y),
            )

        selector_pid = native.process_id(handle)
        candidates: list[tuple[int, int, tuple[int, int, int, int]]] = []
        for child in native.child_windows(handle):
            if not (
                native.is_window(child)
                and native.is_visible(child)
                and native.is_enabled(child)
                and native.process_id(child) == selector_pid
                and native.class_name(child).casefold() == "swt_window0"
            ):
                continue
            client = native.client_rect(child)
            if client is None:
                continue
            x1, y1, x2, y2 = image_rect(client)
            if (
                x2 - x1 < width * 0.6
                or y2 - y1 < height * 0.25
                or not all(
                    x1 <= (header.left + header.right) / 2 <= x2
                    and y1 <= (header.top + header.bottom) / 2 <= y2
                    for header in headers
                )
            ):
                continue
            candidates.append(((x2 - x1) * (y2 - y1), child, (x1, y1, x2, y2)))
        if not candidates:
            return False
        candidates.sort(key=lambda pair: pair[0])
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            return False
        _, canvas_handle, (x1, canvas_top, x2, y2) = candidates[0]
        header_centers = [(header.left + header.right) / 2 for header in headers]
        header_rows = [(header.top + header.bottom) / 2 for header in headers]
        if (
            header_centers != sorted(header_centers)
            or len(set(header_centers)) != len(header_centers)
            or header_centers[-1] - header_centers[0]
            < width * (0.1 if allow_grid_lines else 0.5)
            or max(header_rows) - min(header_rows) > height * 0.03
            or min(header.top for header in headers) < canvas_top
            or max(header.bottom for header in headers) - canvas_top > height * 0.08
            or native.no_vertical_scroll(canvas_handle) is not True
        ):
            return False
        # Header bounds establish the body start; the native client rectangle
        # establishes its end, excluding the horizontal scrollbar and buttons.
        body_left = x1 + 4
        if allow_grid_lines:
            # Ignore a manager's painted row-selector gutter. The checked
            # columns still cover every relevant name/description/value row.
            body_left = max(body_left, min(header.left for header in headers) - 4)
        body = (body_left, max(header.bottom for header in headers) + 5, x2 - 4, y2 - 4)
        if (
            body[0] < 0
            or body[1] < 0
            or body[2] > width
            or body[3] > height
            or body[2] - body[0] < width * 0.5
            or body[3] - body[1] < height * 0.2
        ):
            return False
        first = image.convert("RGB").crop(body)
        blank = _blank_grid_body if allow_grid_lines else _blank_body
        if not blank(first):
            return False
        time.sleep(0.15)
        second_image = dialog.capture_as_image()
        if second_image.size != image.size:
            return False
        second = second_image.convert("RGB").crop(body)
        if not blank(second):
            return False
        return first.tobytes() == second.tobytes()
    except Exception:
        return False


def _blank_body(image: Any) -> bool:
    """Require every inspected body pixel to be near-white and uniform."""
    extrema = image.getextrema()
    return all(low >= 248 and high - low <= 3 for low, high in extrema)


def _blank_grid_body(image: Any) -> bool:
    """Accept only white cells and continuous, sparse grid rules.

    A glyph, row highlight, or partial line leaves pixels outside the full
    horizontal and vertical rules and therefore fails closed.
    """

    width, height = image.size
    if width < 100 or height < 100:
        return False
    marked = bytearray(width * height)
    row_counts = [0] * height
    column_counts = [0] * width
    for index, pixel in enumerate(image.convert("RGB").getdata()):
        if min(pixel) >= 235 and max(pixel) - min(pixel) <= 3:
            continue
        marked[index] = 1
        y, x = divmod(index, width)
        row_counts[y] += 1
        column_counts[x] += 1
    horizontal = {
        y for y, count in enumerate(row_counts) if count >= width * 0.95
    }
    vertical = {
        x for x, count in enumerate(column_counts) if count >= height * 0.95
    }
    if len(horizontal) > max(2, height // 15) or len(vertical) > max(2, width // 15):
        return False
    for index, is_marked in enumerate(marked):
        if is_marked:
            y, x = divmod(index, width)
            if y not in horizontal and x not in vertical:
                return False
    return True
