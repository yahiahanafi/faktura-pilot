from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from faktura_pilot.domain.models import CENT, Item, OrderTotals


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def expected_line_net(
    quantity: Decimal,
    unit_net_price: Decimal,
    discount_percent: Decimal,
) -> Decimal:
    discount_factor = (Decimal("100") - discount_percent) / Decimal("100")
    return round_money(quantity * unit_net_price * discount_factor)


def expected_line_vat(line_net: Decimal, vat_rate_percent: Decimal) -> Decimal:
    return round_money(line_net * vat_rate_percent / Decimal("100"))


def product_gross_price(unit_net_price: Decimal, vat_rate_percent: Decimal) -> Decimal:
    return round_money(unit_net_price * (Decimal("1") + vat_rate_percent / Decimal("100")))


def calculate_order_totals(items: Iterable[Item]) -> OrderTotals:
    item_list = list(items)
    net = sum(
        (
            expected_line_net(item.quantity, item.unit_net_price, item.discount_percent)
            for item in item_list
        ),
        start=Decimal("0"),
    )
    vat = sum(
        (
            expected_line_vat(
                expected_line_net(item.quantity, item.unit_net_price, item.discount_percent),
                item.vat_rate_percent,
            )
            for item in item_list
        ),
        start=Decimal("0"),
    )
    net = round_money(net)
    vat = round_money(vat)
    return OrderTotals(net=net, vat=vat, gross=round_money(net + vat))
