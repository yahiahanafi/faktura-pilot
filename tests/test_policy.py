import unittest
from decimal import Decimal

from faktura_pilot.domain.policy import (
    DebtorIdentity,
    PaymentCode,
    ResolutionAction,
    VatIdentity,
    debtor_matches,
    expected_vat_name,
    normalize_text,
    payment_code_for_method,
    resolve_exact_candidates,
    same_address,
    sku_matches,
    vat_matches,
)
from tests.factories import sample_order


class PolicyTests(unittest.TestCase):
    def test_human_text_normalization_is_exact_after_unicode_and_space_normalization(self) -> None:
        self.assertEqual(normalize_text("  Northstar\u00a0Office   GmbH "), "northstar office gmbh")
        debtor = sample_order().debtor
        candidate = DebtorIdentity(
            company=" NORTHSTAR OFFICE GMBH ",
            first_name="mira",
            last_name="WEBER",
            zip_code="20457",
            city="HAMBURG",
        )
        self.assertTrue(debtor_matches(debtor, candidate))

        candidate = DebtorIdentity(
            "Northstar Office GmbH Trading", "Mira", "Weber", "20457", "Hamburg"
        )
        self.assertFalse(debtor_matches(debtor, candidate))

    def test_sku_comparison_trims_but_preserves_case(self) -> None:
        self.assertTrue(sku_matches(" CHR-ERG-01 ", "CHR-ERG-01"))
        self.assertFalse(sku_matches("CHR-ERG-01", "chr-erg-01"))

    def test_candidate_resolution_reuses_creates_or_pauses_on_ambiguity(self) -> None:
        def is_target(value: str) -> bool:
            return value == "target"

        one = resolve_exact_candidates(["other", "target"], is_target)
        none = resolve_exact_candidates(["other"], is_target)
        many = resolve_exact_candidates(["target", "target"], is_target)

        self.assertEqual(one.action, ResolutionAction.REUSE)
        self.assertEqual(one.match, "target")
        self.assertEqual(none.action, ResolutionAction.CREATE)
        self.assertEqual(many.action, ResolutionAction.MANUAL_REVIEW)
        self.assertEqual(many.exact_match_count, 2)

    def test_vat_match_requires_exact_name_value_and_standard_code(self) -> None:
        self.assertEqual(expected_vat_name(Decimal("19.00")), "VAT 19%")
        correct = VatIdentity("VAT 19%", Decimal("19"), "S")
        wrong_value = VatIdentity("VAT 19%", Decimal("7"), "S")
        wrong_code = VatIdentity("VAT 19%", Decimal("19"), "R")

        self.assertTrue(vat_matches(Decimal("19"), correct))
        self.assertFalse(vat_matches(Decimal("19"), wrong_value))
        self.assertFalse(vat_matches(Decimal("19"), wrong_code))

    def test_payment_method_mapping_is_explicit(self) -> None:
        self.assertEqual(payment_code_for_method("Bank Transfer"), PaymentCode.CREDIT_TRANSFER)
        self.assertEqual(payment_code_for_method("credit card"), PaymentCode.CREDIT_CARD)
        self.assertEqual(
            payment_code_for_method("SEPA Direct Debit"), PaymentCode.SEPA_DIRECT_DEBIT
        )
        self.assertIsNone(payment_code_for_method("Cash"))

    def test_address_equality_includes_optional_components(self) -> None:
        debtor = sample_order().debtor
        self.assertFalse(same_address(debtor.billing_address, debtor.delivery_address))
        self.assertTrue(same_address(debtor.billing_address, debtor.billing_address.model_copy()))


if __name__ == "__main__":
    unittest.main()
