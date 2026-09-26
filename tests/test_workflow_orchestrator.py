import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    DebtorCandidate,
    ElementNotFound,
    OrderEditorRef,
    PaymentMethodCandidate,
    ProductCandidate,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.domain.models import OrderTotals, Payment, PaymentStatus
from faktura_pilot.workflow.orchestrator import WorkflowInputError, WorkflowOrchestrator
from faktura_pilot.workflow.state import ReviewBundle, WorkflowCheckpoint, WorkflowState
from faktura_pilot.workflow.store import WorkflowStore
from tests.factories import sample_order
from tests.workflow_fakes import FixedExtractor, ScriptedGateway


def _debtor_candidate(token: str = "debtor-existing") -> DebtorCandidate:
    debtor = sample_order().debtor
    return DebtorCandidate(
        token,
        debtor.company,
        debtor.first_name,
        debtor.last_name,
        debtor.billing_address.zip_code,
        debtor.billing_address.city,
    )


class _PersistingDebtorGateway(ScriptedGateway):
    """Fake the Debtor editor and selector while retaining the full saved record."""

    def __init__(self) -> None:
        super().__init__()
        self.pending_debtor = None
        self.saved_debtor = None
        self.selected_debtors: list[DebtorCandidate] = []
        self.verified_debtor = None

    def fill_debtor(self, debtor) -> None:
        self.pending_debtor = debtor.model_copy(deep=True)

    def save_debtor(self) -> DebtorCandidate:
        if self.pending_debtor is None:
            raise AssertionError("the Debtor form was saved before it was filled")
        self.save_debtor_calls += 1
        self.saved_debtor = self.pending_debtor.model_copy(deep=True)
        debtor = self.saved_debtor
        candidate = DebtorCandidate(
            f"persisted-debtor-{self.save_debtor_calls}",
            debtor.company,
            debtor.first_name,
            debtor.last_name,
            debtor.billing_address.zip_code,
            debtor.billing_address.city,
        )
        self.debtors.append(candidate)
        return candidate

    def select_debtor(self, candidate: DebtorCandidate) -> None:
        self.selected_debtors.append(candidate)

    def verify_order_debtor(self, debtor) -> VerificationResult:
        self.verified_debtor = debtor.model_copy(deep=True)
        return self.verified(self.saved_debtor == debtor)


@dataclass(frozen=True)
class _DatedDocumentRow:
    number: str
    type: str
    reference: str
    total: Decimal
    state: str
    linked_order_number: str | None
    document_date: date | None


def _dated_row(row, document_date: date | None) -> _DatedDocumentRow:
    return _DatedDocumentRow(
        number=row.number,
        type=row.type,
        reference=row.reference,
        total=row.total,
        state=row.state,
        linked_order_number=row.linked_order_number,
        document_date=document_date,
    )


def _product_candidates() -> list[ProductCandidate]:
    return [
        ProductCandidate("chair", "CHR-ERG-01", "Ergonomic chair", Decimal("19")),
        ProductCandidate("mat", "MAT-DESK-02", "Desk mat", Decimal("19")),
    ]


class WorkflowOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _prepare_document_date_readback(gateway: ScriptedGateway) -> None:
        if getattr(gateway, "_test_document_date_prepared", False):
            return
        original_verify = gateway.verify_order_document
        original_find = gateway.find_documents

        def dated(row, source):
            document_date = getattr(row, "document_date", None)
            return _dated_row(row, document_date or source.order_date)

        def verify_order_document(source, order_number):
            return dated(original_verify(source, order_number), source)

        def find_documents(source, document_type, linked_order_number=None):
            rows = original_find(source, document_type, linked_order_number)
            if document_type.casefold() != "order":
                return rows
            return [dated(row, source) for row in rows]

        gateway.verify_order_document = verify_order_document
        gateway.find_documents = find_documents
        gateway._test_document_date_prepared = True

    def run_workflow(
        self,
        gateway: ScriptedGateway,
        source=None,
        run_id: str = "case-1",
        store: WorkflowStore | None = None,
    ):
        source = source or sample_order()
        gateway.current_source = source
        self._prepare_document_date_readback(gateway)
        store = store or WorkflowStore(self.root / "runs")
        runner = WorkflowOrchestrator(FixedExtractor(source), gateway, store)
        return runner.run(self.root / "order.png", run_id=run_id), store

    def test_all_existing_master_data_completes_paid_flow(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()],
            products=_product_candidates(),
            vats=[VatCandidate("vat-19", "VAT 19%", Decimal("19"), "S")],
            payment_methods=[PaymentMethodCandidate("method", "Bank Transfer", "Credit transfer")],
        )

        result, _ = self.run_workflow(gateway)

        self.assertEqual(result.state, WorkflowState.COMPLETE)
        self.assertEqual(result.order_number, "ORD-100")
        self.assertEqual(result.checkpoint.draft_order_number, "ORD-100")
        self.assertEqual(result.invoice_number, "INV-200")
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.create_product_calls, 0)
        self.assertEqual(gateway.create_vat_calls, 0)
        self.assertEqual(gateway.save_order_calls, 1)
        self.assertEqual(gateway.save_invoice_calls, 1)
        self.assertEqual(gateway.payment_sources[0].payment.status, PaymentStatus.PAID)

    def test_fresh_run_opens_new_order_without_adopting_matching_open_editor(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()],
            products=_product_candidates(),
        )
        gateway.order_ref = OrderEditorRef("unrelated-open-editor", "ORD-EXISTING")
        discoveries = []
        gateway.discover_open_order = lambda source: discoveries.append(source) or gateway.order_ref

        result, _ = self.run_workflow(gateway)

        self.assertTrue(result.complete)
        self.assertEqual(discoveries, [])
        self.assertEqual(gateway.open_order_calls, 1)

    def test_resume_before_order_open_starts_a_new_order(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.current_source = source
        self._prepare_document_date_readback(gateway)
        gateway.order_ref = OrderEditorRef("unrelated-open-editor", "ORD-EXISTING")
        discoveries = []
        gateway.discover_open_order = (
            lambda current: discoveries.append(current) or gateway.order_ref
        )
        store = WorkflowStore(self.root / "resume-before-order")
        store.create(
            WorkflowCheckpoint(
                run_id="resume-before-order",
                state=WorkflowState.VALIDATED,
                source=source,
            )
        )

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "resume-before-order"
        )

        self.assertTrue(result.complete)
        self.assertEqual(discoveries, [])
        self.assertEqual(gateway.open_order_calls, 1)

    def test_legacy_validated_checkpoint_does_not_open_another_draft(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway()
        store = WorkflowStore(self.root / "legacy-validated")
        store.create(
            WorkflowCheckpoint(
                run_id="legacy-validated", state=WorkflowState.VALIDATED,
                format_version=1, source=source,
            )
        )

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "legacy-validated"
        )
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "reconcile open Order")
        self.assertEqual(gateway.open_order_calls, 0)

    def test_resume_after_order_open_rediscovers_checkpoint_order(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.current_source = source
        self._prepare_document_date_readback(gateway)
        resumed_order = OrderEditorRef("resumed-order-editor", "ORD-100")
        discoveries = []

        def discover_checkpoint_order(current_source):
            discoveries.append(current_source)
            gateway.order_ref = resumed_order
            return resumed_order

        gateway.discover_open_order = discover_checkpoint_order
        store = WorkflowStore(self.root / "resume-after-order-open")
        store.create(
            WorkflowCheckpoint(
                run_id="resume-after-order-open",
                state=WorkflowState.ORDER_OPEN,
                source=source,
            )
        )

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "resume-after-order-open"
        )

        self.assertTrue(result.complete)
        self.assertEqual(discoveries, [source])
        self.assertEqual(gateway.open_order_calls, 0)
        self.assertEqual(result.checkpoint.draft_order_number, "ORD-100")

    def test_resume_rejects_a_different_draft_order_number(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway()
        gateway.order_ref = OrderEditorRef("other-order", "ORD-OTHER")
        store = WorkflowStore(self.root / "wrong-draft-order")
        store.create(
            WorkflowCheckpoint(
                run_id="wrong-draft-order",
                state=WorkflowState.ORDER_OPEN,
                source=source,
                draft_order_number="ORD-EXPECTED",
            )
        )

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "wrong-draft-order"
        )
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "restore open Order")
        self.assertEqual(result.checkpoint.review.expected["number"], "ORD-EXPECTED")
        self.assertEqual(result.checkpoint.review.observed["number"], "ORD-OTHER")
        self.assertEqual(gateway.open_order_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 0)

    def test_saved_order_date_matching_source_allows_invoice_creation(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())

        result, _ = self.run_workflow(gateway, run_id="matching-order-date")

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_invoice_calls, 1)

    def test_saved_order_date_mismatch_pauses_before_invoice_creation(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        self._prepare_document_date_readback(gateway)
        original_verify = gateway.verify_order_document

        def verify_with_wrong_date(current_source, order_number):
            row = original_verify(current_source, order_number)
            return _dated_row(row, current_source.order_date + timedelta(days=1))

        gateway.verify_order_document = verify_with_wrong_date
        result, _ = self.run_workflow(gateway, source, run_id="mismatching-order-date")

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "verify saved Order")
        self.assertEqual(gateway.create_invoice_calls, 0)
        self.assertEqual(gateway.save_invoice_calls, 0)

    def test_all_missing_master_data_completes_unpaid_flow(self) -> None:
        source = sample_order().model_copy(
            update={
                "payment": Payment(
                    method="Bank Transfer", status=PaymentStatus.UNPAID, payment_date=None
                )
            }
        )
        gateway = ScriptedGateway()

        result, _ = self.run_workflow(gateway, source)

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_debtor_calls, 1)
        self.assertEqual(gateway.save_debtor_calls, 1)
        self.assertEqual(gateway.create_payment_method_calls, 1)
        self.assertEqual(gateway.create_product_calls, 2)
        self.assertEqual(gateway.save_product_calls, 2)
        self.assertEqual(gateway.create_vat_calls, 1)
        self.assertEqual(gateway.payment_sources[0].payment.status, PaymentStatus.UNPAID)
        self.assertIsNone(gateway.payment_sources[0].payment.payment_date)

    def test_missing_debtor_resolves_payment_before_opening_contact_form(self) -> None:
        gateway = ScriptedGateway()
        events: list[str] = []
        for name in (
            "open_debtor_selector", "find_debtors", "find_payment_methods",
            "create_payment_method", "return_to_order", "open_new_debtor",
            "fill_debtor", "select_payment_method", "save_debtor",
        ):
            original = getattr(gateway, name)

            def logged(*args, _name=name, _original=original, **kwargs):
                events.append(_name)
                return _original(*args, **kwargs)

            setattr(gateway, name, logged)

        result, _ = self.run_workflow(gateway, run_id="payment-before-debtor")

        self.assertTrue(result.complete)
        self.assertLess(events.index("find_debtors"), events.index("find_payment_methods"))
        self.assertLess(events.index("find_payment_methods"), events.index("create_payment_method"))
        self.assertLess(events.index("create_payment_method"), events.index("return_to_order"))
        restored = events.index("return_to_order")
        self.assertLess(restored, events.index("open_debtor_selector", restored + 1))
        self.assertLess(
            events.index("open_debtor_selector", restored + 1),
            events.index("open_new_debtor"),
        )
        self.assertLess(events.index("fill_debtor"), events.index("select_payment_method"))
        self.assertLess(events.index("select_payment_method"), events.index("save_debtor"))
        self.assertEqual(events.count("find_payment_methods"), 2)

    def test_reused_debtor_does_not_search_or_create_payment_terms(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()], products=_product_candidates()
        )

        def unexpected_payment_search(_query):
            raise AssertionError("existing Debtor must skip payment-term resolution")

        gateway.find_payment_methods = unexpected_payment_search
        result, _ = self.run_workflow(gateway, run_id="reuse-debtor-skip-terms")

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.create_debtor_calls, 0)

    def test_conflicting_payment_definition_stops_before_debtor_form(self) -> None:
        gateway = ScriptedGateway(payment_methods=[
            PaymentMethodCandidate("conflict", "Bank Transfer", "Cash")
        ])

        result, _ = self.run_workflow(gateway, run_id="payment-conflict-before-debtor")

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "resolve payment method")
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 0)

    def test_duplicate_exact_payment_terms_pause_before_debtor_form(self) -> None:
        gateway = ScriptedGateway(payment_methods=[
            PaymentMethodCandidate("method-1", "Bank Transfer", "Credit transfer"),
            PaymentMethodCandidate("method-2", "Bank Transfer", "Credit transfer"),
        ])

        result, _ = self.run_workflow(gateway, run_id="duplicate-payment-before-debtor")

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "resolve payment method")
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 0)

    def test_new_debtor_is_saved_reselected_and_verified_with_all_source_fields(self) -> None:
        source = sample_order().model_copy(
            update={
                "payment": Payment(
                    method="Bank Transfer", status=PaymentStatus.UNPAID, payment_date=None
                )
            }
        )
        gateway = _PersistingDebtorGateway()

        result, _ = self.run_workflow(gateway, source, run_id="new-debtor-readback")

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_debtor_calls, 1)
        self.assertEqual(gateway.save_debtor_calls, 1)
        self.assertEqual(gateway.saved_debtor.model_dump(), source.debtor.model_dump())
        self.assertEqual(gateway.verified_debtor.model_dump(), source.debtor.model_dump())
        self.assertEqual(len(gateway.selected_debtors), 1)
        selected = gateway.selected_debtors[0]
        self.assertEqual(selected.company, source.debtor.company)
        self.assertEqual(selected.first_name, source.debtor.first_name)
        self.assertEqual(selected.last_name, source.debtor.last_name)
        self.assertEqual(selected.zip_code, source.debtor.billing_address.zip_code)
        self.assertEqual(selected.city, source.debtor.billing_address.city)

    def test_existing_unsupported_payment_method_is_reused_for_new_debtor(self) -> None:
        source = sample_order().model_copy(
            update={
                "payment": Payment(
                    method="Custom Wallet", status=PaymentStatus.UNPAID, payment_date=None
                )
            }
        )
        gateway = ScriptedGateway(
            payment_methods=[PaymentMethodCandidate("custom-wallet", "Custom Wallet")]
        )

        result, _ = self.run_workflow(gateway, source)

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 1)
        self.assertEqual(gateway.payment_sources[0].payment.method, "Custom Wallet")

    def test_missing_unsupported_payment_method_pauses_without_creating_it(self) -> None:
        source = sample_order().model_copy(
            update={
                "payment": Payment(
                    method="Custom Wallet", status=PaymentStatus.UNPAID, payment_date=None
                )
            }
        )
        gateway = ScriptedGateway()

        result, _ = self.run_workflow(gateway, source)

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "resolve payment method")
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 0)
        self.assertEqual(gateway.save_order_calls, 0)

    def test_final_persisted_verification_gates_workflow_completion(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())

        def failed_final_verification(_source, _order_number, _invoice_number):
            return VerificationResult(
                verified=False,
                observations=("persisted Invoice state differs from the extracted status",),
                expected={"invoice_state": "Paid"},
                observed={"invoice_state": "Open"},
            )

        gateway.verify_final_documents = failed_final_verification
        result, store = self.run_workflow(gateway, run_id="final-verification")

        persisted = store.load("final-verification")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "saved Order and linked Invoice")
        self.assertEqual(result.checkpoint.resume_state, WorkflowState.INVOICE_SAVED)
        self.assertEqual(persisted.state, WorkflowState.WAITING_FOR_REVIEW)
        self.assertEqual(gateway.save_order_calls, 1)
        self.assertEqual(gateway.save_invoice_calls, 1)

    def test_mixed_existing_and_missing_records_are_resolved_once(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()],
            products=[_product_candidates()[0]],
        )

        result, _ = self.run_workflow(gateway)

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.create_product_calls, 1)
        self.assertEqual(gateway.create_vat_calls, 1)
        self.assertEqual(gateway.fill_order_line_calls, 2)

    def test_existing_product_without_readable_vat_pauses_before_reuse(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()],
            products=[ProductCandidate("chair", "CHR-ERG-01", "Ergonomic chair")],
        )

        result, _ = self.run_workflow(gateway)

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "verify Product VAT CHR-ERG-01")
        self.assertEqual(gateway.selected_products, [])
        self.assertEqual(gateway.fill_order_line_calls, 0)

    def test_ambiguous_exact_debtor_pauses_before_creation_or_save(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate("d1"), _debtor_candidate("d2")])

        result, _ = self.run_workflow(gateway)

        self.assertTrue(result.waiting_for_review)
        self.assertIsNotNone(result.checkpoint.review)
        self.assertEqual(result.checkpoint.review.failed_step, "resolve Debtor")
        self.assertEqual(len(result.checkpoint.review.candidates), 2)
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.save_order_calls, 0)

    def test_source_total_mismatch_is_rejected_before_ui_attachment(self) -> None:
        source = sample_order().model_copy(
            update={
                "totals": OrderTotals(net=Decimal("999"), vat=Decimal("0"), gross=Decimal("999"))
            }
        )
        gateway = ScriptedGateway()
        runner = WorkflowOrchestrator(
            FixedExtractor(source), gateway, WorkflowStore(self.root / "runs")
        )

        with self.assertRaisesRegex(WorkflowInputError, "source totals"):
            runner.run(self.root / "order.png", run_id="bad-totals")

        self.assertEqual(gateway.preflight_calls, 0)
        self.assertEqual(gateway.open_order_calls, 0)

    def test_ui_total_mismatch_pauses_without_saving_order(self) -> None:
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()],
            products=_product_candidates(),
        )
        gateway.fail_order_totals = True

        result, _ = self.run_workflow(gateway)

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.review.failed_step, "Order totals")
        self.assertEqual(gateway.save_order_calls, 0)
        self.assertEqual(gateway.save_invoice_calls, 0)

    def test_uncertain_order_save_is_adopted_after_positive_document_check(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.order_save_unknown = True
        gateway.order_save_persists_before_unknown = True
        result, store = self.run_workflow(gateway, run_id="resume-order-save")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "save_order")

        resumed = WorkflowOrchestrator(FixedExtractor(sample_order()), gateway, store).resume(
            "resume-order-save"
        )

        self.assertTrue(resumed.complete)
        self.assertEqual(gateway.save_order_calls, 1)
        self.assertEqual(gateway.save_invoice_calls, 1)

    def test_uncertain_order_save_without_document_stays_pending_and_is_not_retried(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.order_save_unknown = True
        result, store = self.run_workflow(gateway, run_id="uncertain-empty-order")
        self.assertTrue(result.waiting_for_review)

        resumed = WorkflowOrchestrator(FixedExtractor(sample_order()), gateway, store).resume(
            "uncertain-empty-order"
        )

        self.assertTrue(resumed.waiting_for_review)
        self.assertEqual(resumed.checkpoint.pending_action.name, "save_order")
        self.assertEqual(gateway.save_order_calls, 1)

    def test_uncertain_invoice_save_is_adopted_after_positive_document_check(self) -> None:
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.invoice_save_unknown = True
        gateway.invoice_save_persists_before_unknown = True
        result, store = self.run_workflow(gateway, run_id="resume-invoice-save")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "save_invoice")

        resumed = WorkflowOrchestrator(FixedExtractor(sample_order()), gateway, store).resume(
            "resume-invoice-save"
        )

        self.assertTrue(resumed.complete)
        self.assertEqual(gateway.save_invoice_calls, 1)

    def test_uncertain_payment_method_creation_reuses_positive_exact_match(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(
            payment_methods=[PaymentMethodCandidate("method-1", "Bank Transfer", "Credit transfer")]
        )
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        self._prepare_document_date_readback(gateway)
        store = WorkflowStore(self.root / "payment-method-recovery")
        checkpoint = WorkflowCheckpoint(
            run_id="payment-method-recovery",
            state=WorkflowState.ORDER_OPEN,
            source=source,
        )
        checkpoint.begin_action(
            "create_payment_method",
            method_name="Bank Transfer",
            code="Credit transfer",
        )
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="payment-method-recovery",
                failed_step="create payment method",
                reason="creation result was uncertain",
            )
        )
        store.create(checkpoint)

        actions: list[str] = []
        original_find = gateway.find_payment_methods
        original_open = gateway.open_new_debtor

        def find_method(query):
            actions.append("find_payment_methods")
            return original_find(query)

        def open_debtor():
            actions.append("open_new_debtor")
            return original_open()

        gateway.find_payment_methods = find_method
        gateway.open_new_debtor = open_debtor
        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "payment-method-recovery"
        )

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 1)
        self.assertEqual(actions.count("find_payment_methods"), 2)
        self.assertLess(
            actions.index("find_payment_methods", 1), actions.index("open_new_debtor")
        )

    def test_uncertain_mutations_without_positive_postcondition_are_never_replayed(self) -> None:
        source = sample_order()
        scenarios = [
            ("save_debtor", WorkflowState.ORDER_OPEN, {}, None, None),
            ("save_product", WorkflowState.DEBTOR_RESOLVED, {"item_index": 0}, None, None),
            ("create_vat", WorkflowState.DEBTOR_RESOLVED, {"rate": "19"}, None, None),
            ("save_order", WorkflowState.ITEMS_RESOLVED, {"document_type": "Order"}, None, None),
            (
                "create_linked_invoice",
                WorkflowState.ORDER_VERIFIED,
                {"order_number": "ORD-100"},
                "ORD-100",
                None,
            ),
            (
                "save_invoice",
                WorkflowState.PAYMENT_APPLIED,
                {"linked_order_number": "ORD-100"},
                "ORD-100",
                None,
            ),
            (
                "create_payment_method",
                WorkflowState.ORDER_OPEN,
                {"method_name": "Bank Transfer", "code": "Credit transfer"},
                None,
                None,
            ),
        ]

        for action, state, details, order_number, invoice_number in scenarios:
            with self.subTest(action=action):
                run_id = f"pending-{action}"
                gateway = ScriptedGateway()
                gateway.current_source = source
                gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
                store = WorkflowStore(self.root / action)
                checkpoint = WorkflowCheckpoint(
                    run_id=run_id,
                    state=state,
                    source=source,
                    order_number=order_number,
                    invoice_number=invoice_number,
                )
                checkpoint.begin_action(action, **details)
                checkpoint.wait_for_review(
                    ReviewBundle(run_id=run_id, failed_step=action, reason="uncertain action")
                )
                store.create(checkpoint)

                result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(run_id)

                self.assertTrue(result.waiting_for_review)
                self.assertEqual(result.checkpoint.pending_action.name, action)
                self.assertEqual(gateway.save_debtor_calls, 0)
                self.assertEqual(gateway.save_product_calls, 0)
                self.assertEqual(gateway.create_vat_calls, 0)
                self.assertEqual(gateway.save_order_calls, 0)
                self.assertEqual(gateway.create_invoice_calls, 0)
                self.assertEqual(gateway.save_invoice_calls, 0)
                self.assertEqual(gateway.create_payment_method_calls, 0)
                self.assertEqual(gateway.fill_order_line_calls, 0)

    def test_pending_order_line_is_idempotently_filled_during_resume(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(products=_product_candidates())
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "P000003")
        self._prepare_document_date_readback(gateway)
        store = WorkflowStore(self.root / "pending-fill-order-line")
        checkpoint = WorkflowCheckpoint(
            run_id="pending-fill-order-line",
            state=WorkflowState.DEBTOR_RESOLVED,
            source=source,
        )
        checkpoint.begin_action(
            "fill_order_line", item_index=0, sku=source.items[0].sku
        )
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="pending-fill-order-line",
                failed_step="resolve Product 1",
                reason="selected Product row became visible after verification",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "pending-fill-order-line"
        )

        self.assertTrue(result.complete)
        self.assertEqual(gateway.fill_order_line_calls, 2)
        self.assertEqual(gateway.order_lines[0], source.items[0])
        self.assertEqual(gateway.order_lines[1], source.items[1])
        self.assertEqual(gateway.save_order_calls, 1)
        self.assertEqual(gateway.save_invoice_calls, 1)

    def test_interrupted_order_open_without_number_does_not_adopt_other_draft(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        original_open = gateway.open_new_order

        def open_then_lose_result():
            original_open()
            raise ActionOutcomeUnknown("Order editor opened before interruption")

        gateway.open_new_order = open_then_lose_result
        result, store = self.run_workflow(gateway, run_id="interrupted-order-open")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "open_order")

        gateway.open_new_order = original_open
        resumed = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "interrupted-order-open"
        )
        self.assertTrue(resumed.waiting_for_review)
        self.assertEqual(resumed.checkpoint.pending_action.name, "open_order")
        self.assertEqual(gateway.open_order_calls, 1)

    def test_interrupted_order_header_recovers_run_owned_editor(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        original_fill = gateway.fill_order_header

        def fail_header(current_source):
            original_fill(current_source)
            raise ActionOutcomeUnknown("Header result was uncertain")

        gateway.fill_order_header = fail_header
        result, store = self.run_workflow(gateway, run_id="interrupted-order-header")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.details["order_number"], "ORD-100")
        self.assertEqual(result.checkpoint.draft_order_number, "ORD-100")

        gateway.fill_order_header = original_fill
        self._prepare_document_date_readback(gateway)
        resumed = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "interrupted-order-header"
        )
        self.assertTrue(resumed.complete)
        self.assertEqual(gateway.open_order_calls, 1)

    def test_interrupted_product_selection_adopts_verified_line_without_reselecting(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        original_select = gateway.select_product

        def select_then_lose_result(candidate):
            original_select(candidate)
            gateway.order_lines[0] = source.items[0]
            raise ActionOutcomeUnknown("Product selected before interruption")

        gateway.select_product = select_then_lose_result
        result, store = self.run_workflow(gateway, run_id="interrupted-product-select")
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "fill_order_line")

        gateway.select_product = original_select
        self._prepare_document_date_readback(gateway)
        resumed = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "interrupted-product-select"
        )
        self.assertTrue(resumed.complete)
        self.assertEqual(len(gateway.selected_products), 2)
        self.assertEqual(gateway.fill_order_line_calls, 1)

    def test_save_product_recovery_adopts_existing_row_without_reselecting(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()], products=_product_candidates())
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        gateway.order_lines[0] = source.items[0]
        self._prepare_document_date_readback(gateway)
        store = WorkflowStore(self.root / "save-product-recovery")
        checkpoint = WorkflowCheckpoint(
            run_id="save-product-recovery",
            state=WorkflowState.DEBTOR_RESOLVED,
            source=source,
        )
        checkpoint.begin_action(
            "save_product", item_index=0, sku=source.items[0].sku,
            selection_not_started=True,
        )
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="save-product-recovery", failed_step="save Product",
                reason="save result was uncertain",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "save-product-recovery"
        )
        self.assertTrue(result.complete)
        self.assertEqual(gateway.save_product_calls, 0)
        self.assertEqual(gateway.fill_order_line_calls, 1)
        self.assertEqual([candidate.sku for candidate in gateway.selected_products],
                         [source.items[1].sku])

    def test_save_product_recovery_stops_when_order_row_is_not_visible(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(products=_product_candidates())
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        product_searches = []
        gateway.find_products = lambda sku: product_searches.append(sku) or []

        def missing_row(item, index):
            raise ElementNotFound("exact SKU is not visible")

        gateway.verify_order_line = missing_row
        store = WorkflowStore(self.root / "missing-product-row")
        checkpoint = WorkflowCheckpoint(
            run_id="missing-product-row",
            state=WorkflowState.DEBTOR_RESOLVED,
            source=source,
        )
        checkpoint.begin_action(
            "save_product", item_index=0, sku=source.items[0].sku,
            selection_not_started=True,
        )
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="missing-product-row", failed_step="save Product",
                reason="save result was uncertain",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "missing-product-row"
        )
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "save_product")
        self.assertIn("Restore the Order Items grid", result.checkpoint.review.reason)
        self.assertEqual(product_searches, [])
        self.assertEqual(gateway.selected_products, [])

    def test_save_product_recovery_stops_on_ambiguous_order_row_readback(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(products=_product_candidates())
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        store = WorkflowStore(self.root / "ambiguous-product-row")
        checkpoint = WorkflowCheckpoint(
            run_id="ambiguous-product-row",
            state=WorkflowState.DEBTOR_RESOLVED,
            source=source,
        )
        checkpoint.begin_action("save_product", item_index=0, sku=source.items[0].sku)
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="ambiguous-product-row", failed_step="save Product",
                reason="save result was uncertain",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "ambiguous-product-row"
        )
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.pending_action.name, "save_product")
        self.assertIn("could not confirm an exact existing row", result.checkpoint.review.reason)
        self.assertEqual(gateway.selected_products, [])

    def test_reconciled_debtor_save_advances_without_second_lookup(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(
            debtors=[_debtor_candidate()], products=_product_candidates()
        )
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        self._prepare_document_date_readback(gateway)
        original_find = gateway.find_debtors
        searches = []

        def find_once(query):
            searches.append(query)
            return original_find(query)

        gateway.find_debtors = find_once
        store = WorkflowStore(self.root / "reconcile-saved-debtor")
        checkpoint = WorkflowCheckpoint(
            run_id="reconcile-saved-debtor",
            state=WorkflowState.ORDER_OPEN,
            source=source,
        )
        checkpoint.begin_action("save_debtor", company=source.debtor.company)
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="reconcile-saved-debtor",
                failed_step="save Debtor",
                reason="Save result was uncertain",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "reconcile-saved-debtor"
        )
        self.assertTrue(result.complete)
        self.assertEqual(searches, [source.debtor.company])
        self.assertEqual(gateway.save_debtor_calls, 0)

    def test_reconciled_debtor_stays_pending_when_order_readback_fails(self) -> None:
        source = sample_order()
        gateway = ScriptedGateway(debtors=[_debtor_candidate()])
        gateway.current_source = source
        gateway.order_ref = OrderEditorRef("order-token", "ORD-100")
        gateway.verify_order_debtor = lambda debtor: gateway.verified(False)
        store = WorkflowStore(self.root / "unverified-saved-debtor")
        checkpoint = WorkflowCheckpoint(
            run_id="unverified-saved-debtor",
            state=WorkflowState.ORDER_OPEN,
            source=source,
        )
        checkpoint.begin_action("save_debtor", company=source.debtor.company)
        checkpoint.wait_for_review(
            ReviewBundle(
                run_id="unverified-saved-debtor",
                failed_step="save Debtor",
                reason="Save result was uncertain",
            )
        )
        store.create(checkpoint)

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "unverified-saved-debtor"
        )
        self.assertTrue(result.waiting_for_review)
        self.assertEqual(result.checkpoint.resume_state, WorkflowState.ORDER_OPEN)
        self.assertEqual(result.checkpoint.pending_action.name, "save_debtor")
        self.assertEqual(gateway.save_debtor_calls, 0)
        self.assertEqual(gateway.save_order_calls, 0)
