from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from faktura_pilot.automation.gateway import FakturamaGateway
from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    DocumentRow,
    ElementNotFound,
    GatewayError,
    InvoiceEditorRef,
    OrderEditorRef,
    PaymentMethodCandidate,
    PostconditionFailed,
    VerificationResult,
)
from faktura_pilot.domain.calculations import calculate_order_totals
from faktura_pilot.domain.models import CENT, Debtor, OrderSource
from faktura_pilot.domain.policy import ResolutionAction, normalize_text
from faktura_pilot.extraction.protocol import ExtractionService
from faktura_pilot.workflow.policies import (
    conflicting_vat,
    payment_code,
    resolve_debtor,
    resolve_payment_method,
    resolve_product,
    resolve_vat,
)
from faktura_pilot.workflow.state import (
    InvoiceProvenance,
    ReviewBundle,
    WorkflowCheckpoint,
    WorkflowState,
)
from faktura_pilot.workflow.store import WorkflowStore, WorkflowStoreError


class WorkflowNeedsReview(RuntimeError):
    def __init__(
        self,
        step: str,
        reason: str,
        *,
        expected: dict[str, Any] | None = None,
        observed: dict[str, Any] | None = None,
        candidates: list[Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.step = step
        self.expected = expected or {}
        self.observed = observed or {}
        self.candidates = candidates or []


class WorkflowInputError(ValueError):
    pass


class WorkflowRunResult:
    def __init__(self, checkpoint: WorkflowCheckpoint, review_path: Path | None = None) -> None:
        self.run_id = checkpoint.run_id
        self.state = checkpoint.state
        self.order_number = checkpoint.order_number
        self.invoice_number = checkpoint.invoice_number
        self.review_path = review_path
        self.checkpoint = checkpoint

    @property
    def complete(self) -> bool:
        return self.state is WorkflowState.COMPLETE

    @property
    def waiting_for_review(self) -> bool:
        return self.state is WorkflowState.WAITING_FOR_REVIEW


def _debtor_search_term(debtor: Debtor) -> str:
    company = debtor.company.strip()
    if company:
        return company
    return " ".join(
        value.strip()
        for value in (debtor.first_name, debtor.last_name)
        if value and value.strip()
    )


class WorkflowOrchestrator:
    """Deterministic Order-first coordinator around an extraction and UI gateway."""

    def __init__(
        self,
        extractor: ExtractionService,
        gateway: FakturamaGateway,
        store: WorkflowStore,
        *,
        executable: Path | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.extractor = extractor
        self.gateway = gateway
        self.store = store
        self.executable = executable
        self.progress = progress
        self._checkpoint: WorkflowCheckpoint | None = None
        self._active_step = "starting"
        self._order_ref: OrderEditorRef | None = None
        self._invoice_ref: InvoiceEditorRef | None = None

    def run(self, image_path: Path, run_id: str | None = None) -> WorkflowRunResult:
        source = self.extractor.extract(Path(image_path))
        self._validate_source(source)
        identifier = run_id or uuid.uuid4().hex
        checkpoint = WorkflowCheckpoint(
            run_id=identifier,
            state=WorkflowState.EXTRACTED,
            source=source,
            source_image=str(Path(image_path).resolve()),
        )
        self.store.create(checkpoint)
        self._checkpoint = checkpoint
        self._event("extraction_validated", {"reference": source.external_reference})
        return self._execute()

    def resume(
        self, run_id: str, *, continue_after_review: bool = False
    ) -> WorkflowRunResult:
        checkpoint = self.store.load(run_id)
        if checkpoint.source is None:
            raise WorkflowStoreError("run has no validated source; it cannot be resumed")
        if checkpoint.state is WorkflowState.COMPLETE:
            return WorkflowRunResult(checkpoint)
        self._validate_source(checkpoint.source)
        review = checkpoint.review
        checkpoint.prepare_resume()
        self._checkpoint = checkpoint
        self.store.save(checkpoint)
        self._set_step(f"resume {checkpoint.state.value}")
        try:
            self._set_step("connect to Fakturama")
            self.gateway.attach_or_launch(self.executable)
            self._set_step("check Fakturama environment")
            self._verify_preflight(self.gateway.preflight())
            self._set_step("check source currency")
            self.gateway.ensure_currency(checkpoint.source.currency, allow_change=False)
            if (
                checkpoint.format_version < 2
                and checkpoint.state is WorkflowState.VALIDATED
                and checkpoint.pending_action is None
            ):
                raise WorkflowNeedsReview(
                    "reconcile open Order",
                    "This legacy checkpoint cannot prove whether an Order editor was opened "
                    "before interruption. Review Fakturama before starting a new run.",
                )
            self._restore_invoice_context()
            self._recover_pending(skip_resolved=continue_after_review)
            if continue_after_review and review is not None:
                self._continue_after_manual_resolution(review.failed_step)
            return self._execute(already_attached=True)
        except (WorkflowNeedsReview, GatewayError) as exc:
            return self._pause(exc)

    def _continue_after_manual_resolution(self, failed_step: str) -> None:
        """Verify manually completed work and advance only past proven results."""
        checkpoint = self._require_checkpoint()
        source = self._require_source()
        if checkpoint.pending_action is not None:
            raise WorkflowNeedsReview(
                "continue after manual review",
                "An action is still pending reconciliation; it cannot be skipped.",
                observed={"pending_action": checkpoint.pending_action.name},
            )

        state = checkpoint.state
        self._set_step(f"verify manual resolution after {failed_step}")

        if state is WorkflowState.ORDER_OPEN:
            self._ensure_order_ref()
            result = self.gateway.verify_order_debtor(source.debtor)
            if result.verified:
                self._advance(WorkflowState.DEBTOR_RESOLVED)
                self._event(
                    "manual_review_step_skipped",
                    {"failed_step": failed_step, "state": WorkflowState.DEBTOR_RESOLVED.value},
                )
            return

        if state is WorkflowState.DEBTOR_RESOLVED:
            self._ensure_order_ref()
            changed = False
            for index, item in enumerate(source.items):
                result = self.gateway.verify_order_line(item, index)
                if result.verified:
                    if index not in checkpoint.completed_item_indexes:
                        checkpoint.completed_item_indexes.append(index)
                        changed = True
                    self._event(
                        "manual_order_line_verified",
                        {"item_index": index, "sku": item.sku},
                    )
                    continue

                has_visible_values = any(
                    value is not None and str(value).strip()
                    for value in result.observed.values()
                )
                if has_visible_values:
                    raise WorkflowNeedsReview(
                        f"continue after Product {item.sku}",
                        "The Order contains a partially filled or mismatched line. Correct that "
                        "line before continuing so it is not added twice.",
                        expected=result.expected,
                        observed=result.observed,
                    )
                if index in checkpoint.completed_item_indexes:
                    checkpoint.completed_item_indexes.remove(index)
                    changed = True

            if changed:
                self._save_checkpoint()
            if len(checkpoint.completed_item_indexes) == len(source.items):
                totals = self.gateway.verify_order_totals(source)
                if totals.verified:
                    self._advance(WorkflowState.ITEMS_RESOLVED)
                    self._event(
                        "manual_review_step_skipped",
                        {"failed_step": failed_step, "state": WorkflowState.ITEMS_RESOLVED.value},
                    )
            return

        if state is WorkflowState.ITEMS_RESOLVED:
            rows = self.gateway.find_documents(source, "Order")
            if len(rows) > 1:
                raise WorkflowNeedsReview(
                    "continue after Order Save",
                    "Multiple matching Orders are visible; none can be adopted safely.",
                    candidates=rows,
                )
            if not rows:
                return
            row = rows[0]
            expected_number = checkpoint.draft_order_number
            if not expected_number or row.number != expected_number:
                raise WorkflowNeedsReview(
                    "continue after Order Save",
                    "The saved Order cannot be tied to this run's draft Order number.",
                    expected={"number": expected_number},
                    observed={"number": row.number},
                    candidates=[row],
                )
            self._verify_order_row(row, source, expected_number)
            checkpoint.order_number = row.number
            self._advance(WorkflowState.ORDER_SAVED)
            self._event(
                "manual_review_step_skipped",
                {"failed_step": failed_step, "state": WorkflowState.ORDER_SAVED.value},
            )
            return

        if state is WorkflowState.ORDER_SAVED:
            number = self._require_order_number()
            row = self.gateway.verify_order_document(source, number)
            self._verify_order_row(row, source, number)
            self._advance(WorkflowState.ORDER_VERIFIED)
            self._event(
                "manual_review_step_skipped",
                {"failed_step": failed_step, "state": WorkflowState.ORDER_VERIFIED.value},
            )
            return

        if state is WorkflowState.ORDER_VERIFIED:
            order_number = self._require_order_number()
            invoice_ref = self.gateway.discover_open_invoice(source, order_number)
            if invoice_ref is not None:
                self._check_invoice_provenance(invoice_ref)
                self._require_verified(
                    self.gateway.verify_invoice_copied_order(source, order_number),
                    "Invoice copied from Order",
                )
                self._invoice_ref = invoice_ref
                if checkpoint.invoice_provenance is None:
                    self._record_invoice_provenance(invoice_ref)
                self._advance(WorkflowState.INVOICE_OPEN)
                self._event(
                    "manual_review_step_skipped",
                    {"failed_step": failed_step, "state": WorkflowState.INVOICE_OPEN.value},
                )
                return
            rows = self.gateway.find_documents(source, "Invoice", order_number)
            if rows:
                raise WorkflowNeedsReview(
                    "continue after linked Invoice creation",
                    "A linked Invoice is already saved, but its editor could not be recovered. "
                    "It will not create another Invoice.",
                    candidates=rows,
                )
            return

        if state is WorkflowState.INVOICE_OPEN:
            verify_payment = getattr(self.gateway, "verify_invoice_payment", None)
            if not callable(verify_payment):
                return
            self._ensure_invoice_ref()
            if verify_payment(source).verified:
                self._confirm_and_advance(WorkflowState.PAYMENT_APPLIED)
                self._event(
                    "manual_review_step_skipped",
                    {"failed_step": failed_step, "state": WorkflowState.PAYMENT_APPLIED.value},
                )
            return

        if state is WorkflowState.PAYMENT_APPLIED:
            order_number = self._require_order_number()
            rows = self.gateway.find_documents(source, "Invoice", order_number)
            if len(rows) > 1:
                raise WorkflowNeedsReview(
                    "continue after Invoice Save",
                    "Multiple linked Invoices are visible; none can be adopted safely.",
                    candidates=rows,
                )
            if rows:
                row = rows[0]
                if row.type.casefold() != "invoice" or row.linked_order_number != order_number:
                    raise WorkflowNeedsReview(
                        "continue after Invoice Save",
                        "The visible document is not the expected Invoice linked to this Order.",
                        expected={"type": "Invoice", "linked_order_number": order_number},
                        observed={"type": row.type, "linked_order_number": row.linked_order_number},
                        candidates=[row],
                    )
                checkpoint.invoice_number = row.number
                self._advance(WorkflowState.INVOICE_SAVED)
                self._event(
                    "manual_review_step_skipped",
                    {"failed_step": failed_step, "state": WorkflowState.INVOICE_SAVED.value},
                )
            return

        if state is WorkflowState.INVOICE_SAVED:
            order_number = self._require_order_number()
            invoice_number = self._require_invoice_number()
            self._require_verified(
                self.gateway.verify_final_documents(source, order_number, invoice_number),
                "saved Order and linked Invoice",
            )
            verify_payment = getattr(self.gateway, "verify_invoice_payment", None)
            if callable(verify_payment):
                self._require_verified(verify_payment(source), "saved Invoice payment fields")
            self._advance(WorkflowState.COMPLETE)
            self._event(
                "manual_review_step_skipped",
                {"failed_step": failed_step, "state": WorkflowState.COMPLETE.value},
            )

    def _execute(self, *, already_attached: bool = False) -> WorkflowRunResult:
        checkpoint = self._require_checkpoint()
        source = self._require_source()
        try:
            if checkpoint.state is WorkflowState.EXTRACTED:
                self._set_step("source validation")
                self._validate_source(source)
                self._advance(WorkflowState.VALIDATED)

            if checkpoint.state is WorkflowState.VALIDATED:
                if not already_attached:
                    self._set_step("connect to Fakturama")
                    self.gateway.attach_or_launch(self.executable)
                    self._set_step("check Fakturama environment")
                    preflight = self.gateway.preflight()
                    self._verify_preflight(preflight)
                    self._set_step("prepare source currency")
                    self.gateway.ensure_currency(source.currency, allow_change=True)
                if source.shipping:
                    self._set_step("prepare shipping before opening Order")
                    self._begin_action("prepare_shipping")
                    self.gateway.prepare_order_adjustments(source)
                    self._confirm_action()
                self._set_step("open Order")
                self._begin_action("open_order", reference=source.external_reference)
                self._order_ref = self.gateway.open_new_order()
                if self._order_ref.number:
                    checkpoint.draft_order_number = self._order_ref.number
                    checkpoint.pending_action.details["order_number"] = self._order_ref.number
                    self._save_checkpoint()
                self.gateway.fill_order_header(source)
                if not self.gateway.order_is_open(self._order_ref):
                    raise WorkflowNeedsReview(
                        "open Order",
                        "Fakturama did not retain the expected Order editor after header entry.",
                        expected={"reference": source.external_reference},
                    )
                self._confirm_and_advance(WorkflowState.ORDER_OPEN)

            if checkpoint.state.value in {
                WorkflowState.ORDER_OPEN.value,
                WorkflowState.DEBTOR_RESOLVED.value,
                WorkflowState.ITEMS_RESOLVED.value,
            }:
                self._ensure_order_ref()

            if checkpoint.state is WorkflowState.ORDER_OPEN:
                self._set_step("resolve Debtor")
                self._resolve_debtor(source)
                self._advance(WorkflowState.DEBTOR_RESOLVED)

            if checkpoint.state is WorkflowState.DEBTOR_RESOLVED:
                self._set_step("resolve Products")
                self._resolve_products(source)
                self._advance(WorkflowState.ITEMS_RESOLVED)

            if checkpoint.state is WorkflowState.ITEMS_RESOLVED:
                self._set_step("apply Order discount and shipping")
                self._begin_action("apply_order_adjustments")
                self._require_verified(
                    self.gateway.apply_order_adjustments(source), "Order adjustments"
                )
                self._confirm_action()
                self._set_step("save Order")
                self._require_verified(self.gateway.verify_order_totals(source), "Order totals")
                existing = self.gateway.find_documents(source, "Order")
                if existing:
                    raise WorkflowNeedsReview(
                        "save Order",
                        "A matching Order is already present before this run's Save action.",
                        expected={"reference": source.external_reference, "count": 0},
                        observed={"count": len(existing)},
                        candidates=existing,
                    )
                self._begin_action(
                    "save_order",
                    document_type="Order",
                    reference=source.external_reference,
                    total=str(source.totals.gross),
                    baseline_count=0,
                )
                self._require_order_ref()
                order_number = self.gateway.save_order()
                if not order_number.strip():
                    raise ActionOutcomeUnknown("Order Save returned no document number")
                checkpoint.order_number = order_number.strip()
                self._confirm_and_advance(WorkflowState.ORDER_SAVED)

            if checkpoint.state is WorkflowState.ORDER_SAVED:
                self._set_step("verify saved Order")
                order_number = self._require_order_number()
                row = self.gateway.verify_order_document(source, order_number)
                self._verify_order_row(row, source, order_number)
                self._advance(WorkflowState.ORDER_VERIFIED)

            if checkpoint.state is WorkflowState.ORDER_VERIFIED:
                self._set_step("create linked Invoice")
                self._begin_action(
                    "create_linked_invoice",
                    order_number=self._require_order_number(),
                )
                self._invoice_ref = self.gateway.create_linked_invoice(self._require_order_number())
                self._record_invoice_provenance(self._invoice_ref)
                self._require_verified(
                    self.gateway.verify_invoice_copied_order(source, self._require_order_number()),
                    "Invoice copied from Order",
                )
                self._confirm_and_advance(WorkflowState.INVOICE_OPEN)

            if checkpoint.state is WorkflowState.INVOICE_OPEN:
                self._set_step("apply payment status")
                self._ensure_invoice_ref()
                self._begin_action(
                    "apply_payment",
                    status=source.payment.status.value,
                    payment_date=source.payment.payment_date.isoformat()
                    if source.payment.payment_date
                    else None,
                    total=str(source.totals.gross),
                )
                result = self.gateway.apply_payment(source)
                self._require_verified(result, "Invoice payment status")
                self._confirm_and_advance(WorkflowState.PAYMENT_APPLIED)

            if checkpoint.state is WorkflowState.PAYMENT_APPLIED:
                self._set_step("save Invoice")
                order_number = self._require_order_number()
                existing = self.gateway.find_documents(source, "Invoice", order_number)
                if existing:
                    raise WorkflowNeedsReview(
                        "save Invoice",
                        "A linked Invoice is already present before this run's Save action.",
                        expected={"linked_order_number": order_number, "count": 0},
                        observed={"count": len(existing)},
                        candidates=existing,
                    )
                self._begin_action(
                    "save_invoice",
                    document_type="Invoice",
                    linked_order_number=order_number,
                    reference=source.external_reference,
                    total=str(source.totals.gross),
                    baseline_count=0,
                )
                invoice_number = self.gateway.save_invoice()
                if not invoice_number.strip():
                    raise ActionOutcomeUnknown("Invoice Save returned no document number")
                checkpoint.invoice_number = invoice_number.strip()
                self._confirm_and_advance(WorkflowState.INVOICE_SAVED)

            if checkpoint.state is WorkflowState.INVOICE_SAVED:
                self._set_step("verify saved Invoice and Order")
                self._require_verified(
                    self.gateway.verify_final_documents(
                        source,
                        self._require_order_number(),
                        self._require_invoice_number(),
                    ),
                    "saved Order and linked Invoice",
                )
                verify_payment = getattr(self.gateway, "verify_invoice_payment", None)
                if callable(verify_payment):
                    self._require_verified(
                        verify_payment(source), "saved Invoice payment fields"
                    )
                self._advance(WorkflowState.COMPLETE)

            return WorkflowRunResult(checkpoint)
        except (WorkflowNeedsReview, GatewayError) as exc:
            return self._pause(exc)

    def _resolve_debtor(self, source: OrderSource) -> None:
        gateway = self.gateway
        reference = source.external_reference
        debtor = source.debtor
        search_term = _debtor_search_term(debtor)
        gateway.open_debtor_selector()
        candidates = gateway.find_debtors(search_term)
        resolution = resolve_debtor(debtor, candidates)
        if resolution.action is ResolutionAction.MANUAL_REVIEW:
            raise WorkflowNeedsReview(
                "resolve Debtor",
                resolution.reason,
                expected={
                    "company": debtor.company,
                    "first_name": debtor.first_name,
                    "last_name": debtor.last_name,
                    "zip_code": debtor.billing_address.zip_code,
                    "city": debtor.billing_address.city,
                },
                candidates=candidates,
            )
        if resolution.action is ResolutionAction.REUSE:
            assert resolution.match is not None
            gateway.select_debtor(resolution.match)
        else:
            payment_method = self._resolve_payment_method_for_new_debtor(
                source.payment.method
            )
            self._set_step("return to Order before Debtor creation")
            self._return_to_order()
            gateway.open_debtor_selector()
            self._set_step("create Debtor")
            gateway.open_new_debtor()
            gateway.fill_debtor(debtor)
            gateway.select_payment_method(payment_method)

            self._begin_action(
                "save_debtor",
                company=debtor.company,
                first_name=debtor.first_name,
                last_name=debtor.last_name,
                zip_code=debtor.billing_address.zip_code,
                city=debtor.billing_address.city,
            )
            gateway.save_debtor()
            self._return_to_order()
            gateway.open_debtor_selector()
            created_candidates = gateway.find_debtors(search_term)
            created_resolution = resolve_debtor(debtor, created_candidates)
            if (
                created_resolution.action is not ResolutionAction.REUSE
                or created_resolution.match is None
            ):
                raise WorkflowNeedsReview(
                    "select created Debtor",
                    "The saved Debtor is not a unique exact match in the Order selector.",
                    expected={
                        "company": debtor.company,
                        "first_name": debtor.first_name,
                        "last_name": debtor.last_name,
                        "zip_code": debtor.billing_address.zip_code,
                        "city": debtor.billing_address.city,
                        "reference": reference,
                    },
                    candidates=created_candidates,
                )
            gateway.select_debtor(created_resolution.match)
            self._confirm_action()

        self._set_step("verify Order Debtor")
        self._require_verified(gateway.verify_order_debtor(debtor), "Order Debtor and addresses")

    def _resolve_payment_method_for_new_debtor(
        self, method_name: str
    ) -> PaymentMethodCandidate:
        gateway = self.gateway
        self._set_step(f"search payment method {method_name}")
        candidates = gateway.find_payment_methods(method_name)
        try:
            code = payment_code(method_name)
        except ValueError:
            code = None

        if code is None:
            exact_methods = [
                candidate for candidate in candidates
                if normalize_text(candidate.name) == normalize_text(method_name)
            ]
            if len(exact_methods) == 1:
                return exact_methods[0]
            reason = (
                "multiple exact payment methods are available"
                if exact_methods
                else "no exact payment method is available and no creation-code mapping "
                "is defined"
            )
            raise WorkflowNeedsReview(
                "resolve payment method",
                reason,
                expected={"name": method_name},
                candidates=exact_methods or candidates,
            )

        resolution = resolve_payment_method(method_name, code, candidates)
        if resolution.action is ResolutionAction.MANUAL_REVIEW:
            raise WorkflowNeedsReview(
                "resolve payment method",
                resolution.reason,
                expected={"name": method_name, "code": code.value},
                candidates=candidates,
            )
        if resolution.action is ResolutionAction.REUSE:
            assert resolution.match is not None
            return resolution.match

        self._set_step(f"create payment method {method_name}")
        self._begin_action(
            "create_payment_method", method_name=method_name, code=code.value
        )
        created = gateway.create_payment_method(method_name, code.value)
        self._set_step(f"verify payment method {method_name}")
        candidates = gateway.find_payment_methods(method_name)
        verified = resolve_payment_method(method_name, code, candidates)
        if verified.action is not ResolutionAction.REUSE or verified.match is None:
            raise WorkflowNeedsReview(
                "verify payment method",
                "The created payment method was not uniquely visible after creation.",
                expected={"name": method_name, "code": code.value},
                observed={"returned": created},
                candidates=candidates,
            )
        self._confirm_action()
        return verified.match

    def _resolve_products(self, source: OrderSource) -> None:
        gateway = self.gateway
        for index, item in enumerate(source.items):
            if index in self._require_checkpoint().completed_item_indexes:
                continue
            self._set_step(f"search Product {index + 1} ({item.sku})")
            gateway.open_product_selector()
            candidates = gateway.find_products(item.sku)
            product_resolution = resolve_product(item.sku, candidates)
            if product_resolution.action is ResolutionAction.MANUAL_REVIEW:
                raise WorkflowNeedsReview(
                    f"resolve Product {item.sku}",
                    product_resolution.reason,
                    expected={"sku": item.sku},
                    candidates=candidates,
                )
            if product_resolution.action is ResolutionAction.REUSE:
                assert product_resolution.match is not None
                candidate = product_resolution.match
                observed_vat = getattr(candidate, "vat_rate_percent", None)
                if observed_vat is None:
                    observed_vat = getattr(candidate, "vat_percent", None)
                if observed_vat is None:
                    raise WorkflowNeedsReview(
                        f"verify Product VAT {item.sku}",
                        "The existing Product's VAT rate could not be read; it will not be reused.",
                        expected={"vat_rate_percent": str(item.vat_rate_percent)},
                        observed={"vat_rate_percent": None},
                        candidates=[candidate],
                    )
                if Decimal(str(observed_vat)) != item.vat_rate_percent:
                    raise WorkflowNeedsReview(
                        f"verify Product VAT {item.sku}",
                        "The existing Product's visible VAT rate differs from the source order.",
                        expected={"vat_rate_percent": str(item.vat_rate_percent)},
                        observed={"vat_rate_percent": str(observed_vat)},
                        candidates=[candidate],
                    )
                self._set_step(f"select Product {index + 1} ({item.sku})")
                self._begin_action(
                    "fill_order_line", sku=item.sku, item_index=index,
                    expected_net=str(item.source_line_net_total),
                )
                gateway.select_product(candidate)
            else:
                self._set_step(f"check VAT {item.vat_rate_percent}% for {item.sku}")
                vat_candidates = gateway.find_vats(item.vat_rate_percent)
                vat_resolution = resolve_vat(item.vat_rate_percent, vat_candidates)
                collisions = conflicting_vat(item.vat_rate_percent, vat_candidates)
                if collisions:
                    raise WorkflowNeedsReview(
                        f"resolve VAT {item.vat_rate_percent}%",
                        "A VAT record uses the requested name or rate with conflicting settings.",
                        expected={
                            "value_percent": str(item.vat_rate_percent),
                            "e_invoice_code": "S",
                        },
                        candidates=collisions,
                    )
                if vat_resolution.action is ResolutionAction.MANUAL_REVIEW:
                    raise WorkflowNeedsReview(
                        f"resolve VAT {item.vat_rate_percent}%",
                        vat_resolution.reason,
                        expected={"value_percent": str(item.vat_rate_percent)},
                        candidates=vat_candidates,
                    )
                if vat_resolution.action is ResolutionAction.REUSE:
                    assert vat_resolution.match is not None
                    vat = vat_resolution.match
                else:
                    self._set_step(f"create VAT {item.vat_rate_percent}%")
                    self._begin_action("create_vat", rate=str(item.vat_rate_percent))
                    gateway.create_vat(item.vat_rate_percent)
                    self._set_step(f"verify VAT {item.vat_rate_percent}%")
                    vat_candidates = gateway.find_vats(item.vat_rate_percent)
                    vat_resolution = resolve_vat(item.vat_rate_percent, vat_candidates)
                    if (
                        vat_resolution.action is not ResolutionAction.REUSE
                        or vat_resolution.match is None
                    ):
                        raise WorkflowNeedsReview(
                            f"verify VAT {item.vat_rate_percent}%",
                            "The created VAT record is not a unique exact match after creation.",
                            expected={
                                "value_percent": str(item.vat_rate_percent),
                                "e_invoice_code": "S",
                            },
                            candidates=vat_candidates,
                        )
                    vat = vat_resolution.match
                    self._confirm_action()
                self._set_step(f"open Product editor for {item.sku}")
                gateway.open_new_product()
                self._set_step(f"fill Product {item.sku}")
                gateway.fill_product(item, vat)
                self._set_step(f"save Product {item.sku}")
                self._begin_action(
                    "save_product", sku=item.sku, item_index=index,
                    selection_not_started=True,
                )
                gateway.save_product()
                self._set_step(f"verify saved Product {item.sku}")
                self._return_to_order()
                gateway.open_product_selector()
                created_candidates = gateway.find_products(item.sku)
                created_resolution = resolve_product(item.sku, created_candidates)
                if (
                    created_resolution.action is not ResolutionAction.REUSE
                    or created_resolution.match is None
                ):
                    raise WorkflowNeedsReview(
                        f"select created Product {item.sku}",
                        "The saved Product is not a unique exact match in the Order selector.",
                        expected={"sku": item.sku},
                        candidates=created_candidates,
                    )
                self._set_step(f"select Product {index + 1} ({item.sku})")
                self._replace_pending_with_line(item, index)
                gateway.select_product(created_resolution.match)

            self._set_step(f"fill Order line {index + 1} ({item.sku})")
            result = gateway.fill_order_line(item)
            self._require_verified(result, f"Order line {item.sku}")
            self._require_checkpoint().completed_item_indexes.append(index)
            self._confirm_action()
            self._event(
                "order_line_verified",
                {"index": index, "sku": item.sku, "net": str(item.source_line_net_total)},
            )

    def _recover_pending(self, *, skip_resolved: bool = False) -> None:
        checkpoint = self._require_checkpoint()
        pending = checkpoint.pending_action
        if pending is None:
            return
        source = self._require_source()
        self._set_step(f"reconcile {pending.name}")
        self._event("reconcile_started", {"action": pending.name})

        if pending.name == "prepare_shipping":
            self.gateway.prepare_order_adjustments(source)
            self._confirm_action()
            return

        if pending.name == "open_order":
            number = pending.details.get("order_number")
            if not number:
                raise WorkflowNeedsReview(
                    "reconcile open Order",
                    "The run did not record the new Order editor's number before interruption. "
                    "An unrelated draft must not be adopted or replaced automatically.",
                    expected={"reference": source.external_reference},
                )
            order_ref = self.gateway.discover_open_order(source)
            if order_ref is None or order_ref.number != number:
                raise WorkflowNeedsReview(
                    "reconcile open Order",
                    "The run's numbered Order editor could not be uniquely rediscovered. "
                    "Opening another would risk a duplicate draft.",
                    expected={"reference": source.external_reference, "order_number": number},
                    observed={"order_number": order_ref.number if order_ref else None},
                )
            self._order_ref = order_ref
            checkpoint.draft_order_number = number
            self._save_checkpoint()
            self.gateway.fill_order_header(source)
            if not self.gateway.order_is_open(order_ref):
                raise WorkflowNeedsReview(
                    "reconcile open Order",
                    "The recovered Order editor is not active after header entry.",
                    expected={"reference": source.external_reference},
                )
            self._confirm_and_advance(WorkflowState.ORDER_OPEN)
            return

        if pending.name == "apply_order_adjustments":
            self._ensure_order_ref()
            self._require_verified(
                self.gateway.apply_order_adjustments(source), "Order adjustments"
            )
            self._confirm_action()
            return

        if pending.name == "save_order":
            rows = self.gateway.find_documents(source, "Order")
            if len(rows) > 1:
                raise WorkflowNeedsReview(
                    "reconcile Order Save", "Multiple matching Orders are visible.", candidates=rows
                )
            if rows:
                row = rows[0]
                self._verify_order_row(row, source, row.number)
                checkpoint.order_number = row.number
                self._confirm_and_advance(WorkflowState.ORDER_SAVED)
                return
            raise WorkflowNeedsReview(
                "reconcile Order Save",
                "No matching Order is visible after an uncertain Save. The Save will not be "
                "repeated.",
                expected={"reference": source.external_reference},
            )

        if pending.name == "save_invoice":
            order_number = self._require_order_number()
            rows = self.gateway.find_documents(source, "Invoice", order_number)
            if len(rows) > 1:
                raise WorkflowNeedsReview(
                    "reconcile Invoice Save",
                    "Multiple linked Invoices are visible.",
                    candidates=rows,
                )
            if rows:
                checkpoint.invoice_number = rows[0].number
                self._confirm_and_advance(WorkflowState.INVOICE_SAVED)
                return
            raise WorkflowNeedsReview(
                "reconcile Invoice Save",
                "No linked Invoice is visible after an uncertain Save. The Save will not be "
                "repeated.",
                expected={"linked_order_number": order_number},
            )

        if pending.name == "create_linked_invoice":
            invoice_ref = self.gateway.discover_open_invoice(source, self._require_order_number())
            if invoice_ref is not None:
                self._check_invoice_provenance(invoice_ref)
                self._invoice_ref = invoice_ref
                self._require_verified(
                    self.gateway.verify_invoice_copied_order(source, self._require_order_number()),
                    "Invoice copied from Order",
                )
                self._confirm_and_advance(WorkflowState.INVOICE_OPEN)
                return
            rows = self.gateway.find_documents(source, "Invoice", self._require_order_number())
            raise WorkflowNeedsReview(
                "reconcile linked Invoice creation",
                "The linked Invoice editor could not be rediscovered; creation will not be "
                "repeated.",
                candidates=rows,
            )

        if pending.name in {"save_debtor", "save_product"}:
            self._ensure_order_ref()
            self._return_to_order()
            if pending.name == "save_debtor":
                self.gateway.open_debtor_selector()
                candidates = self.gateway.find_debtors(_debtor_search_term(source.debtor))
                resolution = resolve_debtor(source.debtor, candidates)
                step = "reconcile Debtor Save"
            else:
                item_index = int(pending.details["item_index"])
                item = source.items[item_index]
                self._set_step(f"inspect Order line {item.sku} before Product recovery")
                try:
                    existing_line = self.gateway.verify_order_line(item, item_index)
                except ElementNotFound as exc:
                    raise WorkflowNeedsReview(
                        "reconcile Product Save",
                        "The exact SKU is not visible in the active Order grid. Its absence "
                        "cannot be proven, so Product selection will not be repeated. "
                        "Restore the Order Items grid for review.",
                        expected={"sku": item.sku, "item_index": item_index},
                    ) from exc
                if not existing_line.verified and not any(
                    value is not None and str(value).strip()
                    for value in existing_line.observed.values()
                ):
                    raise WorkflowNeedsReview(
                        "reconcile Product Save",
                        "Order line readback could not confirm an exact existing row. "
                        "Product selection will not be repeated.",
                        expected={"sku": item.sku, "item_index": item_index},
                        observed=existing_line.observed,
                    )
                # A unique exact row is already attached to this Order. Searching
                # again may itself accept a product in some Fakturama settings.
                self._replace_pending_with_line(item, item_index)
                self._recover_pending(skip_resolved=skip_resolved)
                return
            if resolution.action is ResolutionAction.MANUAL_REVIEW:
                expected = (
                    {
                        "company": source.debtor.company,
                        "first_name": source.debtor.first_name,
                        "last_name": source.debtor.last_name,
                        "zip_code": source.debtor.billing_address.zip_code,
                        "city": source.debtor.billing_address.city,
                    }
                    if pending.name == "save_debtor"
                    else {"sku": item.sku, "vat_rate_percent": str(item.vat_rate_percent)}
                )
                raise WorkflowNeedsReview(
                    step, resolution.reason, expected=expected, candidates=candidates
                )
            if resolution.action is not ResolutionAction.REUSE or resolution.match is None:
                raise WorkflowNeedsReview(
                    step,
                    "No exact saved master record is visible after an uncertain Save. It will not "
                    "be repeated.",
                    candidates=candidates,
                )
            if pending.name == "save_product":
                self._replace_pending_with_line(item, item_index)
                self._recover_pending(skip_resolved=skip_resolved)
            else:
                self.gateway.select_debtor(resolution.match)
                self._require_verified(
                    self.gateway.verify_order_debtor(source.debtor),
                    "Order Debtor and addresses",
                )
                self._confirm_and_advance(WorkflowState.DEBTOR_RESOLVED)
            return

        if pending.name == "create_vat":
            rate = Decimal(str(pending.details["rate"]))
            candidates = self.gateway.find_vats(rate)
            resolution = resolve_vat(rate, candidates)
            collisions = conflicting_vat(rate, candidates)
            if collisions:
                raise WorkflowNeedsReview(
                    "reconcile VAT creation",
                    "Conflicting VAT records are visible.",
                    candidates=collisions,
                )
            if resolution.action is ResolutionAction.MANUAL_REVIEW:
                raise WorkflowNeedsReview(
                    "reconcile VAT creation", resolution.reason, candidates=candidates
                )
            if resolution.action is not ResolutionAction.REUSE or resolution.match is None:
                raise WorkflowNeedsReview(
                    "reconcile VAT creation",
                    "No exact VAT record is visible after an uncertain creation. It will not be "
                    "repeated.",
                    candidates=candidates,
                )
            checkpoint.confirm_action()
            self._save_checkpoint()
            return

        if pending.name == "create_payment_method":
            code = payment_code(source.payment.method)
            candidates = self.gateway.find_payment_methods(source.payment.method)
            resolution = resolve_payment_method(source.payment.method, code, candidates)
            if resolution.action is ResolutionAction.MANUAL_REVIEW:
                raise WorkflowNeedsReview(
                    "reconcile payment method", resolution.reason, candidates=candidates
                )
            if resolution.action is ResolutionAction.CREATE:
                raise WorkflowNeedsReview(
                    "reconcile payment method",
                    "No exact method is visible after uncertain creation. Inspect the terms of "
                    "payment manager before resuming; creation will not be repeated.",
                    expected={"name": source.payment.method, "code": code.value},
                )
            self._ensure_order_ref()
            self._return_to_order()
            checkpoint.confirm_action()
            self._save_checkpoint()
            return

        if pending.name == "apply_payment":
            self._ensure_invoice_ref()
            if skip_resolved:
                verify_payment = getattr(self.gateway, "verify_invoice_payment", None)
                if callable(verify_payment) and verify_payment(source).verified:
                    self._confirm_and_advance(WorkflowState.PAYMENT_APPLIED)
                    return
            # apply_payment is an idempotent ensure-state operation: it sets the
            # same paid/method/date/value fields and verifies their readback.
            self._require_verified(self.gateway.apply_payment(source), "Invoice payment status")
            self._confirm_and_advance(WorkflowState.PAYMENT_APPLIED)
            return

        if pending.name == "fill_order_line":
            self._ensure_order_ref()
            self._return_to_order()
            item_index = int(pending.details["item_index"])
            item = source.items[item_index]
            # Selection may already have added the row. Read it before any edit;
            # if no exact row can be identified, the gateway pauses for review.
            observed = self.gateway.verify_order_line(item, item_index)
            if not observed.verified:
                self._require_verified(
                    self.gateway.fill_order_line(item),
                    f"reconcile Order line {item.sku}",
                )
            else:
                self._require_verified(observed, f"reconcile Order line {item.sku}")
            if item_index not in checkpoint.completed_item_indexes:
                checkpoint.completed_item_indexes.append(item_index)
            self._confirm_action()
            return

        raise WorkflowNeedsReview(
            "reconcile workflow action",
            f"Unknown pending action {pending.name!r}; no action was repeated.",
            observed={"pending_action": pending.model_dump(mode="json")},
        )

    def _verify_preflight(self, result: Any) -> None:
        mismatches: dict[str, Any] = {}
        if result.version != "2.2.0":
            mismatches["version"] = result.version
        if result.language != "English":
            mismatches["language"] = result.language
        if not result.dpi_aware:
            mismatches["dpi_aware"] = result.dpi_aware
        if mismatches:
            raise WorkflowNeedsReview(
                "Fakturama preflight",
                "Fakturama does not match the configured application requirements.",
                expected={"version": "2.2.0", "language": "English", "dpi_aware": True},
                observed=mismatches,
            )

    @staticmethod
    def _validate_source(source: OrderSource) -> None:
        if source.currency != "EUR":
            raise WorkflowInputError(
                f"currency {source.currency!r} is not supported by this assessment workflow"
            )
        totals = calculate_order_totals(
            source.items, source.order_discount_percent, source.shipping
        )
        if any(
            abs(getattr(source.totals, field) - getattr(totals, field)) > CENT
            for field in ("net", "vat", "gross")
        ):
            raise WorkflowInputError("source totals do not match deterministic line calculations")

    def _verify_order_row(self, row: DocumentRow, source: OrderSource, number: str) -> None:
        issues = []
        if row.number != number:
            issues.append("document number differs")
        if row.type.casefold() != "order":
            issues.append(f"document type is {row.type!r}")
        if row.reference != source.external_reference:
            issues.append("Cust.Ref. differs")
        if getattr(row, "document_date", None) != source.order_date:
            issues.append("document date differs")
        if abs(Decimal(str(row.total)) - source.totals.gross) > CENT:
            issues.append("gross total differs")
        if row.state.strip().casefold() != "open":
            issues.append(f"Order state is {row.state!r}, expected Open")
        if row.linked_order_number is not None:
            issues.append("an Order row unexpectedly links to another Order")
        if issues:
            raise WorkflowNeedsReview(
                "verify saved Order",
                "; ".join(issues),
                expected={
                    "number": number,
                    "type": "Order",
                    "reference": source.external_reference,
                    "document_date": source.order_date.isoformat(),
                    "total": str(source.totals.gross),
                    "state": "Open",
                },
                observed=_jsonable(row),
                candidates=[row],
            )

    def _ensure_order_ref(self) -> None:
        checkpoint = self._require_checkpoint()
        if self._order_ref is not None and self.gateway.order_is_open(self._order_ref):
            if (
                checkpoint.draft_order_number is not None
                and self._order_ref.number != checkpoint.draft_order_number
            ):
                raise WorkflowNeedsReview(
                    "restore open Order",
                    "The active Order number differs from this run's draft Order number.",
                    expected={"number": checkpoint.draft_order_number},
                    observed={"number": self._order_ref.number},
                )
            return
        self._set_step("restore open Order")
        discovered = self.gateway.discover_open_order(self._require_source())
        if discovered is None:
            raise WorkflowNeedsReview(
                "restore open Order",
                "The same open Order editor could not be rediscovered. No follow-up action was "
                "taken.",
                expected={"reference": self._require_source().external_reference},
            )
        if (
            checkpoint.draft_order_number is not None
            and discovered.number != checkpoint.draft_order_number
        ):
            raise WorkflowNeedsReview(
                "restore open Order",
                "The discovered Order number differs from this run's draft Order number.",
                expected={"number": checkpoint.draft_order_number},
                observed={"number": discovered.number},
            )
        self._order_ref = discovered
        if checkpoint.draft_order_number is None and discovered.number:
            checkpoint.draft_order_number = discovered.number
            self._save_checkpoint()
            self._event("draft_order_identified", {"number": discovered.number})

    def _record_invoice_provenance(self, invoice_ref: InvoiceEditorRef) -> None:
        checkpoint = self._require_checkpoint()
        order_number = self._require_order_number()
        if invoice_ref.linked_order_number != order_number:
            raise WorkflowNeedsReview(
                "create linked Invoice",
                "The created Invoice did not retain the source Order action context.",
                expected={"source_order_number": order_number},
                observed={"source_order_number": invoice_ref.linked_order_number},
            )
        checkpoint.invoice_provenance = InvoiceProvenance(
            source_order_number=order_number,
            invoice_number=invoice_ref.number,
            proposed_invoice_date=invoice_ref.proposed_invoice_date,
            proposed_service_date=invoice_ref.proposed_service_date,
        )
        self._save_checkpoint()
        self._event(
            "invoice_provenance_recorded",
            {
                "source_order_number": order_number,
                "invoice_number": invoice_ref.number,
            },
        )

    def _restore_invoice_context(self) -> None:
        provenance = self._require_checkpoint().invoice_provenance
        if provenance is None:
            return
        restore = getattr(self.gateway, "restore_invoice_context", None)
        if callable(restore):
            restore(provenance, self._require_source())

    def _check_invoice_provenance(self, invoice_ref: InvoiceEditorRef) -> None:
        provenance = self._require_checkpoint().invoice_provenance
        if provenance is None:
            return
        expected = {
            "invoice_number": provenance.invoice_number,
            "proposed_invoice_date": provenance.proposed_invoice_date,
            "proposed_service_date": provenance.proposed_service_date,
        }
        observed = {
            "invoice_number": invoice_ref.number,
            "proposed_invoice_date": invoice_ref.proposed_invoice_date,
            "proposed_service_date": invoice_ref.proposed_service_date,
        }
        mismatches = [
            field for field, value in expected.items()
            if value is not None and observed[field] != value
        ]
        if mismatches:
            raise WorkflowNeedsReview(
                "restore linked Invoice",
                "The open Invoice differs from its recorded creation: "
                + ", ".join(mismatches),
                expected=expected,
                observed=observed,
            )

    def _ensure_invoice_ref(self) -> None:
        if self._invoice_ref is not None:
            return
        self._set_step("restore linked Invoice")
        self._restore_invoice_context()
        self._invoice_ref = self.gateway.discover_open_invoice(
            self._require_source(), self._require_order_number()
        )
        if self._invoice_ref is None:
            raise WorkflowNeedsReview(
                self._active_step,
                "The linked Invoice editor could not be rediscovered. No payment or Save action "
                "was taken.",
                expected={"order_number": self._require_order_number()},
            )
        self._check_invoice_provenance(self._invoice_ref)

    def _return_to_order(self) -> None:
        reference = self._require_order_ref()
        self.gateway.return_to_order(reference)
        if not self.gateway.order_is_open(reference):
            raise WorkflowNeedsReview(
                self._active_step,
                "The original Order editor did not return to the foreground after master-data "
                "creation.",
                expected={"reference": self._require_source().external_reference},
            )

    def _require_verified(self, result: VerificationResult, step: str) -> None:
        try:
            result.require_verified(step)
        except PostconditionFailed as exc:
            raise WorkflowNeedsReview(
                step,
                str(exc),
                expected=result.expected,
                observed=result.observed,
            ) from exc
        for path in result.evidence_paths:
            self._event("evidence", {"step": step, "path": str(path)})

    def _begin_action(self, name: str, **details: Any) -> None:
        checkpoint = self._require_checkpoint()
        checkpoint.begin_action(name, **details)
        self._save_checkpoint()
        self._event("action_started", {"name": name, **details})

    def _replace_pending_with_line(self, item: Any, index: int) -> None:
        checkpoint = self._require_checkpoint()
        previous = checkpoint.pending_action.name if checkpoint.pending_action else None
        checkpoint.confirm_action()
        details = {
            "sku": item.sku,
            "item_index": index,
            "expected_net": str(item.source_line_net_total),
        }
        checkpoint.begin_action("fill_order_line", **details)
        self._save_checkpoint()
        if previous is not None:
            self._event("action_confirmed", {"name": previous})
        self._event("action_started", {"name": "fill_order_line", **details})

    def _confirm_action(self) -> None:
        checkpoint = self._require_checkpoint()
        previous = checkpoint.pending_action.name if checkpoint.pending_action else "unknown"
        checkpoint.confirm_action()
        self._save_checkpoint()
        self._event("action_confirmed", {"name": previous})

    def _confirm_and_advance(self, state: WorkflowState) -> None:
        checkpoint = self._require_checkpoint()
        action = checkpoint.pending_action.name if checkpoint.pending_action else None
        checkpoint.confirm_action()
        checkpoint.mark_state(state)
        self._save_checkpoint()
        if action is not None:
            self._event("action_confirmed", {"name": action})
        self._event("state_verified", {"state": state.value})

    def _advance(self, state: WorkflowState) -> None:
        checkpoint = self._require_checkpoint()
        checkpoint.mark_state(state)
        self._save_checkpoint()
        self._event("state_verified", {"state": state.value})

    def _pause(self, error: Exception) -> WorkflowRunResult:
        checkpoint = self._require_checkpoint()
        if isinstance(error, WorkflowNeedsReview):
            step = error.step
            reason = str(error)
            expected = error.expected
            observed = error.observed
            candidates = error.candidates
        else:
            step = self._active_step
            reason = str(error) or error.__class__.__name__
            expected = {}
            observed = {}
            candidates = []
        screenshot_path: str | None = None
        try:
            captured = self.gateway.capture_evidence(f"review-{step}")
            if captured is not None:
                screenshot_path = str(captured)
        except Exception as capture_error:
            self._event("evidence_capture_failed", {"error": str(capture_error)})
        review = ReviewBundle(
            run_id=checkpoint.run_id,
            failed_step=step,
            reason=reason,
            expected=_jsonable(expected),
            observed=_jsonable(observed),
            candidates=[_jsonable(candidate) for candidate in candidates],
            screenshot_path=screenshot_path,
        )
        checkpoint.wait_for_review(review)
        self.store.save_review(review)
        self._save_checkpoint()
        self._event(
            "waiting_for_review",
            {
                "step": step,
                "reason": reason,
                "pending_action": checkpoint.pending_action.name
                if checkpoint.pending_action
                else None,
            },
        )
        return WorkflowRunResult(
            checkpoint, self.store.run_directory(checkpoint.run_id) / "review.json"
        )

    def _save_checkpoint(self) -> None:
        checkpoint = self._require_checkpoint()
        self.store.save(checkpoint)

    def _set_step(self, step: str) -> None:
        self._active_step = step
        self._event("step_started", {"step": step})

    def _event(self, name: str, details: dict[str, Any]) -> None:
        checkpoint = self._require_checkpoint()
        clean_details = _jsonable(details)
        self.store.event(checkpoint.run_id, name, clean_details)
        if self.progress is not None:
            try:
                self.progress(name, clean_details)
            except Exception:
                # Console reporting must never change the workflow outcome.
                pass

    def _require_checkpoint(self) -> WorkflowCheckpoint:
        if self._checkpoint is None:
            raise RuntimeError("workflow checkpoint is not initialized")
        return self._checkpoint

    def _require_source(self) -> OrderSource:
        source = self._require_checkpoint().source
        if source is None:
            raise RuntimeError("workflow checkpoint has no validated source")
        return source

    def _require_order_ref(self) -> OrderEditorRef:
        self._ensure_order_ref()
        assert self._order_ref is not None
        return self._order_ref

    def _require_order_number(self) -> str:
        number = self._require_checkpoint().order_number
        if not number:
            raise WorkflowNeedsReview("workflow recovery", "The verified Order number is missing.")
        return number

    def _require_invoice_number(self) -> str:
        number = self._require_checkpoint().invoice_number
        if not number:
            raise WorkflowNeedsReview(
                "workflow recovery", "The verified Invoice number is missing."
            )
        return number


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, (str, int, float)):
        return value.value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)
