import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from faktura_pilot.automation.models import DocumentRow, InvoiceEditorRef, VerificationResult
from faktura_pilot.workflow.orchestrator import WorkflowOrchestrator
from faktura_pilot.workflow.state import (
    InvoiceProvenance,
    ReviewBundle,
    WorkflowCheckpoint,
    WorkflowState,
)
from faktura_pilot.workflow.store import WorkflowStore
from tests.factories import sample_order
from tests.workflow_fakes import FixedExtractor, ScriptedGateway


class InvoiceRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temporary.name) / "runs")
        self.source = sample_order()
        self.gateway = ScriptedGateway()
        self.gateway.current_source = self.source
        self.gateway.documents.append(
            DocumentRow(
                "ORD-100", "Order", self.source.external_reference,
                self.source.totals.gross, "Open",
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def runner(self) -> WorkflowOrchestrator:
        return WorkflowOrchestrator(FixedExtractor(self.source), self.gateway, self.store)

    def test_creation_provenance_is_saved_before_copy_verification_failure(self) -> None:
        checkpoint = WorkflowCheckpoint(
            run_id="invoice-copy-fails",
            state=WorkflowState.ORDER_VERIFIED,
            source=self.source,
            order_number="ORD-100",
        )
        self.store.create(checkpoint)
        self.gateway.create_linked_invoice = Mock(
            return_value=InvoiceEditorRef(
                "invoice-token", "INV-200", "ORD-100", "25 Sept 2026", "25 Sept 2026"
            )
        )
        self.gateway.verify_invoice_copied_order = Mock(
            return_value=VerificationResult(False, observations=("copied amount differs",))
        )

        result = self.runner().resume(checkpoint.run_id)
        persisted = self.store.load(checkpoint.run_id)

        self.assertTrue(result.waiting_for_review)
        self.assertEqual(persisted.pending_action.name, "create_linked_invoice")
        self.assertEqual(persisted.invoice_provenance.source_order_number, "ORD-100")
        self.assertEqual(persisted.invoice_provenance.invoice_number, "INV-200")
        self.assertEqual(persisted.invoice_provenance.proposed_invoice_date, "25 Sept 2026")
        self.assertEqual(persisted.invoice_provenance.proposed_service_date, "25 Sept 2026")
        self.gateway.create_linked_invoice.assert_called_once_with("ORD-100")

    def _pending_creation(self, run_id: str) -> None:
        checkpoint = WorkflowCheckpoint(
            run_id=run_id,
            state=WorkflowState.ORDER_VERIFIED,
            source=self.source,
            order_number="ORD-100",
            invoice_provenance=InvoiceProvenance(
                source_order_number="ORD-100",
                invoice_number="INV-200",
                proposed_invoice_date="25 Sept 2026",
                proposed_service_date="25 Sept 2026",
                evidence_path="evidence/linked-invoice-created.png",
            ),
        )
        checkpoint.begin_action("create_linked_invoice", order_number="ORD-100")
        checkpoint.wait_for_review(
            ReviewBundle(run_id=run_id, failed_step="create linked Invoice", reason="interrupted")
        )
        self.store.create(checkpoint)

    def test_resume_restores_context_and_reconciles_matching_invoice(self) -> None:
        self._pending_creation("restore-invoice")
        self.gateway.invoice_ref = InvoiceEditorRef(
            "invoice-token", "INV-200", "ORD-100", "25 Sept 2026", "25 Sept 2026"
        )
        self.gateway.restore_invoice_context = Mock()
        self.gateway.verify_invoice_payment = Mock(return_value=VerificationResult(True))

        result = self.runner().resume("restore-invoice")

        self.assertTrue(result.complete)
        self.assertEqual(self.gateway.create_invoice_calls, 0)
        self.gateway.restore_invoice_context.assert_called_once()
        provenance, source = self.gateway.restore_invoice_context.call_args.args
        self.assertEqual(provenance.evidence_path, "evidence/linked-invoice-created.png")
        self.assertEqual(source, self.source)
        self.gateway.verify_invoice_payment.assert_called_once_with(self.source)

    def test_resume_rejects_changed_proposed_invoice_date(self) -> None:
        self._pending_creation("changed-date")
        self.gateway.invoice_ref = InvoiceEditorRef(
            "invoice-token", "INV-200", "ORD-100", "26 Sept 2026", "25 Sept 2026"
        )
        self.gateway.restore_invoice_context = Mock()

        result = self.runner().resume("changed-date")

        self.assertTrue(result.waiting_for_review)
        self.assertIn("proposed_invoice_date", result.checkpoint.review.reason)
        self.assertEqual(result.checkpoint.pending_action.name, "create_linked_invoice")
        self.assertEqual(self.gateway.create_invoice_calls, 0)


if __name__ == "__main__":
    unittest.main()
