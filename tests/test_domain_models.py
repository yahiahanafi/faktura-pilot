import unittest
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from faktura_pilot.domain.calculations import (
    calculate_order_totals,
    expected_line_net,
    product_gross_price,
)
from faktura_pilot.domain.models import OrderSource, PaymentStatus
from tests.factories import sample_order


class DomainModelTests(unittest.TestCase):
    def test_canonical_order_parses_required_fields_and_decimals(self) -> None:
        order = sample_order()

        self.assertEqual(order.order_date, date(2026, 7, 14))
        self.assertEqual(order.currency, "EUR")
        self.assertEqual(order.source_customer_id, "CUST-1007")
        self.assertEqual(order.debtor.billing_address.additional_name, "Northstar Office GmbH")
        self.assertEqual(
            order.debtor.delivery_address.additional_name,
            "Northstar Office Warehouse",
        )
        self.assertEqual(order.payment.status, PaymentStatus.PAID)
        self.assertEqual(order.items[0].unit_net_price, Decimal("250.00"))
        self.assertEqual(order.totals.gross, Decimal("678.30"))

    def test_paid_order_requires_payment_date(self) -> None:
        order_data = sample_order().model_dump(mode="python")
        order_data["payment"]["payment_date"] = None

        with self.assertRaisesRegex(ValidationError, "paid order requires"):
            OrderSource.model_validate(order_data)

    def test_unpaid_order_cannot_have_payment_date(self) -> None:
        order_data = sample_order().model_dump(mode="python")
        order_data["payment"]["status"] = "UNPAID"

        with self.assertRaisesRegex(ValidationError, "unpaid order must not"):
            OrderSource.model_validate(order_data)

    def test_rejects_empty_items_nonpositive_quantity_and_invalid_vat(self) -> None:
        order_data = sample_order().model_dump(mode="python")
        order_data["items"] = []
        with self.assertRaises(ValidationError):
            OrderSource.model_validate(order_data)

        order_data = sample_order().model_dump(mode="python")
        order_data["items"][0]["quantity"] = "0"
        with self.assertRaisesRegex(ValidationError, "quantity must be greater"):
            OrderSource.model_validate(order_data)

        order_data = sample_order().model_dump(mode="python")
        order_data["items"][0]["vat_rate_percent"] = "101"
        with self.assertRaisesRegex(ValidationError, "VAT percentage"):
            OrderSource.model_validate(order_data)

    def test_rejects_inconsistent_line_and_order_totals(self) -> None:
        order_data = sample_order().model_dump(mode="python")
        order_data["items"][0]["source_line_net_total"] = "451.00"
        with self.assertRaisesRegex(ValidationError, "source line net total"):
            OrderSource.model_validate(order_data)

        order_data = sample_order().model_dump(mode="python")
        order_data["totals"]["gross"] = "700.00"
        with self.assertRaisesRegex(ValidationError, "source gross total"):
            OrderSource.model_validate(order_data)

    def test_rejects_invalid_metadata_and_extra_fields(self) -> None:
        order_data = sample_order().model_dump(mode="python")
        order_data["extraction"]["image_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            OrderSource.model_validate(order_data)

        order_data = sample_order().model_dump(mode="python")
        order_data["unexpected"] = "not allowed"
        with self.assertRaises(ValidationError):
            OrderSource.model_validate(order_data)

    def test_line_and_vat_math_use_decimal_half_up_rounding(self) -> None:
        self.assertEqual(
            expected_line_net(Decimal("2"), Decimal("250"), Decimal("10")),
            Decimal("450.00"),
        )
        self.assertEqual(product_gross_price(Decimal("250"), Decimal("19")), Decimal("297.50"))
        self.assertEqual(product_gross_price(Decimal("1.005"), Decimal("0")), Decimal("1.01"))

        totals = calculate_order_totals(sample_order().items)
        self.assertEqual(totals.net, Decimal("570.00"))
        self.assertEqual(totals.vat, Decimal("108.30"))
        self.assertEqual(totals.gross, Decimal("678.30"))


if __name__ == "__main__":
    unittest.main()
