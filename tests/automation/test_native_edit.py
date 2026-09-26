from __future__ import annotations

import ctypes
import unittest

from faktura_pilot.automation.native_edit import native_edit_text


class FakeNativeEdit:
    def __init__(self, *, text: str | None = "", class_name: str = "Edit", valid=True):
        self.text = text
        self.control_class = class_name
        self.valid = valid
        self.calls: list[tuple[str, int]] = []

    def is_window(self, handle: int) -> bool:
        self.calls.append(("is_window", handle))
        return self.valid

    def class_name(self, handle: int) -> str:
        self.calls.append(("class_name", handle))
        return self.control_class

    def window_text(self, handle: int) -> str | None:
        self.calls.append(("window_text", handle))
        return self.text


class NativeEditTests(unittest.TestCase):
    def test_invalid_or_missing_handle_never_calls_native_reader(self):
        native = FakeNativeEdit(text="ignored")
        oversized = 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)
        for handle in (None, 0, -1, True, "123", oversized):
            with self.subTest(handle=handle):
                self.assertIsNone(native_edit_text(handle, native=native))
        self.assertEqual(native.calls, [])

    def test_stale_handle_and_other_control_class_are_unavailable(self):
        stale = FakeNativeEdit(text="stale", valid=False)
        self.assertIsNone(native_edit_text(123, native=stale))
        self.assertEqual(stale.calls, [("is_window", 123)])

        combo = FakeNativeEdit(text="named control", class_name="ComboBox")
        self.assertIsNone(native_edit_text(123, native=combo))
        self.assertEqual(
            combo.calls, [("is_window", 123), ("class_name", 123)]
        )

    def test_native_edit_returns_exact_unicode_text_without_trimming(self):
        value = "  VAT 19% — München  "
        native = FakeNativeEdit(text=value)
        self.assertEqual(native_edit_text(123, native=native), value)
        self.assertEqual(
            native.calls,
            [
                ("is_window", 123),
                ("class_name", 123),
                ("window_text", 123),
            ],
        )

    def test_native_edit_blank_is_distinct_from_unavailable(self):
        blank = FakeNativeEdit(text="")
        unavailable = FakeNativeEdit(text=None)
        self.assertEqual(native_edit_text(123, native=blank), "")
        self.assertIsNone(native_edit_text(123, native=unavailable))

    def test_native_read_error_fails_closed(self):
        native = FakeNativeEdit(text="stale")

        def fail(_handle: int) -> str:
            raise OSError("window disappeared")

        native.window_text = fail
        self.assertIsNone(native_edit_text(123, native=native))


if __name__ == "__main__":
    unittest.main()

