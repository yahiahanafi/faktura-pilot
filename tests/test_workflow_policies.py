import unittest
from decimal import Decimal

from faktura_pilot.automation.models import DebtorCandidate, PaymentMethodCandidate, VatCandidate
from faktura_pilot.domain.policy import ResolutionAction
from faktura_pilot.workflow.policies import (
    conflicting_vat,
    payment_code,
    resolve_debtor,
    resolve_payment_method,
    resolve_vat,
)
from tests.factories import sample_order


class WorkflowPolicyTests(unittest.TestCase):
    def test_debtor_policy_requires_one_exact_identity(self) -> None:
        debtor = sample_order().debtor
        candidate = DebtorCandidate(
            "one", debtor.company.upper(), "Mira", "Weber", "20457", "Hamburg"
        )

        self.assertEqual(resolve_debtor(debtor, [candidate]).action, ResolutionAction.REUSE)
        self.assertEqual(
            resolve_debtor(debtor, [candidate, candidate]).action,
            ResolutionAction.MANUAL_REVIEW,
        )

    def test_payment_method_matches_source_name_and_mapped_definition_code(self) -> None:
        mapping = payment_code("Bank Transfer")
        visible = PaymentMethodCandidate("method-1", "Bank Transfer", mapping.value)

        resolution = resolve_payment_method("Bank Transfer", mapping, [visible])
        self.assertEqual(resolution.action, ResolutionAction.REUSE)
        self.assertEqual(resolution.match, visible)

        wrong_code = PaymentMethodCandidate("method-2", "Bank Transfer", "SEPA Direct Debit")
        self.assertEqual(
            resolve_payment_method("Bank Transfer", mapping, [wrong_code]).action,
            ResolutionAction.CREATE,
        )

    def test_vat_conflict_is_not_treated_as_missing(self) -> None:
        wrong = VatCandidate("vat-1", "VAT 19%", Decimal("7"), "S")

        self.assertEqual(resolve_vat(Decimal("19"), [wrong]).action, ResolutionAction.CREATE)
        self.assertEqual(conflicting_vat(Decimal("19"), [wrong]), [wrong])
