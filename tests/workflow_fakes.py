from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    DebtorCandidate,
    DocumentRow,
    InvoiceEditorRef,
    OrderEditorRef,
    PaymentMethodCandidate,
    PreflightResult,
    ProductCandidate,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.domain.models import Debtor, Item, OrderSource


class ScriptedGateway:
    def __init__(
        self,
        *,
        debtors: list[DebtorCandidate] | None = None,
        products: list[ProductCandidate] | None = None,
        vats: list[VatCandidate] | None = None,
        payment_methods: list[PaymentMethodCandidate] | None = None,
    ) -> None:
        self.debtors = list(debtors or [])
        self.products = list(products or [])
        self.vats = list(vats or [])
        self.payment_methods = list(payment_methods or [])
        self.order_ref: OrderEditorRef | None = None
        self.invoice_ref: InvoiceEditorRef | None = None
        self.selected_products: list[ProductCandidate] = []
        self.documents: list[DocumentRow] = []
        self.order_lines: dict[int, Item] = {}
        self.preflight_calls = 0
        self.open_order_calls = 0
        self.create_debtor_calls = 0
        self.save_debtor_calls = 0
        self.create_payment_method_calls = 0
        self.create_vat_calls = 0
        self.create_product_calls = 0
        self.save_product_calls = 0
        self.fill_order_line_calls = 0
        self.save_order_calls = 0
        self.create_invoice_calls = 0
        self.apply_payment_calls = 0
        self.save_invoice_calls = 0
        self.order_save_unknown = False
        self.order_save_persists_before_unknown = False
        self.invoice_save_unknown = False
        self.invoice_save_persists_before_unknown = False
        self.fail_order_totals = False
        self.fail_line_verification = False
        self.payment_sources: list[OrderSource] = []

    def attach_or_launch(self, executable: Path | None = None) -> None:
        del executable

    def preflight(
        self, expected_version: str = "2.2.0", expected_language: str = "English"
    ) -> PreflightResult:
        del expected_version, expected_language
        self.preflight_calls += 1
        return PreflightResult("Fakturama", 42, "2.2.0", "English", True)

    def ensure_currency(self, currency: str, *, allow_change: bool = False) -> None:
        self.currency = currency

    def open_new_order(self) -> OrderEditorRef:
        self.open_order_calls += 1
        self.order_ref = OrderEditorRef("order-token", "ORD-100")
        return self.order_ref

    def discover_open_order(self, source: OrderSource) -> OrderEditorRef | None:
        del source
        return self.order_ref

    def discover_open_invoice(
        self, source: OrderSource, order_number: str
    ) -> InvoiceEditorRef | None:
        del source
        if self.invoice_ref and self.invoice_ref.linked_order_number == order_number:
            return self.invoice_ref
        return None

    def order_is_open(self, ref: OrderEditorRef) -> bool:
        return self.order_ref == ref

    def fill_order_header(self, source: OrderSource) -> None:
        self.current_source = source

    def open_debtor_selector(self) -> None:
        pass

    def find_debtors(self, query: str) -> list[DebtorCandidate]:
        return [
            candidate
            for candidate in self.debtors
            if query.casefold() in candidate.company.casefold()
        ]

    def select_debtor(self, candidate: DebtorCandidate) -> None:
        del candidate

    def open_new_debtor(self) -> None:
        self.create_debtor_calls += 1

    def fill_debtor(self, debtor: Debtor) -> None:
        del debtor

    def find_payment_methods(self, query: str) -> list[PaymentMethodCandidate]:
        return [
            candidate
            for candidate in self.payment_methods
            if query.casefold() in candidate.name.casefold()
        ]

    def create_payment_method(self, name: str, code: str) -> PaymentMethodCandidate:
        self.create_payment_method_calls += 1
        candidate = PaymentMethodCandidate(
            f"payment-{self.create_payment_method_calls}", name, code
        )
        self.payment_methods.append(candidate)
        return candidate

    def select_payment_method(self, candidate: PaymentMethodCandidate) -> None:
        del candidate

    def save_debtor(self) -> DebtorCandidate:
        self.save_debtor_calls += 1
        source = self.current_source
        debtor = source.debtor
        candidate = DebtorCandidate(
            f"debtor-{self.save_debtor_calls}",
            debtor.company,
            debtor.first_name,
            debtor.last_name,
            debtor.billing_address.zip_code,
            debtor.billing_address.city,
        )
        self.debtors.append(candidate)
        return candidate

    def return_to_order(self, ref: OrderEditorRef) -> None:
        self.order_ref = ref

    def verify_order_debtor(self, debtor: Debtor) -> VerificationResult:
        del debtor
        return self.verified()

    def find_vats(self, rate: Decimal) -> list[VatCandidate]:
        del rate
        return list(self.vats)

    def create_vat(self, rate: Decimal) -> VatCandidate:
        self.create_vat_calls += 1
        candidate = VatCandidate(
            f"vat-{self.create_vat_calls}",
            f"VAT {format(rate.normalize(), 'f')}%",
            rate,
            "S",
        )
        self.vats.append(candidate)
        return candidate

    def open_product_selector(self) -> None:
        pass

    def find_products(self, sku: str) -> list[ProductCandidate]:
        return [candidate for candidate in self.products if candidate.sku.strip() == sku.strip()]

    def select_product(self, candidate: ProductCandidate) -> None:
        self.selected_products.append(candidate)

    def open_new_product(self) -> None:
        self.create_product_calls += 1

    def fill_product(self, item: Item, vat: VatCandidate) -> None:
        self.new_product = (item, vat)

    def save_product(self) -> ProductCandidate:
        self.save_product_calls += 1
        item, _vat = self.new_product
        candidate = ProductCandidate(
            f"product-{self.save_product_calls}", item.sku, item.description, item.vat_rate_percent
        )
        self.products.append(candidate)
        return candidate

    def fill_order_line(self, item: Item) -> VerificationResult:
        self.fill_order_line_calls += 1
        index = len(self.order_lines)
        self.order_lines[index] = item
        return self.verified(not self.fail_line_verification)

    def verify_order_line(self, item: Item, item_index: int) -> VerificationResult:
        return self.verified(self.order_lines.get(item_index) == item)

    def prepare_order_adjustments(self, source: OrderSource) -> None:
        self.prepared_adjustment_source = source

    def apply_order_adjustments(self, source: OrderSource) -> VerificationResult:
        self.adjustment_source = source
        return self.verified()

    def verify_order_totals(self, source: OrderSource) -> VerificationResult:
        return self.verified(not self.fail_order_totals)

    def save_order(self) -> str:
        self.save_order_calls += 1
        if self.order_save_unknown and self.order_save_persists_before_unknown:
            self.documents.append(
                DocumentRow(
                    "ORD-100",
                    "Order",
                    self.current_source.external_reference,
                    self.current_source.totals.gross,
                    "Open",
                )
            )
        if self.order_save_unknown:
            raise ActionOutcomeUnknown("simulated uncertain Order Save")
        self.documents.append(
            DocumentRow(
                "ORD-100",
                "Order",
                self.current_source.external_reference,
                self.current_source.totals.gross,
                "Open",
            )
        )
        return "ORD-100"

    def verify_order_document(self, source: OrderSource, order_number: str) -> DocumentRow:
        del source
        return next(
            row for row in self.documents if row.number == order_number and row.type == "Order"
        )

    def create_linked_invoice(self, order_number: str) -> InvoiceEditorRef:
        self.create_invoice_calls += 1
        self.invoice_ref = InvoiceEditorRef("invoice-token", linked_order_number=order_number)
        return self.invoice_ref

    def verify_invoice_copied_order(
        self, source: OrderSource, order_number: str
    ) -> VerificationResult:
        del source
        return self.verified(order_number == "ORD-100")

    def apply_payment(self, source: OrderSource) -> VerificationResult:
        self.apply_payment_calls += 1
        self.payment_sources.append(source)
        return self.verified()

    def save_invoice(self) -> str:
        self.save_invoice_calls += 1
        if self.invoice_save_unknown and self.invoice_save_persists_before_unknown:
            self.documents.append(
                DocumentRow(
                    "INV-200",
                    "Invoice",
                    self.current_source.external_reference,
                    self.current_source.totals.gross,
                    "Paid",
                    "ORD-100",
                )
            )
        if self.invoice_save_unknown:
            raise ActionOutcomeUnknown("simulated uncertain Invoice Save")
        self.documents.append(
            DocumentRow(
                "INV-200",
                "Invoice",
                self.current_source.external_reference,
                self.current_source.totals.gross,
                "Paid",
                "ORD-100",
            )
        )
        return "INV-200"

    def verify_final_documents(
        self, source: OrderSource, order_number: str, invoice_number: str
    ) -> VerificationResult:
        orders = self.find_documents(source, "Order")
        invoices = self.find_documents(source, "Invoice", order_number)
        verified = (
            len(orders) == 1
            and len(invoices) == 1
            and orders[0].number == order_number
            and invoices[0].number == invoice_number
        )
        return self.verified(verified)

    def find_documents(
        self, source: OrderSource, document_type: str, linked_order_number: str | None = None
    ) -> list[DocumentRow]:
        return [
            row
            for row in self.documents
            if row.type.casefold() == document_type.casefold()
            and row.reference == source.external_reference
            and (linked_order_number is None or row.linked_order_number == linked_order_number)
        ]

    def capture_evidence(self, label: str) -> Path | None:
        del label
        return None

    @property
    def current_source(self) -> OrderSource:
        return self._source

    @current_source.setter
    def current_source(self, source: OrderSource) -> None:
        self._source = source

    @staticmethod
    def verified(verified: bool = True) -> VerificationResult:
        return VerificationResult(verified, observations=() if verified else ("mismatch",))


class FixedExtractor:
    def __init__(self, source: OrderSource) -> None:
        self.source = source

    def extract(self, image_path: Path) -> OrderSource:
        del image_path
        return self.source
