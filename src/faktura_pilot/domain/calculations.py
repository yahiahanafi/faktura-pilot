from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from faktura_pilot.domain.models import CENT, Item, OrderTotals, Shipping


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


def calculate_order_totals(
    items: Iterable[Item],
    order_discount_percent: Decimal = Decimal("0"),
    shipping: Shipping | None = None,
) -> OrderTotals:
    # Fakturama applies the order rebate to goods, then adds shipping. It groups
    # goods by VAT rate. Fixed shipping is stored as a rounded gross price;
    # Fakturama derives its unrounded net from that price before summing VAT.
    net_by_vat_rate: dict[Decimal, Decimal] = {}
    legacy_vat = Decimal("0")
    for item in items:
        line_net = expected_line_net(item.quantity, item.unit_net_price, item.discount_percent)
        net_by_vat_rate[item.vat_rate_percent] = (
            net_by_vat_rate.get(item.vat_rate_percent, Decimal("0")) + line_net
        )
        legacy_vat += expected_line_vat(line_net, item.vat_rate_percent)

    if order_discount_percent == Decimal("0") and shipping is None:
        net = round_money(sum(net_by_vat_rate.values(), Decimal("0")))
        vat = round_money(legacy_vat)
        return OrderTotals(net=net, vat=vat, gross=round_money(net + vat))

    discount_factor = (Decimal("100") - order_discount_percent) / Decimal("100")
    discounted_net_by_vat_rate = {
        rate: amount * discount_factor for rate, amount in net_by_vat_rate.items()
    }
    net = sum(discounted_net_by_vat_rate.values(), Decimal("0"))
    vat = sum((amount * rate / Decimal("100")
               for rate, amount in discounted_net_by_vat_rate.items()), Decimal("0"))
    if shipping is not None:
        shipping_gross = product_gross_price(shipping.net_amount, shipping.vat_rate_percent)
        shipping_net = shipping_gross / (Decimal("1") + shipping.vat_rate_percent / Decimal("100"))
        net += shipping_net
        vat += shipping_gross - shipping_net
    return OrderTotals(net=round_money(net), vat=round_money(vat), gross=round_money(net + vat))
