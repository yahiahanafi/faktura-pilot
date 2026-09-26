from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    GatewayError,
    ManualReviewRequired,
    PaymentMethodCandidate,
    PostconditionFailed,
    VatCandidate,
)
from faktura_pilot.automation.resolver import Bounds, OCRMatch, TesseractOCR
from faktura_pilot.automation.windows import WindowsFakturamaGateway
from faktura_pilot.domain.policy import payment_code_for_method
from tests.automation.test_resolver import FakeElement, FakeOCR, FakeRect, FakeWindow
from tests.automation.test_windows_gateway import (
    RoleCheckbox,
    VisibleElement,
    attached_gateway,
    product_editor_window,
)
from tests.factories import sample_order


class EdgeNoiseOCR(TesseractOCR):
    """Saved empty selector's header/footer text plus a right-border glyph."""

    def read_text_data(self, image, *, config=""):
        words = [
            ("First", 403, 163, 68, 24, 1),
            ("Name", 487, 163, 92, 24, 1),
            ("Name", 695, 163, 92, 24, 1),
            ("Company", 914, 162, 154, 32, 1),
            ("ZIP", 1214, 163, 53, 24, 1),
            ("City", 1460, 162, 135, 32, 1),
            ("~", 1584, 207, 16, 1, 2),
            ("Cancel", 1391, 1007, 85, 38, 3),
        ]
        columns = ("text", "left", "top", "width", "height", "line_num")
        data = {name: [word[index] for word in words] for index, name in enumerate(columns)}
        data["block_num"] = [1] * len(words)
        data["par_num"] = [1] * len(words)
        return data


def empty_selector_with_edge_noise():
    dialog = FakeWindow([FakeElement("Search", "Edit", value="Northstar Office")])
    dialog.capture_as_image = lambda: SimpleNamespace(width=1600, height=1100)
    gateway = attached_gateway(dialog)
    gateway.ocr = EdgeNoiseOCR()
    return gateway, dialog


def selector_ocr_data_with_background_headers(
    *, duplicate_inside_dialog: bool, include_ocr_footer: bool = True
):
    columns = (
        ("First", 100, 42),
        ("Name", 145, 42),
        ("Name", 210, 42),
        ("Company", 280, 74),
        ("ZIP", 390, 28),
        ("City", 470, 32),
    )
    words = [(text, left, 20, width, 20, 1) for text, left, width in columns]
    if duplicate_inside_dialog:
        words.extend(
            (text, left, 100, width, 20, 2) for text, left, width in columns
        )
    words.extend(
        [
            ("No", 100, 200, 20, 20, 3),
            ("entries", 125, 200, 50, 20, 3),
        ]
    )
    if include_ocr_footer:
        words.extend(
            [
                ("OK", 250, 500, 25, 20, 4),
                ("Cancel", 340, 500, 60, 20, 4),
            ]
        )
    words.extend(
        (text, left, 560, width, 20, 5) for text, left, width in columns
    )
    names = ("text", "left", "top", "width", "height", "line_num")
    data = {name: [word[index] for word in words] for index, name in enumerate(names)}
    data["block_num"] = [1] * len(words)
    data["par_num"] = [1] * len(words)
    return data


def selector_gateway_with_background_headers(
    *, duplicate_inside_dialog: bool, native_footer: bool = False
):
    buttons = (
        [
            VisibleElement("OK", "Button", bounds=FakeRect(250, 500, 275, 520)),
            VisibleElement("Cancel", "Button", bounds=FakeRect(340, 500, 400, 520)),
        ]
        if native_footer else []
    )
    dialog = FakeWindow(buttons, bounds=FakeRect(0, 0, 600, 650))
    dialog.capture_as_image = lambda: SimpleNamespace(width=600, height=650)
    gateway = attached_gateway(dialog)
    gateway.ocr = TesseractOCR()
    gateway.ocr.read_text_data = Mock(
        return_value=selector_ocr_data_with_background_headers(
            duplicate_inside_dialog=duplicate_inside_dialog,
            include_ocr_footer=not native_footer,
        )
    )
    return gateway, dialog


def selector_gateway_with_customer_row(*, extra_line: str):
    headers = (
        ("First", 100, 42),
        ("Name", 145, 42),
        ("Name", 210, 42),
        ("Company", 280, 74),
        ("ZIP", 390, 28),
        ("City", 470, 32),
    )
    words = [(text, left, 20, width, 20, 1) for text, left, width in headers]
    words.extend(
        [
            ("Marta", 105, 80, 55, 20, 2),
            ("Klein", 210, 80, 42, 20, 2),
            ("Northstar", 280, 80, 74, 20, 2),
            ("10117", 390, 80, 35, 20, 2),
            ("Berlin", 470, 80, 32, 20, 2),
            (extra_line, 485, 150, 15, 20, 3),
            ("Cancel", 340, 500, 60, 20, 4),
        ]
    )
    names = ("text", "left", "top", "width", "height", "line_num")
    data = {name: [word[index] for word in words] for index, name in enumerate(names)}
    data["block_num"] = [1] * len(words)
    data["par_num"] = [1] * len(words)
    dialog = FakeWindow(bounds=FakeRect(0, 0, 600, 650))
    dialog.capture_as_image = lambda: SimpleNamespace(width=600, height=650)
    gateway = attached_gateway(dialog)
    gateway.ocr = TesseractOCR()
    gateway.ocr.read_text_data = Mock(return_value=data)
    return gateway, dialog


class DebtorFlowGateway(WindowsFakturamaGateway):
    """Record Debtor form actions while exercising the real address flow."""

    def __init__(self) -> None:
        super().__init__(timeout_seconds=0.03, poll_interval_seconds=0.001)
        self.events: list[tuple] = []

    def _require_any_labels(self, *labels):
        return None

    def _activate_debtor_tab(self, label):
        self.events.append(("tab", label))
        return None

    def _active_editor_tab(self):
        return "New Debtor editor"

    def _address_form_scope(self, context):
        self.events.append(("scope", context[-1]))
        return context[-1]

    def _set_composite_row(self, label, values, *, scope):
        self.events.append(("composite", label, values, scope))
        return True

    def _set_field(self, labels, value, *, scope=None, **kwargs):
        self.events.append(("field", labels[0], value, scope))
        return True

    def _assign_address_roles(self, roles):
        self.events.append(("roles", tuple(roles)))

    def _open_or_create_delivery_address(self):
        self.events.append(("create_additional",))
        return "additional address #1"

    def _select_named_option(self, labels, value, *, optional=False):
        self.events.append(("select", labels[0], value))
        return True


def separate_address_role_popup(*, invoice_checked=False, committed_override=None):
    editor = VisibleElement("New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300))
    main_address = VisibleElement(
        "Main address", "Tab", bounds=FakeRect(760, 500, 1700, 1100)
    )
    row = VisibleElement("", "Pane", bounds=FakeRect(780, 590, 1400, 650))
    field = VisibleElement(
        "", "Edit", bounds=FakeRect(940, 605, 1110, 635), value=""
    )
    invoice = RoleCheckbox(
        "Invoice address", "CheckBox", checked=invoice_checked,
        bounds=FakeRect(1130, 650, 1320, 680),
    )
    delivery = RoleCheckbox(
        "Delivery address", "CheckBox", bounds=FakeRect(1130, 690, 1320, 720)
    )
    popup = VisibleElement(
        "Address type options", "Pane", bounds=FakeRect(1115, 640, 1340, 740)
    )
    popup.handle = 331352
    popup.add(invoice, delivery)
    state = {"open": False}

    def open_popup():
        state["open"] = True

    def dismiss_popup():
        selected = [
            name for name, checkbox in (
                ("Invoice address", invoice),
                ("Delivery address", delivery),
            )
            if checkbox.is_checked()
        ]
        field.set_edit_text(
            committed_override if committed_override is not None else ", ".join(selected)
        )
        state["open"] = False

    label = VisibleElement(
        "address type", "Text", bounds=FakeRect(800, 605, 920, 635),
        on_click=dismiss_popup,
    )
    selector = VisibleElement(
        "", "Button", bounds=FakeRect(1120, 605, 1150, 635),
        on_click=open_popup,
    )
    row.add(label, field, selector)
    main_address.add(row)
    editor.add(main_address)
    window = FakeWindow([editor])
    gateway = attached_gateway(window)
    gateway._current_window = lambda: popup if state["open"] else window
    gateway._focus_window_for_automation = lambda target: None
    return gateway, editor, field, invoice, delivery, label, selector, state


def nested_address_gateway():
    state = {"section": "Miscellaneous", "address": "Main address"}
    selections = []
    editor = VisibleElement("New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300))
    addresses = VisibleElement("Addresses", "Tab")
    addresses.is_visible = lambda: state["section"] == "Addresses"
    addresses_item = VisibleElement("Addresses", "TabItem")
    addresses_item.is_selected = lambda: state["section"] == "Addresses"

    def select_addresses():
        selections.append("Addresses")
        state["section"] = "Addresses"

    addresses_item.select = select_addresses
    editor.add(addresses_item, addresses)
    pages = {}
    for label in ("Main address", "additional address #1"):
        page = VisibleElement(label, "Tab")
        page.is_visible = lambda label=label: (
            state["section"] == "Addresses" and state["address"] == label
        )
        item = VisibleElement(label, "TabItem")
        item.is_visible = lambda: state["section"] == "Addresses"
        item.is_selected = lambda label=label: (
            state["section"] == "Addresses" and state["address"] == label
        )

        def select_page(label=label):
            selections.append(label)
            state["address"] = label

        item.select = select_page
        editor.add(item, page)
        pages[label] = page
    gateway = attached_gateway(FakeWindow([editor]))
    gateway.ocr = FakeOCR([])
    return gateway, pages, selections


class DebtorRequirementTests(unittest.TestCase):
    def test_native_company_write_preserves_literal_value_and_emits_reversible_edit(self):
        self._assert_native_contact_write(
            "Company", "Northstar {Office} + ^ % ~ GmbH"
        )

    def _assert_native_contact_write(self, label, value):
        events = []
        element = FakeElement(label, "Edit", value="")
        element.handle = 1234
        original_set = element.set_edit_text
        original_click = element.click_input

        def set_text(text):
            events.append(("set", text))
            original_set(text)

        def click():
            events.append(("click",))
            original_click()

        def has_focus():
            events.append(("focus_check",))
            return True

        element.set_edit_text = set_text
        element.click_input = click
        element.has_keyboard_focus = has_focus
        keyboard = ModuleType("pywinauto.keyboard")
        keyboard.send_keys = Mock(
            side_effect=lambda keys, **kwargs: events.append(
                ("keys", keys, kwargs)
            )
        )
        package = ModuleType("pywinauto")
        package.__path__ = []

        with (
            patch(
                "faktura_pilot.automation.windows.os",
                SimpleNamespace(name="nt"),
            ),
            patch.dict(
                sys.modules,
                {"pywinauto": package, "pywinauto.keyboard": keyboard},
            ),
            patch.object(
                WindowsFakturamaGateway,
                "_focus_window_for_automation",
                side_effect=lambda target: events.append(("foreground", target)),
            ),
        ):
            WindowsFakturamaGateway._write_element(element, value)

        self.assertEqual(element.get_value(), value)
        self.assertEqual(
            events,
            [
                ("foreground", element),
                ("set", value),
                ("click",),
                ("focus_check",),
                (
                    "keys",
                    "{END}{SPACE}{BACKSPACE}",
                    {"pause": 0.01, "vk_packet": False},
                ),
            ],
        )

    def test_native_company_write_requires_keyboard_focus_before_event(self):
        element = FakeElement("Company", "Edit", value="")
        element.handle = 1234
        element.has_keyboard_focus = lambda: False
        keyboard = ModuleType("pywinauto.keyboard")
        keyboard.send_keys = Mock()
        package = ModuleType("pywinauto")
        package.__path__ = []

        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch.dict(
                sys.modules,
                {"pywinauto": package, "pywinauto.keyboard": keyboard},
            ),
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
            self.assertRaisesRegex(ManualReviewRequired, "keyboard focus"),
        ):
            WindowsFakturamaGateway._write_element(element, "literal + value")

        keyboard.send_keys.assert_not_called()

    def test_email_and_handleless_company_use_normal_set_text(self):
        for label, handle in (("Company", None), ("E-Mail", 1234)):
            with self.subTest(label=label):
                element = FakeElement(label, "Edit", value="")
                if handle is not None:
                    element.handle = handle
                keyboard = ModuleType("pywinauto.keyboard")
                keyboard.send_keys = Mock()
                package = ModuleType("pywinauto")
                package.__path__ = []

                with (
                    patch(
                        "faktura_pilot.automation.windows.os",
                        SimpleNamespace(name="nt"),
                    ),
                    patch.dict(
                        sys.modules,
                        {"pywinauto": package, "pywinauto.keyboard": keyboard},
                    ),
                    patch.object(
                        WindowsFakturamaGateway, "_focus_window_for_automation"
                    ) as focus,
                ):
                    WindowsFakturamaGateway._write_element(element, "mira+sales@example.de")

                self.assertEqual(element.get_value(), "mira+sales@example.de")
                self.assertEqual(element.click_count, 0)
                focus.assert_not_called()
                keyboard.send_keys.assert_not_called()

    def test_main_window_close_guard_skips_generic_close_action(self):
        main = FakeWindow([VisibleElement("Close", "Button")])
        main.handle = 77
        gateway = attached_gateway(main)

        for current in (main, FakeWindow([VisibleElement("Close", "Button")])):
            with self.subTest(same_wrapper=current is main):
                current.handle = 77
                with (
                    patch.object(gateway, "_current_window", return_value=current),
                    patch.object(
                        gateway, "_dialog_is_open",
                        side_effect=AssertionError("main window is not a dialog"),
                    ),
                ):
                    gateway._close_dialog_if_present(("terms of payment", "Payment"))
                self.assertEqual(
                    [element.click_count for element in current.descendants()], [0]
                )

    def test_payment_lookup_uses_manager_reader_without_debtor_dropdown(self):
        gateway = WindowsFakturamaGateway()
        candidates = [
            PaymentMethodCandidate("term-1", "Bank Transfer", "Credit transfer")
        ]

        with (
            patch(
                "faktura_pilot.automation.payment_terms.find",
                return_value=candidates,
            ) as manager_find,
            patch.object(
                gateway, "_debtor_payment_options",
                side_effect=AssertionError("Debtor dropdown must not be used"),
            ),
        ):
            observed = gateway.find_payment_methods("Bank Transfer")

        self.assertIs(observed, candidates)
        manager_find.assert_called_once_with(gateway, "Bank Transfer")

    def test_nested_address_tab_activates_parent_addresses_after_miscellaneous(self):
        for target in ("Main address", "additional address #1"):
            with self.subTest(target=target):
                gateway, pages, selections = nested_address_gateway()

                active = gateway._activate_debtor_tab(target)

                self.assertIs(active, pages[target])
                self.assertEqual(
                    selections,
                    ["Addresses"] + ([] if target == "Main address" else [target]),
                )

    def test_payment_label_selects_unnamed_payment_combo_box(self):
        source_method = PaymentMethodCandidate(
            "bank-transfer", "Bank Transfer", "Credit transfer"
        )
        label = VisibleElement(
            "Payment", "Text", bounds=FakeRect(800, 600, 950, 635)
        )
        combo = VisibleElement(
            "", "ComboBox", bounds=FakeRect(970, 600, 1400, 635), value=""
        )
        gateway = attached_gateway(FakeWindow([label, combo]))

        gateway.select_payment_method(source_method)

        self.assertEqual(combo.get_value(), source_method.name)

    def test_separate_role_popup_commits_both_roles_for_shared_address(self):
        (
            gateway, editor, field, invoice, delivery, label, selector, state
        ) = separate_address_role_popup()
        self.assertNotIn(invoice, editor.descendants())

        gateway._assign_address_roles(("Invoice address", "Delivery address"))

        self.assertTrue(invoice.is_checked())
        self.assertTrue(delivery.is_checked())
        self.assertEqual(field.get_value(), "Invoice address, Delivery address")
        self.assertFalse(state["open"])
        self.assertEqual(selector.click_count, 1)
        self.assertEqual(label.click_count, 1)

    def test_separate_role_popup_clears_inherited_invoice_for_delivery_only(self):
        gateway, _, field, invoice, delivery, label, selector, state = (
            separate_address_role_popup(invoice_checked=True)
        )

        gateway._assign_address_roles(("Delivery address",))

        self.assertFalse(invoice.is_checked())
        self.assertTrue(delivery.is_checked())
        self.assertEqual(field.get_value(), "Delivery address")
        self.assertFalse(state["open"])
        self.assertEqual(selector.click_count, 1)
        self.assertEqual(label.click_count, 1)

    def test_separate_role_popup_rejects_uncommitted_field_mismatch(self):
        gateway, _, field, invoice, delivery, _, _, state = (
            separate_address_role_popup(committed_override="Invoice address")
        )

        with self.assertRaises(GatewayError):
            gateway._assign_address_roles(("Invoice address", "Delivery address"))

        self.assertTrue(invoice.is_checked())
        self.assertTrue(delivery.is_checked())
        self.assertEqual(field.get_value(), "Invoice address")
        self.assertFalse(state["open"])

    def test_save_precheck_rejects_changed_company_before_dispatch(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        gateway._last_source = source

        with (
            patch.object(gateway, "_active_editor_tab", return_value="editor"),
            patch.object(gateway, "_read_optional", return_value="Changed Company"),
            patch.object(gateway, "_dispatch_save") as dispatch_save,
        ):
            with self.assertRaisesRegex(PostconditionFailed, "Company before Save"):
                gateway.save_debtor()

        dispatch_save.assert_not_called()

    def test_save_precheck_rejects_changed_delivery_email_before_dispatch(self):
        source = sample_order()
        gateway = WindowsFakturamaGateway()
        gateway._last_source = source
        gateway._billing_address_tab_label = "Main address"
        gateway._delivery_address_tab_label = "additional address #1"

        def read_field(labels, *, scope):
            label = labels[0]
            if label == "Company":
                return source.debtor.company
            if label == "Email" and scope == "additional address #1":
                return "changed@example.org"
            if label == "Email":
                return source.debtor.email
            if label == "Telephone":
                return source.debtor.telephone
            raise AssertionError(f"unexpected readback: {labels!r}, {scope!r}")

        with (
            patch.object(gateway, "_active_editor_tab", return_value="editor"),
            patch.object(
                gateway, "_address_form_scope",
                side_effect=lambda context: context[-1],
            ),
            patch.object(gateway, "_read_optional", side_effect=read_field),
            patch.object(gateway, "_verify_address_fields") as verify_addresses,
            patch.object(gateway, "_dispatch_save") as dispatch_save,
        ):
            with self.assertRaisesRegex(
                PostconditionFailed, "additional address #1 Email before Save"
            ):
                gateway.save_debtor()

        verify_addresses.assert_not_called()
        dispatch_save.assert_not_called()

    def test_address_scope_reuses_activated_tab_without_second_lookup(self):
        gateway = WindowsFakturamaGateway()
        active_tab = object()

        with (
            patch.object(gateway, "_activate_debtor_tab", return_value=active_tab) as activate,
            patch.object(
                gateway, "_active_debtor_tab",
                side_effect=AssertionError("activated tab must be reused"),
            ),
        ):
            scope = gateway._address_form_scope(("Main address",))

        self.assertIs(scope, active_tab)
        activate.assert_called_once_with("Main address")

    def test_address_fields_follow_form_order(self):
        address = sample_order().debtor.billing_address
        gateway = DebtorFlowGateway()

        gateway._fill_address(address, context=("Main address",))

        self.assertEqual(
            [event for event in gateway.events if event[0] in {"field", "composite"}],
            [
                ("field", "Additional name", address.additional_name, "Main address"),
                ("field", "Street", address.street, "Main address"),
                (
                    "composite",
                    "ZIP - City",
                    (address.zip_code, address.city),
                    "Main address",
                ),
                ("field", "Country", address.country, "Main address"),
            ],
        )

    def test_shared_billing_and_delivery_uses_one_address_with_both_roles(self):
        source_debtor = sample_order().debtor
        debtor = source_debtor.model_copy(
            update={"delivery_address": source_debtor.billing_address}
        )
        gateway = DebtorFlowGateway()

        gateway.fill_debtor(debtor)

        self.assertEqual(
            [event for event in gateway.events if event[0] == "scope"],
            [("scope", "Main address")],
        )
        self.assertEqual(
            [event for event in gateway.events if event[0] == "roles"],
            [("roles", ("Invoice address", "Delivery address"))],
        )
        self.assertNotIn(("create_additional",), gateway.events)
        self.assertEqual(
            next(event for event in gateway.events if event[0] in {"field", "composite"}),
            ("field", "Company", debtor.company, "New Debtor editor"),
        )
        self.assertIn(("field", "Email", debtor.email, None), gateway.events)
        self.assertIn(("field", "Telephone", debtor.telephone, None), gateway.events)
        self.assertLess(
            gateway.events.index(("roles", ("Invoice address", "Delivery address"))),
            gateway.events.index(("tab", "Miscellaneous")),
        )

    def test_distinct_delivery_uses_new_address_with_delivery_role_only(self):
        debtor = sample_order().debtor
        gateway = DebtorFlowGateway()

        gateway.fill_debtor(debtor)

        self.assertEqual(
            [event for event in gateway.events if event[0] == "scope"],
            [("scope", "Main address"), ("scope", "additional address #1")],
        )
        self.assertIn(
            ("field", "Email", debtor.email, "additional address #1"),
            gateway.events,
        )
        self.assertIn(
            ("field", "Telephone", debtor.telephone, "additional address #1"),
            gateway.events,
        )
        self.assertEqual(
            [event for event in gateway.events if event[0] == "roles"],
            [
                ("roles", ("Invoice address",)),
                ("roles", ("Delivery address",)),
            ],
        )
        self.assertEqual(gateway.events.count(("create_additional",)), 1)
        self.assertLess(
            gateway.events.index(("roles", ("Invoice address",))),
            gateway.events.index(("create_additional",)),
        )
        self.assertLess(
            gateway.events.index(("roles", ("Delivery address",))),
            gateway.events.index(("tab", "Miscellaneous")),
        )

    def test_hidden_main_address_tab_fills_every_source_address_field_and_reads_back(self):
        address = sample_order().debtor.billing_address
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 2200, 1300)
        )
        tab_item = FakeElement("Main address", "TabItem")
        tab_item.is_selected = lambda: 1
        tab_item.is_visible = lambda: False
        main_address = FakeElement("Main address", "Tab")
        main_address.is_visible = lambda: False

        def row(label: str, top: int, control_type: str = "Edit") -> FakeElement:
            container = VisibleElement("", "Pane", bounds=FakeRect(780, top, 1500, top + 45))
            field = VisibleElement(
                "", control_type, bounds=FakeRect(1000, top + 5, 1450, top + 35), value=""
            )
            container.add(
                VisibleElement(
                    label, "Text", bounds=FakeRect(800, top + 5, 980, top + 35)
                ),
                field,
            )
            main_address.add(container)
            return field

        street = row("Street", 520)
        additional_name = row("Additional name", 580)
        zip_city = VisibleElement("", "Pane", bounds=FakeRect(780, 640, 1500, 685))
        zip_code = VisibleElement(
            "", "Edit", bounds=FakeRect(1000, 645, 1140, 675), value=""
        )
        city = VisibleElement("", "Edit", bounds=FakeRect(1160, 645, 1450, 675), value="")
        zip_city.add(
            VisibleElement("ZIP - City", "Text", bounds=FakeRect(800, 645, 980, 675)),
            zip_code,
            city,
        )
        main_address.add(zip_city)
        country = row("Country", 700, "ComboBox")
        editor.add(tab_item, main_address)
        gateway = attached_gateway(FakeWindow([editor]))

        gateway._fill_address(address, context=("Main address",))
        result = gateway._verify_address_fields(address, context=("Main address",))

        self.assertEqual(street.get_value(), address.street)
        self.assertEqual(additional_name.get_value(), address.additional_name)
        self.assertEqual(zip_code.get_value(), address.zip_code)
        self.assertEqual(city.get_value(), address.city)
        self.assertEqual(country.get_value(), address.country)
        self.assertEqual(tab_item.click_count, 0)
        self.assertTrue(result.verified, result.observations)
        self.assertEqual(
            result.observed,
            {
                "Street": address.street,
                "ZIP": address.zip_code,
                "City": address.city,
                "Country": address.country,
                "Additional name": address.additional_name,
            },
        )

    def test_zip_city_row_with_telefax_edit_fills_only_address_fields(self):
        address = sample_order().debtor.billing_address
        editor = VisibleElement(
            "New Debtor", "Tab", bounds=FakeRect(700, 200, 3800, 1300)
        )
        main_address = VisibleElement(
            "Main address", "Tab", bounds=FakeRect(800, 300, 3780, 1270)
        )
        street = VisibleElement("Street", "Edit", value="")
        additional_name = VisibleElement("Additional name", "Edit", value="")
        country = VisibleElement("Country", "ComboBox", value="")
        shared_row = VisibleElement("", "Pane", bounds=FakeRect(950, 1000, 3760, 1120))
        zip_code = VisibleElement(
            "", "Edit", bounds=FakeRect(1118, 1041, 1240, 1089), value=""
        )
        city = VisibleElement(
            "", "Edit", bounds=FakeRect(1256, 1041, 2380, 1089), value=""
        )
        telefax = VisibleElement(
            "", "Edit", bounds=FakeRect(2608, 1041, 3744, 1089), value="KEEP-FAX"
        )
        shared_row.add(
            VisibleElement(
                "ZIP - City", "Text", bounds=FakeRect(986, 1043, 1108, 1085)
            ),
            zip_code,
            city,
            VisibleElement(
                "Telefax", "Text", bounds=FakeRect(2508, 1043, 2598, 1085)
            ),
            telefax,
        )
        main_address.add(street, additional_name, shared_row, country)
        editor.add(main_address)
        gateway = attached_gateway(FakeWindow([editor]))

        gateway._fill_address(address, context=("Main address",))
        readback = gateway._verify_address_fields(address, context=("Main address",))

        self.assertEqual(zip_code.get_value(), address.zip_code)
        self.assertEqual(city.get_value(), address.city)
        self.assertEqual(street.get_value(), address.street)
        self.assertEqual(country.get_value(), address.country)
        self.assertEqual(additional_name.get_value(), address.additional_name)
        self.assertEqual(telefax.get_value(), "KEEP-FAX")
        self.assertTrue(readback.verified, readback.observations)

    def test_selector_accepts_complete_customer_row_despite_scrollbar_arrow_line(self):
        gateway, dialog = selector_gateway_with_customer_row(extra_line=">")

        with patch(
            "faktura_pilot.automation.windows.verified_empty_selector",
            return_value=False,
        ):
            rows = gateway._ocr_debtor_table_rows(dialog, "Northstar Office GmbH")

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            {key: rows[0][key] for key in (
                "first_name", "last_name", "company", "zip_code", "city"
            )},
            {
                "first_name": "Marta",
                "last_name": "Klein",
                "company": "Northstar",
                "zip_code": "10117",
                "city": "Berlin",
            },
        )

    def test_selector_rejects_alphanumeric_city_only_line_beside_valid_row(self):
        gateway, dialog = selector_gateway_with_customer_row(extra_line="Bogus")

        with (
            patch(
                "faktura_pilot.automation.windows.verified_empty_selector",
                return_value=False,
            ),
            self.assertRaisesRegex(ManualReviewRequired, "cannot be verified"),
        ):
            gateway._ocr_debtor_table_rows(dialog, "Northstar Office GmbH")

    def test_selector_ignores_background_headers_below_dialog_footer(self):
        gateway, dialog = selector_gateway_with_background_headers(
            duplicate_inside_dialog=False
        )

        rows = gateway._ocr_debtor_table_rows(dialog, "Northstar Office")

        self.assertEqual(rows, [])
        gateway.ocr.read_text_data.assert_called_once()

    def test_selector_uses_visible_button_footer_when_ocr_misses_footer(self):
        gateway, dialog = selector_gateway_with_background_headers(
            duplicate_inside_dialog=False, native_footer=True
        )
        data = gateway.ocr.read_text_data.return_value
        self.assertNotIn("OK", data["text"])
        self.assertNotIn("Cancel", data["text"])

        rows = gateway._ocr_debtor_table_rows(dialog, "Northstar Office")

        self.assertEqual(rows, [])
        gateway.ocr.read_text_data.assert_called_once()

    def test_selector_rejects_duplicate_headers_inside_dialog(self):
        gateway, dialog = selector_gateway_with_background_headers(
            duplicate_inside_dialog=True
        )

        with self.assertRaisesRegex(ManualReviewRequired, "unique set"):
            gateway._ocr_debtor_table_rows(dialog, "Northstar Office")

    def test_verified_empty_selector_ignores_ocr_border_glyph(self):
        gateway, dialog = empty_selector_with_edge_noise()

        with (
            patch.object(gateway, "_focus_window_for_automation"),
            patch(
                "faktura_pilot.automation.windows.verified_empty_selector",
                return_value=True,
            ) as verified_empty,
        ):
            rows = gateway._ocr_debtor_table_rows(dialog, "Northstar Office")

        self.assertEqual(rows, [])
        verified_empty.assert_called_once()

    def test_unverified_empty_selector_still_rejects_ocr_border_glyph(self):
        gateway, dialog = empty_selector_with_edge_noise()

        with (
            patch.object(gateway, "_focus_window_for_automation"),
            patch(
                "faktura_pilot.automation.windows.verified_empty_selector",
                return_value=False,
            ),
            self.assertRaisesRegex(ManualReviewRequired, "stable empty result"),
        ):
            gateway._ocr_debtor_table_rows(dialog, "Northstar Office")

    def test_payment_creation_delegates_to_manager_workflow(self):
        name = "Bank Transfer"
        mapped_code = payment_code_for_method(name)
        self.assertIsNotNone(mapped_code)
        gateway = WindowsFakturamaGateway()
        created = PaymentMethodCandidate("term-1", name, mapped_code.value)

        with patch(
            "faktura_pilot.automation.payment_terms.create",
            return_value=created,
        ) as manager_create:
            observed = gateway.create_payment_method(name, mapped_code.value)

        self.assertIs(observed, created)
        manager_create.assert_called_once_with(gateway, name, mapped_code.value)


class ProductSelectorRequirementTests(unittest.TestCase):
    def test_product_selector_search_writes_exact_sku_without_accepting_row(self):
        sku = "SKU-{A}+^%~"
        search = FakeElement("Search", "Edit", value="")
        dialog = FakeWindow([search])
        gateway = attached_gateway(dialog)
        row = FakeElement("", "DataItem")

        def rows_after_search(*, window):
            self.assertIs(window, dialog)
            self.assertEqual(search.get_value(), sku)
            return [row]

        with (
            patch.object(gateway, "_visible_dialog_root", return_value=dialog),
            patch.object(gateway, "_wait_stable_rows", side_effect=rows_after_search),
            patch.object(
                gateway, "_row_values",
                return_value=({"Item Number": sku, "Name": "Widget", "VAT": "19"}, "row"),
            ),
            patch.object(gateway, "_close_selector_dialog") as close_selector,
        ):
            candidates = gateway.find_products(sku)

        self.assertEqual(search.get_value(), sku)
        self.assertEqual(search.click_count, 0)
        self.assertEqual([candidate.sku for candidate in candidates], [sku])
        close_selector.assert_not_called()

    def test_product_search_unexpected_selector_close_has_unknown_outcome(self):
        sku = "SKU-100"
        search = FakeElement("Search", "Edit", value="")
        dialog = FakeWindow([search])
        gateway = attached_gateway(dialog)

        with (
            patch.object(
                gateway, "_visible_dialog_root", side_effect=[dialog, None]
            ),
            patch.object(gateway, "_wait_stable_rows") as wait_rows,
            self.assertRaisesRegex(ActionOutcomeUnknown, "closed unexpectedly"),
        ):
            gateway.find_products(sku)

        self.assertEqual(search.get_value(), sku)
        wait_rows.assert_not_called()

    def test_nonmatching_visible_product_is_read_only_after_exact_search(self):
        sku = "TARGET-100"
        search = FakeElement("Search", "Edit", value="")
        dialog = FakeWindow([search])
        gateway = attached_gateway(dialog)
        row = FakeElement("", "DataItem")

        def rows_after_search(*, window):
            self.assertIs(window, dialog)
            self.assertEqual(search.get_value(), sku)
            return [row]

        with (
            patch.object(gateway, "_visible_dialog_root", return_value=dialog),
            patch.object(gateway, "_wait_stable_rows", side_effect=rows_after_search),
            patch.object(
                gateway, "_row_values",
                return_value=(
                    {"Item Number": "OTHER-200", "Name": "Other", "VAT": "19"},
                    "row",
                ),
            ),
            patch.object(gateway, "_close_selector_dialog") as close_selector,
            patch.object(gateway, "open_new_product") as open_new_product,
        ):
            candidates = gateway.find_products(sku)

        self.assertEqual([candidate.sku for candidate in candidates], ["OTHER-200"])
        close_selector.assert_called_once_with(("Select a product",), dialog)
        open_new_product.assert_not_called()


class VatAndNativeReadRequirementTests(unittest.TestCase):
    def test_vat_code_is_selected_before_percentage_is_entered(self):
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03)
        events = []
        fields = {}

        def set_field(labels, value, **kwargs):
            fields[labels[0]] = value
            events.append(("field", labels[0], value))
            return True

        def select_option(labels, value, *, optional):
            self.assertFalse(optional)
            self.assertEqual(value, "S (Standard rate)")
            fields["Value"] = "0"
            events.append(("code", value))
            return True

        def save(description):
            self.assertEqual(description, "save VAT rate")
            self.assertEqual(fields["Value"], "19")
            events.append(("save",))

        with (
            patch.object(gateway, "_open_data_manager"),
            patch.object(gateway, "find_vats", return_value=[]),
            patch.object(gateway, "_list_rows", return_value=[]),
            patch.object(gateway, "_has_edit_field", return_value=True),
            patch.object(
                gateway, "_click_action",
                side_effect=lambda query, description, ready: self.assertTrue(ready()),
            ),
            patch.object(gateway, "_set_field", side_effect=set_field),
            patch.object(gateway, "_select_named_option", side_effect=select_option),
            patch.object(gateway, "_dispatch_save", side_effect=save),
            patch.object(
                gateway, "_manager_contains_vat",
                side_effect=lambda name, rate: (
                    name == "VAT 19%"
                    and rate == Decimal("19")
                    and fields["Value"] == "19"
                ),
            ),
            patch.object(gateway, "_close_dialog_if_present"),
        ):
            created = gateway.create_vat(Decimal("19"))

        self.assertEqual((created.name, created.e_invoice_code), ("VAT 19%", "S"))
        self.assertLess(events.index(("code", "S (Standard rate)")), events.index(
            ("field", "Value", "19")
        ))
        self.assertLess(events.index(("field", "Value", "19")), events.index(
            ("save",)
        ))

    def test_native_edit_blank_readback_never_falls_back_to_uia_name(self):
        search = FakeElement("Search", "Edit", value="")
        search.handle = 123

        with patch(
            "faktura_pilot.automation.windows.native_edit_text",
            return_value="",
        ) as native_text:
            displayed = WindowsFakturamaGateway._element_value(search)
            raw = WindowsFakturamaGateway._raw_element_value(search)

        self.assertEqual(displayed, "")
        self.assertEqual(raw, "")
        self.assertEqual(native_text.call_count, 2)
        native_text.assert_called_with(123)

    def test_native_edit_text_takes_precedence_over_empty_uia_value(self):
        search = FakeElement("Search", "Edit", value="")
        search.handle = 123

        with patch(
            "faktura_pilot.automation.windows.native_edit_text",
            return_value="VAT 19%",
        ):
            displayed = WindowsFakturamaGateway._element_value(search)
            raw = WindowsFakturamaGateway._raw_element_value(search)

        self.assertEqual(displayed, "VAT 19%")
        self.assertEqual(raw, "VAT 19%")



class VatNativeValueRequirementTests(unittest.TestCase):
    def test_native_numeric_value_uses_focused_keyboard_entry(self):
        element = FakeElement("Value", "Edit", value="0")
        element.handle = 1234
        events = []
        element.set_focus = lambda: events.append(("focus",))
        element.has_keyboard_focus = lambda: events.append(("focus_check",)) or True
        element.type_keys = lambda keys, **kwargs: events.append(("keys", keys, kwargs))
        element.set_edit_text = lambda value: self.fail("native Value must use keyboard entry")

        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch.object(
                WindowsFakturamaGateway,
                "_focus_window_for_automation",
                side_effect=lambda target: events.append(("foreground", target)),
            ),
        ):
            WindowsFakturamaGateway._write_element(element, "19")

        self.assertEqual(
            events,
            [
                ("foreground", element),
                ("focus",),
                ("focus_check",),
                ("keys", "^a", {"set_foreground": False}),
                ("keys", "19", {"set_foreground": False, "pause": 0.02}),
            ],
        )

    def test_native_numeric_value_requires_focus_before_typing(self):
        element = FakeElement("Value", "Edit", value="0")
        element.handle = 1234
        element.set_focus = Mock()
        element.has_keyboard_focus = lambda: False
        element.type_keys = Mock()
        element.set_edit_text = Mock()

        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
            self.assertRaisesRegex(ManualReviewRequired, "VAT Value field"),
        ):
            WindowsFakturamaGateway._write_element(element, "19")

        element.set_focus.assert_called_once_with()
        element.type_keys.assert_not_called()
        element.set_edit_text.assert_not_called()

    def test_handleless_value_uses_normal_set_text(self):
        element = FakeElement("Value", "Edit", value="0")
        with patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")):
            WindowsFakturamaGateway._write_element(element, "19")
        self.assertEqual(element.get_value(), "19")


class VatOcrParserRequirementTests(unittest.TestCase):
    @staticmethod
    def _word(text, x, y):
        return OCRMatch(text, Bounds(x, y, x + max(20, len(text) * 8), y + 20))

    def _parse_rows(self, row_names, *, observed_name="VAT 19%", code="S (Standard rate)"):
        gateway = WindowsFakturamaGateway(timeout_seconds=0.03)
        manager = FakeWindow(bounds=FakeRect(0, 0, 600, 400))
        words = [
            self._word("Standard", 10, 20),
            self._word("Name", 100, 20),
            self._word("Description", 250, 20),
            self._word("Value", 400, 20),
        ]
        for index, row_name in enumerate(row_names):
            y = 70 + 40 * index
            words.extend([
                self._word(part, 110 + part_index * 35, y)
                for part_index, part in enumerate(row_name.split())
            ])
            words.append(self._word("19%", 410, y))
        ocr = object.__new__(TesseractOCR)
        ocr.read_words = Mock(return_value=words)
        gateway.ocr = ocr
        image = SimpleNamespace(width=600, height=400)
        mouse = ModuleType("pywinauto.mouse")
        mouse.double_click = Mock()
        package = ModuleType("pywinauto")
        package.__path__ = []
        package.mouse = mouse

        with (
            patch.object(gateway, "_capture_window", return_value=image),
            patch.object(gateway, "_read_from_window", return_value="VAT 19%"),
            patch.object(gateway, "_focus_window_for_automation"),
            patch.object(gateway, "_wait_until"),
            patch.object(
                gateway,
                "_read_optional",
                side_effect=lambda labels: observed_name if labels == ("Name",) else "19",
            ),
            patch.object(gateway, "_read_combo_optional", return_value=code),
            patch("faktura_pilot.automation.windows.verified_empty_selector", return_value=False),
            patch.dict(sys.modules, {"pywinauto": package, "pywinauto.mouse": mouse}),
        ):
            candidates = gateway._ocr_vat_candidates(manager, "VAT 19%")
        return candidates, mouse.double_click

    def test_unexpected_visible_row_is_rejected_before_opening(self):
        with self.assertRaisesRegex(ManualReviewRequired, "unexpected row"):
            self._parse_rows(["VAT 7%"])

    def test_duplicate_exact_rows_remain_distinct_candidates(self):
        candidates, double_click = self._parse_rows(["VAT 19%", "VAT 19%"])
        self.assertEqual(len(candidates), 2)
        self.assertEqual([candidate.name for candidate in candidates], ["VAT 19%"] * 2)
        self.assertEqual(double_click.call_count, 2)

    def test_selected_detail_must_match_grid_row(self):
        with self.assertRaisesRegex(ManualReviewRequired, "does not match"):
            self._parse_rows(["VAT 19%"], observed_name="VAT 7%")

    def test_nonstandard_code_is_preserved_as_conflict(self):
        candidates, _ = self._parse_rows(["VAT 19%"], code="AA (Exempt)")
        self.assertEqual([candidate.e_invoice_code for candidate in candidates], ["AA (Exempt)"])
        gateway = WindowsFakturamaGateway()
        with (
            patch.object(gateway, "_open_data_manager"),
            patch.object(gateway, "find_vats", return_value=candidates),
            self.assertRaisesRegex(ManualReviewRequired, "conflicting definition"),
        ):
            gateway.create_vat(Decimal("19"))


class ProductEntryRequirementTests(unittest.TestCase):
    @staticmethod
    def _item_and_vat():
        item = sample_order().items[0]
        vat = VatCandidate(
            token="vat-19",
            name="VAT 19%",
            value_percent=item.vat_rate_percent,
            e_invoice_code="S",
        )
        return item, vat

    def test_native_product_numeric_field_uses_focus_keyboard_and_readback(self):
        for labels in (
            ("Price (gross)", "Price gross"),
            ("cost price (net)", "Cost price (net)", "Cost price"),
            ("Stock",),
            ("Discount",),
        ):
            with self.subTest(labels=labels):
                self._assert_native_numeric_entry(labels)

    def _assert_native_numeric_entry(self, labels):
        element = FakeElement(labels[0], "Edit", value="0.00")
        element.handle = 1234
        events = []
        element.set_focus = lambda: events.append(("focus",))
        element.has_keyboard_focus = lambda: events.append(("focus_check",)) or True

        def type_keys(keys, **kwargs):
            events.append(("keys", keys, kwargs))
            if keys != "^a":
                element._value = keys

        element.type_keys = type_keys
        element.set_edit_text = lambda value: self.fail("numeric edit bypassed keyboard")
        gateway = attached_gateway(FakeWindow([element]))
        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch("faktura_pilot.automation.windows.native_edit_text", return_value=None),
            patch.object(
                WindowsFakturamaGateway,
                "_focus_window_for_automation",
                side_effect=lambda target: events.append(("foreground", target)),
            ),
        ):
            self.assertTrue(gateway._set_field(labels, "297.50"))

        self.assertEqual(element.get_value(), "297.50")
        self.assertEqual(
            events,
            [
                ("foreground", gateway._main_window),
                ("foreground", element),
                ("focus",),
                ("focus_check",),
                ("keys", "^a", {"set_foreground": False}),
                ("keys", "297.50", {"with_spaces": True, "set_foreground": False, "pause": 0.02}),
            ],
        )
    def test_price_entry_uses_observed_decimal_comma(self):
        for displayed in ("0,00 EUR", "29.750,00 EUR", "1.234,50"):
            with self.subTest(displayed=displayed):
                element = FakeElement("Price (gross)", "Edit", value=displayed)
                element.handle = 1234
                element.set_focus = Mock()
                element.has_keyboard_focus = lambda: True
                entries = []

                def type_keys(keys, entries=entries, element=element, **kwargs):
                    if keys != "^a":
                        entries.append(keys)
                        element._value = keys

                element.type_keys = type_keys
                gateway = attached_gateway(FakeWindow([element]))
                with (
                    patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
                    patch("faktura_pilot.automation.windows.native_edit_text", return_value=None),
                    patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
                ):
                    self.assertTrue(gateway._set_field(("Price (gross)",), "297.50"))
                self.assertEqual(entries, ["297,50"])

    def test_native_product_numeric_field_requires_focus_before_typing(self):
        element = FakeElement("Price (gross)", "Edit", value="0.00")
        element.handle = 1234
        element.set_focus = Mock()
        element.has_keyboard_focus = lambda: False
        element.type_keys = Mock()
        gateway = attached_gateway(FakeWindow([element]))
        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
            self.assertRaisesRegex(ManualReviewRequired, "keyboard focus"),
        ):
            gateway._set_field(("Price (gross)", "Price gross"), "297.50")
        element.type_keys.assert_not_called()

    def test_native_product_numeric_field_rejects_uncommitted_readback(self):
        element = FakeElement("Price (gross)", "Edit", value="0.00")
        element.handle = 1234
        element.set_focus = Mock()
        element.has_keyboard_focus = lambda: True
        element.type_keys = Mock()
        gateway = attached_gateway(FakeWindow([element]))
        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch("faktura_pilot.automation.windows.native_edit_text", return_value=None),
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
            self.assertRaisesRegex(PostconditionFailed, "did not read back"),
        ):
            gateway._set_field(("Price (gross)", "Price gross"), "297.50")
        self.assertEqual(element.type_keys.call_count, 2)

    def test_fill_product_selects_vat_before_entering_price(self):
        item, vat = self._item_and_vat()
        gateway = WindowsFakturamaGateway()
        events = []
        fields = {}

        def select_vat(labels, value, *, optional):
            self.assertEqual(labels, ("VAT", "VAT rate"))
            self.assertEqual(value, vat.name)
            self.assertFalse(optional)
            fields["Price (gross)"] = "0.00"
            events.append(("vat", value))

        def set_field(labels, value, *, resolver):
            self.assertIs(resolver, field_resolver)
            fields[labels[0]] = value
            events.append(("field", labels[0], value))
            return True

        field_resolver = Mock()
        field_resolver.freeze.return_value = field_resolver
        with (
            patch.object(gateway, "_require_any_labels"),
            patch.object(gateway, "_resolver", return_value=field_resolver),
            patch.object(gateway, "_read_optional", return_value=""),
            patch.object(gateway, "_select_named_option", side_effect=select_vat),
            patch.object(gateway, "_set_field", side_effect=set_field),
            patch.object(gateway, "_verify_fields", return_value=Mock(verified=True)) as verify,
        ):
            gateway.fill_product(item, vat)

        self.assertEqual(fields["Price (gross)"], "297.50")
        self.assertLess(events.index(("vat", vat.name)), events.index(
            ("field", "Price (gross)", "297.50")
        ))
        verify.assert_called_once()

    def test_fill_product_refuses_different_existing_sku_before_writes(self):
        item, vat = self._item_and_vat()
        gateway = WindowsFakturamaGateway()
        with (
            patch.object(gateway, "_require_any_labels"),
            patch.object(gateway, "_read_optional", return_value="OTHER-SKU"),
            patch.object(gateway, "_select_named_option") as select_vat,
            patch.object(gateway, "_set_field") as set_field,
            self.assertRaisesRegex(ManualReviewRequired, "different SKU"),
        ):
            gateway.fill_product(item, vat)
        select_vat.assert_not_called()
        set_field.assert_not_called()

    def test_native_description_uses_keyboard_with_spaces_and_readback(self):
        element = FakeElement("Description", "Edit", value="")
        element.handle = 1234
        element.set_focus = Mock()
        element.has_keyboard_focus = lambda: True
        events = []

        def type_keys(keys, **kwargs):
            events.append((keys, kwargs))
            if keys != "^a":
                element._value = keys

        element.type_keys = type_keys
        element.set_edit_text = lambda value: self.fail("Description bypassed keyboard")
        gateway = attached_gateway(FakeWindow([element]))
        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch("faktura_pilot.automation.windows.native_edit_text", return_value=None),
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
        ):
            self.assertTrue(gateway._set_field(("Description",), "Ergonomic chair"))

        self.assertEqual(element.get_value(), "Ergonomic chair")
        self.assertEqual(events, [
            ("^a", {"set_foreground": False}),
            ("Ergonomic chair", {"with_spaces": True, "set_foreground": False, "pause": 0.02}),
        ])

    def test_save_product_stops_if_description_resets_after_save(self):
        item, vat = self._item_and_vat()
        window, fields = product_editor_window()
        gateway = attached_gateway(window)
        fields["Item Number"]._value = item.sku
        fields["Name"]._value = item.description
        fields["Description"]._value = item.description
        fields["Price (gross)"]._value = "297.50"
        fields["cost price (net)"]._value = "0.00"
        fields["VAT"]._value = vat.name
        fields["Stock"]._value = "0.00"

        def reset_description(description):
            self.assertEqual(description, f"save Product {item.sku}")
            fields["Description"]._value = ""

        with (
            patch.object(gateway, "_dispatch_save", side_effect=reset_description) as save,
            patch.object(gateway, "capture_evidence"),
            patch.object(gateway, "_wait_until") as wait,
            self.assertRaisesRegex(PostconditionFailed, "Description"),
        ):
            gateway.save_product()

        save.assert_called_once()
        wait.assert_not_called()


class ProductSelectorOcrRequirementTests(unittest.TestCase):
    def _candidate_for_vat_words(self, vat_words):
        sku = "CHR-ERG-01"
        gateway = WindowsFakturamaGateway()
        dialog = FakeWindow()
        image = SimpleNamespace(width=1000, height=600)
        raw = {
            "text": list(vat_words),
            "left": [800 + index * 70 for index in range(len(vat_words))],
            "top": [80] * len(vat_words),
            "height": [20] * len(vat_words),
        }
        item_header = OCRMatch("Item No.", Bounds(100, 20, 180, 40))
        vat_header = OCRMatch("VAT", Bounds(800, 20, 850, 40))
        sku_match = OCRMatch(sku, Bounds(110, 80, 200, 100))
        ocr = object.__new__(TesseractOCR)
        ocr.read_text_data = Mock(return_value=raw)

        def find_text(data, labels, *, exact=False):
            self.assertIs(data, raw)
            if labels == ("Item No.",):
                return [item_header]
            if labels == (sku,):
                self.assertTrue(exact)
                return [sku_match]
            if labels == ("VAT",):
                return [vat_header]
            raise AssertionError(labels)

        ocr.find_text_in_data = Mock(side_effect=find_text)
        gateway.ocr = ocr
        with (
            patch.object(WindowsFakturamaGateway, "_focus_window_for_automation"),
            patch.object(gateway, "_capture_window", return_value=image),
        ):
            return gateway._ocr_product_candidate(dialog, sku)

    def test_product_ocr_accepts_repeated_equivalent_vat_percentages(self):
        candidate = self._candidate_for_vat_words(("VAT", "19%", "(19.0%)"))
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.sku, "CHR-ERG-01")
        self.assertEqual(candidate.vat_rate_percent, Decimal("19"))

    def test_product_ocr_rejects_conflicting_vat_percentages(self):
        with self.assertRaisesRegex(ManualReviewRequired, "uniquely"):
            self._candidate_for_vat_words(("VAT", "19%", "(7.0%)"))

    def test_discover_order_closes_both_modal_selectors_before_window_scan(self):
        gateway = attached_gateway(FakeWindow())
        dialogs = {
            "Select the address": FakeWindow(),
            "Select a product": FakeWindow(),
        }
        closed = []

        def visible(labels, *, required):
            self.assertFalse(required)
            return dialogs[labels[0]]

        def scan_windows():
            self.assertEqual(
                closed,
                [(title, dialogs[title]) for title in dialogs],
            )
            return []

        with (
            patch.object(gateway, "_visible_dialog_root", side_effect=visible),
            patch.object(
                gateway,
                "_close_selector_dialog",
                side_effect=lambda labels, dialog: closed.append((labels[0], dialog)),
            ),
            patch.object(gateway, "_application_windows", side_effect=scan_windows),
        ):
            self.assertIsNone(gateway.discover_open_order(sample_order()))


class OrderGridOcrRequirementTests(unittest.TestCase):
    @staticmethod
    def _vat_cell_values(vat_words):
        sku_bounds = Bounds(10, 100, 90, 140)
        cells = {"VAT": Bounds(100, 100, 400, 140)}
        words = [
            OCRMatch(text, Bounds(110 + index * 100, 110, 180 + index * 100, 130))
            for index, text in enumerate(vat_words)
        ]
        return WindowsFakturamaGateway._visible_order_cell_values(
            words, sku_bounds, cells
        )

    def test_order_grid_vat_accepts_repeated_equivalent_percentages(self):
        observed = self._vat_cell_values(("19%", "(19.0%)"))
        self.assertEqual(observed["VAT"], "19")

    def test_order_grid_vat_rejects_conflicting_percentages(self):
        observed = self._vat_cell_values(("19%", "(7.0%)"))
        self.assertIsNone(observed["VAT"])

    def test_order_grid_moves_focus_before_each_ocr_capture(self):
        window = FakeWindow(bounds=FakeRect(0, 0, 1000, 600))
        editor = FakeElement("Order", "Tab", bounds=FakeRect(0, 0, 1000, 600))
        pane = FakeElement("Items", "Pane", bounds=FakeRect(0, 0, 250, 500))
        gateway = attached_gateway(window)
        sku_word = OCRMatch("CHR-ERG-01", Bounds(300, 100, 410, 120))
        image = SimpleNamespace(width=1000, height=600)
        gateway.ocr = SimpleNamespace(read_words=Mock(return_value=[sku_word]))
        events = []

        with (
            patch.object(gateway, "_active_order_editor_tab", return_value=editor),
            patch.object(gateway, "_items_pane", return_value=pane),
            patch.object(gateway, "_focus_window_for_automation"),
            patch.object(
                gateway,
                "_focus_order_header_for_grid",
                side_effect=lambda target: events.append(("focus", target)),
            ),
            patch.object(
                gateway,
                "_capture_window",
                side_effect=lambda target: events.append(("capture", target)) or image,
            ),
            patch.object(gateway, "_visible_order_sku_word", return_value=sku_word),
            patch.object(
                gateway,
                "_click_control",
                side_effect=lambda target, control: events.append(("click", target)),
            ),
            patch.object(
                gateway, "_visible_order_cell_bounds",
                return_value={"VAT": Bounds(500, 100, 600, 120)},
            ),
            patch.object(gateway, "_visible_order_cell_values", return_value={"VAT": "19"}),
            patch("faktura_pilot.automation.windows.time.sleep"),
        ):
            result = gateway._capture_order_line_ocr("CHR-ERG-01", select_row=True)

        self.assertEqual(result["observed"], {"VAT": "19"})
        self.assertEqual(events, [
            ("focus", editor),
            ("capture", window),
            ("click", window),
            ("focus", editor),
            ("capture", window),
        ])
    def test_failed_header_focus_blocks_grid_capture_and_input(self):
        editor = FakeElement("Order", "Tab", value="")
        editor.handle = 1234
        header = FakeElement("Cust.Ref.", "Edit", value="REFERENCE")
        header.set_focus = Mock()
        header.has_keyboard_focus = lambda: False
        header.type_keys = Mock()
        editor.add(header)
        gateway = attached_gateway(FakeWindow([editor]))
        with (
            patch("faktura_pilot.automation.windows.os", SimpleNamespace(name="nt")),
            patch.object(gateway, "_active_order_editor_tab", return_value=editor),
            patch.object(gateway, "_items_pane", return_value=FakeElement("Items", "Pane")),
            patch.object(gateway, "_capture_window") as capture,
            patch.object(gateway, "_click_control") as click,
            patch.object(gateway, "_focus_window_for_automation"),
            self.assertRaisesRegex(ManualReviewRequired, "move focus out"),
        ):
            gateway._capture_order_line_ocr("CHR-ERG-01", select_row=True)
        self.assertEqual(header.set_focus.call_count, 2)
        self.assertEqual(header.click_count, 1)
        header.type_keys.assert_not_called()
        capture.assert_not_called()
        click.assert_not_called()


class OrderResumeAndDiscountRequirementTests(unittest.TestCase):
    def test_negative_display_discount_matches_source_but_wrong_price_fails(self):
        gateway = WindowsFakturamaGateway()
        item = sample_order().items[0]
        observed = {
            "Qty.": "2",
            "U.Price": "250.00",
            "VAT": "19",
            "Discount": "-10.0",
            "Price": "450.00",
        }
        self.assertTrue(gateway._order_line_ocr_result(item, {"observed": observed}).verified)
        observed["Price"] = "500.00"
        result = gateway._order_line_ocr_result(item, {"observed": observed})
        self.assertFalse(result.verified)
        self.assertTrue(any("Price" in issue for issue in result.observations))

    def test_unique_selected_order_draft_reuses_editor_without_second_ocr_or_click(self):
        source = sample_order()
        window = FakeWindow()
        tab = VisibleElement("*New Order", "TabItem")
        editor = FakeElement("New Order", "Tab")
        match = OCRMatch("New Order", Bounds(100, 20, 200, 40))
        gateway = attached_gateway(window)

        def read_field(target, labels):
            self.assertIs(target, editor)
            return source.external_reference if labels[0] == "Cust.Ref." else "ORD-100"

        with (
            patch.object(gateway, "_visible_dialog_root", return_value=None),
            patch.object(gateway, "_application_windows", return_value=[window]),
            patch.object(gateway, "_new_order_tab_items", return_value=[tab]),
            patch.object(gateway, "_order_tab_ocr_matches", return_value=[match]) as ocr,
            patch.object(gateway, "_select_open_order_tab", return_value=editor) as select,
            patch.object(gateway, "_active_order_editor_tab", return_value=editor) as active,
            patch.object(gateway, "_read_from_window", side_effect=read_field),
        ):
            discovered = gateway.discover_open_order(source)

        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.number, "ORD-100")
        ocr.assert_called_once_with(window, 1)
        select.assert_called_once_with(window, tab, ocr_matches=[match])
        active.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
