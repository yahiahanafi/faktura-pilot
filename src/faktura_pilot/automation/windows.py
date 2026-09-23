from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    DebtorCandidate,
    DocumentRow,
    ElementNotFound,
    GatewayError,
    InvoiceEditorRef,
    ManualReviewRequired,
    OrderEditorRef,
    PaymentMethodCandidate,
    PostconditionFailed,
    PreflightResult,
    ProductCandidate,
    TransitionTimeout,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.automation.resolver import (
    ControlQuery,
    ControlResolver,
    OCRProvider,
    TesseractOCR,
    element_bounds,
    element_name,
    element_type,
    normalize_label,
)
from faktura_pilot.domain.calculations import expected_line_net, product_gross_price
from faktura_pilot.domain.models import Address, Debtor, Item, OrderSource, PaymentStatus
from faktura_pilot.domain.policy import sku_matches

_ROW_TYPES = {"DataItem", "ListItem", "TreeItem", "Row"}
_CELL_TYPES = {"DataItem", "Text", "Edit", "ComboBox", "CheckBox", "Custom"}
_MONEY_TOLERANCE = Decimal("0.01")


def _safe(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _children(obj: Any) -> list[Any]:
    try:
        return list(obj.children())
    except Exception:
        return []


def _descendants(obj: Any) -> list[Any]:
    try:
        return list(obj.descendants())
    except Exception:
        return []


def _all_text(obj: Any) -> str:
    parts = [element_name(obj)]
    parts.extend(element_name(child) for child in _descendants(obj))
    return " ".join(part for part in parts if part).strip()


def _date_text(value: date) -> str:
    return value.isoformat()


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _parse_decimal(text: str) -> Decimal | None:
    cleaned = text.strip().replace("\u00a0", " ")
    cleaned = re.sub(r"[^0-9,.-]", "", cleaned)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _normalized_equal(expected: str, actual: str) -> bool:
    return normalize_label(expected) == normalize_label(actual)


def _e_invoice_code(value: str) -> str:
    normalized = normalize_label(value)
    if normalized == "s" or normalized.startswith("sstandardrate"):
        return "S"
    return value.strip()


class WindowsFakturamaGateway:
    """UIA-first gateway for the Fakturama 2.2.0 English workflow.

    All controls are re-discovered from the current UIA tree at the time of each
    action. OCR targets are derived from the active window's current screenshot
    and bounds; this class does not store screen coordinates or layout templates.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
        ocr_engine: OCRProvider | None = None,
        evidence_directory: Path | None = None,
        app_factory: Callable[..., Any] | None = None,
        desktop_factory: Callable[..., Any] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("wait timeouts must be positive")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.ocr = ocr_engine if ocr_engine is not None else TesseractOCR()
        self.evidence_directory = evidence_directory
        self._app_factory = app_factory
        self._desktop_factory = desktop_factory
        self._application: Any | None = None
        self._main_window: Any | None = None
        self._order_ref: OrderEditorRef | None = None
        self._invoice_ref: InvoiceEditorRef | None = None
        self._last_source: OrderSource | None = None
        self._last_order_number: str | None = None
        self._last_invoice_number: str | None = None
        self._save_timeout_seconds = max(timeout_seconds, 30.0)
        self._dpi_aware = False

    def attach_or_launch(self, executable: Path | None = None) -> None:
        if self._app_factory is None:
            try:
                from pywinauto import Application
            except ImportError as exc:
                raise GatewayError(
                    "Windows Fakturama automation requires pywinauto; install the automation extra"
                ) from exc
            app_factory = Application
        else:
            app_factory = self._app_factory

        self._dpi_aware = self._set_process_dpi_awareness()
        application = app_factory(backend="uia")
        if executable is not None:
            executable = executable.expanduser().resolve()
            if not executable.is_file():
                raise GatewayError(f"configured Fakturama executable does not exist: {executable}")
            try:
                application.connect(path=str(executable), timeout=self.timeout_seconds)
            except Exception:
                try:
                    application.start(str(executable), timeout=self.timeout_seconds)
                except Exception as exc:
                    raise GatewayError(f"could not attach to or launch Fakturama: {exc}") from exc
        else:
            try:
                application.connect(title_re=r"(?i).*fakturama.*", timeout=self.timeout_seconds)
            except Exception as exc:
                raise GatewayError(
                    "Fakturama is not running; provide its executable path to launch it"
                ) from exc

        self._application = application
        try:
            self._main_window = application.top_window()
        except Exception as exc:
            raise GatewayError(
                f"connected to Fakturama but could not discover its main window: {exc}"
            ) from exc
        if self._main_window is None:
            raise GatewayError("connected to Fakturama but no top-level window was found")

    def preflight(
        self,
        expected_version: str = "2.2.0",
        expected_language: str = "English",
    ) -> PreflightResult:
        window = self._require_window()
        title = self._window_title(window)
        if "fakturama" not in title.casefold():
            raise ManualReviewRequired(f"active window does not look like Fakturama: {title!r}")
        process_id = int(_safe(window, "process_id", 0) or 0)
        root_text = _all_text(window)
        root_folded = root_text.casefold()
        language = "English" if all(token in root_folded for token in ("file", "data")) else None
        version = self._executable_version()
        warnings: list[str] = []
        if version is None:
            raise ManualReviewRequired(
                f"could not confirm Fakturama {expected_version} from the running installation"
            )
        elif not version.startswith(expected_version):
            raise ManualReviewRequired(
                f"expected Fakturama {expected_version}, found executable version {version}"
            )
        if language is None:
            raise ManualReviewRequired(
                f"could not confirm the {expected_language} UI from visible menu labels"
            )
        return PreflightResult(
            application_title=title,
            process_id=process_id,
            version=version,
            language=language,
            dpi_aware=self._dpi_aware,
            warnings=tuple(warnings),
        )

    def open_new_order(self) -> OrderEditorRef:
        self._click_action(
            ControlQuery.one_of("Order", control_types=("Button", "MenuItem", "SplitButton")),
            "open New Order",
            lambda: self._has_order_editor(),
        )
        self._order_ref = OrderEditorRef(token=uuid4().hex, number=self._read_optional(("No.",)))
        self._last_source = None
        self._last_order_number = None
        return self._order_ref

    def discover_open_order(self, source: OrderSource) -> OrderEditorRef | None:
        matches: list[tuple[Any, str | None]] = []
        for window in self._application_windows():
            title = self._window_title(window)
            text = _all_text(window)
            is_order = "order" in title.casefold() or "new order" in text.casefold()
            reference = self._read_from_window(window, ("Cust.Ref.", "Customer reference"))
            if is_order and reference and _normalized_equal(source.external_reference, reference):
                matches.append((window, self._read_from_window(window, ("No.",))))
        if len(matches) > 1:
            raise ManualReviewRequired(
                f"multiple open Orders match customer reference {source.external_reference!r}"
            )
        if not matches:
            return None
        window, number = matches[0]
        self._main_window = window
        self._order_ref = OrderEditorRef(token=uuid4().hex, number=number)
        self._last_source = source
        return self._order_ref

    def discover_open_invoice(
        self,
        source: OrderSource,
        order_number: str,
    ) -> InvoiceEditorRef | None:
        matches: list[tuple[Any, str | None]] = []
        for window in self._application_windows():
            title = self._window_title(window)
            text = _all_text(window)
            if "invoice" not in title.casefold() and "invoice" not in text.casefold():
                continue
            reference = self._read_from_window(window, ("Cust.Ref.", "Customer reference"))
            if not reference or not _normalized_equal(source.external_reference, reference):
                continue
            linked_order_number = self._read_from_window(
                window, ("Order No.", "Order number", "Follow-up from")
            )
            if not linked_order_number:
                raise ManualReviewRequired(
                    "an open Invoice has the source customer reference, but its linked Order "
                    "number cannot be read; it will not be adopted"
                )
            if not _normalized_equal(order_number, linked_order_number):
                raise ManualReviewRequired(
                    f"an open Invoice with the source customer reference is linked to "
                    f"{linked_order_number!r}, not the expected Order {order_number!r}"
                )
            matches.append((window, linked_order_number))

        if len(matches) > 1:
            raise ManualReviewRequired(
                f"multiple open Invoices match customer reference "
                f"{source.external_reference!r} and Order {order_number!r}"
            )
        if not matches:
            return None

        window, linked_order_number = matches[0]
        self._main_window = window
        self._invoice_ref = InvoiceEditorRef(
            token=uuid4().hex,
            number=self._read_from_window(window, ("No.",)),
            linked_order_number=linked_order_number,
        )
        self._last_source = source
        self._last_order_number = linked_order_number
        return self._invoice_ref

    def order_is_open(self, ref: OrderEditorRef) -> bool:
        if not self._is_attached():
            return False
        if self._order_ref and self._order_ref.token == ref.token:
            return self._has_order_editor()
        if ref.number:
            return any(
                "order" in self._window_title(window).casefold()
                and _normalized_equal(ref.number, self._read_from_window(window, ("No.",)))
                for window in self._application_windows()
            )
        return self._has_order_editor()

    def fill_order_header(self, source: OrderSource) -> None:
        self._require_editor("order")
        self._set_field(("Date",), _date_text(source.order_date))
        self._set_field(("Cust.Ref.", "Customer reference"), source.external_reference)
        self._select_named_option(
            ("Price mode", "Document price mode", "Net"), "Net", optional=False
        )
        self._select_named_option(("VAT mode", "Tax mode", "With VAT"), "With VAT", optional=False)
        self._last_source = source
        self._verify_fields(
            {"Cust.Ref.": source.external_reference},
            labels={"Cust.Ref.": ("Cust.Ref.", "Customer reference")},
            step="Order header",
        ).require_verified("Order header")

    def open_debtor_selector(self) -> None:
        self._require_editor("order")
        self._click_action(
            ControlQuery.one_of(
                "Select the address",
                "Select address",
                "Choose address",
                "Select debtor",
                control_types=("Button", "SplitButton", "Image", "Custom"),
                ancestor_labels=("Addresses",),
            ),
            "open the Order's address selector",
            lambda: self._dialog_is_open(("Select the address", "Search", "New Contact")),
        )

    def find_debtors(self, query: str) -> list[DebtorCandidate]:
        self._require_dialog("address selector", ("Select the address", "Search", "New Contact"))
        self._search_if_available(query)
        rows = self._wait_stable_rows()
        candidates: list[DebtorCandidate] = []
        for row in rows:
            values, row_text = self._row_values(row)
            company = self._value(values, "Company", "Company name", "Name", "Contact")
            first_name = self._value(values, "First name", "First Name", "Given name")
            last_name = self._value(values, "Last name", "Last Name", "Surname")
            zip_code = self._value(values, "ZIP", "Zip", "ZIP code", "Postal code", "Postcode")
            city = self._value(values, "City", "Town")
            if not company or not zip_code or not city:
                if row_text:
                    raise ManualReviewRequired(
                        "Debtor selector returned a row whose company, ZIP, or city cannot be read "
                        "through UI Automation; resolve it manually instead of risking a duplicate"
                    )
                continue
            candidates.append(
                DebtorCandidate(
                    token=self._token(
                        {
                            "company": company,
                            "first_name": first_name,
                            "last_name": last_name,
                            "zip_code": zip_code,
                            "city": city,
                            "row": row_text,
                        }
                    ),
                    company=company,
                    first_name=first_name or None,
                    last_name=last_name or None,
                    zip_code=zip_code,
                    city=city,
                    billing_address=self._value(values, "Invoice address", "Billing address"),
                    delivery_address=self._value(values, "Delivery address"),
                )
            )
        return candidates

    def select_debtor(self, candidate: DebtorCandidate) -> None:
        row = self._find_row_by_token(candidate.token, self._debtor_row_token)
        self._click_element(row)
        self._click_if_present(("OK", "Select", "Use"), control_types=("Button",))
        self._wait_until(
            "the address selector to close",
            lambda: not self._dialog_is_open(("Select the address", "Search", "New Contact")),
        )

    def open_new_debtor(self) -> None:
        self._click_action(
            ControlQuery.one_of("New Contact", control_types=("Button", "MenuItem")),
            "open a new Debtor form",
            lambda: (
                self._has_edit_field(("Company", "Company name"))
                and self._has_any_labels("Main address", "Addresses")
            ),
        )

    def fill_debtor(self, debtor: Debtor) -> None:
        self._require_any_labels("Main address", "Addresses", "Payment")
        self._click_if_present(("Main address",), control_types=("TabItem", "Button"))
        for labels, value in (
            (("Company", "Company name"), debtor.company),
            (("First Name", "First name"), debtor.first_name),
            (("Last Name", "Last name"), debtor.last_name),
            (("Email", "E-mail"), debtor.email),
            (("Telephone", "Phone"), debtor.telephone),
        ):
            if value is not None:
                self._set_field(labels, value)

        billing_role = self._find_checkbox(("Invoice address", "Invoice"))
        if billing_role is not None:
            self._set_checkbox(billing_role, True)
        self._fill_address(debtor.billing_address, context=("Main address", "Invoice address"))

        if self._addresses_equal(debtor.billing_address, debtor.delivery_address):
            delivery_page = self._click_if_present(
                ("Delivery address",), control_types=("TabItem", "Button")
            )
            if delivery_page:
                self._fill_address(debtor.delivery_address, context=())
            else:
                delivery_role = self._find_checkbox(("Delivery address", "Delivery"))
                if delivery_role is None:
                    raise ManualReviewRequired(
                        "cannot assign the existing address to the Delivery role"
                    )
                self._set_checkbox(delivery_role, True)
        else:
            self._open_or_create_delivery_address()
            self._fill_address(debtor.delivery_address, context=("Delivery address",))
            role = self._find_checkbox(("Delivery address", "Delivery"))
            if role is not None:
                self._set_checkbox(role, True)

        self._click_if_present(("Miscellaneous",), control_types=("TabItem", "Button"))
        if debtor.alias is not None:
            self._set_field(("Alias",), debtor.alias)
        self._set_field(("Discount", "Discount %", "Discount [%]"), "0")
        self._select_named_option(
            ("Price mode", "Pricing", "Price calculation"), "Net", optional=False
        )

    def find_payment_methods(self, query: str) -> list[PaymentMethodCandidate]:
        self._open_data_manager("terms of payment")
        self._search_if_available(query)
        rows = self._wait_stable_rows()
        candidates = []
        for row in rows:
            values, row_text = self._row_values(row)
            name = self._value(values, "Name", "Terms of payment", "Payment method")
            if not name:
                if row_text:
                    raise ManualReviewRequired(
                        "payment-term row does not expose its name through UIA"
                    )
                continue
            code = self._value(values, "Code", "Payment code", "Type") or None
            candidates.append(
                PaymentMethodCandidate(
                    token=self._token({"name": name, "code": code, "row": row_text}),
                    name=name,
                    code=code,
                )
            )
        self._close_dialog_if_present(("terms of payment", "Payment"))
        return candidates

    def create_payment_method(self, name: str, code: str) -> PaymentMethodCandidate:
        self._open_data_manager("terms of payment")
        duplicates = self._list_rows()
        for row in duplicates:
            values, _ = self._row_values(row)
            existing_name = self._value(values, "Name", "Terms of payment", "Payment method")
            if existing_name and _normalized_equal(existing_name, name):
                raise ManualReviewRequired(
                    f"payment term {name!r} already exists; verify its definition before reusing it"
                )
        self._click_action(
            ControlQuery.one_of(
                "New", "New terms of payment", "Add", control_types=("Button", "MenuItem")
            ),
            "create payment terms",
            lambda: (
                self._has_edit_field(("Name",))
                and self._has_edit_field(("Discount days", "Discount days (cash)"))
            ),
        )
        self._set_field(("Name",), name)
        self._set_field(("Description",), name)
        self._set_field(("Code", "Payment code", "Type"), code, optional=True)
        for labels in (
            ("Cash discount", "Cash discount [%]", "Discount"),
            ("Discount days", "Discount days (cash)"),
            ("Net days", "Days net"),
        ):
            self._set_field(labels, "0")
        for labels in (
            ("Account", "Account number"),
            ("Status text", "Status text open"),
            ("Status text paid"),
        ):
            self._clear_field(labels, optional=True)
        standard = self._find_checkbox(("Standard", "Set as standard", "Default payment terms"))
        if standard is not None:
            self._set_checkbox(standard, False)
        self._dispatch_save("save payment terms")
        try:
            self._wait_until(
                f"payment terms {name!r} to appear in the manager",
                lambda: self._manager_contains_payment_method(name, code),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                f"payment-term Save was activated but {name!r} could not be confirmed "
                "in the manager"
            ) from exc
        self._close_dialog_if_present(("terms of payment", "Payment"))
        return PaymentMethodCandidate(
            token=self._token({"name": name, "code": code}), name=name, code=code
        )

    def select_payment_method(self, candidate: PaymentMethodCandidate) -> None:
        self._require_any_labels("Payment", "Payment method", "Terms of payment")
        selected = self._select_named_option(
            ("Payment method", "Terms of payment", "Payment terms"),
            candidate.name,
            optional=False,
        )
        if not selected:
            raise PostconditionFailed(f"could not select payment method {candidate.name!r}")

    def save_debtor(self) -> DebtorCandidate:
        debtor = self._last_source.debtor if self._last_source else None
        if debtor is None:
            raise GatewayError(
                "Debtor save requires the source order to be associated with this run"
            )
        self._verify_address_fields(debtor.billing_address, ("Invoice address",)).require_verified(
            "Debtor invoice address before save"
        )
        self._verify_address_fields(
            debtor.delivery_address, ("Delivery address",)
        ).require_verified("Debtor delivery address before save")
        self._dispatch_save("save Debtor")
        try:
            self._wait_until(
                "the Debtor editor to close",
                lambda: (
                    self._dialog_is_open(("Select the address", "Search", "New Contact"))
                    or self._has_order_editor()
                ),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                "Debtor Save was activated but the original selector or Order was not restored; "
                "inspect before retrying"
            ) from exc
        if self._dialog_is_open(("Select the address", "Search", "New Contact")):
            self._close_dialog_if_present(("Select the address", "Search", "New Contact"))
        return DebtorCandidate(
            token=self._token(
                {
                    "company": debtor.company,
                    "first_name": debtor.first_name,
                    "last_name": debtor.last_name,
                    "zip_code": debtor.billing_address.zip_code,
                    "city": debtor.billing_address.city,
                }
            ),
            company=debtor.company,
            first_name=debtor.first_name,
            last_name=debtor.last_name,
            zip_code=debtor.billing_address.zip_code,
            city=debtor.billing_address.city,
            billing_address=self._address_string(debtor.billing_address),
            delivery_address=self._address_string(debtor.delivery_address),
        )

    def return_to_order(self, ref: OrderEditorRef) -> None:
        if not self._order_ref or self._order_ref.token != ref.token:
            if ref.number is None:
                raise ManualReviewRequired(
                    "cannot rediscover the original open Order without its number"
                )
        self._activate_document_tab("New Order", ref.number)
        if not self.order_is_open(ref):
            raise PostconditionFailed("the original Order editor could not be restored")

    def verify_order_debtor(self, debtor: Debtor) -> VerificationResult:
        expected = {
            "company": debtor.company,
            "billing_address": self._address_string(debtor.billing_address),
            "delivery_address": self._address_string(debtor.delivery_address),
        }
        observed = {
            "company": self._read_optional(("Company", "Debtor", "Address")),
            "billing_address": self._read_address_display("Invoice address", "Addresses"),
            "delivery_address": self._read_address_display("Delivery address", "Addresses"),
        }
        errors = []
        if observed["company"] and not _normalized_equal(debtor.company, observed["company"]):
            errors.append("selected Debtor company does not match the source")
        elif not observed["company"]:
            errors.append("selected Debtor company could not be read back")
        for key, address in (
            ("billing_address", debtor.billing_address),
            ("delivery_address", debtor.delivery_address),
        ):
            observed_address = observed[key]
            if observed_address is None:
                errors.append(f"selected {key.replace('_', ' ')} could not be read back")
            else:
                expected_components = (
                    address.street,
                    address.zip_code,
                    address.city,
                    address.country,
                    address.additional_name,
                    address.address_specification,
                    address.district,
                )
                if not all(
                    normalize_label(value) in normalize_label(observed_address)
                    for value in expected_components
                    if value
                ):
                    errors.append(f"selected {key.replace('_', ' ')} differs from the source")
        result = VerificationResult(
            verified=not errors,
            observations=tuple(errors) or ("Debtor and both addresses match",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence("debtor-verification-failed")
        return result

    def find_vats(self, rate: Decimal) -> list[VatCandidate]:
        self._open_data_manager("VATs")
        rows = self._wait_stable_rows()
        candidates: list[VatCandidate] = []
        for row in rows:
            values, row_text = self._row_values(row)
            name = self._value(values, "Name")
            amount = _parse_decimal(self._value(values, "Value", "VAT", "Rate", "Percentage") or "")
            code = _e_invoice_code(
                self._value(values, "E-Invoice code", "E-Invoice Code", "Code") or ""
            )
            if name is None and amount is None:
                continue
            if name is None or amount is None or not code:
                self._close_dialog_if_present(("VAT", "VATs"))
                raise ManualReviewRequired(
                    "VAT manager did not expose name, percentage, and E-Invoice code for every row"
                )
            candidates.append(
                VatCandidate(
                    token=self._token(
                        {"name": name, "value_percent": str(amount), "code": code, "row": row_text}
                    ),
                    name=name,
                    value_percent=amount,
                    e_invoice_code=code,
                )
            )
        self._close_dialog_if_present(("VAT", "VATs"))
        return candidates

    def create_vat(self, rate: Decimal) -> VatCandidate:
        self._open_data_manager("VATs")
        expected_name = f"VAT {format(rate.normalize(), 'f')}%"
        for row in self._list_rows():
            values, _ = self._row_values(row)
            name = self._value(values, "Name")
            value = _parse_decimal(self._value(values, "Value", "VAT", "Rate", "Percentage") or "")
            code = _e_invoice_code(
                self._value(values, "E-Invoice code", "E-Invoice Code", "Code") or ""
            )
            if name and normalize_label(name) == normalize_label(expected_name):
                if value == rate and normalize_label(code) == "s":
                    self._close_dialog_if_present(("VAT", "VATs"))
                    return VatCandidate(
                        token=self._token({"name": name, "rate": str(rate), "code": code}),
                        name=name,
                        value_percent=rate,
                        e_invoice_code=code,
                    )
                self._close_dialog_if_present(("VAT", "VATs"))
                raise ManualReviewRequired(
                    f"VAT {expected_name!r} exists with a conflicting definition"
                )
        self._click_action(
            ControlQuery.one_of("New", "New VAT", "Add", control_types=("Button", "MenuItem")),
            "create a VAT rate",
            lambda: (
                self._has_edit_field(("Name",))
                and self._has_edit_field(("Value", "VAT", "Percentage"))
            ),
        )
        self._set_field(("Name",), expected_name)
        self._set_field(("Description",), expected_name, optional=True)
        self._set_field(("Value", "VAT", "Percentage"), _decimal_text(rate))
        self._select_named_option(
            ("E-Invoice code", "E-Invoice Code", "Code"), "S (Standard rate)", optional=False
        )
        self._dispatch_save("save VAT rate")
        try:
            self._wait_until(
                f"VAT {expected_name!r} to appear in the VAT manager",
                lambda: self._manager_contains_vat(expected_name, rate),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                f"VAT Save was activated but {expected_name!r} could not be confirmed "
                "in the manager"
            ) from exc
        self._close_dialog_if_present(("VAT", "VATs"))
        return VatCandidate(
            token=self._token({"name": expected_name, "rate": str(rate), "code": "S"}),
            name=expected_name,
            value_percent=rate,
            e_invoice_code="S",
        )

    def find_products(self, sku: str) -> list[ProductCandidate]:
        selector_open = self._dialog_is_open(("Select a product", "Search", "New product"))
        if not selector_open:
            self._require_editor("order")
            self.open_product_selector()
        self._search_if_available(sku)
        rows = self._wait_stable_rows()
        candidates: list[ProductCandidate] = []
        for row in rows:
            values, row_text = self._row_values(row)
            item_number = self._value(
                values, "Item Number", "Item number", "SKU", "Product number", "Number"
            )
            if not item_number:
                if row_text:
                    raise ManualReviewRequired(
                        "Product selector does not expose an exact SKU through UIA"
                    )
                continue
            vat = _parse_decimal(self._value(values, "VAT", "VAT rate", "Tax rate") or "")
            name = self._value(values, "Name", "Description") or None
            candidates.append(
                ProductCandidate(
                    token=self._token(
                        {"sku": item_number, "name": name, "vat": str(vat), "row": row_text}
                    ),
                    sku=item_number,
                    name=name,
                    vat_rate_percent=vat,
                )
            )
        if not any(candidate.sku.strip() == sku.strip() for candidate in candidates):
            # Return to the same Order before consulting Data > VATs. Product
            # creation reopens this selector through open_new_product().
            self._close_dialog_if_present(("Select a product", "Search", "New product"))
        return candidates

    def open_product_selector(self) -> None:
        self._require_editor("order")
        self._open_product_selector()

    def select_product(self, candidate: ProductCandidate) -> None:
        row = self._find_row_by_token(candidate.token, self._product_row_token)
        self._click_element(row)
        self._click_if_present(("OK", "Select", "Use"), control_types=("Button",))
        self._wait_until(
            "the Product selector to close",
            lambda: not self._dialog_is_open(("Select a product", "Search", "New product")),
        )

    def open_new_product(self) -> None:
        if not self._dialog_is_open(("Select a product", "Search", "New product")):
            self._open_product_selector()
        self._click_action(
            ControlQuery.one_of("New product", control_types=("Button", "MenuItem")),
            "open a new Product form",
            lambda: (
                self._has_edit_field(("Item Number", "Item number"))
                and self._has_edit_field(("Price (gross)", "Price gross"))
            ),
        )

    def fill_product(self, item: Item, vat: VatCandidate) -> None:
        self._require_any_labels("Item Number", "Price (gross)", "cost price (net)")
        price = product_gross_price(item.unit_net_price, item.vat_rate_percent)
        for labels, value in (
            (("Item Number", "Item number"), item.sku),
            (("Name",), item.description),
            (("Description",), item.description),
            (("Price (gross)", "Price gross"), _decimal_text(price)),
            (("cost price (net)", "Cost price (net)", "Cost price"), "0.00"),
            (("Stock",), "0.00"),
        ):
            self._set_field(labels, value, optional=labels[0] in {"Description", "Stock"})
        self._select_named_option(("VAT", "VAT rate"), vat.name, optional=False)
        self._verify_fields(
            {
                "Item Number": item.sku,
                "Price (gross)": _decimal_text(price),
            },
            step=f"Product {item.sku}",
        ).require_verified(f"Product {item.sku}")

    def save_product(self) -> ProductCandidate:
        sku = self._read_optional(("Item Number", "Item number"))
        name = self._read_optional(("Name",))
        if not sku:
            raise PostconditionFailed("cannot save Product because Item Number is empty")
        self._dispatch_save(f"save Product {sku}")
        try:
            self._wait_until(
                "the Product editor to close",
                lambda: (
                    self._dialog_is_open(("Select a product", "Search", "New product"))
                    or self._has_order_editor()
                ),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                f"Product {sku} Save was activated but its editor did not close; "
                "inspect before retrying"
            ) from exc
        if self._dialog_is_open(("Select a product", "Search", "New product")):
            self._close_dialog_if_present(("Select a product", "Search", "New product"))
        return ProductCandidate(
            token=self._token({"sku": sku, "name": name}),
            sku=sku,
            name=name,
        )

    def fill_order_line(self, item: Item) -> VerificationResult:
        row = self._find_order_line_row(item.sku)
        expected_values = self._expected_line_values(item)
        observations: list[str] = []
        for column, value in expected_values.items():
            cell = self._row_cell(row, column)
            if cell is None:
                observations.append(
                    f"Order line column {column!r} is not exposed through UI Automation"
                )
                continue
            self._write_element(cell, value)
            actual = self._element_value(cell)
            if actual is None or not self._equivalent_value(column, value, actual):
                observations.append(f"{column}: expected {value}, observed {actual!r}")
        result = VerificationResult(
            verified=not observations,
            observations=tuple(observations) or (f"Product {item.sku} line fields match",),
            expected=expected_values,
            observed={
                column: self._element_value(cell)
                if (cell := self._row_cell(row, column)) is not None
                else None
                for column in expected_values
            },
        )
        if not result.verified:
            self.capture_evidence(f"line-verification-{item.sku}")
        return result

    def verify_order_line(self, item: Item, item_index: int) -> VerificationResult:
        if item_index < 0:
            raise ValueError("item_index must be zero or greater")
        try:
            row = self._find_order_line_row(item.sku)
        except ElementNotFound:
            return VerificationResult(
                verified=False,
                observations=(f"Order line {item_index + 1} for SKU {item.sku!r} is not visible",),
                expected=self._expected_line_values(item),
                observed={},
            )
        expected_values = self._expected_line_values(item)
        observed: dict[str, str | None] = {}
        issues: list[str] = []
        for column, expected in expected_values.items():
            cell = self._row_cell(row, column)
            actual = self._element_value(cell) if cell is not None else None
            observed[column] = actual
            if actual is None or not self._equivalent_value(column, expected, actual):
                issues.append(f"{column}: expected {expected}, observed {actual!r}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or (f"Order line {item_index + 1} for {item.sku} matches",),
            expected=expected_values,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence(f"line-resume-verification-{item.sku}")
        return result

    @staticmethod
    def _expected_line_values(item: Item) -> dict[str, str]:
        return {
            "Qty.": _decimal_text(item.quantity),
            "U.Price": _decimal_text(item.unit_net_price),
            "VAT": _decimal_text(item.vat_rate_percent),
            "Discount": _decimal_text(item.discount_percent),
            "Price": _decimal_text(
                expected_line_net(item.quantity, item.unit_net_price, item.discount_percent)
            ),
        }

    def verify_order_totals(self, source: OrderSource) -> VerificationResult:
        expected = {
            "net": source.totals.net,
            "vat": source.totals.vat,
            "gross": source.totals.gross,
        }
        labels = {
            "net": ("Net total", "Net"),
            "vat": ("VAT total", "VAT"),
            "gross": ("Gross total", "Total", "Amount due"),
        }
        observed: dict[str, Decimal | None] = {}
        issues: list[str] = []
        for key, amount in expected.items():
            raw = self._read_optional(labels[key])
            parsed = _parse_decimal(raw or "")
            observed[key] = parsed
            if parsed is None:
                issues.append(f"{key} total is not accessible for readback")
            elif abs(parsed - amount) > _MONEY_TOLERANCE:
                issues.append(f"{key} total expected {amount:.2f}, observed {parsed:.2f}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("Order totals match the source",),
            expected={key: _decimal_text(value) for key, value in expected.items()},
            observed={
                key: _decimal_text(value) if value is not None else None
                for key, value in observed.items()
            },
        )
        if not result.verified:
            self.capture_evidence("order-totals-verification-failed")
        return result

    def save_order(self) -> str:
        number = self._read_optional(("No.",))
        if not number:
            raise PostconditionFailed("Order has no visible generated No. before Save")
        self._dispatch_save(f"save Order {number}")
        try:
            self._open_documents_view()
            matches = self.find_documents(self._require_source(), "Order")
            exact = [
                row
                for row in matches
                if row.number == number
                and abs(row.total - self._require_source().totals.gross) <= _MONEY_TOLERANCE
            ]
            if len(exact) != 1:
                raise ManualReviewRequired(
                    f"expected one visible saved Order {number!r} after Save, found {len(exact)}"
                )
        except Exception as exc:
            if isinstance(exc, ActionOutcomeUnknown):
                raise
            raise ActionOutcomeUnknown(
                f"Order {number} Save was activated but the saved Documents row could not "
                "be confirmed; inspect before retrying"
            ) from exc
        self._last_order_number = number
        return number

    def verify_order_document(self, source: OrderSource, order_number: str) -> DocumentRow:
        matches = self.find_documents(source, "Order")
        exact = [
            row
            for row in matches
            if row.number == order_number
            and _normalized_equal(row.reference, source.external_reference)
            and abs(row.total - source.totals.gross) <= _MONEY_TOLERANCE
        ]
        if len(exact) != 1:
            raise ManualReviewRequired(
                f"expected one saved Order {order_number!r} for {source.external_reference!r}; "
                f"found {len(exact)} matching Documents rows"
            )
        if not _normalized_equal(exact[0].state, "Open"):
            raise PostconditionFailed(
                f"saved Order {order_number!r} is not Open: {exact[0].state!r}"
            )
        return exact[0]

    def create_linked_invoice(self, order_number: str) -> InvoiceEditorRef:
        if not order_number.strip():
            raise ValueError("order_number is required to create a linked Invoice")
        source = self._require_source()
        documents = self.find_documents(source, "Order")
        if len([row for row in documents if row.number == order_number]) != 1:
            raise ManualReviewRequired(f"could not establish one saved Order {order_number!r}")
        self._last_order_number = order_number
        self._open_documents_view()
        order_ui_row = self._find_document_row(order_number)
        self._click_element(order_ui_row)
        self._click_action(
            ControlQuery.one_of(
                "Create a follow-up document",
                "Follow-up document",
                control_types=("Button", "MenuItem", "SplitButton"),
            ),
            "open Order follow-up document actions",
            lambda: self._has_control("Invoice", ("MenuItem", "Button", "ListItem")),
        )
        self._click_action(
            ControlQuery.one_of("Invoice", control_types=("MenuItem", "Button", "ListItem")),
            "create a linked Invoice",
            lambda: self._has_invoice_editor(),
            save=True,
        )
        number = self._read_optional(("No.",))
        self._invoice_ref = InvoiceEditorRef(
            token=uuid4().hex,
            number=number,
            linked_order_number=order_number,
        )
        return self._invoice_ref

    def verify_invoice_copied_order(
        self,
        source: OrderSource,
        order_number: str,
    ) -> VerificationResult:
        expected = {
            "Cust.Ref.": source.external_reference,
            "gross": _decimal_text(source.totals.gross),
            "linked_order_number": order_number,
        }
        observed: dict[str, str | None] = {
            "Cust.Ref.": self._read_optional(("Cust.Ref.", "Customer reference")),
            "gross": self._read_optional(("Gross total", "Total", "Amount due")),
            "linked_order_number": self._read_optional(
                ("Order No.", "Order number", "Follow-up from")
            ),
        }
        issues: list[str] = []
        reference = observed["Cust.Ref."]
        if not reference or not _normalized_equal(source.external_reference, reference):
            issues.append("Invoice customer reference differs from the source Order")
        gross = _parse_decimal(observed["gross"] or "")
        if gross is None or abs(gross - source.totals.gross) > _MONEY_TOLERANCE:
            issues.append("Invoice gross total differs from the source Order")
        linked = observed["linked_order_number"]
        if not linked:
            issues.append("Invoice's source Order number could not be read back")
        elif not _normalized_equal(order_number, linked):
            issues.append("Invoice's visible source Order number differs from the saved Order")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues)
            or ("Invoice reference and gross total match the saved Order",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence("invoice-copy-verification-failed")
        self._last_source = source
        self._last_order_number = order_number
        return result

    def apply_payment(self, source: OrderSource) -> VerificationResult:
        self._require_editor("invoice")
        self._select_named_option(
            ("Payment method", "Terms of payment"), source.payment.method, optional=False
        )
        paid_control = self._find_checkbox(("Paid", "Payment received", "Paid status"))
        if paid_control is None:
            raise ElementNotFound("Invoice paid status checkbox could not be resolved")
        is_paid = source.payment.status is PaymentStatus.PAID
        self._set_checkbox(paid_control, is_paid)
        if is_paid:
            assert source.payment.payment_date is not None
            self._set_field(("Payment date", "Paid date"), _date_text(source.payment.payment_date))
            self._set_field(("Value", "Payment value"), _decimal_text(source.totals.gross))
        else:
            self._clear_field(("Payment date", "Paid date"), optional=True)
            self._clear_field(("Value", "Payment value"), optional=True)

        method = self._read_optional(("Payment method", "Terms of payment"))
        date_value = self._read_optional(("Payment date", "Paid date"))
        value = _parse_decimal(self._read_optional(("Value", "Payment value")) or "")
        paid = self._checkbox_state(paid_control)
        expected_paid = is_paid
        issues: list[str] = []
        if not method or not _normalized_equal(source.payment.method, method):
            issues.append("payment method did not read back exactly")
        if paid is None or paid != expected_paid:
            issues.append("paid status did not read back as expected")
        if is_paid:
            if not date_value or not self._date_matches(source.payment.payment_date, date_value):
                issues.append("payment date did not read back as expected")
            if value is None or abs(value - source.totals.gross) > _MONEY_TOLERANCE:
                issues.append("payment value does not equal the full Invoice gross total")
        elif date_value or value not in (None, Decimal("0")):
            issues.append("unpaid Invoice unexpectedly has a payment date or value")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("Invoice payment fields match the extracted status",),
            expected={
                "method": source.payment.method,
                "paid": expected_paid,
                "payment_date": _date_text(source.payment.payment_date)
                if source.payment.payment_date
                else None,
                "value": _decimal_text(source.totals.gross) if is_paid else None,
            },
            observed={
                "method": method,
                "paid": paid,
                "payment_date": date_value,
                "value": str(value) if value is not None else None,
            },
        )
        if not result.verified:
            self.capture_evidence("payment-verification-failed")
        return result

    def save_invoice(self) -> str:
        number = self._read_optional(("No.",))
        if not number:
            raise PostconditionFailed("Invoice has no visible generated No. before Save")
        self._dispatch_save(f"save Invoice {number}")
        try:
            source = self._require_source()
            self._open_documents_view()
            matches = self.find_documents(source, "Invoice", self._last_order_number)
            exact = [
                row
                for row in matches
                if row.number == number and abs(row.total - source.totals.gross) <= _MONEY_TOLERANCE
            ]
            if len(exact) != 1:
                raise ManualReviewRequired(
                    f"expected one visible saved Invoice {number!r} after Save, found {len(exact)}"
                )
        except Exception as exc:
            if isinstance(exc, ActionOutcomeUnknown):
                raise
            raise ActionOutcomeUnknown(
                f"Invoice {number} Save was activated but the saved Documents row could not "
                "be confirmed; inspect before retrying"
            ) from exc
        self._last_invoice_number = number
        return number

    def verify_final_documents(
        self,
        source: OrderSource,
        order_number: str,
        invoice_number: str,
    ) -> VerificationResult:
        orders = self.find_documents(source, "Order", linked_order_number=None)
        invoices = self.find_documents(source, "Invoice", linked_order_number=order_number)
        order_rows = [row for row in orders if row.number == order_number]
        invoice_rows = [row for row in invoices if row.number == invoice_number]
        issues: list[str] = []
        if len(order_rows) != 1:
            issues.append(f"expected one saved Order row, found {len(order_rows)}")
        elif not _normalized_equal(order_rows[0].state, "Open"):
            issues.append(f"source Order is not Open (state {order_rows[0].state!r})")
        if len(invoice_rows) != 1:
            issues.append(
                f"expected one Invoice row linked to Order {order_number}, "
                f"found {len(invoice_rows)}"
            )
        elif abs(invoice_rows[0].total - source.totals.gross) > _MONEY_TOLERANCE:
            issues.append("saved Invoice total differs from source gross total")
        elif source.payment.status is PaymentStatus.PAID and not _normalized_equal(
            invoice_rows[0].state, "Paid"
        ):
            issues.append(f"Invoice is not Paid (state {invoice_rows[0].state!r})")
        elif source.payment.status is PaymentStatus.UNPAID and _normalized_equal(
            invoice_rows[0].state, "Paid"
        ):
            issues.append("unpaid source Invoice is marked Paid")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("saved Order and linked Invoice are verified",),
            expected={
                "order_number": order_number,
                "invoice_number": invoice_number,
                "reference": source.external_reference,
                "total": _decimal_text(source.totals.gross),
                "order_state": "Open",
                "invoice_state": source.payment.status.value.title(),
            },
            observed={
                "order": asdict(order_rows[0]) if len(order_rows) == 1 else None,
                "invoice": asdict(invoice_rows[0]) if len(invoice_rows) == 1 else None,
            },
        )
        if not result.verified:
            self.capture_evidence("final-verification-failed")
        return result

    def find_documents(
        self,
        source: OrderSource,
        document_type: str,
        linked_order_number: str | None = None,
    ) -> list[DocumentRow]:
        restore = self._active_editor_context()
        try:
            self._open_documents_view()
            self._search_if_available(source.external_reference)
            rows = self._wait_stable_rows()
            documents: list[DocumentRow] = []
            for row in rows:
                values, row_text = self._row_values(row)
                number = self._value(values, "No.", "Number", "Document number", "Document No.")
                row_type = self._value(values, "Type", "Document type")
                reference = self._value(values, "Cust.Ref.", "Customer reference", "Reference")
                total = _parse_decimal(
                    self._value(values, "Total", "Gross", "Amount", "Value") or ""
                )
                state = self._value(values, "State", "Status", "Paid")
                linked = self._value(
                    values, "Order No.", "Order number", "Source Order", "Follow-up from"
                )
                if not number or not row_type or not reference or total is None or not state:
                    if row_text and source.external_reference.casefold() in row_text.casefold():
                        raise ManualReviewRequired(
                            "Documents contains the source reference but does not expose "
                            "all required columns"
                        )
                    continue
                if not _normalized_equal(row_type, document_type):
                    continue
                if not _normalized_equal(reference, source.external_reference):
                    continue
                if linked_order_number and (
                    not linked or not _normalized_equal(linked_order_number, linked)
                ):
                    continue
                documents.append(
                    DocumentRow(
                        number=number,
                        type=row_type,
                        reference=reference,
                        total=total,
                        state=state,
                        linked_order_number=linked,
                    )
                )
            return documents
        finally:
            if restore is not None:
                self._restore_editor_context(*restore)

    def capture_evidence(self, label: str) -> Path | None:
        if self.evidence_directory is None:
            return None
        window = self._require_window()
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "evidence"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.evidence_directory / f"{timestamp}-{safe_label}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image = window.capture_as_image()
            image.save(target)
        except Exception as exc:
            raise GatewayError(f"could not save Fakturama evidence screenshot: {exc}") from exc
        return target

    # Semantic resolution and guarded interactions

    def _resolver(self, window: Any | None = None) -> ControlResolver:
        active = window if window is not None else self._current_window()
        return ControlResolver(active, screenshot=lambda: active.capture_as_image(), ocr=self.ocr)

    def _click_action(
        self,
        query: ControlQuery,
        description: str,
        postcondition: Callable[[], bool],
        *,
        save: bool = False,
        timeout: float | None = None,
    ) -> None:
        # Resolve immediately before the effect, so stale wrapper bounds are not reused.
        window = self._current_window()
        control = self._resolver(window).resolve(query)
        try:
            self._click_control(window, control)
        except Exception as exc:
            if save:
                raise ActionOutcomeUnknown(f"{description} may have been activated: {exc}") from exc
            raise
        try:
            self._wait_until(description, postcondition, timeout=timeout)
        except TransitionTimeout as exc:
            if save:
                raise ActionOutcomeUnknown(
                    f"{description} was activated, but the saved state could not be established; "
                    "inspect Fakturama before retrying"
                ) from exc
            raise

    def _dispatch_save(self, description: str) -> None:
        # Resolving the Save target is read-only. Once dispatched, any uncertainty
        # is surfaced as unknown and the caller must reconcile through Documents.
        window = self._current_window()
        control = self._resolver(window).resolve(
            ControlQuery.one_of("Save", control_types=("Button", "MenuItem"))
        )
        try:
            self._click_control(window, control)
        except Exception as exc:
            raise ActionOutcomeUnknown(f"{description} may have taken effect: {exc}") from exc

    def _click(self, query: ControlQuery) -> None:
        window = self._current_window()
        self._click_control(window, self._resolver(window).resolve(query))

    @staticmethod
    def _click_control(window: Any, control: Any) -> None:
        if control.element is not None:
            element = control.element
            try:
                element.click_input()
            except Exception:
                invoke = getattr(element, "invoke", None)
                if callable(invoke):
                    invoke()
                else:
                    raise
            return
        match = control.ocr_match
        if match is None:
            raise ElementNotFound("resolved control has neither UIA element nor OCR bounds")
        # OCR bounds are relative to capture_as_image(); convert through the live
        # window rectangle before sending a desktop click, including non-zero origins.
        rect = _safe(window, "rectangle")
        if rect is None:
            raise GatewayError("cannot convert OCR bounds without the current window rectangle")
        screen_x = int(rect.left) + match.bounds.center[0]
        screen_y = int(rect.top) + match.bounds.center[1]
        click_at_screen = getattr(window, "click_at_screen", None)
        if callable(click_at_screen):
            click_at_screen((screen_x, screen_y))
            return
        try:
            from pywinauto.mouse import click
        except ImportError as exc:
            raise GatewayError("OCR clicking requires pywinauto on Windows") from exc
        click(coords=(screen_x, screen_y))

    def _set_field(self, labels: Sequence[str], value: str, *, optional: bool = False) -> bool:
        query = ControlQuery.one_of(*labels)
        try:
            control = self._resolver().resolve_edit(query)
        except ElementNotFound:
            if optional:
                return False
            raise
        self._write_element(control.element, value)
        actual = (
            self._raw_element_value(control.element)
            if value == ""
            else self._element_value(control.element)
        )
        if value == "" and actual is None:
            raise PostconditionFailed(f"field {labels[0]!r} could not be verified as blank")
        if actual is None or not self._equivalent_value(labels[0], value, actual):
            raise PostconditionFailed(f"field {labels[0]!r} did not read back the entered value")
        return True

    def _clear_field(self, labels: Sequence[str], *, optional: bool = False) -> bool:
        try:
            return self._set_field(labels, "", optional=optional)
        except ElementNotFound:
            if optional:
                return False
            raise

    @staticmethod
    def _write_element(element: Any, value: str) -> None:
        control_type = element_type(element)
        if control_type == "ComboBox":
            for method_name in ("select", "select_by_text"):
                method = getattr(element, method_name, None)
                if callable(method):
                    try:
                        method(value)
                        return
                    except Exception:
                        continue
        set_text = getattr(element, "set_edit_text", None)
        if callable(set_text):
            try:
                set_text(value)
                return
            except Exception:
                pass
        click = getattr(element, "click_input", None)
        if not callable(click):
            raise GatewayError("resolved field cannot receive keyboard input")
        click()
        try:
            element.type_keys("^a", set_foreground=True)
            # Send keys with special characters escaped so customer data is literal.
            escaped = value.replace("{", "{{}").replace("}", "{}}").replace("+", "{+}")
            escaped = escaped.replace("^", "{^}").replace("%", "{%}").replace("~", "{~}")
            element.type_keys(escaped, with_spaces=True, set_foreground=True)
        except Exception as exc:
            raise GatewayError(f"could not enter text into the resolved field: {exc}") from exc

    def _read_optional(self, labels: Sequence[str]) -> str | None:
        query = ControlQuery.one_of(*labels)
        try:
            control = self._resolver().resolve_edit(query)
            return self._element_value(control.element)
        except ElementNotFound:
            try:
                control = self._resolver().resolve(query)
                return self._element_value(control.element) if control.element is not None else None
            except ElementNotFound:
                return None

    def _read_from_window(self, window: Any, labels: Sequence[str]) -> str | None:
        try:
            control = self._resolver(window).resolve_edit(ControlQuery.one_of(*labels))
            return self._element_value(control.element)
        except ElementNotFound:
            return None

    @staticmethod
    def _element_value(element: Any) -> str | None:
        if element is None:
            return None
        for method_name in ("get_value", "selected_text", "window_text"):
            value = _safe(element, method_name)
            if value is not None and str(value).strip():
                return str(value).strip()
        name = element_name(element)
        return name or None

    @staticmethod
    def _raw_element_value(element: Any) -> str | None:
        if element is None:
            return None
        for method_name in ("get_value", "selected_text", "window_text"):
            value = _safe(element, method_name)
            if value is not None:
                return str(value).strip()
        return None

    def _select_named_option(
        self,
        labels: Sequence[str],
        option: str,
        *,
        optional: bool,
    ) -> bool:
        try:
            control = self._resolver().resolve_edit(ControlQuery.one_of(*labels))
        except ElementNotFound:
            if optional:
                return False
            raise
        element = control.element
        selected = False
        for method_name in ("select", "select_by_text"):
            method = getattr(element, method_name, None)
            if callable(method):
                try:
                    method(option)
                    selected = True
                    break
                except Exception:
                    continue
        if not selected:
            try:
                self._click_control(self._current_window(), control)
                self._wait_until(
                    f"option {option!r} to appear",
                    lambda: self._has_any_labels(option),
                )
                self._click(
                    ControlQuery.one_of(option, control_types=("ListItem", "MenuItem", "DataItem"))
                )
                selected = True
            except (GatewayError, TransitionTimeout):
                if optional:
                    return False
                raise
        actual = self._element_value(element)
        if actual is not None and not _normalized_equal(option, actual):
            raise PostconditionFailed(f"expected option {option!r}, observed {actual!r}")
        return selected

    def _find_checkbox(self, labels: Sequence[str]) -> Any | None:
        try:
            return (
                self._resolver()
                .resolve(
                    ControlQuery.one_of(
                        *labels, control_types=("CheckBox", "RadioButton"), allow_ocr=False
                    )
                )
                .element
            )
        except ElementNotFound:
            return None

    @staticmethod
    def _checkbox_state(checkbox: Any) -> bool | None:
        for method_name in ("is_checked", "get_toggle_state"):
            state = _safe(checkbox, method_name)
            if state is not None:
                if isinstance(state, bool):
                    return state
                try:
                    return int(state) != 0
                except (ValueError, TypeError):
                    continue
        return None

    def _set_checkbox(self, checkbox: Any, checked: bool) -> None:
        state = self._checkbox_state(checkbox)
        if state is None:
            raise ManualReviewRequired(
                f"cannot read the current state of checkbox {element_name(checkbox)!r}"
            )
        if state != checked:
            try:
                checkbox.click_input()
            except Exception:
                toggle = getattr(checkbox, "toggle", None)
                if not callable(toggle):
                    raise
                toggle()
        if self._checkbox_state(checkbox) != checked:
            raise PostconditionFailed(
                f"checkbox {element_name(checkbox)!r} did not change as expected"
            )

    def _verify_fields(
        self,
        expected: dict[str, str],
        *,
        labels: dict[str, Sequence[str]] | None = None,
        step: str,
    ) -> VerificationResult:
        observed: dict[str, str | None] = {}
        issues: list[str] = []
        for key, value in expected.items():
            raw = self._read_optional((labels or {}).get(key, (key,)))
            observed[key] = raw
            if raw is None or not self._equivalent_value(key, value, raw):
                issues.append(f"{key}: expected {value!r}, observed {raw!r}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or (f"{step} fields match",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence(f"{step}-readback-failed")
        return result

    @staticmethod
    def _equivalent_value(label: str, expected: str, actual: str) -> bool:
        if normalize_label(label) in {
            "price",
            "pricegross",
            "costpricenet",
            "value",
            "total",
            "net",
            "gross",
            "qty",
            "uprice",
            "vat",
            "discount",
            "stock",
        }:
            left = _parse_decimal(expected)
            right = _parse_decimal(actual)
            return left is not None and right is not None and abs(left - right) <= _MONEY_TOLERANCE
        return _normalized_equal(expected, actual)

    def _fill_address(self, address: Address, *, context: Sequence[str]) -> None:
        # Context entries are a tab path, not aliases; resolve each at action time.
        for label in context:
            self._click_if_present((label,), control_types=("TabItem", "Button"))
        fields = (
            (("Street", "Street and number", "Address"), address.street),
            (("ZIP", "ZIP code", "Postal code", "Postcode"), address.zip_code),
            (("City", "Town"), address.city),
            (("Country",), address.country),
            (("District", "County"), address.district),
        )
        for labels, value in fields:
            if value is not None:
                self._set_field(labels, value)
        if address.additional_name is not None:
            self._set_field(("Additional name",), address.additional_name)
        if address.address_specification is not None:
            self._set_field(
                ("Address specification", "Address line 2", "Additional address"),
                address.address_specification,
            )

    def _verify_address_fields(
        self, address: Address, context: Sequence[str]
    ) -> VerificationResult:
        for label in context:
            self._click_if_present((label,), control_types=("TabItem", "Button"))
        expected = {
            "Street": address.street,
            "ZIP": address.zip_code,
            "City": address.city,
            "Country": address.country,
        }
        if address.additional_name:
            expected["Additional name"] = address.additional_name
        if address.address_specification:
            expected["Address specification"] = address.address_specification
        return self._verify_fields(
            expected,
            labels={
                "ZIP": ("ZIP", "ZIP code", "Postal code"),
                "Address specification": (
                    "Address specification",
                    "Address line 2",
                    "Additional address",
                ),
            },
            step="address",
        )

    def _addresses_equal(self, first: Address, second: Address) -> bool:
        return all(
            normalize_label(str(getattr(first, field) or ""))
            == normalize_label(str(getattr(second, field) or ""))
            for field in (
                "street",
                "zip_code",
                "city",
                "country",
                "additional_name",
                "address_specification",
                "district",
            )
        )

    @staticmethod
    def _address_string(address: Address) -> str:
        return ", ".join(
            value
            for value in (
                address.street,
                getattr(address, "additional_name", None),
                address.address_specification,
                address.district,
                address.zip_code,
                address.city,
                address.country,
            )
            if value
        )

    def _read_address_display(self, tab_label: str, ancestor: str) -> str | None:
        try:
            self._click_if_present((tab_label,), control_types=("TabItem", "Button", "ListItem"))
        except GatewayError:
            pass
        values = []
        for labels in (
            ("Street", "Address"),
            ("ZIP", "ZIP code"),
            ("City",),
            ("Country",),
            ("Additional name",),
            ("Address specification", "Address line 2", "Additional address"),
            ("District", "County"),
        ):
            value = self._read_optional(labels)
            if value:
                values.append(value)
        return ", ".join(values) if values else None

    # List, table, and record helpers

    def _search_if_available(self, query: str) -> bool:
        try:
            self._set_field(("Search",), query, optional=True)
            return True
        except PostconditionFailed:
            return True

    def _wait_stable_rows(self) -> list[Any]:
        previous: tuple[str, ...] | None = None
        stable = 0
        end = time.monotonic() + self.timeout_seconds
        latest: list[Any] = []
        while time.monotonic() < end:
            latest = self._list_rows()
            signature = tuple(_all_text(row) for row in latest)
            if signature == previous:
                stable += 1
                if stable >= 2:
                    return latest
            else:
                stable = 0
                previous = signature
            time.sleep(self.poll_interval_seconds)
        if latest:
            return latest
        raise TransitionTimeout("Fakturama selector results did not stabilize before timeout")

    def _list_rows(self) -> list[Any]:
        rows = [
            element
            for element in _descendants(self._current_window())
            if element_type(element) in _ROW_TYPES
        ]
        # Discard nested row wrappers when a data row itself contains row-like children.
        row_ids = {id(row) for row in rows}
        result = []
        for row in rows:
            nested = [child for child in _descendants(row) if id(child) in row_ids]
            if not nested:
                if _all_text(row):
                    result.append(row)
        return result

    def _has_edit_field(self, labels: Sequence[str]) -> bool:
        try:
            self._resolver().resolve_edit(ControlQuery.one_of(*labels, allow_ocr=False))
            return True
        except ElementNotFound:
            return False

    def _manager_contains_payment_method(self, name: str, code: str) -> bool:
        for row in self._list_rows():
            values, _ = self._row_values(row)
            observed_name = self._value(values, "Name", "Terms of payment", "Payment method")
            observed_code = self._value(values, "Code", "Payment code", "Type")
            if observed_name and _normalized_equal(observed_name, name):
                return observed_code is not None and _normalized_equal(observed_code, code)
        return False

    def _manager_contains_vat(self, name: str, rate: Decimal) -> bool:
        for row in self._list_rows():
            values, _ = self._row_values(row)
            observed_name = self._value(values, "Name")
            observed_rate = _parse_decimal(
                self._value(values, "Value", "VAT", "Rate", "Percentage") or ""
            )
            observed_code = _e_invoice_code(
                self._value(values, "E-Invoice code", "E-Invoice Code", "Code") or ""
            )
            if observed_name and _normalized_equal(observed_name, name):
                return observed_rate == rate and normalize_label(observed_code) == "s"
        return False

    def _row_values(self, row: Any) -> tuple[dict[str, str], str]:
        cells = self._row_cells(row)
        table = self._table_container(row)
        headers = self._table_headers(table) if table is not None else []
        values: dict[str, str] = {}
        for index, cell in enumerate(cells):
            value = self._element_value(cell) or element_name(cell)
            if value:
                if index < len(headers):
                    values[headers[index]] = value
                # Accessible cell names often already include the column name.
                info = _safe(cell, "element_info")
                automation_id = str(_safe(info, "automation_id", "") or "")
                if automation_id:
                    values[automation_id] = value
        if not values:
            row_name = element_name(row)
            if row_name:
                values["Name"] = row_name
        return values, _all_text(row)

    def _row_cells(self, row: Any) -> list[Any]:
        children = _children(row)
        cells = [child for child in children if element_type(child) in _CELL_TYPES]
        if cells:
            return sorted(
                cells,
                key=lambda cell: element_bounds(cell).left if element_bounds(cell) else 0,
            )
        descendants = [child for child in _descendants(row) if element_type(child) in _CELL_TYPES]
        # Prefer leaf values to parent containers with duplicate text.
        leaves = [
            child
            for child in descendants
            if not any(element_type(sub) in _CELL_TYPES for sub in _children(child))
        ]
        return sorted(
            leaves or descendants,
            key=lambda cell: element_bounds(cell).left if element_bounds(cell) else 0,
        )

    def _table_container(self, row: Any) -> Any | None:
        current = row
        for _ in range(8):
            parent = _safe(current, "parent")
            if parent is None:
                return None
            if element_type(parent) in {"DataGrid", "Table", "List", "Tree"}:
                return parent
            current = parent
        return None

    def _table_headers(self, table: Any) -> list[str]:
        headers = [
            element
            for element in _descendants(table)
            if element_type(element) in {"HeaderItem", "ColumnHeader"} and element_name(element)
        ]
        headers.sort(
            key=lambda element: element_bounds(element).left if element_bounds(element) else 0
        )
        return [element_name(element) for element in headers]

    @staticmethod
    def _value(values: dict[str, str], *labels: str) -> str | None:
        normalized = {normalize_label(key): value for key, value in values.items()}
        for label in labels:
            value = normalized.get(normalize_label(label))
            if value:
                return value.strip()
        # Header-less rows are mapped in visible left-to-right order at caller when safe.
        return None

    @staticmethod
    def _token(values: dict[str, Any]) -> str:
        return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _debtor_row_token(self, row: Any) -> str:
        values, row_text = self._row_values(row)
        return self._token(
            {
                "company": self._value(values, "Company", "Company name", "Name", "Contact"),
                "first_name": self._value(values, "First name", "First Name", "Given name"),
                "last_name": self._value(values, "Last name", "Last Name", "Surname"),
                "zip_code": self._value(
                    values, "ZIP", "Zip", "ZIP code", "Postal code", "Postcode"
                ),
                "city": self._value(values, "City", "Town"),
                "row": row_text,
            }
        )

    def _product_row_token(self, row: Any) -> str:
        values, row_text = self._row_values(row)
        return self._token(
            {
                "sku": self._value(
                    values, "Item Number", "Item number", "SKU", "Product number", "Number"
                ),
                "name": self._value(values, "Name", "Description"),
                "vat": str(
                    _parse_decimal(self._value(values, "VAT", "VAT rate", "Tax rate") or "")
                ),
                "row": row_text,
            }
        )

    def _find_row_by_token(self, token: str, token_for_row: Callable[[Any], str]) -> Any:
        matches = [row for row in self._list_rows() if token_for_row(row) == token]
        if not matches:
            raise ElementNotFound(
                "candidate disappeared from the current selector; re-run exact lookup"
            )
        if len(matches) > 1:
            raise ManualReviewRequired("candidate token matches multiple visible rows")
        return matches[0]

    def _row_cell(self, row: Any, header: str) -> Any | None:
        table = self._table_container(row)
        if table is None:
            return None
        headers = self._table_headers(table)
        indices = [
            index
            for index, title in enumerate(headers)
            if normalize_label(title) == normalize_label(header)
        ]
        if len(indices) != 1:
            if not indices:
                return None
            raise ManualReviewRequired(f"Order grid contains ambiguous {header!r} columns")
        cells = self._row_cells(row)
        return cells[indices[0]] if indices[0] < len(cells) else None

    def _find_order_line_row(self, sku: str) -> Any:
        rows = self._list_rows()
        matches = []
        unreadable_matches = []
        for row in rows:
            values, row_text = self._row_values(row)
            observed_sku = self._value(
                values,
                "Item Number",
                "Item number",
                "Item No.",
                "SKU",
                "Product number",
                "Product No.",
                "Article number",
            )
            if observed_sku is not None:
                if sku_matches(sku, observed_sku):
                    matches.append(row)
                continue
            # Row text is useful only to distinguish a missing exact identity
            # from an absent line. It never authorizes editing a row.
            if normalize_label(sku) in normalize_label(row_text):
                unreadable_matches.append(row)
        if unreadable_matches:
            raise ManualReviewRequired(
                f"Order row mentions SKU {sku!r}, but its exact SKU cell is not accessible"
            )
        if len(matches) != 1:
            if not matches:
                raise ElementNotFound(
                    f"selected Product line with exact SKU {sku!r} is not visible in the Order grid"
                )
            raise ManualReviewRequired(f"multiple Order lines have exact SKU {sku!r}")
        return matches[0]

    @staticmethod
    def _click_element(element: Any) -> None:
        element.click_input()

    def _open_product_selector(self) -> None:
        if self._dialog_is_open(("Select a product", "Search", "New product")):
            return
        self._click_action(
            ControlQuery.one_of(
                "Select a product",
                "Select product",
                "Choose product",
                control_types=("Button", "SplitButton", "Image", "Custom"),
            ),
            "open the Order's Product selector",
            lambda: self._dialog_is_open(("Select a product", "Search", "New product")),
        )

    def _open_data_manager(self, item_label: str) -> None:
        # When a manager is already open, do not issue another menu click.
        if self._dialog_is_open((item_label,)):
            return
        self._click_action(
            ControlQuery.one_of("Data", control_types=("MenuItem", "MenuBarItem", "Button")),
            f"open the Data menu for {item_label}",
            lambda: self._has_any_labels(item_label),
        )
        self._click_action(
            ControlQuery.one_of(item_label, control_types=("MenuItem", "ListItem")),
            f"open {item_label} manager",
            lambda: self._dialog_is_open((item_label, "New", "Search")),
        )

    def _open_documents_view(self) -> None:
        if self._documents_grid_visible():
            return
        self._click_action(
            ControlQuery.one_of("Documents", control_types=("Button", "TabItem", "MenuItem")),
            "open Documents",
            lambda: self._documents_grid_visible(),
        )

    def _documents_grid_visible(self) -> bool:
        return any(
            element_type(element) in {"HeaderItem", "ColumnHeader"}
            and normalize_label(element_name(element)) in {"type", "custref", "customerreference"}
            for element in _descendants(self._current_window())
        )

    def _find_document_row(self, number: str) -> Any:
        matches = []
        for row in self._list_rows():
            values, _ = self._row_values(row)
            row_number = self._value(values, "No.", "Number", "Document number", "Document No.")
            if row_number and _normalized_equal(row_number, number):
                matches.append(row)
        if not matches:
            raise ElementNotFound(f"Documents has no visible row for Order {number!r}")
        if len(matches) > 1:
            raise ManualReviewRequired(f"Documents shows more than one row for Order {number!r}")
        return matches[0]

    def _active_editor_context(self) -> tuple[str, str | None] | None:
        if self._has_invoice_editor():
            number = (
                self._invoice_ref.number if self._invoice_ref else self._read_optional(("No.",))
            )
            return ("Invoice", number)
        if self._has_order_editor():
            number = self._order_ref.number if self._order_ref else self._read_optional(("No.",))
            return ("New Order", number)
        return None

    def _restore_editor_context(self, title: str, number: str | None) -> None:
        if title == "Invoice":
            self._activate_document_tab("Invoice", number)
            self._wait_until("the original Invoice editor to return", self._has_invoice_editor)
        else:
            self._activate_document_tab(title, number)
            self._wait_until("the original Order editor to return", self._has_order_editor)

    def _open_or_create_delivery_address(self) -> None:
        try:
            control = self._resolver().resolve(
                ControlQuery.one_of(
                    "Delivery address",
                    control_types=("TabItem", "Button", "ListItem"),
                    allow_ocr=False,
                )
            )
            self._click_control(self._current_window(), control)
            return
        except ElementNotFound:
            pass
        self._click_action(
            ControlQuery.one_of(
                "New address", "Add address", "New", control_types=("Button", "MenuItem")
            ),
            "add a separate Delivery address",
            lambda: self._has_any_labels("Delivery address", "Street", "ZIP"),
        )

    def _close_dialog_if_present(self, likely_labels: Sequence[str]) -> None:
        if not self._dialog_is_open(likely_labels):
            return
        try:
            self._click(
                ControlQuery.one_of(
                    "Cancel", "Close", "Done", control_types=("Button", "MenuItem"), allow_ocr=True
                )
            )
        except (ElementNotFound, ManualReviewRequired):
            raise ManualReviewRequired(
                f"the {likely_labels[0]!r} dialog is open but has no unambiguous close action"
            ) from None
        self._wait_until(
            f"the {likely_labels[0]!r} dialog to close",
            lambda: not self._dialog_is_open(likely_labels),
        )

    def _activate_document_tab(self, title: str, number: str | None) -> None:
        labels = (f"{title} {number}", title) if number else (title,)
        try:
            self._click(
                ControlQuery.one_of(*labels, control_types=("TabItem", "Button"), allow_ocr=False)
            )
        except ElementNotFound:
            # It may already be active. Do not synthesize a positional click.
            if not self._has_order_editor():
                raise

    def _record_appears_in_documents(self, number: str, doc_type: str) -> bool:
        if not self._is_attached():
            return False
        try:
            rows = self._list_rows()
            for row in rows:
                values, text = self._row_values(row)
                row_number = self._value(values, "No.", "Number", "Document number", "Document No.")
                row_type = self._value(values, "Type", "Document type")
                if (
                    row_number
                    and _normalized_equal(row_number, number)
                    and (not row_type or _normalized_equal(row_type, doc_type))
                ):
                    return True
                if _normalized_equal(number, text) and doc_type.casefold() in text.casefold():
                    return True
        except Exception:
            return False
        return False

    # Window state and wait helpers

    def _is_attached(self) -> bool:
        return self._application is not None and self._main_window is not None

    def _require_window(self) -> Any:
        if not self._is_attached():
            raise GatewayError("Fakturama is not attached; call attach_or_launch first")
        return self._current_window()

    def _require_editor(self, kind: str) -> None:
        if kind == "order" and not self._has_order_editor():
            raise ManualReviewRequired("the expected Order editor is not open")
        if kind == "invoice" and not self._has_invoice_editor():
            raise ManualReviewRequired("the expected Invoice editor is not open")

    def _require_any_labels(self, *labels: str) -> None:
        if not self._has_any_labels(*labels):
            raise ManualReviewRequired(f"expected editor controls {labels!r} are not visible")

    def _require_dialog(self, description: str, labels: Sequence[str]) -> None:
        if not self._dialog_is_open(labels):
            raise ManualReviewRequired(f"expected {description} is not open")

    def _current_window(self) -> Any:
        self._require_app()
        try:
            active = self._application.top_window()
            if active is not None:
                return active
        except Exception:
            pass
        if self._main_window is None:
            raise GatewayError("Fakturama main window is unavailable")
        return self._main_window

    def _application_windows(self) -> list[Any]:
        self._require_app()
        try:
            windows = list(self._application.windows())
            return windows or [self._current_window()]
        except Exception:
            return [self._current_window()]

    def _require_app(self) -> None:
        if self._application is None:
            raise GatewayError("Fakturama is not attached; call attach_or_launch first")

    @staticmethod
    def _window_title(window: Any) -> str:
        title = _safe(window, "window_text", "")
        return str(title or element_name(window) or "").strip()

    def _has_order_editor(self) -> bool:
        title = self._window_title(self._current_window()).casefold()
        text = _all_text(self._current_window()).casefold()
        return ("order" in title or "new order" in text) and self._has_any_labels(
            "Cust.Ref.", "Date", "No."
        )

    def _has_invoice_editor(self) -> bool:
        title = self._window_title(self._current_window()).casefold()
        text = _all_text(self._current_window()).casefold()
        return ("invoice" in title or "invoice" in text) and self._has_any_labels(
            "Cust.Ref.", "Date", "No."
        )

    def _has_any_labels(self, *labels: str) -> bool:
        resolver = self._resolver()
        for label in labels:
            try:
                resolver.resolve(ControlQuery.one_of(label, allow_ocr=False))
                return True
            except ElementNotFound:
                continue
        return False

    def _has_control(self, label: str, control_types: Sequence[str]) -> bool:
        try:
            self._resolver().resolve(
                ControlQuery.one_of(label, control_types=control_types, allow_ocr=False)
            )
            return True
        except ElementNotFound:
            return False

    def _dialog_is_open(self, labels: Sequence[str]) -> bool:
        title = normalize_label(self._window_title(self._current_window()))
        if any(normalize_label(label) in title for label in labels):
            return True
        return sum(1 for label in labels if self._has_any_labels(label)) >= min(2, len(labels))

    def _require_source(self) -> OrderSource:
        if self._last_source is None:
            raise GatewayError(
                "the source order has not been associated with the current UI workflow"
            )
        return self._last_source

    def _wait_until(
        self,
        description: str,
        predicate: Callable[[], bool],
        *,
        timeout: float | None = None,
    ) -> None:
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except ManualReviewRequired:
                raise
            except Exception as exc:
                last_error = exc
            time.sleep(self.poll_interval_seconds)
        detail = f"; last observed error: {last_error}" if last_error else ""
        raise TransitionTimeout(f"timed out waiting for {description}{detail}")

    @staticmethod
    def _date_matches(expected: date | None, actual: str) -> bool:
        if expected is None:
            return False
        numbers = re.findall(r"\d+", actual)
        if len(numbers) < 3:
            return False
        interpretations: set[date] = set()
        for order in (
            (int(numbers[0]), int(numbers[1]), int(numbers[2])),
            (int(numbers[2]), int(numbers[1]), int(numbers[0])),
            (int(numbers[2]), int(numbers[0]), int(numbers[1])),
        ):
            try:
                interpretations.add(date(*order))
            except ValueError:
                continue
        return len(interpretations) == 1 and expected in interpretations

    def _executable_version(self) -> str | None:
        if self._main_window is None:
            return None
        path = _safe(self._main_window, "process_path")
        if not path:
            try:
                process_id = int(_safe(self._main_window, "process_id", 0))
                if process_id:
                    try:
                        from win32api import OpenProcess
                        from win32con import PROCESS_QUERY_LIMITED_INFORMATION
                        from win32process import QueryFullProcessImageName

                        handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
                        path = QueryFullProcessImageName(handle, 0)
                    except Exception:
                        try:
                            import psutil

                            path = psutil.Process(process_id).exe()
                        except Exception:
                            path = None
            except Exception:
                path = None
        try:
            if path:
                import win32api

                info = win32api.GetFileVersionInfo(str(path), "\\")
                ms = info["FileVersionMS"]
                ls = info["FileVersionLS"]
                return (
                    f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}."
                    f"{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"
                )
        except Exception:
            pass
        if not path:
            return None
        try:
            install_root = Path(str(path)).resolve().parent
            bundles_info = (
                install_root
                / "configuration"
                / "org.eclipse.equinox.simpleconfigurator"
                / "bundles.info"
            )
            if bundles_info.is_file():
                for line in bundles_info.read_text(encoding="utf-8", errors="replace").splitlines():
                    parts = line.split(",")
                    if len(parts) > 1 and parts[0] == "com.sebulli.fakturama.rcp":
                        return parts[1]
            plugin_directories = (install_root / "plugins", install_root.parent / "plugins")
            for plugins in plugin_directories:
                candidates = list(plugins.glob("com.sebulli.fakturama.rcp_*.jar"))
                if candidates:
                    match = re.search(
                        r"com\.sebulli\.fakturama\.rcp_(\d+(?:\.\d+)+)",
                        candidates[0].name,
                    )
                    if match:
                        return match.group(1)
        except OSError:
            pass
        return None

    @staticmethod
    def _set_process_dpi_awareness() -> bool:
        try:
            import ctypes

            if hasattr(ctypes, "windll"):
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
                    return True
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()
                    return True
        except Exception:
            return False
        return False

    def _click_if_present(
        self,
        labels: Sequence[str],
        *,
        control_types: Sequence[str] = (),
    ) -> bool:
        try:
            control = self._resolver().resolve(
                ControlQuery.one_of(*labels, control_types=control_types, allow_ocr=False)
            )
        except ElementNotFound:
            return False
        self._click_control(self._current_window(), control)
        return True
