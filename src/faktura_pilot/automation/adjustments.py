"""Apply and verify document-level adjustments using the visible totals controls."""
from decimal import Decimal
from typing import Any

from faktura_pilot.automation.models import ManualReviewRequired, VerificationResult
from faktura_pilot.automation.resolver import element_bounds, element_name, element_type


def controls(gateway: Any) -> tuple[Any, Any, Any, Any]:
    editor = gateway._active_order_editor_tab()
    elements = [e for e in editor.descendants() if gateway._visible_enabled(e)]
    discounts = [e for e in elements if element_type(e) == "Edit"
                 and element_name(e) == "Discount"]
    shipping = [e for e in elements if element_type(e) == "ComboBox"
                and element_name(e) == "Shipping"]
    if len(discounts) != 1 or len(shipping) != 1:
        raise ManualReviewRequired("document Discount/Shipping controls are not unique")
    bounds = element_bounds(shipping[0])
    amounts = [e for e in elements if element_type(e) == "Edit"
               and (b := element_bounds(e)) and bounds and b.left >= bounds.right
               and abs(b.center[1] - bounds.center[1]) <= bounds.height / 2]
    if len(amounts) != 1:
        raise ManualReviewRequired("the amount beside Shipping is not uniquely visible")
    return editor, discounts[0], shipping[0], amounts[0]


def verify(gateway: Any, source: Any) -> VerificationResult:
    from faktura_pilot.automation.windows import _parse_decimal

    _, discount, method, amount = controls(gateway)
    # Fakturama formats a reduction as a negative rebate.
    observed_discount = _parse_decimal(gateway._raw_element_value(discount) or "")
    observed_amount = _parse_decimal(gateway._raw_element_value(amount) or "")
    observed_name = gateway._element_value(method)
    expected_amount = source.shipping.net_amount if source.shipping else Decimal("0")
    expected_name = source.shipping.name if source.shipping else "Free of shipping costs"
    expected_discount = -source.order_discount_percent
    issues = []
    if observed_discount != expected_discount:
        issues.append("document discount differs from the source")
    if observed_amount != expected_amount:
        issues.append("document shipping amount differs from the source")
    if observed_name != expected_name:
        issues.append("document shipping method differs from the source")
    return VerificationResult(
        verified=not issues, observations=tuple(issues) or ("document adjustments verified",),
        expected={"discount": str(expected_discount), "shipping": str(expected_amount),
                  "shipping_name": expected_name},
        observed={"discount": str(observed_discount), "shipping": str(observed_amount),
                  "shipping_name": observed_name},
    )


def apply(gateway: Any, source: Any) -> VerificationResult:
    from faktura_pilot.automation.windows import _parse_decimal

    if source.shipping:
        from faktura_pilot.automation.shipping import ensure
        _, kind, number = gateway._document_editor_identity()
        ensure(gateway, source.shipping)
        if kind == "Order":
            ref = gateway.discover_open_order(source)
            if ref is None or ref.number != number:
                raise ManualReviewRequired(
                    "the original Order could not be restored after shipping"
                )
        else:
            gateway._activate_document_tab(kind, number)
    editor, discount, method, amount = controls(gateway)
    wanted_name = source.shipping.name if source.shipping else "Free of shipping costs"
    if (gateway._element_value(method) != wanted_name
            or _parse_decimal(gateway._raw_element_value(amount) or "") !=
            (source.shipping.net_amount if source.shipping else Decimal("0"))):
        handle = getattr(method, "handle", None)
        if handle:
            from pywinauto.controls.win32_controls import ComboBoxWrapper
            native = ComboBoxWrapper(handle)
            if native.item_texts().count(wanted_name) != 1:
                raise ManualReviewRequired(
                    "the shipping method is missing or ambiguous in the Order"
                )
            native.select(wanted_name)
            if native.selected_text() != wanted_name:
                raise ManualReviewRequired("the Order shipping selection did not persist")
        else:
            gateway._select_combo_by_current_value(
                set(), wanted_name, "Shipping", labels=("Shipping",)
            )
    # The shipping master supplies its amount. Verify it; do not detach it by
    # manually editing Fakturama's formatted shipping field.
    editor, discount, _, amount = controls(gateway)
    for field, value, label in (
        (discount, -source.order_discount_percent, "overall Discount"),
    ):
        current = gateway._raw_element_value(field)
        if _parse_decimal(current or "") != value:
            entry = gateway._numeric_entry_text(str(value), current)
            gateway._type_field_text(field, entry, label)
            gateway._focus_order_header_for_grid(editor)
    return verify(gateway, source)
