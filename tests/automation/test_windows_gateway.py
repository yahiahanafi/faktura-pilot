from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    AmbiguousControl,
    ElementNotFound,
    ManualReviewRequired,
    TransitionTimeout,
)
from faktura_pilot.automation.resolver import Bounds, ControlQuery, OCRMatch, ResolvedControl
from faktura_pilot.automation.windows import WindowsFakturamaGateway
from tests.automation.test_resolver import FakeElement, FakeRect, FakeWindow
from tests.factories import sample_order


class FakeApplication:
    def __init__(self, window):
        self.window = window

    def top_window(self):
        return self.window

    def windows(self):
        return [self.window]


class RecordingApplication:
    def __init__(self, window, *, connect_error=None):
        self.window = window
        self.connect_error = connect_error
        self.connect_calls = []
        self.start_calls = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self.connect_error is not None:
            raise self.connect_error

    def start(self, executable, **kwargs):
        self.start_calls.append((executable, kwargs))
        if self.window is None:
            self.window = FakeWindow()

    def top_window(self):
        return self.window

    def windows(self):
        return [self.window]


class FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return list(self._windows)


def attached_gateway(window, *, timeout=0.03):
    gateway = WindowsFakturamaGateway(timeout_seconds=timeout, poll_interval_seconds=0.001)
    gateway._application = FakeApplication(window)
    gateway._main_window = window
    return gateway


class WindowsGatewayTests(unittest.TestCase):
    def test_ocr_click_converts_screenshot_bounds_to_screen_coordinates(self):
        window = FakeWindow(bounds=FakeRect(240, 130, 840, 730))
        target = ResolvedControl(
            ControlQuery.one_of("Save"),
            ocr_match=OCRMatch("Save", Bounds(11, 21, 31, 41)),
        )

        WindowsFakturamaGateway._click_control(window, target)

        self.assertEqual(window.screen_clicks, [(261, 161)])

    def test_state_wait_succeeds_after_postcondition_changes(self):
        gateway = WindowsFakturamaGateway(timeout_seconds=0.1, poll_interval_seconds=0.001)
        attempts = 0

        def ready():
            nonlocal attempts
            attempts += 1
            return attempts >= 3

        gateway._wait_until("test state", ready)

        self.assertEqual(attempts, 3)

    def test_state_wait_raises_typed_timeout(self):
        gateway = WindowsFakturamaGateway(timeout_seconds=0.01, poll_interval_seconds=0.001)

        with self.assertRaises(TransitionTimeout):
            gateway._wait_until("never-ready state", lambda: False)

    def test_action_clicks_once_and_waits_for_postcondition(self):
        state = {"saved": False}
        save = FakeElement("Save", "Button", on_click=lambda: state.__setitem__("saved", True))
        gateway = attached_gateway(FakeWindow([save]))

        gateway._click_action(
            ControlQuery.one_of("Save", control_types=("Button",)),
            "save action",
            lambda: state["saved"],
        )

        self.assertEqual(save.click_count, 1)

    def test_ambiguous_action_is_not_dispatched(self):
        first = FakeElement("Save", "Button")
        second = FakeElement("Save", "Button")
        gateway = attached_gateway(FakeWindow([first, second]))

        with self.assertRaises(AmbiguousControl):
            gateway._click_action(
                ControlQuery.one_of("Save", control_types=("Button",)),
                "ambiguous save",
                lambda: True,
            )

        self.assertEqual(first.click_count, 0)
        self.assertEqual(second.click_count, 0)

    def test_uncertain_save_is_not_repeated_and_has_typed_outcome(self):
        save = FakeElement("Save", "Button")
        gateway = attached_gateway(FakeWindow([save]))

        with self.assertRaises(ActionOutcomeUnknown):
            gateway._click_action(
                ControlQuery.one_of("Save", control_types=("Button",)),
                "save action",
                lambda: False,
                save=True,
            )

        self.assertEqual(save.click_count, 1)

    def test_version_is_read_from_eclipse_bundles_metadata_when_exe_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            executable = install / "Fakturama.exe"
            executable.touch()
            config = install / "configuration" / "org.eclipse.equinox.simpleconfigurator"
            config.mkdir(parents=True)
            (config / "bundles.info").write_text(
                "com.sebulli.fakturama.rcp,2.2.0,plugins/com.sebulli.fakturama.rcp_2.2.0.jar,4,false\n",
                encoding="utf-8",
            )
            window = FakeWindow()
            window.process_path = str(executable)
            gateway = WindowsFakturamaGateway()
            gateway._main_window = window

            self.assertEqual(gateway._executable_version(), "2.2.0")

    def test_version_can_be_read_from_fakturama_plugin_jar_name(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            executable = install / "Fakturama.exe"
            executable.touch()
            plugins = install / "plugins"
            plugins.mkdir()
            (plugins / "com.sebulli.fakturama.rcp_2.2.0.jar").touch()
            window = FakeWindow()
            window.process_path = str(executable)
            gateway = WindowsFakturamaGateway()
            gateway._main_window = window

            self.assertEqual(gateway._executable_version(), "2.2.0")

    def test_version_walks_from_bundled_javaw_to_the_fakturama_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory) / "Fakturama2"
            javaw = install / "jre" / "bin" / "javaw.exe"
            javaw.parent.mkdir(parents=True)
            javaw.touch()
            config = install / "configuration" / "org.eclipse.equinox.simpleconfigurator"
            config.mkdir(parents=True)
            (config / "bundles.info").write_text(
                "com.sebulli.fakturama.rcp,2.2.0,plugins/com.sebulli.fakturama.rcp_2.2.0.jar,4,false\n",
                encoding="utf-8",
            )
            window = FakeWindow()
            window.process_path = str(javaw)
            gateway = WindowsFakturamaGateway()
            gateway._main_window = window

            self.assertEqual(gateway._executable_version(), "2.2.0")

    def test_configured_executable_is_used_as_version_fallback_after_title_attach(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            executable = install / "Fakturama.exe"
            executable.touch()
            config = install / "configuration" / "org.eclipse.equinox.simpleconfigurator"
            config.mkdir(parents=True)
            (config / "bundles.info").write_text(
                "com.sebulli.fakturama.rcp,2.2.0,plugins/com.sebulli.fakturama.rcp_2.2.0.jar,4,false\n",
                encoding="utf-8",
            )
            window = FakeWindow()
            window.process_path = None
            gateway = WindowsFakturamaGateway()
            gateway._main_window = window
            gateway._configured_executable = executable

            self.assertEqual(gateway._executable_version(), "2.2.0")

    def test_version_reader_can_be_injected(self):
        executable = Path("Fakturama.exe")
        read_paths = []
        gateway = WindowsFakturamaGateway(
            version_reader=lambda path: read_paths.append(path) or "2.2.0"
        )
        gateway._configured_executable = executable

        self.assertEqual(gateway._executable_version(), "2.2.0")
        self.assertEqual(read_paths, [executable.resolve()])

    def test_configured_install_version_is_reported_when_no_window_is_open(self):
        executable = Path("Fakturama.exe")
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: None,
            desktop_factory=lambda **kwargs: FakeDesktop([]),
            version_reader=lambda path: "2.2.0" if path == executable.resolve() else None,
        )

        checks = gateway.diagnose(executable)

        self.assertEqual(checks["Version"], "2.2.0")
        self.assertEqual(checks["Window"], "no visible Fakturama window found")

    def test_unrelated_protected_process_does_not_make_process_inventory_uncertain(self):
        class FakePsutil:
            class AccessDenied(Exception):
                pass

            class NoSuchProcess(Exception):
                pass

            @staticmethod
            def process_iter(attrs):
                unrelated = SimpleNamespace(info={"name": "MsMpEng.exe"})

                def denied():
                    raise FakePsutil.AccessDenied()

                unrelated.exe = denied
                unrelated.cmdline = denied
                return [unrelated]

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            with patch.dict(sys.modules, {"psutil": FakePsutil}):
                result = WindowsFakturamaGateway._default_process_exists_for_executable(
                    executable
                )

        self.assertIs(result, False)

    def test_candidate_java_process_access_denial_keeps_launch_check_uncertain(self):
        class FakePsutil:
            class AccessDenied(Exception):
                pass

            class NoSuchProcess(Exception):
                pass

            @staticmethod
            def process_iter(attrs):
                return [SimpleNamespace(info={"name": "javaw.exe"}, exe=denied)]

        def denied():
            raise FakePsutil.AccessDenied()

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            with patch.dict(sys.modules, {"psutil": FakePsutil}):
                result = WindowsFakturamaGateway._default_process_exists_for_executable(
                    executable
                )

        self.assertIsNone(result)

    def test_visible_window_is_attached_by_handle_before_executable_path(self):
        window = FakeWindow()
        window.handle = 1234
        window.process_id = 44
        window.is_visible = lambda: True
        app = RecordingApplication(window)
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            gateway = WindowsFakturamaGateway(
                app_factory=lambda **kwargs: app,
                desktop_factory=lambda **kwargs: FakeDesktop([window]),
                process_checker=lambda path: False,
            )

            gateway.attach_or_launch(executable)

        self.assertEqual(app.connect_calls, [{"handle": 1234, "timeout": 15.0}])
        self.assertEqual(app.start_calls, [])
        self.assertIs(gateway._main_window, window)
        self.assertEqual(gateway._configured_executable, executable.resolve())

    def test_diagnose_only_reads_a_visible_window_and_never_starts_or_clicks(self):
        window = FakeWindow(
            [FakeElement("File", "MenuItem"), FakeElement("Data", "MenuItem")]
        )
        window.handle = 1234
        window.process_id = 44
        window.is_visible = lambda: True
        window.process_path = str(Path("Fakturama2") / "jre" / "bin" / "javaw.exe")
        app = RecordingApplication(window)
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([window]),
            version_reader=lambda path: "2.2.0" if path.name == "javaw.exe" else None,
        )

        checks = gateway.diagnose()

        self.assertIn("PID 44", checks["Window"])
        self.assertEqual(checks["Version"], "2.2.0")
        self.assertTrue(checks["Language"].startswith("English"))
        self.assertEqual(app.connect_calls, [{"handle": 1234, "timeout": 15.0}])
        self.assertEqual(app.start_calls, [])
        self.assertEqual([element.click_count for element in window.descendants()], [0, 0])

    def test_ambiguous_visible_windows_fail_closed_without_connecting_or_launching(self):
        windows = [FakeWindow(), FakeWindow()]
        for index, window in enumerate(windows, start=1):
            window.handle = index
            window.is_visible = lambda: True
        app = RecordingApplication(windows[0])
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            gateway = WindowsFakturamaGateway(
                app_factory=lambda **kwargs: app,
                desktop_factory=lambda **kwargs: FakeDesktop(windows),
            )

            with self.assertRaisesRegex(ManualReviewRequired, "multiple visible Fakturama"):
                gateway.attach_or_launch(executable)

        self.assertEqual(app.connect_calls, [])
        self.assertEqual(app.start_calls, [])

    def test_failed_attach_to_detected_window_never_starts_another_instance(self):
        window = FakeWindow()
        window.handle = 1234
        window.is_visible = lambda: True
        app = RecordingApplication(window, connect_error=PermissionError("access denied"))
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([window]),
            process_checker=lambda path: False,
        )

        with self.assertRaisesRegex(Exception, "a second instance was not started"):
            gateway.attach_or_launch()

        self.assertEqual(app.start_calls, [])

    def test_path_connect_failure_does_not_launch_if_process_presence_is_unknown(self):
        app = RecordingApplication(None, connect_error=PermissionError("access denied"))
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([]),
            process_checker=lambda path: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()

            with self.assertRaisesRegex(Exception, "could not confirm that Fakturama is stopped"):
                gateway.attach_or_launch(executable)

        self.assertEqual(app.start_calls, [])

    def test_path_connect_failure_does_not_launch_if_matching_process_is_detected(self):
        app = RecordingApplication(None, connect_error=PermissionError("access denied"))
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([]),
            process_checker=lambda path: True,
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()

            with self.assertRaisesRegex(Exception, "matching Fakturama process is running"):
                gateway.attach_or_launch(executable)

        self.assertEqual(app.start_calls, [])

    def test_path_connect_failure_can_launch_only_after_a_negative_process_check(self):
        app = RecordingApplication(None, connect_error=RuntimeError("no matching process"))
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([]),
            process_checker=lambda path: False,
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()

            gateway.attach_or_launch(executable)

        self.assertEqual(app.start_calls, [(str(executable.resolve()), {"timeout": 15.0})])

    def test_order_header_maps_source_reference_date_and_modes(self):
        source = sample_order()
        fields = [
            FakeElement("No.", "Edit", value="ORD-100"),
            FakeElement("Date", "Edit", value=""),
            FakeElement("Cust.Ref.", "Edit", value=""),
            FakeElement("Price mode", "ComboBox", value=""),
            FakeElement("VAT mode", "ComboBox", value=""),
        ]
        window = FakeWindow(fields)
        gateway = attached_gateway(window)

        gateway.fill_order_header(source)

        self.assertEqual(fields[1].get_value(), source.order_date.isoformat())
        self.assertEqual(fields[2].get_value(), source.external_reference)
        self.assertEqual(fields[3].get_value(), "Net")
        self.assertEqual(fields[4].get_value(), "With VAT")

    def test_address_fill_and_readback_preserve_addressee_and_specification_separately(self):
        source = sample_order()
        address = source.debtor.delivery_address
        fields = [
            FakeElement("Street", "Edit", value=""),
            FakeElement("ZIP", "Edit", value=""),
            FakeElement("City", "Edit", value=""),
            FakeElement("Country", "Edit", value=""),
            FakeElement("Additional name", "Edit", value=""),
            FakeElement("Address specification", "Edit", value=""),
        ]
        gateway = attached_gateway(FakeWindow(fields))

        gateway._fill_address(address, context=())
        result = gateway._verify_address_fields(address, context=())

        self.assertTrue(result.verified, result.observations)
        self.assertEqual(fields[4].get_value(), "Northstar Office Warehouse")
        self.assertEqual(fields[5].get_value(), "Receiving dock")

    def test_selected_debtor_verification_requires_both_address_addressees(self):
        debtor = sample_order().debtor
        gateway = WindowsFakturamaGateway()
        billing = gateway._address_string(debtor.billing_address)
        delivery = gateway._address_string(debtor.delivery_address)
        without_addressee = delivery.replace("Northstar Office Warehouse, ", "")

        with (
            patch.object(gateway, "_read_optional", return_value=debtor.company),
            patch.object(
                gateway,
                "_read_address_display",
                side_effect=[billing, without_addressee],
            ),
        ):
            result = gateway.verify_order_debtor(debtor)

        self.assertFalse(result.verified)
        self.assertTrue(any("delivery address" in issue for issue in result.observations))

    def test_invoice_copy_verification_requires_the_source_order_number(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        values = {
            "Cust.Ref.": source.external_reference,
            "Gross total": str(source.totals.gross),
            "Order No.": None,
        }

        def read_field(labels):
            return next((values[label] for label in labels if label in values), None)

        with patch.object(gateway, "_read_optional", side_effect=read_field):
            result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertFalse(result.verified)
        self.assertTrue(any("source Order number" in issue for issue in result.observations))

    def test_order_line_mapping_uses_extracted_quantity_price_vat_discount_and_net(self):
        source = sample_order()

        mapped = WindowsFakturamaGateway._expected_line_values(source.items[0])

        self.assertEqual(
            mapped,
            {"Qty.": "2", "U.Price": "250.00", "VAT": "19", "Discount": "10", "Price": "450.00"},
        )

    def test_order_line_lookup_uses_exact_sku_not_a_prefix_match(self):
        gateway = WindowsFakturamaGateway()
        prefix_row = object()
        exact_row = object()
        gateway._list_rows = lambda: [prefix_row, exact_row]
        gateway._row_values = lambda row: (
            {"Item Number": "A-10" if row is prefix_row else "A-1"},
            "A-10" if row is prefix_row else "A-1",
        )

        self.assertIs(gateway._find_order_line_row("A-1"), exact_row)

    def test_order_line_lookup_never_mutates_a_prefix_only_row(self):
        gateway = WindowsFakturamaGateway()
        prefix_row = object()
        gateway._list_rows = lambda: [prefix_row]
        gateway._row_values = lambda row: ({"Item Number": "A-10"}, "A-10")

        with self.assertRaises(ElementNotFound):
            gateway._find_order_line_row("A-1")

    def test_order_line_lookup_pauses_when_matching_row_has_no_exact_sku_cell(self):
        gateway = WindowsFakturamaGateway()
        row = object()
        gateway._list_rows = lambda: [row]
        gateway._row_values = lambda active: ({"Qty.": "2"}, "A-1 Widget 2")

        with self.assertRaises(ManualReviewRequired):
            gateway._find_order_line_row("A-1")

    def test_date_readback_rejects_ambiguous_numeric_format(self):
        self.assertTrue(WindowsFakturamaGateway._date_matches(date(2026, 7, 18), "18/07/2026"))
        self.assertFalse(WindowsFakturamaGateway._date_matches(date(2026, 7, 4), "04/07/2026"))

    def test_invoice_copy_verification_requires_visible_order_link(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        values = iter((source.external_reference, str(source.totals.gross), None))
        gateway._read_optional = lambda labels: next(values)

        result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertFalse(result.verified)
        self.assertIn("Invoice's source Order number could not be read back", result.observations)

    def test_open_invoice_discovery_rejects_wrong_or_unreadable_order_link(self):
        source = sample_order()
        window = FakeWindow()
        gateway = WindowsFakturamaGateway()
        gateway._application_windows = lambda: [window]
        gateway._window_title = lambda active: "Invoice editor"

        def read_wrong_link(active, labels):
            if "Cust.Ref." in labels:
                return source.external_reference
            return "ORD-999"

        gateway._read_from_window = read_wrong_link
        with self.assertRaises(ManualReviewRequired):
            gateway.discover_open_invoice(source, "ORD-100")

        def unreadable_link(active, labels):
            if "Cust.Ref." in labels:
                return source.external_reference
            return None

        gateway._read_from_window = unreadable_link
        with self.assertRaises(ManualReviewRequired):
            gateway.discover_open_invoice(source, "ORD-100")

    def test_open_invoice_discovery_records_exact_visible_order_link(self):
        source = sample_order()
        window = FakeWindow()
        gateway = WindowsFakturamaGateway()
        gateway._application_windows = lambda: [window]
        gateway._window_title = lambda active: "Invoice editor"

        def read_exact_link(active, labels):
            if "Cust.Ref." in labels:
                return source.external_reference
            if "No." in labels:
                return "INV-100"
            return "ORD-100"

        gateway._read_from_window = read_exact_link

        ref = gateway.discover_open_invoice(source, "ORD-100")

        self.assertIsNotNone(ref)
        self.assertEqual(ref.number, "INV-100")
        self.assertEqual(ref.linked_order_number, "ORD-100")

    def test_open_order_discovery_pauses_when_reference_is_ambiguous(self):
        source = sample_order()
        windows = [FakeWindow(), FakeWindow()]
        gateway = WindowsFakturamaGateway()
        gateway._application_windows = lambda: windows
        gateway._window_title = lambda active: "New Order"

        def read_order(active, labels):
            if "Cust.Ref." in labels:
                return source.external_reference
            return "ORD-100" if active is windows[0] else "ORD-101"

        gateway._read_from_window = read_order

        with self.assertRaises(ManualReviewRequired):
            gateway.discover_open_order(source)


if __name__ == "__main__":
    unittest.main()
