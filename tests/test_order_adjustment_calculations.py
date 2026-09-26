import unittest
from decimal import Decimal

from pydantic import ValidationError

from faktura_pilot.domain.calculations import calculate_order_totals
from faktura_pilot.domain.models import OrderSource, Shipping
from tests.factories import sample_order


class OrderAdjustmentCalculationTests(unittest.TestCase):
    def test_discount_applies_before_shipping_and_vat_rounds_by_rate(self) -> None:
        order = sample_order()
        shipping = Shipping(name="Delivery", net_amount="12.50", vat_rate_percent="19")

        totals = calculate_order_totals(order.items, Decimal("5"), shipping)

        self.assertEqual(totals.net, Decimal("554.00"))
        self.assertEqual(totals.vat, Decimal("105.26"))
        self.assertEqual(totals.gross, Decimal("659.27"))

        source = order.model_dump(mode="python")
        source["order_discount_percent"] = "5"
        source["shipping"] = shipping.model_dump(mode="python")
        source["totals"] = totals.model_dump(mode="python")
        self.assertEqual(OrderSource.model_validate(source).totals, totals)

    def test_mixed_vat_rates_keep_discount_in_each_goods_bucket(self) -> None:
        order = sample_order()
        items = [
            order.items[0].model_copy(update={
                "quantity": Decimal("1"),
                "unit_net_price": Decimal("100"),
                "vat_rate_percent": Decimal("19"),
                "discount_percent": Decimal("0"),
            }),
            order.items[1].model_copy(update={
                "quantity": Decimal("1"),
                "unit_net_price": Decimal("100"),
                "vat_rate_percent": Decimal("7"),
                "discount_percent": Decimal("0"),
            }),
        ]
        shipping = Shipping(name="Delivery", net_amount="10", vat_rate_percent="19")

        totals = calculate_order_totals(items, Decimal("10"), shipping)

        self.assertEqual(totals.net, Decimal("190.00"))
        self.assertEqual(totals.vat, Decimal("25.30"))
        self.assertEqual(totals.gross, Decimal("215.30"))

    def test_missing_adjustments_preserve_existing_totals(self) -> None:
        order = sample_order()
        self.assertEqual(order.order_discount_percent, Decimal("0"))
        self.assertIsNone(order.shipping)
        self.assertEqual(calculate_order_totals(order.items), order.totals)

    def test_no_adjustment_keeps_per_line_vat_rounding(self) -> None:
        item = sample_order().items[0].model_copy(update={
            "quantity": Decimal("1"),
            "unit_net_price": Decimal("0.03"),
            "vat_rate_percent": Decimal("19"),
            "discount_percent": Decimal("0"),
        })
        totals = calculate_order_totals([item, item, item])
        self.assertEqual(totals.net, Decimal("0.09"))
        self.assertEqual(totals.vat, Decimal("0.03"))

    def test_rejects_invalid_order_discount_and_shipping(self) -> None:
        source = sample_order().model_dump(mode="python")
        for discount in ("-0.01", "100.01", "NaN", True):
            with self.subTest(discount=discount), self.assertRaises(ValidationError):
                OrderSource.model_validate({**source, "order_discount_percent": discount})

        for shipping in (
            {"name": "", "net_amount": "10", "vat_rate_percent": "19"},
            {"name": "Delivery", "net_amount": "-0.01", "vat_rate_percent": "19"},
            {"name": "Delivery", "net_amount": "10", "vat_rate_percent": "101"},
        ):
            with self.subTest(shipping=shipping), self.assertRaises(ValidationError):
                OrderSource.model_validate({**source, "shipping": shipping})


    def test_discount_only_shipping_only_and_full_discount(self) -> None:
        items = sample_order().items
        shipping = Shipping(name="Shipping", net_amount="12.50", vat_rate_percent="19")
        cases = [
            (Decimal("5"), None, ("541.50", "102.89", "644.39")),
            (Decimal("0"), shipping, ("582.50", "110.68", "693.18")),
            (Decimal("100"), shipping, ("12.50", "2.38", "14.88")),
        ]
        for discount, charge, expected in cases:
            with self.subTest(discount=discount, shipping=charge):
                totals = calculate_order_totals(items, discount, charge)
                self.assertEqual((totals.net, totals.vat, totals.gross),
                                 tuple(Decimal(value) for value in expected))


if __name__ == "__main__":
    unittest.main()
