from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    AmbiguousControl,
    DebtorCandidate,
    DocumentRow,
    ElementNotFound,
    GatewayError,
    InvoiceEditorRef,
    ManualReviewRequired,
    PostconditionFailed,
    TransitionTimeout,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.automation.resolver import (
    Bounds,
    ControlQuery,
    OCRMatch,
    ResolvedControl,
    TesseractOCR,
)
from faktura_pilot.automation.windows import WindowsFakturamaGateway, _parse_date_text
from tests.automation.test_resolver import FakeElement, FakeOCR, FakeRect, FakeWindow
from tests.factories import sample_order


class FakeApplication:
    def __init__(self, window):
        self.window = window
        self._windows = [window]

    def top_window(self):
        return self.window

    def windows(self):
        return list(self._windows)


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
        self.connect_error = None
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


def document_editor(*fields, name="New Order", kind="Order"):
    editor = VisibleElement(name, "Tab")
    editor.add(
        FakeElement("No.", "Text"),
        FakeElement("Cust.Ref.", "Text"),
        FakeElement(kind, "Text"),
        *fields,
    )
    return editor


def order_window_with_address_images(*, image_count=2):
    pane = FakeElement("", "Pane", bounds=FakeRect(792, 537, 928, 703))
    pane.add(FakeElement("Addresses", "Text", bounds=FakeRect(798, 538, 872, 556)))
    images = []
    image_tops = (550, 585, 620)
    for index in range(image_count):
        image = FakeElement(
            "",
            "Image",
            bounds=FakeRect(810, image_tops[index], 830, image_tops[index] + 20),
        )
        image.is_visible = lambda: True
        image.is_enabled = lambda: True
        pane.add(image)
        images.append(image)
    window = FakeWindow([
        document_editor(
            FakeElement("No.", "Edit", value="ORD-100"),
            FakeElement("Date", "Edit", value=""),
            FakeElement("Cust.Ref.", "Edit", value=""),
            FakeElement("Invoice address", "Tab"),
            pane,
        )
    ])
    return window, pane, images


def contact_selector_window(title, company, zip_code, city):
    window = FakeWindow()
    window.element_info.name = title
    search = FakeElement("Search", "Edit", value="")
    grid = FakeElement("", "DataGrid")
    headers = [
        FakeElement("Company", "HeaderItem", bounds=FakeRect(0, 0, 100, 20)),
        FakeElement("First Name", "HeaderItem", bounds=FakeRect(100, 0, 150, 20)),
        FakeElement("Name", "HeaderItem", bounds=FakeRect(150, 0, 200, 20)),
        FakeElement("ZIP", "HeaderItem", bounds=FakeRect(200, 0, 250, 20)),
        FakeElement("City", "HeaderItem", bounds=FakeRect(250, 0, 320, 20)),
    ]
    row = FakeElement("", "DataItem")
    row.add(
        FakeElement(company, "Text", bounds=FakeRect(0, 30, 100, 50)),
        FakeElement("", "Text", bounds=FakeRect(100, 30, 150, 50)),
        FakeElement("", "Text", bounds=FakeRect(150, 30, 200, 50)),
        FakeElement(zip_code, "Text", bounds=FakeRect(200, 30, 250, 50)),
        FakeElement(city, "Text", bounds=FakeRect(250, 30, 320, 50)),
    )
    grid.add(*headers, row)
    window.add(search, FakeElement("OK", "Button"), FakeElement("Cancel", "Button"), grid)
    return window, search, row


def product_editor_window(*, omitted=()):
    fields = {
        "Item Number": FakeElement("Item Number", "Edit", value=""),
        "Name": FakeElement("Name", "Edit", value=""),
        "Description": FakeElement("Description", "Edit", value=""),
        "Price (gross)": FakeElement("Price (gross)", "Edit", value="£0.00"),
        "cost price (net)": FakeElement("cost price (net)", "Edit", value="£0.00"),
        "VAT": FakeElement("VAT", "ComboBox", value="Free of Tax"),
        "Stock": FakeElement("Stock", "Edit", value="0.00"),
        "Category": FakeElement("Category", "ComboBox", value="Keep category"),
        "GTIN": FakeElement("GTIN", "Edit", value="KEEP-GTIN"),
        "supplier code": FakeElement("supplier code", "Edit", value="KEEP-SUPPLIER"),
        "allowance": FakeElement("allowance", "Edit", value="KEEP-ALLOWANCE"),
        "user defined field 1": FakeElement("user defined field 1", "Edit", value="KEEP-1"),
        "user defined field 2": FakeElement("user defined field 2", "Edit", value="KEEP-2"),
        "user defined field 3": FakeElement("user defined field 3", "Edit", value="KEEP-3"),
        "Select a Picture": FakeElement("Select a Picture", "Hyperlink"),
        "delete picture": FakeElement("delete", "Hyperlink"),
    }
    window = FakeWindow(
        [element for name, element in fields.items() if name not in omitted]
    )
    return window, fields


class KeyboardDateField(FakeElement):
    def __init__(self, *args, committed_value: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.committed_value = committed_value
        self.key_calls: list[tuple[str, dict[str, object]]] = []
        self.click_calls: list[dict[str, object]] = []

    def click_input(self, *args, **kwargs) -> None:
        self.click_calls.append(kwargs)
        super().click_input(*args, **kwargs)

    def type_keys(self, keys: str, **kwargs) -> None:
        self.key_calls.append((keys, kwargs))
        if keys == "{TAB}":
            self._value = self.committed_value
        else:
            self._value = keys


class VisibleElement(FakeElement):
    def is_visible(self):
        return True

    def is_enabled(self):
        return True


class RoleCheckbox(VisibleElement):
    def __init__(self, *args, checked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.checked = checked

    def is_checked(self):
        return self.checked

    def click_input(self, *args, **kwargs):
        super().click_input(*args, **kwargs)
        self.checked = not self.checked


class WindowsGatewayTests(unittest.TestCase):
    def test_selector_wrappers_for_one_hwnd_are_one_visible_dialog(self):
        first = FakeWindow()
        second = FakeWindow()
        for window in (first, second):
            window.element_info.name = "Select the address"
            window.handle = 42
        gateway = WindowsFakturamaGateway()
        gateway._current_window = Mock(return_value=first)
        gateway._application_windows = Mock(return_value=[second])

        found = gateway._visible_dialog_root(("Select the address",))

        self.assertIs(found, second)

    def test_distinct_zero_handle_selectors_remain_ambiguous(self):
        first = FakeWindow()
        second = FakeWindow()
        for window in (first, second):
            window.element_info.name = "Select the address"
            window.handle = 0
        gateway = WindowsFakturamaGateway()
        gateway._current_window = Mock(return_value=first)
        gateway._application_windows = Mock(return_value=[second])

        with self.assertRaisesRegex(ManualReviewRequired, "multiple visible windows"):
            gateway._visible_dialog_root(("Select the address",))

    def test_existing_new_order_draft_blocks_another_toolbar_click(self):
        tab = VisibleElement("*New Order", "TabItem")
        gateway = attached_gateway(FakeWindow([tab]))
        gateway._visible_dialog_root = Mock(return_value=None)
        gateway._resolve_toolbar_order = Mock()

        with self.assertRaisesRegex(ManualReviewRequired, "already open"):
            gateway.open_new_order()

        gateway._resolve_toolbar_order.assert_not_called()

    def test_saved_invoice_editor_allows_opening_a_new_order(self):
        saved_tab = VisibleElement("IN000008", "TabItem")
        saved_editor = document_editor(
            FakeElement("No.", "Edit", value="IN000008"),
            FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
            name="IN000008",
            kind="Invoice",
        )
        window = FakeWindow([saved_tab, saved_editor])
        gateway = attached_gateway(window)
        gateway._visible_dialog_root = Mock(return_value=None)
        gateway._has_order_editor = Mock(return_value=True)
        toolbar = ResolvedControl(
            ControlQuery.one_of("Order"),
            element=VisibleElement("Order", "Button"),
        )
        gateway._resolve_toolbar_order = Mock(return_value=toolbar)

        def open_editor(*_args):
            window._children = [
                saved_tab,
                VisibleElement("*New Order", "TabItem"),
                document_editor(FakeElement("No.", "Edit", value="")),
            ]

        gateway._click_control = Mock(side_effect=open_editor)
        gateway._read_optional = Mock(return_value="")

        ref = gateway.open_new_order()

        self.assertIsNotNone(ref.token)
        gateway._resolve_toolbar_order.assert_called_once_with(window)
        gateway._click_control.assert_called_once_with(window, toolbar)

    def test_order_toolbar_ocr_stays_above_actual_document_tabs(self):
        tab = FakeElement("Fakturama", "TabItem", bounds=FakeRect(0, 140, 400, 170))
        tab.is_visible = lambda: True
        window = FakeWindow([tab], bounds=FakeRect(0, 0, 1000, 800))
        gateway = attached_gateway(window)
        toolbar = OCRMatch("Order", Bounds(100, 110, 160, 130))
        document_tab = OCRMatch("Order", Bounds(100, 180, 160, 200))
        gateway.ocr = FakeOCR([toolbar, document_tab])

        resolved = gateway._resolve_toolbar_order(window)

        self.assertEqual(resolved.ocr_match, toolbar)
        self.assertEqual(len(gateway.ocr.calls), 1)

    def test_order_toolbar_focuses_before_ocr_capture(self):
        window = FakeWindow([], bounds=FakeRect(0, 0, 1000, 800))
        focused = []
        window.set_focus = lambda: focused.append(True)
        original_capture = window.capture_as_image

        def capture():
            self.assertTrue(focused, "OCR must not capture an obscuring application")
            return original_capture()

        window.capture_as_image = capture
        gateway = attached_gateway(window)
        gateway.ocr = FakeOCR([OCRMatch("Order", Bounds(100, 60, 160, 80))])
        gateway._resolve_toolbar_order(window)

    def test_click_refuses_to_dispatch_when_focus_did_not_change(self):
        window = FakeWindow()
        window.set_focus = Mock()
        button = FakeElement("Order", "Button")
        target = ResolvedControl(ControlQuery.one_of("Order"), element=button)
        with patch.object(WindowsFakturamaGateway, "_foreground_matches", return_value=False):
            with self.assertRaisesRegex(ManualReviewRequired, "foreground window"):
                WindowsFakturamaGateway._click_control(window, target)
        self.assertEqual(button.click_count, 0)
        window.set_focus.assert_called_once()

    def test_order_toolbar_uses_accessible_create_new_order_without_ocr(self):
        button = VisibleElement("Create: New Order", "Button", bounds=FakeRect(100, 40, 160, 80))
        window = FakeWindow([button], bounds=FakeRect(0, 0, 1000, 800))
        gateway = attached_gateway(window)
        gateway.ocr = FakeOCR([])
        self.assertIs(gateway._resolve_toolbar_order(window).element, button)
        self.assertEqual(gateway.ocr.calls, [])

    def test_order_sku_ocr_preserves_case_and_punctuation(self):
        gateway = WindowsFakturamaGateway()
        exact = OCRMatch("CHR-ERG-01", Bounds(100, 50, 210, 70))
        similar = [
            OCRMatch("chr-erg-01", Bounds(100, 80, 210, 100)),
            OCRMatch("CHRERG01", Bounds(100, 110, 210, 130)),
        ]

        found = gateway._visible_order_sku_word(
            "CHR-ERG-01",
            [*similar, exact],
            SimpleNamespace(left=0, top=0),
            Bounds(0, 0, 600, 200),
            Bounds(0, 0, 60, 200),
        )

        self.assertIs(found, exact)

    def test_read_only_field_verification_scans_one_uia_snapshot(self):
        window = FakeWindow(
            [
                FakeElement("Date", "Edit", value="14 Jul 2026"),
                FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
                FakeElement("No.", "Edit", value="ORD-100"),
            ]
        )
        gateway = attached_gateway(window)
        original_descendants = window.descendants
        scan_count = 0

        def counted_descendants():
            nonlocal scan_count
            scan_count += 1
            return original_descendants()

        window.descendants = counted_descendants
        result = gateway._verify_fields(
            {"Date": "2026-07-14", "Cust.Ref.": "WEB-100", "No.": "ORD-100"},
            step="Order header",
        )

        self.assertTrue(result.verified)
        self.assertEqual(scan_count, 1)

    def test_bounded_field_snapshot_scans_once_and_keeps_exact_readback(self):
        fields = [
            FakeElement(label, "Edit", value="")
            for label in ("Name", "Alias", "Description")
        ]
        window = FakeWindow(fields)
        gateway = attached_gateway(window)
        original_descendants = window.descendants
        scans = 0

        def counted_descendants():
            nonlocal scans
            scans += 1
            return original_descendants()

        window.descendants = counted_descendants
        resolver = gateway._resolver().freeze()
        for field in fields:
            gateway._set_field(
                (field.element_info.name,), field.element_info.name, resolver=resolver
            )

        self.assertEqual(scans, 1)
        self.assertEqual([field.get_value() for field in fields], ["Name", "Alias", "Description"])

    def test_missing_editor_reports_visible_internal_error_text(self):
        dialog = FakeWindow([FakeElement("Currency mismatch: EUR/GBP", "Text")])
        dialog.element_info.name = "Internal Error"
        gateway = attached_gateway(FakeWindow())
        gateway._has_order_editor = Mock(return_value=False)
        gateway._application_windows = Mock(return_value=[dialog])

        with self.assertRaisesRegex(ManualReviewRequired, "Currency mismatch: EUR/GBP"):
            gateway._require_editor("order")

    def test_display_currency_does_not_change_amount_verification(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        values = {
            "net": source.totals.net,
            "vat": source.totals.vat,
            "gross": source.totals.gross,
        }
        gateway._read_optional = Mock(
            side_effect=[f"\u00a3{value:.2f}" for value in values.values()]
        )
        gateway.capture_evidence = Mock()

        result = gateway.verify_order_totals(source)

        self.assertTrue(result.verified)
        gateway.capture_evidence.assert_not_called()

    def test_empty_debtor_selector_reuses_one_ocr_pass_for_headers_and_rows(self):
        tokens = [
            ("First", 100, 20, 42),
            ("Name", 145, 20, 42),
            ("Name", 210, 20, 42),
            ("Company", 280, 20, 74),
            ("ZIP", 390, 20, 28),
            ("City", 470, 20, 32),
            ("No", 100, 80, 20),
            ("entries", 125, 80, 50),
        ]
        data = {
            "text": [word for word, _, _, _ in tokens],
            "block_num": [1] * len(tokens),
            "par_num": [1] * len(tokens),
            "line_num": [1] * 6 + [2] * 2,
            "left": [left for _, left, _, _ in tokens],
            "top": [top for _, _, top, _ in tokens],
            "width": [width for _, _, _, width in tokens],
            "height": [20] * len(tokens),
        }
        dialog = FakeWindow()
        dialog.capture_as_image = lambda: SimpleNamespace(height=600)
        gateway = attached_gateway(dialog)
        gateway.ocr = TesseractOCR()
        gateway.ocr.read_text_data = Mock(return_value=data)

        rows = gateway._ocr_debtor_table_rows(dialog, "Northstar Office GmbH")

        self.assertEqual(rows, [])
        gateway.ocr.read_text_data.assert_called_once()

    def test_ocr_click_converts_screenshot_bounds_to_screen_coordinates(self):
        window = FakeWindow(bounds=FakeRect(240, 130, 840, 730))
        target = ResolvedControl(
            ControlQuery.one_of("Save"),
            ocr_match=OCRMatch("Save", Bounds(11, 21, 31, 41)),
        )

        WindowsFakturamaGateway._click_control(window, target)

        self.assertEqual(window.screen_clicks, [(261, 161)])

    def test_ocr_click_falls_back_when_window_proxy_does_not_support_click_at_screen(self):
        calls = []

        def unsupported_click_at_screen(_point):
            raise AttributeError("not a supported wrapper method")

        window = SimpleNamespace(
            rectangle=lambda: FakeRect(240, 130, 840, 730),
            click_at_screen=unsupported_click_at_screen,
        )
        target = ResolvedControl(
            ControlQuery.one_of("New Debtor"),
            ocr_match=OCRMatch("New Debtor", Bounds(11, 21, 31, 41)),
        )
        mouse_module = SimpleNamespace(click=lambda **kwargs: calls.append(kwargs))

        with patch.dict(
            sys.modules,
            {"pywinauto": SimpleNamespace(mouse=mouse_module), "pywinauto.mouse": mouse_module},
        ):
            WindowsFakturamaGateway._click_control(window, target)

        self.assertEqual(calls, [{"coords": (261, 161)}])

    def test_address_selector_uses_upper_unnamed_image_inside_addresses_pane(self):
        order, _pane, images = order_window_with_address_images()
        selector = FakeWindow([FakeElement("Search", "Edit", value="")])
        selector.element_info.name = "Select the address"
        application = FakeApplication(order)

        def open_selector():
            application.window = selector
            application._windows = [order, selector]

        images[0]._on_click = open_selector
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order

        gateway.open_debtor_selector()

        self.assertEqual(images[0].click_count, 1)
        self.assertEqual(images[1].click_count, 0)
        self.assertIs(application.window, selector)

    def test_open_debtor_selector_reopens_nested_dialog_after_invoice_tab_activation(self):
        order, _pane, images = order_window_with_address_images()
        selector = FakeWindow([FakeElement("Search", "Edit", value="")])
        selector.element_info.name = "Select the address"
        order.add(selector)
        application = FakeApplication(selector)
        application._windows = [order]

        def close_selector():
            order._children.remove(selector)
            application.window = order

        def reopen_selector():
            order.add(selector)
            application.window = selector

        cancel = FakeElement("Cancel", "Button", on_click=close_selector)
        selector.add(cancel)
        images[0]._on_click = reopen_selector
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order

        gateway.open_debtor_selector()

        self.assertIs(gateway._visible_dialog_root(("Select the address",)), selector)
        self.assertEqual(cancel.click_count, 1)
        self.assertEqual([image.click_count for image in images], [1, 0])

    def test_select_debtor_explicitly_selects_invoice_and_delivery_addresses(self):
        source = sample_order()
        candidate = DebtorCandidate(
            "invoice-row", source.debtor.company, source.debtor.first_name,
            source.debtor.last_name, source.debtor.billing_address.zip_code,
            source.debtor.billing_address.city, number="CUST000005",
        )
        delivery_row = DebtorCandidate(
            "delivery-row", candidate.company, candidate.first_name,
            candidate.last_name, candidate.zip_code, candidate.city,
            number=candidate.number,
        )
        gateway = WindowsFakturamaGateway()
        gateway._last_source = source
        actions = []
        gateway._select_address_candidate = lambda row: actions.append(("select", row.token))
        gateway._open_order_address_selector = lambda label: actions.append(("tab", label))
        gateway.find_debtors = lambda query: actions.append(("find", query)) or [delivery_row]
        gateway.verify_order_debtor = Mock()

        gateway.select_debtor(candidate)

        self.assertEqual(actions, [
            ("select", "invoice-row"), ("tab", "Delivery address"),
            ("find", candidate.company), ("select", "delivery-row"),
        ])
        gateway.verify_order_debtor.assert_not_called()

    def test_delivery_selection_refuses_ambiguous_or_different_customer(self):
        source = sample_order()
        candidate = DebtorCandidate(
            "invoice-row", source.debtor.company, source.debtor.first_name,
            source.debtor.last_name, source.debtor.billing_address.zip_code,
            source.debtor.billing_address.city, number="CUST000005",
        )
        duplicate = DebtorCandidate(
            "duplicate-row", candidate.company, candidate.first_name,
            candidate.last_name, candidate.zip_code, candidate.city,
            number=candidate.number,
        )
        other = DebtorCandidate(
            "other-row", candidate.company, candidate.first_name,
            candidate.last_name, candidate.zip_code, candidate.city,
            number="CUST000006",
        )
        gateway = WindowsFakturamaGateway()
        gateway._last_source = source
        selected = []
        gateway._select_address_candidate = lambda row: selected.append(row.token)
        gateway._open_order_address_selector = lambda label: None
        for candidates in ([candidate, duplicate], [other]):
            gateway.find_debtors = lambda query, rows=candidates: rows
            with self.assertRaisesRegex(ManualReviewRequired, "one exact previously selected"):
                gateway.select_debtor(candidate)
        self.assertEqual(selected, ["invoice-row", "invoice-row"])

    def test_visible_nested_swt_selector_windows_fail_closed_when_ambiguous(self):
        order, _pane, _images = order_window_with_address_images()
        first = FakeWindow()
        first.element_info.name = "Select the address"
        second = FakeWindow()
        second.element_info.name = "Select the address"
        order.add(first, second)
        application = FakeApplication(order)
        application._windows = [order]
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order

        with self.assertRaises(ManualReviewRequired):
            gateway._visible_dialog_root(("Select the address",))

    def test_address_selector_image_fallback_fails_closed_when_pane_is_ambiguous(self):
        order, _pane, images = order_window_with_address_images(image_count=3)
        gateway = attached_gateway(order)

        with self.assertRaises(ManualReviewRequired):
            gateway.open_debtor_selector()

        self.assertEqual([image.click_count for image in images], [0, 0, 0])

    def test_debtor_search_and_rows_are_scoped_to_unique_selector_window(self):
        order, _pane, _images = order_window_with_address_images()
        background_search = FakeElement("Search", "Edit", value="")
        background_grid = FakeElement("", "DataGrid")
        background_grid.add(
            FakeElement("Company", "HeaderItem", bounds=FakeRect(0, 0, 100, 20)),
            FakeElement("ZIP", "HeaderItem", bounds=FakeRect(100, 0, 150, 20)),
            FakeElement("City", "HeaderItem", bounds=FakeRect(150, 0, 220, 20)),
        )
        background_row = FakeElement("", "DataItem")
        background_row.add(
            FakeElement("Background Company", "Text", bounds=FakeRect(0, 30, 100, 50)),
            FakeElement("10000", "Text", bounds=FakeRect(100, 30, 150, 50)),
            FakeElement("Elsewhere", "Text", bounds=FakeRect(150, 30, 220, 50)),
        )
        background_grid.add(background_row)
        order.add(background_search, background_grid)
        dialog, selector_search, selector_row = contact_selector_window(
            "Select the address", "Northstar Supplies GmbH", "10115", "Berlin"
        )
        application = FakeApplication(dialog)
        application._windows = [order, dialog]
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order

        candidates = gateway.find_debtors("Northstar")

        self.assertEqual(selector_search.get_value(), "Northstar")
        self.assertEqual(background_search.get_value(), "")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].company, "Northstar Supplies GmbH")
        self.assertNotIn(background_row, gateway._list_rows(window=dialog))
        self.assertEqual(gateway._list_rows(window=dialog), [selector_row])

    def test_debtor_search_fails_closed_for_multiple_visible_selector_windows(self):
        order, _pane, _images = order_window_with_address_images()
        first, first_search, _ = contact_selector_window(
            "Select the address", "Northstar Supplies GmbH", "10115", "Berlin"
        )
        second, second_search, _ = contact_selector_window(
            "Select the address", "Other Company", "10000", "Berlin"
        )
        application = FakeApplication(first)
        application._windows = [order, first, second]
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order

        with self.assertRaises(ManualReviewRequired):
            gateway.find_debtors("Northstar")

        self.assertEqual(first_search.get_value(), "")
        self.assertEqual(second_search.get_value(), "")

    def test_open_new_debtor_activates_existing_tab_with_unique_ocr_match(self):
        inactive_tab = FakeElement("New Debtor", "TabItem")
        window = FakeWindow(
            [
                FakeElement("Company", "Edit", value=""),
                FakeElement("Main address", "TabItem"),
                FakeElement("Addresses", "Text"),
                inactive_tab,
            ]
        )
        application = FakeApplication(window)
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = window
        gateway.ocr = FakeOCR([OCRMatch("New Debtor", Bounds(10, 20, 50, 40))])

        gateway.open_new_debtor()

        self.assertEqual(window.screen_clicks, [(30, 30)])
        self.assertEqual(inactive_tab.click_count, 0)

    def test_open_new_debtor_cancels_selector_then_uses_new_contact(self):
        order, _pane, images = order_window_with_address_images()
        contact = FakeWindow(
            [
                FakeElement("Company", "Edit", value=""),
                FakeElement("Main address", "TabItem"),
                FakeElement("Addresses", "Text"),
            ]
        )
        contact.element_info.name = "New Debtor"
        selector = FakeWindow()
        selector.element_info.name = "Select the address"
        application = FakeApplication(selector)
        application._windows = [order, selector]

        def close_selector():
            application.window = order
            application._windows = [order]

        cancel = FakeElement("Cancel", "Button", on_click=close_selector)
        selector.add(cancel)

        def open_contact():
            application.window = contact
            application._windows = [contact]

        new_contact = FakeElement("New Contact", "Button", on_click=open_contact)
        order.add(new_contact)
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03, poll_interval_seconds=0.001)
        gateway._application = application
        gateway._main_window = order
        gateway.ocr = FakeOCR([])

        gateway.open_new_debtor()

        self.assertEqual(cancel.click_count, 1)
        self.assertEqual(images[0].click_count, 0)
        self.assertEqual(images[1].click_count, 0)
        self.assertEqual(new_contact.click_count, 1)
        self.assertIs(application.window, contact)

    def test_product_search_disables_automatic_single_result_selection_once(self):
        gateway = WindowsFakturamaGateway()
        gateway._main_window = SimpleNamespace(handle=101)
        preference_window = object()
        selector = object()
        state = 1
        toggle_clicks = []
        closed_selectors = []

        def toggle_selection():
            nonlocal state
            toggle_clicks.append(True)
            state = 0

        checkbox = SimpleNamespace(
            get_toggle_state=lambda: state,
            click_input=toggle_selection,
        )
        gateway._visible_dialog_root = lambda labels, required=False: (
            selector if labels[0] == "Select a product" else None
        )
        gateway._close_selector_dialog = lambda labels, dialog: closed_selectors.append(dialog)
        gateway._click = lambda query: None
        gateway._wait_until = lambda description, predicate: self.assertTrue(predicate())
        gateway._current_window = lambda: preference_window
        gateway._window_title = lambda window: "Preferences"
        gateway._resolver = lambda window: SimpleNamespace(
            resolve=lambda query: SimpleNamespace(element=checkbox)
        )
        gateway._click_control = lambda window, control: None

        gateway._ensure_product_search_preferences()
        gateway._ensure_product_search_preferences()

        self.assertTrue(gateway._product_search_configured)
        self.assertEqual(closed_selectors, [selector])
        self.assertEqual(len(toggle_clicks), 1)

    def test_open_new_product_uses_create_button_and_waits_for_visible_draft(self):
        saved_editor_fields = [
            FakeElement("Item Number", "Edit", value="SAVED-SKU"),
            FakeElement("Price (gross)", "Edit", value="£297.50"),
        ]
        window = FakeWindow(saved_editor_fields)
        draft_tab = VisibleElement("*New product", "Tab")
        blank_sku = FakeElement("Item Number", "Edit", value="")
        create_button = FakeElement(
            "Create a new product", "Button",
            on_click=lambda: window.add(draft_tab, blank_sku),
        )
        window.add(create_button)
        gateway = attached_gateway(window)

        gateway.open_new_product()

        self.assertEqual(create_button.click_count, 1)
        self.assertIn(draft_tab, window.descendants())
        self.assertIn(blank_sku, window.descendants())
        self.assertEqual(saved_editor_fields[0].get_value(), "SAVED-SKU")
    def test_fill_product_sets_and_verifies_required_fields_only(self):
        window, fields = product_editor_window()
        gateway = attached_gateway(window)
        item = sample_order().items[0]
        fields["Item Number"]._value = item.sku
        vat = VatCandidate(
            token="vat-19",
            name="VAT 19%",
            value_percent=item.vat_rate_percent,
            e_invoice_code="S",
        )

        gateway.fill_product(item, vat)

        self.assertEqual(fields["Item Number"].get_value(), item.sku)
        self.assertEqual(fields["Name"].get_value(), item.description)
        self.assertEqual(fields["Description"].get_value(), item.description)
        # The source line has a 10% discount; the Product master price must not.
        self.assertEqual(fields["Price (gross)"].get_value(), "297.50")
        self.assertEqual(fields["cost price (net)"].get_value(), "0.00")
        self.assertEqual(fields["VAT"].get_value(), "VAT 19%")
        self.assertEqual(fields["Stock"].get_value(), "0.00")
        for name, expected in (
            ("Category", "Keep category"),
            ("GTIN", "KEEP-GTIN"),
            ("supplier code", "KEEP-SUPPLIER"),
            ("allowance", "KEEP-ALLOWANCE"),
            ("user defined field 1", "KEEP-1"),
            ("user defined field 2", "KEEP-2"),
            ("user defined field 3", "KEEP-3"),
        ):
            self.assertEqual(fields[name].get_value(), expected)
        self.assertEqual(fields["Select a Picture"].click_count, 0)
        self.assertEqual(fields["delete picture"].click_count, 0)

    def test_fill_product_does_not_skip_required_description_or_stock(self):
        item = sample_order().items[0]
        vat = VatCandidate(
            token="vat-19",
            name="VAT 19%",
            value_percent=item.vat_rate_percent,
            e_invoice_code="S",
        )
        for missing in ("Description", "Stock"):
            with self.subTest(missing=missing):
                window, _fields = product_editor_window(omitted=(missing,))
                _fields["Item Number"]._value = item.sku
                gateway = attached_gateway(window)
                gateway.ocr = FakeOCR([])

                with self.assertRaises(ElementNotFound):
                    gateway.fill_product(item, vat)

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

    def test_visible_window_attach_remembers_discovered_executable_for_version(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            window = FakeWindow()
            window.handle = 1234
            window.process_id = 44
            window.process_path = None
            window.is_visible = lambda: True
            app = RecordingApplication(window)
            gateway = WindowsFakturamaGateway(
                app_factory=lambda **kwargs: app,
                desktop_factory=lambda **kwargs: FakeDesktop([window]),
                version_reader=lambda path: (
                    "2.2.0" if path == executable.resolve() else None
                ),
            )

            with patch.object(
                WindowsFakturamaGateway,
                "_discover_fakturama_executable",
                return_value=executable,
            ):
                gateway.attach_or_launch()

            self.assertEqual(gateway._configured_executable, executable.resolve())
            self.assertEqual(gateway._executable_version(), "2.2.0")
            self.assertEqual(app.start_calls, [])

    def test_doctor_discovers_executable_for_visible_window_version(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            window = FakeWindow([FakeElement("File", "MenuItem"), FakeElement("Data", "MenuItem")])
            window.handle = 1234
            window.process_id = 44
            window.process_path = None
            window.is_visible = lambda: True
            app = RecordingApplication(window)
            gateway = WindowsFakturamaGateway(
                app_factory=lambda **kwargs: app,
                desktop_factory=lambda **kwargs: FakeDesktop([window]),
                version_reader=lambda path: (
                    "2.2.0" if path == executable.resolve() else None
                ),
            )

            with patch.object(
                WindowsFakturamaGateway,
                "_discover_fakturama_executable",
                return_value=executable,
            ):
                checks = gateway.diagnose()

        self.assertEqual(checks["Version"], "2.2.0")
        self.assertIn(str(executable.resolve()), checks["Executable"])
        self.assertEqual(app.start_calls, [])

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

    def test_attach_ignores_browser_title_that_mentions_fakturama_package(self):
        window = FakeWindow()
        window.element_info.name = r"Fakturama - D:\Yahia"
        window.handle = 1234
        window.is_visible = lambda: True

        browser = FakeWindow()
        browser.element_info.name = (
            "Failed to persist contents of part "
            "(com.sebulli.fakturama.editors.documentEditor) - Google Search - Google Chrome"
        )
        browser.handle = 5678
        browser.is_visible = lambda: True

        app = RecordingApplication(window)
        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([browser, window]),
        )

        gateway.attach_or_launch()

        self.assertEqual(app.connect_calls, [{"handle": 1234, "timeout": 15.0}])
        self.assertEqual(app.start_calls, [])
        self.assertIs(gateway._main_window, window)

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
        def desktop_factory(**kwargs):
            return FakeDesktop([app.window] if app.start_calls else [])

        gateway = WindowsFakturamaGateway(
            app_factory=lambda **kwargs: app,
            desktop_factory=desktop_factory,
            process_checker=lambda path: False,
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()

            gateway.attach_or_launch(executable)

        self.assertEqual(app.start_calls, [(str(executable.resolve()), {"timeout": 15.0})])

    def test_no_visible_window_launches_a_uniquely_discovered_install(self):
        app = RecordingApplication(None, connect_error=RuntimeError("no matching process"))

        def desktop_factory(**kwargs):
            return FakeDesktop([app.window] if app.start_calls else [])

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            gateway = WindowsFakturamaGateway(
                timeout_seconds=0.1,
                app_factory=lambda **kwargs: app,
                desktop_factory=desktop_factory,
                process_checker=lambda path: False,
            )

            with patch.object(
                WindowsFakturamaGateway,
                "_discover_fakturama_executable",
                return_value=executable,
            ):
                gateway.attach_or_launch()

        self.assertEqual(app.start_calls, [(str(executable.resolve()), {"timeout": 0.1})])
        self.assertEqual(gateway._configured_executable, executable.resolve())
        self.assertIs(gateway._main_window, app.window)

    def test_launch_fails_clearly_when_no_visible_window_appears(self):
        app = RecordingApplication(None, connect_error=RuntimeError("no matching process"))
        gateway = WindowsFakturamaGateway(
            timeout_seconds=0.001,
            launch_timeout_seconds=0.001,
            poll_interval_seconds=0.001,
            app_factory=lambda **kwargs: app,
            desktop_factory=lambda **kwargs: FakeDesktop([]),
            process_checker=lambda path: False,
        )
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()

            with self.assertRaisesRegex(
                GatewayError, r"no visible Fakturama window appeared within 0\.001 seconds"
            ):
                gateway.attach_or_launch(executable)

        self.assertEqual(app.start_calls, [(str(executable.resolve()), {"timeout": 0.001})])

    def test_environment_executable_is_used_for_automatic_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Fakturama.exe"
            executable.touch()
            with patch.dict("os.environ", {"FAKTURAMA_EXE": str(executable)}, clear=True):
                discovered = WindowsFakturamaGateway._discover_fakturama_executable()

        self.assertEqual(discovered, executable.resolve())

    def test_order_header_maps_source_reference_date_and_modes(self):
        source = sample_order()
        fields = [
            FakeElement("No.", "Edit", value="ORD-100"),
            FakeElement("Date", "Edit", value=""),
            FakeElement("Cust.Ref.", "Edit", value=""),
            FakeElement("Price mode", "ComboBox", value=""),
            FakeElement("VAT mode", "ComboBox", value=""),
        ]
        window = FakeWindow([document_editor(*fields)])
        gateway = attached_gateway(window)

        gateway.fill_order_header(source)

        self.assertEqual(fields[1].get_value(), source.order_date.isoformat())
        self.assertEqual(fields[2].get_value(), source.external_reference)
        self.assertEqual(fields[3].get_value(), "Net")
        self.assertEqual(fields[4].get_value(), "With VAT")

    def test_order_date_uses_segments_and_tab_to_commit(self):
        source = sample_order()
        month_names = (
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        )
        normalized_date = (
            f"{source.order_date.day} {month_names[source.order_date.month - 1]} "
            f"{source.order_date.year}"
        )
        date_field = KeyboardDateField(
            "Date",
            "Edit",
            bounds=FakeRect(100, 200, 200, 224),
            value="",
            committed_value=normalized_date,
        )
        fields = [
            FakeElement("No.", "Edit", value="ORD-100"),
            date_field,
            FakeElement("Cust.Ref.", "Edit", value=""),
            FakeElement("Price mode", "ComboBox", value=""),
            FakeElement("VAT mode", "ComboBox", value=""),
        ]
        gateway = attached_gateway(FakeWindow([document_editor(*fields)]))
        expected_input = (
            f"{source.order_date.day} {source.order_date.month} {source.order_date.year}"
        )

        gateway.fill_order_header(source)

        self.assertEqual(date_field.click_count, 1)
        self.assertEqual(date_field.click_calls, [{"coords": (30, 12)}])
        self.assertEqual(
            date_field.key_calls,
            [
                (str(source.order_date.day), {}),
                (str(source.order_date.month), {}),
                ("{RIGHT}", {}),
                (str(source.order_date.year), {}),
                ("{TAB}", {}),
            ],
        )
        self.assertEqual(date_field.get_value(), normalized_date)
        self.assertEqual(_parse_date_text(expected_input), source.order_date)

    def test_order_modes_fall_back_to_their_current_values_when_labels_are_unavailable(self):
        source = sample_order()
        fields = [
            FakeElement("No.", "Edit", value="ORD-100"),
            FakeElement("Date", "Edit", value=""),
            FakeElement("Cust.Ref.", "Edit", value=""),
            FakeElement("", "ComboBox", value="Gross"),
            FakeElement("", "ComboBox", value="Without VAT"),
        ]
        gateway = attached_gateway(FakeWindow([document_editor(*fields)]))

        gateway.fill_order_header(source)

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

    def test_composite_name_and_zip_city_rows_fill_and_verify_left_to_right(self):
        source = sample_order()
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        names = VisibleElement("", "Pane", bounds=FakeRect(780, 300, 1450, 350))
        first = VisibleElement("", "Edit", bounds=FakeRect(1040, 305, 1220, 335), value="")
        last = VisibleElement("", "Edit", bounds=FakeRect(1240, 305, 1430, 335), value="")
        names.add(
            VisibleElement("First Name Last Name", "Text", bounds=FakeRect(800, 305, 1020, 335)),
            first,
            last,
        )
        main = VisibleElement(
            "Main address", "Tab", bounds=FakeRect(760, 400, 1700, 1100)
        )
        address_row = VisibleElement("", "Pane", bounds=FakeRect(780, 550, 1500, 600))
        zip_code = VisibleElement("", "Edit", bounds=FakeRect(980, 555, 1100, 585), value="")
        city = VisibleElement("", "Edit", bounds=FakeRect(1120, 555, 1340, 585), value="")
        address_row.add(
            VisibleElement("ZIP - City", "Text", bounds=FakeRect(800, 555, 960, 585)),
            zip_code,
            city,
        )
        street = VisibleElement("Street", "Edit", value="")
        country = VisibleElement("Country", "Edit", value="")
        additional_name = VisibleElement("Additional name", "Edit", value="")
        main.add(address_row, street, country, additional_name)
        editor.add(names, main)
        gateway = attached_gateway(FakeWindow([editor]))
        gateway.ocr = FakeOCR([])

        gateway._set_composite_row(
            "First Name Last Name",
            (source.debtor.first_name, source.debtor.last_name),
            scope=editor,
        )
        gateway._fill_address(source.debtor.billing_address, context=("Main address",))
        result = gateway._verify_address_fields(
            source.debtor.billing_address, context=("Main address",)
        )

        self.assertEqual(first.get_value(), "Mira")
        self.assertEqual(last.get_value(), "Weber")
        self.assertEqual(zip_code.get_value(), "20457")
        self.assertEqual(city.get_value(), "Hamburg")
        self.assertTrue(result.verified, result.observations)

    def test_composite_row_with_ambiguous_edit_count_fails_closed(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        row = VisibleElement("", "Pane", bounds=FakeRect(780, 300, 1600, 350))
        row.add(
            VisibleElement("First Name Last Name", "Text", bounds=FakeRect(800, 305, 1020, 335)),
            VisibleElement("", "Edit", bounds=FakeRect(1040, 305, 1180, 335), value=""),
            VisibleElement("", "Edit", bounds=FakeRect(1200, 305, 1340, 335), value=""),
            VisibleElement("", "Edit", bounds=FakeRect(1360, 305, 1500, 335), value=""),
        )
        editor.add(row)
        gateway = attached_gateway(FakeWindow([editor]))

        with self.assertRaises(ManualReviewRequired):
            gateway._set_composite_row(
                "First Name Last Name", ("Mira", "Weber"), scope=editor
            )

    def test_active_debtor_tab_does_not_click_its_tabitem(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        tab_item = VisibleElement("Main address", "TabItem", bounds=FakeRect(800, 450, 1000, 490))
        active_tab = VisibleElement("Main address", "Tab", bounds=FakeRect(760, 500, 1700, 1100))
        editor.add(tab_item, active_tab)
        gateway = attached_gateway(FakeWindow([editor]))
        gateway.ocr = FakeOCR([])

        self.assertIs(active_tab, gateway._activate_debtor_tab("Main address"))
        self.assertEqual(tab_item.click_count, 0)

    def test_hidden_swt_tab_with_visible_fields_is_active_address_scope(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        tab_item = FakeElement("Main address", "TabItem")
        tab_item.is_selected = lambda: 1
        tab_item.is_visible = lambda: False
        address = FakeElement("Main address", "Tab")
        address.is_visible = lambda: False
        address.add(
            VisibleElement("Street", "Text"),
            VisibleElement("", "Edit", value=""),
            VisibleElement("Country", "Text"),
            VisibleElement("", "ComboBox", value=""),
        )
        editor.add(tab_item, address)
        gateway = attached_gateway(FakeWindow([editor]))

        self.assertIs(gateway._active_debtor_tab("Main address"), address)

    def test_ocr_debtor_tab_click_ignores_sidebar_match_outside_editor(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(774, 245, 3840, 1279)
        )
        window = FakeWindow([editor], bounds=FakeRect(0, 0, 4000, 2000))
        gateway = attached_gateway(window)
        gateway.ocr = FakeOCR(
            [
                OCRMatch("Miscellaneous", Bounds(105, 1476, 115, 1486)),
                OCRMatch("Miscellaneous", Bounds(1000, 688, 1010, 698)),
            ]
        )

        gateway._activate_debtor_tab("Miscellaneous")

        self.assertEqual(window.screen_clicks, [(1005, 693)])

    def test_address_role_uses_active_address_type_button_and_verifies_checkbox(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        main = VisibleElement(
            "Main address", "Tab", bounds=FakeRect(760, 500, 1700, 1100)
        )
        row = VisibleElement("", "Pane", bounds=FakeRect(780, 590, 1400, 650))
        label = VisibleElement("address type", "Text", bounds=FakeRect(800, 605, 920, 635))
        field = VisibleElement("", "Edit", bounds=FakeRect(940, 605, 1110, 635), value="")
        role_checks = [
            RoleCheckbox(
                "Invoice address", "CheckBox", bounds=FakeRect(1130, 650, 1320, 680)
            ),
            RoleCheckbox(
                "Delivery address", "CheckBox", bounds=FakeRect(1130, 690, 1320, 720)
            ),
        ]
        selector = VisibleElement(
            "", "Button", bounds=FakeRect(1120, 605, 1150, 635),
            on_click=lambda: editor.add(*role_checks),
        )
        label._on_click = lambda: setattr(field, "_value", ", ".join(
            check.window_text() for check in role_checks if check.is_checked()
        ))
        row.add(label, field, selector)
        main.add(row)
        editor.add(main)
        gateway = attached_gateway(FakeWindow([editor]))

        gateway._assign_address_roles(("Delivery address",))

        self.assertEqual(selector.click_count, 1)
        self.assertFalse(role_checks[0].is_checked())
        self.assertTrue(role_checks[1].is_checked())

    def test_delivery_address_creation_uses_only_scoped_plus_button(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 3800, 1300)
        )
        main = VisibleElement(
            "Main address", "Tab", bounds=FakeRect(760, 500, 1700, 1100)
        )
        created_tab = VisibleElement(
            "additional address #1", "Tab", bounds=FakeRect(760, 500, 1700, 1100)
        )
        plus = VisibleElement(
            "+", "Button", bounds=FakeRect(1620, 520, 1660, 560),
            on_click=lambda: editor.add(created_tab),
        )
        main.add(plus)
        unrelated_plus = VisibleElement(
            "+", "Button", bounds=FakeRect(3000, 1150, 3040, 1190)
        )
        other = VisibleElement("", "Pane", bounds=FakeRect(2900, 1100, 3100, 1250))
        other.add(unrelated_plus)
        editor.add(main, other)
        gateway = attached_gateway(FakeWindow([editor]))
        gateway.ocr = FakeOCR([])

        result = gateway._open_or_create_delivery_address()

        self.assertEqual(result, "additional address #1")
        self.assertEqual(plus.click_count, 1)
        self.assertEqual(unrelated_plus.click_count, 0)

    def test_existing_active_additional_address_tab_is_reused(self):
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 3800, 1300)
        )
        additional = VisibleElement(
            "additional address #1", "Tab", bounds=FakeRect(760, 500, 1700, 1100)
        )
        main_item = VisibleElement("Main address", "TabItem", bounds=FakeRect(800, 450, 1000, 490))
        editor.add(main_item, additional)
        gateway = attached_gateway(FakeWindow([editor]))
        gateway.ocr = FakeOCR([])

        result = gateway._open_or_create_delivery_address()

        self.assertEqual(result, "additional address #1")
        self.assertEqual(main_item.click_count, 0)

    def test_selected_debtor_verification_requires_both_addresses_to_match(self):
        debtor = sample_order().debtor
        gateway = WindowsFakturamaGateway()
        billing = gateway._address_string(debtor.billing_address)
        delivery = gateway._address_string(debtor.delivery_address)
        without_street = delivery.replace(f"{debtor.delivery_address.street}, ", "")

        with patch.object(
            gateway,
            "_read_order_address_display",
            side_effect=[billing, without_street],
        ):
            result = gateway.verify_order_debtor(debtor)

        self.assertFalse(result.verified)
        self.assertTrue(any("delivery address" in issue for issue in result.observations))

    def _stub_invoice_copy_readback(self, gateway, source, *, linked="ORD-100"):
        values = {
            "Order Date": source.order_date.strftime("%d %b %Y"),
            "Date": "25 Sep 2026",
            "Service date": "25 Sep 2026",
            "No.": "INV-100",
            "Cust.Ref.": source.external_reference,
            "Gross total": str(source.totals.gross),
        }

        def read_field(labels):
            return next((values[label] for label in labels if label in values), None)

        gateway._read_optional = read_field
        gateway._active_order_editor_tab = Mock(return_value=object())
        gateway._invoice_link = Mock(return_value=linked)
        gateway._invoice_ref = InvoiceEditorRef(
            token="invoice-100", number="INV-100", linked_order_number=linked,
            proposed_invoice_date=values["Date"],
            proposed_service_date=values["Service date"],
        )
        gateway._read_combo_optional = lambda labels: "With VAT"
        gateway.verify_order_totals = lambda current: VerificationResult(True)
        gateway.verify_order_debtor = lambda debtor: VerificationResult(True)
        gateway.verify_order_line = lambda item, index: VerificationResult(True, observed={})

    def test_invoice_copy_compares_order_date_and_preserves_proposed_dates(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        self._stub_invoice_copy_readback(gateway, source)

        result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertTrue(result.verified, result.observations)
        self.assertNotEqual(
            gateway._invoice_ref.proposed_invoice_date,
            source.order_date.strftime("%d %b %Y"),
        )

    def test_invoice_proposed_date_readback_detects_change(self):
        invoice_date = FakeElement("Date", "Edit", value="25 Sep 2026")
        service_date = FakeElement("Service date", "Edit", value="25 Sep 2026")
        editor = document_editor(
            FakeElement("No.", "Edit", value="INV-100"),
            invoice_date, service_date, name="INV-100", kind="Invoice",
        )
        gateway = attached_gateway(FakeWindow([editor]))
        gateway._invoice_ref = InvoiceEditorRef(
            token="invoice-100", number="INV-100", linked_order_number="ORD-100",
            proposed_invoice_date="25 Sep 2026",
            proposed_service_date="25 Sep 2026",
        )

        self.assertEqual(gateway._invoice_default_issues(), [])
        service_date._value = "26 Sep 2026"
        self.assertIn(
            "Invoice proposed Service date changed or cannot be verified",
            gateway._invoice_default_issues(),
        )
        service_date._value = "25 Sep 2026"
        invoice_date._value = "26 Sep 2026"
        self.assertIn(
            "Invoice proposed Date changed or cannot be verified",
            gateway._invoice_default_issues(),
        )

    def test_invoice_payment_dropdown_is_scoped_beside_paid_checkbox(self):
        paid = VisibleElement("paid", "CheckBox", bounds=FakeRect(100, 100, 150, 120))
        method = VisibleElement("", "ComboBox", bounds=FakeRect(160, 100, 280, 120))
        other = VisibleElement("", "ComboBox", bounds=FakeRect(160, 150, 280, 170))
        editor = document_editor(
            FakeElement("No.", "Edit", value="INV-100"),
            paid, method, other, name="INV-100", kind="Invoice",
        )
        gateway = attached_gateway(FakeWindow([editor]))

        self.assertIs(gateway._invoice_payment_method_control(), method)
        editor.add(VisibleElement("", "ComboBox", bounds=FakeRect(300, 100, 420, 120)))
        with self.assertRaisesRegex(ManualReviewRequired, "not unique beside paid"):
            gateway._invoice_payment_method_control()

    def test_order_line_ocr_stops_when_no_safe_scrollbar_exists(self):
        item = sample_order().items[0]
        gateway = WindowsFakturamaGateway()
        gateway._capture_order_line_ocr = Mock(side_effect=ElementNotFound("SKU hidden"))
        gateway._scroll_document_items_down = Mock(return_value=False)

        with self.assertRaisesRegex(ElementNotFound, "SKU hidden"):
            gateway._verify_order_line_ocr(item)

        self.assertEqual(gateway._capture_order_line_ocr.call_count, 1)
        gateway._scroll_document_items_down.assert_called_once_with()

    def test_order_line_ocr_limits_reveal_to_three_scroll_clicks(self):
        item = sample_order().items[0]
        gateway = WindowsFakturamaGateway()
        gateway._capture_order_line_ocr = Mock(side_effect=ElementNotFound("SKU hidden"))
        gateway._scroll_document_items_down = Mock(return_value=True)

        with self.assertRaisesRegex(ElementNotFound, "SKU hidden"):
            gateway._verify_order_line_ocr(item)

        self.assertEqual(gateway._capture_order_line_ocr.call_count, 4)
        self.assertEqual(gateway._scroll_document_items_down.call_count, 3)

    def test_document_items_scroll_clicks_only_unique_right_edge_button(self):
        safe = VisibleElement(
            "Line down", "Button", bounds=FakeRect(460, 400, 490, 420)
        )
        unrelated = VisibleElement(
            "Line down", "Button", bounds=FakeRect(100, 400, 130, 420)
        )
        editor = document_editor(
            FakeElement("No.", "Edit", value="RE000001"),
            FakeElement("Remarks", "Edit", bounds=FakeRect(20, 500, 300, 550)),
            safe, unrelated, name="RE000001", kind="Invoice",
        )
        editor.element_info.rectangle = FakeRect(0, 0, 500, 600)
        gateway = attached_gateway(FakeWindow([editor]))

        self.assertTrue(gateway._scroll_document_items_down())
        self.assertEqual(safe.click_count, 1)
        self.assertEqual(unrelated.click_count, 0)
        editor.add(VisibleElement(
            "Line down", "Button", bounds=FakeRect(455, 430, 490, 450)
        ))
        self.assertFalse(gateway._scroll_document_items_down())
        self.assertEqual(safe.click_count, 1)

    def test_saved_invoice_editor_is_identified_by_visible_header(self):
        editor = document_editor(
            FakeElement("No.", "Edit", value="RE000001"),
            FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
            name="RE000001", kind="Invoice",
        )
        gateway = attached_gateway(FakeWindow([editor]))

        self.assertTrue(gateway._has_invoice_editor())
        self.assertEqual(gateway._document_editor_identity(), (editor, "Invoice", "RE000001"))

    def test_invoice_copy_verification_requires_the_source_order_number(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        self._stub_invoice_copy_readback(gateway, source, linked=None)

        result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertFalse(result.verified)
        self.assertTrue(any("source Order number" in issue for issue in result.observations))

    def test_invoice_copy_verification_rejects_date_vat_and_line_mismatches(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        self._stub_invoice_copy_readback(gateway, source)
        original_read = gateway._read_optional
        gateway._read_optional = lambda labels: (
            (source.order_date.replace(day=source.order_date.day - 1)).strftime("%d %b %Y")
            if labels == ("Order Date",) else original_read(labels)
        )
        gateway._read_combo_optional = lambda labels: "Without VAT"
        gateway.verify_order_line = lambda item, index: VerificationResult(
            False, observations=("Price differs",)
        )

        result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertFalse(result.verified)
        self.assertTrue(any("Invoice Order Date differs" in issue for issue in result.observations))
        self.assertTrue(any("VAT mode" in issue for issue in result.observations))
        self.assertTrue(any("copied line" in issue for issue in result.observations))

    def test_fill_order_line_uses_ocr_for_custom_drawn_grid(self):
        gateway = WindowsFakturamaGateway()
        item = sample_order().items[0]
        expected = VerificationResult(verified=True, observations=("visible row verified",))
        gateway._require_editor = lambda kind: None
        gateway._wait_for_order_line_row = Mock(side_effect=ElementNotFound("custom grid"))
        gateway._fill_order_line_ocr = Mock(return_value=expected)

        result = gateway.fill_order_line(item)

        self.assertIs(result, expected)
        gateway._fill_order_line_ocr.assert_called_once_with(item)

    def test_verify_order_line_uses_ocr_for_custom_drawn_grid(self):
        gateway = WindowsFakturamaGateway()
        item = sample_order().items[0]
        expected = VerificationResult(verified=True, observations=("visible row verified",))
        gateway._require_editor = lambda kind: None
        gateway._wait_for_order_line_row = Mock(side_effect=ElementNotFound("custom grid"))
        gateway._verify_order_line_ocr = Mock(return_value=expected)

        result = gateway.verify_order_line(item, 0)

        self.assertIs(result, expected)
        gateway._verify_order_line_ocr.assert_called_once_with(item)

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
        editor = object()
        gateway._active_order_editor_tab = lambda: editor
        gateway._current_window = lambda: editor
        gateway._list_rows = lambda window=None: [prefix_row, exact_row]
        gateway._row_values = lambda row: (
            {"Item Number": "A-10" if row is prefix_row else "A-1"},
            "A-10" if row is prefix_row else "A-1",
        )

        self.assertIs(gateway._find_order_line_row("A-1"), exact_row)

    def test_order_line_lookup_never_mutates_a_prefix_only_row(self):
        gateway = WindowsFakturamaGateway()
        prefix_row = object()
        editor = object()
        gateway._active_order_editor_tab = lambda: editor
        gateway._current_window = lambda: editor
        gateway._list_rows = lambda window=None: [prefix_row]
        gateway._row_values = lambda row: ({"Item Number": "A-10"}, "A-10")

        with self.assertRaises(ElementNotFound):
            gateway._find_order_line_row("A-1")

    def test_order_line_lookup_pauses_when_matching_row_has_no_exact_sku_cell(self):
        gateway = WindowsFakturamaGateway()
        row = object()
        editor = object()
        gateway._active_order_editor_tab = lambda: editor
        gateway._current_window = lambda: editor
        gateway._list_rows = lambda window=None: [row]
        gateway._row_values = lambda active: ({"Qty.": "2"}, "A-1 Widget 2")

        with self.assertRaises(ManualReviewRequired):
            gateway._find_order_line_row("A-1")

    def test_open_order_discovery_uses_ocr_tab_strip_when_uia_selection_is_unavailable(self):
        first = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(500, 140, 620, 165)
        )
        second = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(620, 140, 740, 165)
        )
        window = FakeWindow([first, second], bounds=FakeRect(100, 100, 900, 700))
        window.capture_as_image = lambda: SimpleNamespace(height=600)
        gateway = attached_gateway(window)
        gateway.ocr = FakeOCR([
            OCRMatch("New Order", Bounds(200, 40, 300, 60)),
            OCRMatch("New Order", Bounds(350, 40, 450, 60)),
        ])
        focused = []
        gateway._focus_window_for_automation = lambda target: focused.append(target)
        editor = object()
        gateway._active_order_editor_tab = lambda: editor

        matches = gateway._order_tab_ocr_matches(window, 2)
        result = gateway._select_open_order_tab(
            window, second, ocr_matches=matches
        )

        self.assertIs(result, editor)
        self.assertEqual(focused, [window, window])
        self.assertEqual(window.screen_clicks, [(500, 150)])
        self.assertEqual(first.click_count + second.click_count, 0)
        self.assertEqual(len(gateway.ocr.calls), 1)

    def test_order_tab_selection_accepts_a_rematerialized_uia_wrapper(self):
        original = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(620, 140, 740, 165)
        )
        first = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(500, 140, 620, 165)
        )
        fresh_wrapper = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(620, 140, 740, 165)
        )
        window = FakeWindow([first, fresh_wrapper], bounds=FakeRect(100, 100, 900, 700))
        gateway = attached_gateway(window)
        gateway._focus_window_for_automation = lambda target: None
        gateway._active_order_editor_tab = lambda: window
        matches = [
            OCRMatch("New Order", Bounds(200, 40, 300, 60)),
            OCRMatch("New Order", Bounds(350, 40, 450, 60)),
        ]

        result = gateway._select_open_order_tab(
            window, original, ocr_matches=matches
        )

        self.assertIs(result, window)
        self.assertEqual(window.screen_clicks, [(500, 150)])

    def test_open_new_order_pauses_for_an_inactive_existing_draft(self):
        tab = FakeElement("*New Order", "TabItem")
        window = FakeWindow([tab])
        gateway = attached_gateway(window)
        gateway._visible_dialog_root = lambda *args, **kwargs: None
        gateway._has_order_editor = lambda: False
        gateway._current_window = lambda: window
        gateway._resolve_toolbar_order = Mock(
            side_effect=AssertionError("must not create another Order")
        )

        with self.assertRaises(ManualReviewRequired):
            gateway.open_new_order()

        gateway._resolve_toolbar_order.assert_not_called()

    def test_saved_document_tab_uses_raw_ocr_for_o_zero_confusion(self):
        number = "PO000008"
        no_field = FakeElement("No.", "Edit", value="OLD-001")
        editor = document_editor(
            no_field, FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
            name="OLD-001",
        )
        editor.element_info.rectangle = FakeRect(0, 140, 600, 700)
        tab = VisibleElement(
            number, "TabItem", bounds=FakeRect(100, 145, 180, 165)
        )
        from types import SimpleNamespace
        tab.iface_selection_item = SimpleNamespace(Select=Mock(side_effect=RuntimeError(
            "Member not found"
        )))
        window = FakeWindow([editor, tab])
        window.click_at_screen = lambda point: setattr(no_field, "_value", number)
        gateway = attached_gateway(window)
        gateway.ocr = TesseractOCR()
        gateway.ocr.find_text = Mock(return_value=[
            OCRMatch(number, Bounds(100, 300, 180, 320))
        ])
        gateway.ocr.read_words = Mock(return_value=[
            OCRMatch("PQ000008", Bounds(100, 145, 180, 165))
        ])

        gateway._activate_document_tab("Order", number)

        self.assertEqual(no_field.get_value(), number)
        gateway.ocr.find_text.assert_called_once()
        gateway.ocr.read_words.assert_called_once_with(
            gateway.ocr.find_text.call_args.args[0], preprocess=False
        )

    def test_saved_invoice_tab_handles_five_read_as_s_and_checks_actual_number(self):
        number = "INV000005"
        no_field = FakeElement("No.", "Edit", value="OLD-001")
        editor = document_editor(
            no_field, FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
            name="OLD-001",
        )
        editor.element_info.rectangle = FakeRect(0, 140, 600, 700)
        tab = VisibleElement(
            number, "TabItem", bounds=FakeRect(100, 145, 180, 165)
        )
        from types import SimpleNamespace
        tab.iface_selection_item = SimpleNamespace(Select=Mock(side_effect=RuntimeError(
            "Member not found"
        )))
        window = FakeWindow([editor, tab])
        window.click_at_screen = lambda point: setattr(no_field, "_value", number)
        gateway = attached_gateway(window)
        gateway.ocr = TesseractOCR()
        gateway.ocr.find_text = Mock(return_value=[
            OCRMatch(number, Bounds(100, 300, 180, 320))
        ])
        gateway.ocr.read_words = Mock(return_value=[
            OCRMatch("INVOOO00S", Bounds(100, 145, 180, 165))
        ])

        gateway._activate_document_tab("Invoice", number)

        self.assertEqual(no_field.get_value(), number)
        gateway.ocr.find_text.assert_called_once()
        gateway.ocr.read_words.assert_called_once_with(
            gateway.ocr.find_text.call_args.args[0], preprocess=False
        )

    def test_saved_tab_uses_uia_selection_without_ocr_or_coordinate_click(self):
        number = "INV000004"
        field = FakeElement("No.", "Edit", value="PO000010")
        editor = document_editor(field, FakeElement("Cust.Ref.", "Edit", value="WEB-100"))
        tab = VisibleElement(number, "TabItem")
        from types import SimpleNamespace
        tab.iface_selection_item = SimpleNamespace(Select=Mock())
        tab.iface_selection_item.Select.side_effect = lambda: setattr(field, "_value", number)
        gateway = attached_gateway(FakeWindow([editor, tab]))
        gateway.ocr = Mock()
        gateway._activate_document_tab("Invoice", number)
        tab.iface_selection_item.Select.assert_called_once()
        gateway.ocr.find_text.assert_not_called()
        self.assertEqual(tab.click_count, 0)

    def test_return_to_order_selects_the_order_tab_and_verifies_its_number(self):
        active_document = {"name": "New Delivery Note"}
        delivery_page = FakeElement("New Delivery Note", "Tab")
        delivery_page.is_visible = lambda: active_document["name"] == "New Delivery Note"
        order_page = document_editor(FakeElement("No.", "Edit", value="P000003"))
        order_page.is_visible = lambda: active_document["name"] == "New Order"

        def activate_order():
            active_document["name"] = "New Order"

        order_tab = VisibleElement(
            "*New Order", "TabItem", bounds=FakeRect(500, 140, 620, 165), on_click=activate_order
        )
        toolbar_calls = []
        toolbar = VisibleElement(
            "New Order",
            "Button",
            bounds=FakeRect(180, 70, 250, 110),
            on_click=lambda: toolbar_calls.append("toolbar"),
        )
        window = FakeWindow([delivery_page, order_page, order_tab, toolbar])
        window.element_info.name = r"Fakturama - D:\Yahia"
        clicks = []
        def click_at_screen(point):
            clicks.append(point)
            activate_order()
        window.click_at_screen = click_at_screen
        gateway = attached_gateway(window)
        gateway.ocr = FakeOCR([OCRMatch("New Order", Bounds(500, 140, 620, 165))])

        self.assertFalse(gateway._has_order_editor("P000003"))

        gateway._activate_document_tab("New Order", "P000003")

        self.assertEqual(len(clicks), 1)
        self.assertEqual(order_tab.click_count, 0)
        self.assertEqual(toolbar_calls, [])
        self.assertTrue(gateway._has_order_editor("P000003"))

    def test_order_line_lookup_reads_only_the_active_order_grid(self):
        gateway = WindowsFakturamaGateway()
        editor = object()
        order_row = object()
        delivery_row = object()
        gateway._active_order_editor_tab = lambda: editor
        gateway._current_window = lambda: editor
        gateway._list_rows = lambda window=None: (
            [order_row] if window is editor else [delivery_row]
        )
        gateway._row_values = lambda row: (
            {"Item No.": "A-1"}, "A-1 Order row"
        )

        self.assertIs(gateway._find_order_line_row("A-1"), order_row)

    def test_order_line_lookup_finds_visible_grid_sibling_scoped_to_active_order(self):
        editor = FakeElement("New Order", "Tab", bounds=FakeRect(0, 0, 1000, 700))
        editor.is_visible = lambda: True

        def make_grid(sku, *, visible):
            grid = FakeElement("", "DataGrid")
            headers = (
                "Pos.", "Qty.", "Item No.", "Picture", "Name", "Description",
                "VAT", "U.Price", "Discount", "Price",
            )
            grid.add(*[
                FakeElement(
                    header, "HeaderItem",
                    bounds=FakeRect(index * 80, 100, (index + 1) * 80, 120),
                )
                for index, header in enumerate(headers)
            ])
            row = FakeElement("", "DataItem", bounds=FakeRect(0, 200, 800, 225))
            row.is_visible = lambda: visible
            values = (
                "1", "1.00", sku, "", "Widget", "", "VAT (19%)", "£250.00", "0.00 %", "£250.00"
            )
            row.add(*[
                FakeElement(
                    value, "Text",
                    bounds=FakeRect(index * 80, 200, (index + 1) * 80, 225),
                )
                for index, value in enumerate(values)
            ])
            grid.add(row)
            return grid, row

        visible_grid, expected_row = make_grid("A-1", visible=True)
        hidden_grid, _ = make_grid("A-1", visible=False)
        window = FakeWindow([editor, visible_grid, hidden_grid])
        gateway = attached_gateway(window)
        gateway._active_order_editor_tab = lambda: editor

        self.assertIs(gateway._find_order_line_row("A-1"), expected_row)

    def test_order_line_lookup_waits_for_a_newly_selected_product_to_render(self):
        gateway = WindowsFakturamaGateway(timeout_seconds=0.1, poll_interval_seconds=0.001)
        row = object()
        calls = 0

        editor = object()
        gateway._active_order_editor_tab = lambda: editor
        gateway._current_window = lambda: editor

        def list_rows(window=None):
            nonlocal calls
            calls += 1
            return [row] if calls >= 3 else []

        gateway._list_rows = list_rows
        gateway._row_values = lambda active: ({"Item No.": "A-1"}, "A-1 Widget")

        self.assertIs(gateway._wait_for_order_line_row("A-1"), row)
        self.assertGreaterEqual(calls, 3)

    def test_find_documents_reads_an_unambiguous_saved_date(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        row = object()
        values = {
            "No.": "ORD-100",
            "Type": "Order",
            "Date": source.order_date.strftime("%d %b %Y"),
            "Cust.Ref.": source.external_reference,
            "Total": str(source.totals.gross),
            "State": "Open",
        }
        manager = object()
        search_calls = []
        row_scopes = []
        gateway._active_editor_context = lambda: None
        gateway._open_documents_view = lambda: None
        gateway._data_manager_tab = lambda label: manager if label == "Documents" else None
        gateway._set_field = lambda labels, value, *, window: search_calls.append(
            (labels, value, window)
        )
        gateway._wait_stable_rows = lambda *, window: row_scopes.append(window) or [row]
        gateway._row_values = lambda current: (values, "ORD-100 Order")

        rows = gateway.find_documents(source, "Order")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].document_date, source.order_date)
        self.assertEqual(search_calls, [(("Search",), source.external_reference, manager)])
        self.assertEqual(row_scopes, [manager])

    def test_saved_number_tab_is_identified_by_order_header(self):
        editor = document_editor(
            FakeElement("No.", "Edit", value="PO000009"),
            FakeElement("Cust.Ref.", "Edit", value="WEB-100"),
            name="PO000009",
        )
        gateway = attached_gateway(FakeWindow([editor]))

        self.assertIs(gateway._active_order_editor_tab(), editor)
        self.assertEqual(gateway._document_editor_identity(), (editor, "Order", "PO000009"))

    def test_document_ocr_requires_each_header_to_be_unique(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        gateway.ocr = TesseractOCR()
        gateway._capture_window = Mock(return_value=object())
        manager = FakeWindow()

        gateway.ocr.read_words = Mock(return_value=[])
        with self.assertRaisesRegex(ManualReviewRequired, "no unique Document header"):
            gateway._ocr_document_rows(manager, source, "Order", None)
        gateway.ocr.read_words.assert_called_once_with(
            gateway._capture_window.return_value, preprocess=False
        )

        gateway.ocr.read_words = Mock(return_value=[
            OCRMatch("Document", Bounds(10, 10, 90, 30)),
            OCRMatch("Document", Bounds(110, 10, 190, 30)),
        ])
        with self.assertRaisesRegex(ManualReviewRequired, "no unique Document header"):
            gateway._ocr_document_rows(manager, source, "Order", None)
        gateway.ocr.read_words.assert_called_once_with(
            gateway._capture_window.return_value, preprocess=False
        )

    def test_documents_cell_ocr_recovers_paid_open_and_decimal_total(self):
        source = sample_order()
        headers = [
            OCRMatch(label, Bounds(left, 10, left + 65, 25))
            for label, left in (
                ("Document", 10), ("Date", 100), ("Name", 200),
                ("Cust.Ref.", 300), ("State", 450), ("Total", 550),
                ("Printed", 650),
            )
        ]
        words = [
            *headers,
            OCRMatch("RE000001", Bounds(20, 50, 85, 65)),
            OCRMatch(str(source.order_date.day), Bounds(100, 50, 118, 65)),
            OCRMatch(source.order_date.strftime("%b"), Bounds(122, 50, 146, 65)),
            OCRMatch(str(source.order_date.year), Bounds(150, 50, 185, 65)),
            OCRMatch(source.external_reference, Bounds(315, 50, 430, 65)),
            OCRMatch("67830€", Bounds(560, 50, 620, 65)),
        ]
        image = Mock()
        image.crop.return_value.width = 120
        image.crop.return_value.height = 35
        gateway = WindowsFakturamaGateway()
        gateway.ocr = TesseractOCR()
        gateway.ocr.read_words = Mock(return_value=words)
        gateway.ocr.read_text_data = Mock(side_effect=[
            {"text": ["RE000001"]},
            {"text": ["W", "paid"]},
            {"text": ["678,30", "\u20ac"]},
        ])
        gateway._capture_window = Mock(return_value=image)
        gateway._read_from_window = Mock(return_value=source.external_reference)
        gateway._wait_until = Mock()
        editor = document_editor(
            FakeElement("No.", "Edit", value="RE000001"),
            FakeElement("Cust.Ref.", "Edit", value=source.external_reference),
            name="RE000001", kind="Invoice",
        )
        gateway._document_editor_identity = Mock(
            return_value=(editor, "Invoice", "RE000001")
        )
        gateway._invoice_link = Mock(return_value="PO000009")
        manager = FakeWindow()

        with (
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch("pywinauto.mouse.double_click") as double_click,
        ):
            rows = gateway._ocr_document_rows(manager, source, "Invoice", "PO000009")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "paid")
        self.assertEqual(rows[0].total, source.totals.gross)
        self.assertEqual(rows[0].linked_order_number, "PO000009")
        self.assertEqual(gateway.ocr.read_text_data.call_count, 3)
        self.assertTrue(all(
            call.kwargs["config"] == "--psm 7"
            for call in gateway.ocr.read_text_data.call_args_list
        ))
        gateway.ocr.read_words.assert_called_once_with(image, preprocess=False)
        double_click.assert_called_once()

        # The visible Open row's icon was read as two leading glyphs and a grid bar.
        gateway.ocr.read_text_data = Mock(side_effect=[
            {"text": ["RE000001"]},
            {"text": ["Ei", "open", "|"]},
            {"text": ["678,30", "\u20ac"]},
        ])
        with (
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch("pywinauto.mouse.double_click"),
        ):
            open_rows = gateway._ocr_document_rows(manager, source, "Invoice", "PO000009")
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0].state, "open")
        self.assertEqual(open_rows[0].total, source.totals.gross)

        # Focused OCR can insert an extra zero; retain the independent table
        # reading, but require the authoritative editor number to match it.
        gateway.ocr.read_text_data = Mock(side_effect=[
            {"text": ["RE0000001"]},
            {"text": ["paid"]},
            {"text": ["678,30", "\u20ac"]},
        ])
        gateway._wait_until = Mock(
            side_effect=lambda label, predicate: self.assertTrue(predicate())
        )
        with (
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch("pywinauto.mouse.double_click"),
        ):
            recovered = gateway._ocr_document_rows(manager, source, "Invoice", "PO000009")
        self.assertEqual(recovered[0].number, "RE000001")

        gateway.ocr.read_words.return_value = [
            OCRMatch(source.external_reference[:8] + "...", word.bounds)
            if word.text == source.external_reference else word for word in words
        ]
        gateway.ocr.read_text_data = Mock(side_effect=[
            {"text": ["RE000001"]}, {"text": ["paid"]}, {"text": ["678,30"]},
        ])
        with (
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch("pywinauto.mouse.double_click"),
        ):
            clipped = gateway._ocr_document_rows(manager, source, "Invoice", "PO000009")
        self.assertEqual(clipped[0].reference, source.external_reference)

        gateway._wait_until = Mock()
        gateway._document_editor_identity.return_value = (editor, "Invoice", "RE999999")
        gateway.ocr.read_text_data = Mock(side_effect=[
            {"text": ["RE0000001"]}, {"text": ["paid"]}, {"text": ["678,30"]},
        ])
        with (
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch("pywinauto.mouse.double_click"),
            self.assertRaisesRegex(ManualReviewRequired, "differs from row"),
        ):
            gateway._ocr_document_rows(manager, source, "Invoice", "PO000009")

    def test_linked_invoice_uses_saved_order_button_without_extra_save(self):
        source = sample_order()
        follow_up = FakeElement("Invoice", "Button")
        unrelated = FakeElement("Invoice", "Button")
        editor = document_editor(
            FakeElement("No.", "Edit", value="PO000009"),
            FakeElement("Cust.Ref.", "Edit", value=source.external_reference),
            follow_up,
            name="PO000009",
        )
        gateway = attached_gateway(FakeWindow([editor, unrelated]))
        gateway._last_source = source
        gateway.find_documents = Mock(return_value=[DocumentRow(
            number="PO000009", type="Order", reference=source.external_reference,
            total=source.totals.gross, state="Open", document_date=source.order_date,
        )])
        gateway._wait_until = Mock()
        gateway._read_optional = Mock(return_value="RE000001")
        gateway._dispatch_save = Mock()

        invoice = gateway.create_linked_invoice("PO000009")

        self.assertEqual(invoice.linked_order_number, "PO000009")
        self.assertEqual(invoice.number, "RE000001")
        self.assertEqual(follow_up.click_count, 1)
        self.assertEqual(unrelated.click_count, 0)
        gateway._dispatch_save.assert_not_called()

    def test_verify_order_document_requires_matching_saved_date(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        common = dict(
            number="ORD-100",
            type="Order",
            reference=source.external_reference,
            total=source.totals.gross,
            state="Open",
        )
        gateway.find_documents = lambda *args, **kwargs: [DocumentRow(**common)]
        with self.assertRaises(ManualReviewRequired):
            gateway.verify_order_document(source, "ORD-100")
        gateway.find_documents = lambda *args, **kwargs: [
            DocumentRow(**common, document_date=source.order_date - timedelta(days=1))
        ]
        with self.assertRaises(PostconditionFailed):
            gateway.verify_order_document(source, "ORD-100")

    def test_verified_saved_order_restores_source_and_number_context(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        row = DocumentRow(
            number="PO000009", type="Order", reference=source.external_reference,
            total=source.totals.gross, state="Open", document_date=source.order_date,
        )
        gateway.find_documents = Mock(return_value=[row])

        self.assertIs(gateway.verify_order_document(source, "PO000009"), row)
        self.assertIs(gateway._last_source, source)
        self.assertEqual(gateway._last_order_number, "PO000009")

    def test_date_readback_rejects_ambiguous_numeric_format(self):
        self.assertTrue(WindowsFakturamaGateway._date_matches(date(2026, 7, 18), "18/07/2026"))
        self.assertFalse(WindowsFakturamaGateway._date_matches(date(2026, 7, 4), "04/07/2026"))

    def test_invoice_copy_verification_requires_visible_order_link(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        self._stub_invoice_copy_readback(gateway, source, linked=None)

        result = gateway.verify_invoice_copied_order(source, "ORD-100")

        self.assertFalse(result.verified)
        self.assertIn("Invoice's source Order number could not be read back", result.observations)

    def test_open_invoice_discovery_rejects_wrong_or_unreadable_order_link(self):
        source = sample_order()
        editor = document_editor(
            FakeElement("No.", "Edit", value="INV-100"),
            FakeElement("Cust.Ref.", "Edit", value=source.external_reference),
            name="INV-100", kind="Invoice",
        )
        gateway = attached_gateway(FakeWindow([editor]))

        gateway._invoice_link = Mock(return_value="ORD-999")
        with self.assertRaises(ManualReviewRequired):
            gateway.discover_open_invoice(source, "ORD-100")

        gateway._invoice_link = Mock(return_value=None)
        with self.assertRaises(ManualReviewRequired):
            gateway.discover_open_invoice(source, "ORD-100")

    def test_open_invoice_discovery_records_exact_visible_order_link(self):
        source = sample_order()
        editor = document_editor(
            FakeElement("No.", "Edit", value="INV-100"),
            FakeElement("Cust.Ref.", "Edit", value=source.external_reference),
            FakeElement("Date", "Edit", value="25 Sep 2026"),
            FakeElement("Service date", "Edit", value="25 Sep 2026"),
            name="INV-100", kind="Invoice",
        )
        gateway = attached_gateway(FakeWindow([editor]))
        gateway._invoice_link = Mock(return_value="ORD-100")
        gateway._invoice_ref = InvoiceEditorRef(
            token="observed-invoice", number="INV-100", linked_order_number="ORD-100",
            proposed_invoice_date="24 Sep 2026",
            proposed_service_date="24 Sep 2026",
        )

        ref = gateway.discover_open_invoice(source, "ORD-100")

        self.assertIsNotNone(ref)
        self.assertEqual(ref.number, "INV-100")
        self.assertEqual(ref.linked_order_number, "ORD-100")
        self.assertEqual(ref.proposed_invoice_date, "25 Sep 2026")
        self.assertEqual(ref.proposed_service_date, "25 Sep 2026")
        self.assertEqual(gateway._invoice_ref.proposed_invoice_date, "24 Sep 2026")
        self.assertEqual(gateway._invoice_ref.proposed_service_date, "24 Sep 2026")

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
