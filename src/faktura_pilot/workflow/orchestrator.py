from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from faktura_pilot.automation.gateway import FakturamaGateway
from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    DocumentRow,
    GatewayError,
    InvoiceEditorRef,
    OrderEditorRef,
    PostconditionFailed,
    VerificationResult,
)
from faktura_pilot.domain.calculations import calculate_order_totals
from faktura_pilot.domain.models import CENT, OrderSource
from faktura_pilot.domain.policy import ResolutionAction
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


class WorkflowOrchestrator:
    """Deterministic Order-first coordinator around an extraction and UI gateway."""

    def __init__(
        self,
        extractor: ExtractionService,
        gateway: FakturamaGateway,
        store: WorkflowStore,
        *,
        executable: Path | None = None,
    ) -> None:
        self.extractor = extractor
        self.gateway = gateway
        self.store = store
        self.executable = executable
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

    def resume(self, run_id: str) -> WorkflowRunResult:
        checkpoint = self.store.load(run_id)
        if checkpoint.source is None:
            raise WorkflowStoreError("run has no validated source; it cannot be resumed")
        if checkpoint.state is WorkflowState.COMPLETE:
            return WorkflowRunResult(checkpoint)
        self._validate_source(checkpoint.source)
        checkpoint.prepare_resume()
        self._checkpoint = checkpoint
        self.store.save(checkpoint)
        try:
            self.gateway.attach_or_launch(self.executable)
            self._verify_preflight(self.gateway.preflight())
            self._recover_pending()
            return self._execute(already_attached=True)
        except (WorkflowNeedsReview, GatewayError) as exc:
            return self._pause(exc)

    def _execute(self, *, already_attached: bool = False) -> WorkflowRunResult:
        checkpoint = self._require_checkpoint()
        source = self._require_source()
        try:
            if checkpoint.state is WorkflowState.EXTRACTED:
                self._active_step = "source validation"
                self._validate_source(source)
                self._advance(WorkflowState.VALIDATED)

            if checkpoint.state is WorkflowState.VALIDATED:
                if not already_attached:
                    self.gateway.attach_or_launch(self.executable)
                    preflight = self.gateway.preflight()
                    self._verify_preflight(preflight)
                self._active_step = "open Order"
                self._order_ref = self.gateway.discover_open_order(source)
                if self._order_ref is None:
                    self._order_ref = self.gateway.open_new_order()
                self.gateway.fill_order_header(source)
                if not self.gateway.order_is_open(self._order_ref):
                    raise WorkflowNeedsReview(
                        "open Order",
                        "Fakturama did not retain the expected Order editor after header entry.",
                        expected={"reference": source.external_reference},
                    )
                self._advance(WorkflowState.ORDER_OPEN)

            if checkpoint.state.value in {
                WorkflowState.ORDER_OPEN.value,
                WorkflowState.DEBTOR_RESOLVED.value,
                WorkflowState.ITEMS_RESOLVED.value,
            }:
                self._ensure_order_ref()

            if checkpoint.state is WorkflowState.ORDER_OPEN:
                self._active_step = "resolve Debtor"
                self._resolve_debtor(source)
                self._advance(WorkflowState.DEBTOR_RESOLVED)

            if checkpoint.state is WorkflowState.DEBTOR_RESOLVED:
                self._active_step = "resolve Products"
                self._resolve_products(source)
                self._advance(WorkflowState.ITEMS_RESOLVED)

            if checkpoint.state is WorkflowState.ITEMS_RESOLVED:
                self._active_step = "save Order"
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
                self._active_step = "verify saved Order"
                order_number = self._require_order_number()
                row = self.gateway.verify_order_document(source, order_number)
                self._verify_order_row(row, source, order_number)
                self._advance(WorkflowState.ORDER_VERIFIED)

            if checkpoint.state is WorkflowState.ORDER_VERIFIED:
                self._active_step = "create linked Invoice"
                self._begin_action(
                    "create_linked_invoice",
                    order_number=self._require_order_number(),
                )
                self._invoice_ref = self.gateway.create_linked_invoice(self._require_order_number())
                self._require_verified(
                    self.gateway.verify_invoice_copied_order(source, self._require_order_number()),
                    "Invoice copied from Order",
                )
                self._confirm_and_advance(WorkflowState.INVOICE_OPEN)

            if checkpoint.state is WorkflowState.INVOICE_OPEN:
                self._active_step = "apply payment status"
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
                self._active_step = "save Invoice"
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
                self._active_step = "verify saved Invoice and Order"
                self._require_verified(
                    self.gateway.verify_final_documents(
                        source,
                        self._require_order_number(),
                        self._require_invoice_number(),
                    ),
                    "saved Order and linked Invoice",
                )
                self._advance(WorkflowState.COMPLETE)

            return WorkflowRunResult(checkpoint)
        except (WorkflowNeedsReview, GatewayError) as exc:
            return self._pause(exc)

    def _resolve_debtor(self, source: OrderSource) -> None:
        gateway = self.gateway
        reference = source.external_reference
        debtor = source.debtor
        gateway.open_debtor_selector()
        candidates = gateway.find_debtors(debtor.company)
        resolution = resolve_debtor(debtor, candidates)
        if resolution.action is ResolutionAction.MANUAL_REVIEW:
            raise WorkflowNeedsReview(
                "resolve Debtor",
                resolution.reason,
                expected={"company": debtor.company, "zip_code": debtor.billing_address.zip_code},
                candidates=candidates,
            )
        if resolution.action is ResolutionAction.REUSE:
            assert resolution.match is not None
            gateway.select_debtor(resolution.match)
        else:
            code = payment_code(source.payment.method)
            self._active_step = "create Debtor and payment method"
            gateway.open_new_debtor()
            gateway.fill_debtor(debtor)
            payment_candidates = gateway.find_payment_methods(source.payment.method)
            payment_resolution = resolve_payment_method(
                source.payment.method, code, payment_candidates
            )
            if payment_resolution.action is ResolutionAction.MANUAL_REVIEW:
                raise WorkflowNeedsReview(
                    "resolve payment method",
                    payment_resolution.reason,
                    expected={"name": source.payment.method, "code": code.value},
                    candidates=payment_candidates,
                )
            if payment_resolution.action is ResolutionAction.REUSE:
                assert payment_resolution.match is not None
                gateway.select_payment_method(payment_resolution.match)
            else:
                self._begin_action(
                    "create_payment_method",
                    method_name=source.payment.method,
                    code=code.value,
                )
                created_method = gateway.create_payment_method(source.payment.method, code.value)
                payment_candidates = gateway.find_payment_methods(source.payment.method)
                verified_method = resolve_payment_method(
                    source.payment.method, code, payment_candidates
                )
                if (
                    verified_method.action is not ResolutionAction.REUSE
                    or verified_method.match is None
                ):
                    raise WorkflowNeedsReview(
                        "verify payment method",
                        "The created payment method was not uniquely visible after creation.",
                        expected={"name": source.payment.method, "code": code.value},
                        observed={"returned": created_method},
                        candidates=payment_candidates,
                    )
                gateway.select_payment_method(verified_method.match)
                self._confirm_action()

            self._begin_action(
                "save_debtor",
                company=debtor.company,
                zip_code=debtor.billing_address.zip_code,
                city=debtor.billing_address.city,
            )
            gateway.save_debtor()
            self._return_to_order()
            gateway.open_debtor_selector()
            created_candidates = gateway.find_debtors(debtor.company)
            created_resolution = resolve_debtor(debtor, created_candidates)
            if (
                created_resolution.action is not ResolutionAction.REUSE
                or created_resolution.match is None
            ):
                raise WorkflowNeedsReview(
                    "select created Debtor",
                    "The saved Debtor is not a unique exact match in the Order selector.",
                    expected={"company": debtor.company, "reference": reference},
                    candidates=created_candidates,
                )
            gateway.select_debtor(created_resolution.match)
            self._confirm_action()

        self._active_step = "verify Order Debtor"
        self._require_verified(gateway.verify_order_debtor(debtor), "Order Debtor and addresses")

    def _resolve_products(self, source: OrderSource) -> None:
        gateway = self.gateway
        for index, item in enumerate(source.items):
            if index in self._require_checkpoint().completed_item_indexes:
                continue
            self._active_step = f"resolve Product {index + 1} ({item.sku})"
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
                gateway.select_product(candidate)
            else:
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
                    self._begin_action("create_vat", rate=str(item.vat_rate_percent))
                    gateway.create_vat(item.vat_rate_percent)
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
                gateway.open_new_product()
                gateway.fill_product(item, vat)
                self._begin_action("save_product", sku=item.sku, item_index=index)
                gateway.save_product()
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
                gateway.select_product(created_resolution.match)
                self._confirm_action()

            self._begin_action(
                "fill_order_line",
                sku=item.sku,
                item_index=index,
                expected_net=str(item.source_line_net_total),
            )
            result = gateway.fill_order_line(item)
            self._require_verified(result, f"Order line {item.sku}")
            self._require_checkpoint().completed_item_indexes.append(index)
            self._confirm_action()
            self._event(
                "order_line_verified",
                {"index": index, "sku": item.sku, "net": str(item.source_line_net_total)},
            )

    def _recover_pending(self) -> None:
        checkpoint = self._require_checkpoint()
        pending = checkpoint.pending_action
        if pending is None:
            return
        source = self._require_source()
        self._active_step = f"reconcile {pending.name}"
        self._event("reconcile_started", {"action": pending.name})

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
                candidates = self.gateway.find_debtors(source.debtor.company)
                resolution = resolve_debtor(source.debtor, candidates)
                step = "reconcile Debtor Save"
            else:
                item_index = int(pending.details["item_index"])
                item = source.items[item_index]
                self.gateway.open_product_selector()
                candidates = self.gateway.find_products(item.sku)
                resolution = resolve_product(item.sku, candidates)
                step = "reconcile Product Save"
            if resolution.action is ResolutionAction.MANUAL_REVIEW:
                raise WorkflowNeedsReview(step, resolution.reason, candidates=candidates)
            if resolution.action is not ResolutionAction.REUSE or resolution.match is None:
                raise WorkflowNeedsReview(
                    step,
                    "No exact saved master record is visible after an uncertain Save. It will not "
                    "be repeated.",
                    candidates=candidates,
                )
            checkpoint.confirm_action()
            self._save_checkpoint()
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
                    "No exact method is visible; restore the Debtor payment-method editor before "
                    "resuming.",
                    expected={"name": source.payment.method, "code": code.value},
                )
            self._ensure_order_ref()
            self._return_to_order()
            checkpoint.confirm_action()
            self._save_checkpoint()
            return

        if pending.name == "apply_payment":
            self._ensure_invoice_ref()
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
            self._require_verified(
                self.gateway.verify_order_line(item, item_index),
                f"reconcile Order line {item.sku}",
            )
            if item_index not in checkpoint.completed_item_indexes:
                checkpoint.completed_item_indexes.append(item_index)
            self._confirm_action()
            self._save_checkpoint()
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
        totals = calculate_order_totals(source.items)
        if any(
            abs(getattr(source.totals, field) - getattr(totals, field)) > CENT
            for field in ("net", "vat", "gross")
        ):
            raise WorkflowInputError("source totals do not match deterministic line calculations")
        try:
            payment_code(source.payment.method)
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from exc

    def _verify_order_row(self, row: DocumentRow, source: OrderSource, number: str) -> None:
        issues = []
        if row.number != number:
            issues.append("document number differs")
        if row.type.casefold() != "order":
            issues.append(f"document type is {row.type!r}")
        if row.reference != source.external_reference:
            issues.append("Cust.Ref. differs")
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
                    "total": str(source.totals.gross),
                    "state": "Open",
                },
                observed=_jsonable(row),
                candidates=[row],
            )

    def _ensure_order_ref(self) -> None:
        if self._order_ref is not None and self.gateway.order_is_open(self._order_ref):
            return
        self._order_ref = self.gateway.discover_open_order(self._require_source())
        if self._order_ref is None:
            raise WorkflowNeedsReview(
                self._active_step,
                "The same open Order editor could not be rediscovered. No follow-up action was "
                "taken.",
                expected={"reference": self._require_source().external_reference},
            )

    def _ensure_invoice_ref(self) -> None:
        if self._invoice_ref is not None:
            return
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

    def _event(self, name: str, details: dict[str, Any]) -> None:
        checkpoint = self._require_checkpoint()
        self.store.event(checkpoint.run_id, name, _jsonable(details))

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
