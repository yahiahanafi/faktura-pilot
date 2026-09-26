import unittest
from decimal import Decimal

from faktura_pilot.automation.models import (
    DebtorCandidate,
    PaymentMethodCandidate,
    ProductCandidate,
    VatCandidate,
)
from faktura_pilot.domain.policy import ResolutionAction
from faktura_pilot.workflow.policies import (
    conflicting_vat,
    payment_code,
    resolve_debtor,
    resolve_payment_method,
    resolve_product,
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

    def test_debtor_identity_conflict_requires_manual_review(self) -> None:
        debtor = sample_order().debtor
        conflict = DebtorCandidate(
            "conflict",
            debtor.company,
            debtor.first_name,
            debtor.last_name,
            "10000",
            "Berlin",
        )

        resolution = resolve_debtor(debtor, [conflict])

        self.assertEqual(resolution.action, ResolutionAction.MANUAL_REVIEW)
        self.assertIsNone(resolution.match)

        exact = DebtorCandidate(
            "exact",
            debtor.company,
            debtor.first_name,
            debtor.last_name,
            debtor.billing_address.zip_code,
            debtor.billing_address.city,
        )
        self.assertEqual(
            resolve_debtor(debtor, [exact, conflict]).action,
            ResolutionAction.MANUAL_REVIEW,
        )

    def test_product_policy_reuses_only_a_unique_exact_sku(self) -> None:
        candidate = ProductCandidate(
            "product-1", "CHR-ERG-01", "Ergonomic chair", Decimal("19")
        )

        self.assertEqual(
            resolve_product("CHR-ERG-01", [candidate]).action,
            ResolutionAction.REUSE,
        )
        self.assertEqual(
            resolve_product("CHR-ERG-01", [candidate, candidate]).action,
            ResolutionAction.MANUAL_REVIEW,
        )
        self.assertEqual(
            resolve_product("CHR-ERG-02", [candidate]).action,
            ResolutionAction.CREATE,
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
            ResolutionAction.MANUAL_REVIEW,
        )

    def test_payment_method_conflict_is_review_even_if_an_exact_row_also_exists(self) -> None:
        mapping = payment_code("Bank Transfer")
        exact = PaymentMethodCandidate("method-1", "Bank Transfer", mapping.value)
        conflict = PaymentMethodCandidate("method-2", "Bank Transfer", "SEPA Direct Debit")

        resolution = resolve_payment_method("Bank Transfer", mapping, [exact, conflict])

        self.assertEqual(resolution.action, ResolutionAction.MANUAL_REVIEW)
        self.assertIsNone(resolution.match)

    def test_vat_conflict_is_not_treated_as_missing(self) -> None:
        wrong = VatCandidate("vat-1", "VAT 19%", Decimal("7"), "S")

        self.assertEqual(resolve_vat(Decimal("19"), [wrong]).action, ResolutionAction.MANUAL_REVIEW)
        self.assertEqual(conflicting_vat(Decimal("19"), [wrong]), [wrong])
