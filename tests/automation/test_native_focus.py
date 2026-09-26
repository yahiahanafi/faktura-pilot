import unittest

from faktura_pilot.automation.native_focus import ensure_foreground, top_level_handle


class FakeForeground:
    target = 10
    other = 20

    def __init__(self, mode="direct", *, minimized=False, enabled=True, valid=True):
        self.mode = mode
        self.minimized = minimized
        self.enabled = enabled
        self.valid = valid
        self.active = self.other
        self.attached = set()
        self.calls = []
        self.set_count = 0

    def is_window(self, handle):
        return self.valid and handle == self.target

    def is_enabled(self, handle):
        return self.enabled

    def is_iconic(self, handle):
        return self.minimized

    def show_window_async(self, handle):
        self.calls.append(("restore", handle))
        self.minimized = False
        return True

    def foreground(self):
        return self.active

    def set_foreground(self, handle):
        self.calls.append(("foreground", handle))
        self.set_count += 1
        if self.mode == "direct" or (self.mode == "attached" and self.attached == {100, 200}):
            self.active = handle
        if self.mode == "raise" and self.set_count == 2:
            raise OSError("activation failed")
        return self.active == handle

    def bring_to_top(self, handle):
        self.calls.append(("top", handle))
        return True

    def window_thread(self, handle):
        return {self.other: 100, self.target: 200}.get(handle, 0)

    def current_thread(self):
        return 300

    def attach_input(self, source, target, attach):
        self.calls.append(("attach", source, target, attach))
        if attach and self.mode == "attach_blocked" and target == 200:
            return False
        if not attach and self.mode == "detach_blocked":
            return False
        if attach:
            self.attached.add(target)
        else:
            self.attached.remove(target)
        return True


class FakeAncestors:
    def __init__(self, roots=None, *, failure=False):
        self.roots = {10: 10, 11: 10} if roots is None else roots
        self.failure = failure

    def is_window(self, handle):
        return handle in self.roots

    def root_ancestor(self, handle):
        if self.failure:
            raise OSError("window disappeared")
        return self.roots[handle]


class NativeFocusTests(unittest.TestCase):
    def test_top_level_handle_resolves_child_to_exact_root(self):
        native = FakeAncestors()
        self.assertEqual(top_level_handle(11, native=native), 10)
        self.assertEqual(top_level_handle(10, native=native), 10)
        self.assertIsNone(top_level_handle(12, native=native))

    def test_top_level_handle_rejects_stale_or_invalid_root(self):
        for handle in (0, -1, True, "11", None):
            with self.subTest(handle=handle):
                self.assertIsNone(top_level_handle(handle, native=FakeAncestors()))
        self.assertIsNone(top_level_handle(11, native=FakeAncestors({11: 0})))
        self.assertIsNone(top_level_handle(11, native=FakeAncestors({11: 99})))
        self.assertIsNone(top_level_handle(11, native=FakeAncestors(failure=True)))

    def test_rejects_invalid_or_disabled_target_without_activation(self):
        for handle in (0, -1, True, "10", None):
            with self.subTest(handle=handle):
                self.assertFalse(ensure_foreground(handle, native=FakeForeground(), timeout=0))
        for native in (FakeForeground(valid=False), FakeForeground(enabled=False)):
            self.assertFalse(ensure_foreground(10, native=native, timeout=0))
            self.assertEqual(native.calls, [])

    def test_already_foreground_does_not_repeat_activation(self):
        native = FakeForeground()
        native.active = native.target
        self.assertTrue(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.calls, [])

    def test_minimized_target_is_restored_and_direct_activation_verified(self):
        native = FakeForeground(minimized=True)
        self.assertTrue(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.calls, [("restore", 10), ("foreground", 10)])

    def test_fallback_attaches_then_detaches_both_input_queues(self):
        native = FakeForeground(mode="attached")
        self.assertTrue(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.active, 10)
        self.assertEqual(native.attached, set())
        self.assertEqual(
            [call for call in native.calls if call[0] == "attach"],
            [
                ("attach", 300, 100, True),
                ("attach", 300, 200, True),
                ("attach", 300, 200, False),
                ("attach", 300, 100, False),
            ],
        )

    def test_blocked_activation_fails_closed_and_cleans_up(self):
        native = FakeForeground(mode="blocked")
        self.assertFalse(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.active, native.other)
        self.assertEqual(native.attached, set())

    def test_partial_attachment_failure_detaches_successful_attachment(self):
        native = FakeForeground(mode="attach_blocked")
        self.assertFalse(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.attached, set())
        self.assertEqual(native.calls[-1], ("attach", 300, 100, False))

    def test_native_error_during_fallback_detaches_before_returning_false(self):
        native = FakeForeground(mode="raise")
        self.assertFalse(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.attached, set())

    def test_failed_detachment_cannot_report_success(self):
        native = FakeForeground(mode="detach_blocked")
        native.mode = "attached"
        original_attach = native.attach_input

        def fail_detach(source, target, attach):
            if not attach:
                native.calls.append(("attach", source, target, attach))
                native.attached.discard(target)
                return False
            return original_attach(source, target, attach)

        native.attach_input = fail_detach
        self.assertFalse(ensure_foreground(10, native=native, timeout=0))
        self.assertEqual(native.attached, set())


if __name__ == "__main__":
    unittest.main()
