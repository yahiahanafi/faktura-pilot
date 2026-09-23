import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from faktura_pilot.automation.models import (
    DebtorCandidate,
    OrderEditorRef,
    PaymentMethodCandidate,
    ProductCandidate,
    VatCandidate,
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

    def run_workflow(
        self,
        gateway: ScriptedGateway,
        source=None,
        run_id: str = "case-1",
        store: WorkflowStore | None = None,
    ):
        source = source or sample_order()
        gateway.current_source = source
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
        self.assertEqual(result.invoice_number, "INV-200")
        self.assertEqual(gateway.create_debtor_calls, 0)
        self.assertEqual(gateway.create_product_calls, 0)
        self.assertEqual(gateway.create_vat_calls, 0)
        self.assertEqual(gateway.save_order_calls, 1)
        self.assertEqual(gateway.save_invoice_calls, 1)
        self.assertEqual(gateway.payment_sources[0].payment.status, PaymentStatus.PAID)

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

        result = WorkflowOrchestrator(FixedExtractor(source), gateway, store).resume(
            "payment-method-recovery"
        )

        self.assertTrue(result.complete)
        self.assertEqual(gateway.create_payment_method_calls, 0)
        self.assertEqual(gateway.save_debtor_calls, 1)

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
            (
                "fill_order_line",
                WorkflowState.DEBTOR_RESOLVED,
                {"item_index": 0, "sku": "CHR-ERG-01"},
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
