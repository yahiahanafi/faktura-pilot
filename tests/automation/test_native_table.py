import unittest
from types import SimpleNamespace
from unittest.mock import patch

from faktura_pilot.automation.native_table import (
    unique_table_row_count,
    verified_empty_selector,
)
from faktura_pilot.automation.resolver import Bounds


class FakeNative:
    def __init__(
        self,
        *,
        count=0,
        classes=None,
        visible=None,
        enabled=None,
        pids=None,
        scroll=None,
        client=None,
    ):
        self.count = count
        self.classes = {2: "SysListView32"} if classes is None else classes
        self.visible = visible or {}
        self.enabled = enabled or {}
        self.pids = pids or {}
        self.queries = []
        self.scroll = scroll or {}
        self.client = client or {}

    def window_rect(self, handle):
        return (451, 142, 1091, 582) if handle == 1 else self.client.get(handle)

    def client_rect(self, handle):
        return self.client.get(handle)

    def no_vertical_scroll(self, handle):
        return self.scroll.get(handle, True)

    def is_window(self, handle):
        return handle in {1, *self.classes}

    def is_visible(self, handle):
        return self.visible.get(handle, True)

    def is_enabled(self, handle):
        return self.enabled.get(handle, True)

    def process_id(self, handle):
        return self.pids.get(handle, 42)

    def child_windows(self, handle):
        return list(self.classes) if handle == 1 else []

    def class_name(self, handle):
        return self.classes[handle]

    def item_count(self, handle):
        self.queries.append(handle)
        return self.count


class NativeTableTests(unittest.TestCase):
    def test_unique_native_table_verifies_zero_or_positive_rows(self):
        native = FakeNative(count=0)
        self.assertEqual(unique_table_row_count(SimpleNamespace(handle=1), native=native), 0)
        self.assertEqual(native.queries, [2])
        native.count = 3
        self.assertEqual(unique_table_row_count(SimpleNamespace(handle=1), native=native), 3)

    def test_missing_ambiguous_or_wrong_class_never_means_empty(self):
        cases = (
            FakeNative(classes={}),
            FakeNative(classes={2: "SWT_Window0"}),
            FakeNative(classes={2: "SysListView32", 3: "SysListView32"}),
        )
        for native in cases:
            with self.subTest(classes=native.classes):
                self.assertIsNone(unique_table_row_count(SimpleNamespace(handle=1), native=native))
                self.assertEqual(native.queries, [])

    def test_hidden_disabled_or_foreign_process_table_is_unknown(self):
        cases = (
            FakeNative(visible={2: False}),
            FakeNative(enabled={2: False}),
            FakeNative(pids={2: 99}),
            FakeNative(visible={1: False}),
            FakeNative(enabled={1: False}),
        )
        for native in cases:
            with self.subTest(native=native.__dict__):
                self.assertIsNone(unique_table_row_count(SimpleNamespace(handle=1), native=native))
                self.assertEqual(native.queries, [])

    def test_blank_swt_canvas_requires_stable_body_and_no_vertical_scroll(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        native = FakeNative(
            classes={2: "SWT_Window0", 3: "SWT_Window0"},
            client={2: (458, 202, 1079, 510), 3: (458, 170, 1079, 525)},
        )
        headers = [
            Bounds(210, 160, 280, 188),
            Bounds(660, 160, 760, 188),
            Bounds(1410, 160, 1510, 188),
        ]
        first = Image.new("RGB", (1600, 1100), "white")
        second = first.copy()
        dialog = SimpleNamespace(handle=1, capture_as_image=lambda: second)
        with patch("faktura_pilot.automation.native_table.time.sleep"):
            self.assertTrue(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )
            self.assertFalse(
                verified_empty_selector(
                    dialog, first, headers, search_verified=False, native=native
                )
            )
            first.putpixel((800, 400), (0, 0, 0))
            self.assertFalse(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )
            first.putpixel((800, 400), (255, 255, 255))
            second.putpixel((800, 400), (0, 0, 0))
            self.assertFalse(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )
            second.putpixel((800, 400), (255, 255, 255))
            self.assertFalse(
                verified_empty_selector(
                    dialog,
                    first,
                    [*headers[:2], Bounds(1700, 160, 1800, 188)],
                    search_verified=True,
                    native=native,
                )
            )
            body_level_headers = [Bounds(header.left, 400, header.right, 428) for header in headers]
            self.assertFalse(
                verified_empty_selector(
                    dialog, first, body_level_headers, search_verified=True, native=native
                )
            )
            native.scroll[2] = False
            self.assertFalse(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )
            native.scroll[2] = True
            native.client[3] = native.client[2]
            self.assertFalse(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )
            native.client[3] = (458, 170, 1079, 525)
            native.enabled[1] = False
            self.assertFalse(
                verified_empty_selector(dialog, first, headers, search_verified=True, native=native)
            )

    def test_embedded_grid_accepts_only_stable_empty_grid_rules(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        native = FakeNative(
            count=0,
            classes={2: "SWT_Window0", 3: "SysListView32"},
            client={2: (458, 202, 1079, 510)},
        )
        headers = [
            Bounds(210, 160, 280, 188),
            Bounds(660, 160, 760, 188),
            Bounds(1410, 160, 1510, 188),
        ]
        first = Image.new("RGB", (1600, 1100), "white")
        draw = ImageDraw.Draw(first)
        draw.rectangle((206, 194, 800, 245), fill=(240, 240, 240))
        for y in (250, 500, 750):
            draw.line((200, y, 1570, y), fill=(225, 225, 225))
        for x in (400, 800):
            draw.line((x, 190, x, 930), fill=(225, 225, 225))
        second = first.copy()
        parent = SimpleNamespace(
            handle=1, is_visible=lambda: False,
            capture_as_image=lambda: second,
        )
        with patch("faktura_pilot.automation.native_table.time.sleep"):
            # A generic selector would trust the unrelated native ListView.
            # Grid mode must skip it and inspect the scoped SWT canvas.
            self.assertTrue(
                verified_empty_selector(
                    parent, first, headers, search_verified=True, native=native
                )
            )
            self.assertTrue(
                verified_empty_selector(
                    parent, first, headers, search_verified=True,
                    native=native, allow_grid_lines=True,
                )
            )
            self.assertEqual(native.queries, [3])
            compact_headers = [
                Bounds(210, 160, 250, 188),
                Bounds(300, 160, 340, 188),
                Bounds(410, 160, 450, 188),
            ]
            self.assertTrue(
                verified_empty_selector(
                    parent, first, compact_headers, search_verified=True,
                    native=native, allow_grid_lines=True,
                )
            )
            native.count = None
            self.assertFalse(
                verified_empty_selector(
                    parent, first, compact_headers, search_verified=True,
                    native=native,
                )
            )
            first_with_text = first.copy()
            ImageDraw.Draw(first_with_text).rectangle(
                (420, 310, 435, 324), fill=(0, 0, 0)
            )
            self.assertFalse(
                verified_empty_selector(
                    parent, first_with_text, headers, search_verified=True,
                    native=native, allow_grid_lines=True,
                )
            )
            second.putpixel((450, 310), (0, 0, 0))
            self.assertFalse(
                verified_empty_selector(
                    parent, first, headers, search_verified=True,
                    native=native, allow_grid_lines=True,
                )
            )
            second.putpixel((450, 310), (255, 255, 255))
            native.scroll[2] = False
            self.assertFalse(
                verified_empty_selector(
                    parent, first, headers, search_verified=True,
                    native=native, allow_grid_lines=True,
                )
            )

    def test_invalid_selector_or_native_query_failure_is_unknown(self):
        for handle in (None, 0, -1, "invalid"):
            with self.subTest(handle=handle):
                self.assertIsNone(
                    unique_table_row_count(SimpleNamespace(handle=handle), native=FakeNative())
                )
        native = FakeNative(count=None)
        self.assertIsNone(unique_table_row_count(SimpleNamespace(handle=1), native=native))
        native.count = -1
        self.assertIsNone(unique_table_row_count(SimpleNamespace(handle=1), native=native))

        def fail(_handle):
            raise OSError("stale window")

        native.item_count = fail
        self.assertIsNone(unique_table_row_count(SimpleNamespace(handle=1), native=native))


if __name__ == "__main__":
    unittest.main()
