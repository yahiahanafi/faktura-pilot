"""Search-first preparation of a fixed-VAT shipping method."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from faktura_pilot.automation.models import ActionOutcomeUnknown, ManualReviewRequired
from faktura_pilot.automation.native_table import verified_empty_selector
from faktura_pilot.automation.resolver import (
    ControlQuery,
    OCRMatch,
    TesseractOCR,
    element_bounds,
    element_name,
    element_type,
)
from faktura_pilot.automation.windows import _parse_decimal


def _editor_matches(gateway: Any) -> list[Any]:
    matches = []
    for tab in gateway._current_window().descendants():
        if element_type(tab) != "Tab" or not gateway._visible_enabled(tab):
            continue
        labels = {element_name(child) for child in tab.descendants()
                  if element_type(child) in {"Text", "Edit", "ComboBox"}}
        if {"Name", "Description", "Gross", "VAT Calculation"}.issubset(labels):
            matches.append(tab)
    return matches


def editor(gateway: Any) -> Any:
    matches = _editor_matches(gateway)
    if len(matches) != 1:
        raise ManualReviewRequired("the shipping editor is not uniquely visible")
    return matches[0]

def _find_exact(gateway: Any, name: str) -> bool:
    """Open one exact row, or prove an empty result."""
    gateway._open_data_manager("Shippings")
    manager = gateway._data_manager_tab("Shippings")
    if manager is None:
        raise ManualReviewRequired("the Shippings manager is not visible")
    search = name.split()[0]
    gateway._set_field(("Search",), search, window=manager)
    gateway._wait_stable_rows(window=manager)
    if not isinstance(gateway.ocr, TesseractOCR):
        raise ManualReviewRequired("shipping search requires the visible table OCR reader")
    image = gateway._capture_window(manager)
    words = gateway.ocr.read_words(image, preprocess=False)
    # A search such as "Standard" also appears above the Standard column.
    # Use the neighbouring Value header to restrict all labels to the table row.
    value_headers = [word for word in words if word.text == "Value"]
    if len(value_headers) != 1:
        raise ManualReviewRequired("Shippings has no unique Value column")
    header_y = value_headers[0].bounds.center[1]
    headers = []
    for label in ("Standard", "Name", "Description", "Value"):
        found = [word.bounds for word in words if word.text == label
                 and abs(word.bounds.center[1] - header_y) <= 20]
        if len(found) != 1:
            raise ManualReviewRequired(f"Shippings has no unique {label} column")
        headers.append(found[0])
    if ([box.left for box in headers] != sorted(box.left for box in headers)
            or max(box.top for box in headers) - min(box.top for box in headers) > 30):
        raise ManualReviewRequired("Shippings table headers are not aligned")
    if verified_empty_selector(
        manager, image, headers,
        search_verified=gateway._read_from_window(manager, ("Search",), allow_ocr=False)
        == search,
        allow_grid_lines=True,
    ):
        return False
    names = [word for word in words
             if word.bounds.top > max(header.bottom for header in headers)
             and headers[1].left - 10 <= word.bounds.left < headers[2].left - 10
             and word.text.strip() not in {"|", "¦"}]
    groups: list[list[OCRMatch]] = []
    for word in sorted(names, key=lambda item: (item.bounds.center[1], item.bounds.left)):
        group = next((row for row in groups
                      if abs(row[0].bounds.center[1] - word.bounds.center[1]) <= 14), None)
        if group is None:
            groups.append([word])
        else:
            group.append(word)
    if not groups:
        raise ManualReviewRequired("Shippings results are unreadable, not proven empty")
    exact = []
    for group in groups:
        row_name = " ".join(word.text for word in sorted(group, key=lambda w: w.bounds.left))
        truncated = "…" in row_name or "..." in row_name
        prefix = row_name.split("…", 1)[0].split("...", 1)[0].rstrip()
        if (row_name.casefold() == name.casefold()
                or (truncated and prefix and name.casefold().startswith(prefix.casefold()))):
            # A clipped label is only a locator. The opened editor must expose
            # the full exact Name before this method can report a match.
            exact.append(group[0].bounds.center)
    if len(exact) != 1:
        raise ManualReviewRequired(
            "shipping search has nonempty or duplicate results; exact absence/identity "
            "cannot be established safely"
        )
    bounds = element_bounds(manager)
    if bounds is None:
        raise ManualReviewRequired("Shippings manager bounds are unavailable")
    from pywinauto.mouse import double_click

    point = exact[0]
    double_click(coords=(bounds.left + round(point[0] * bounds.width / image.width),
                         bounds.top + round(point[1] * bounds.height / image.height)))
    gateway._wait_until("the selected shipping definition",
                        lambda: _editor_name(gateway) == name)
    return True


def _editor_name(gateway: Any) -> str | None:
    try:
        return gateway._read_optional(("Name",), scope=editor(gateway))
    except ManualReviewRequired:
        return None


def _combo(gateway: Any, scope: Any, label: str) -> Any:
    return gateway._resolver(scope).resolve(ControlQuery.one_of(
        label, control_types=("ComboBox",), allow_ocr=False,
    )).element


def _combo_value(gateway: Any, scope: Any, label: str) -> str:
    value = gateway._element_value(_combo(gateway, scope, label))
    if not value:
        raise ManualReviewRequired(f"shipping {label} cannot be read")
    return value


def _select_combo(gateway: Any, scope: Any, label: str, option: str) -> None:
    control = _combo(gateway, scope, label)
    if gateway._element_value(control) != option:
        for method_name in ("select", "select_by_text"):
            method = getattr(control, method_name, None)
            if callable(method):
                try:
                    method(option)
                    break
                except Exception:
                    continue
        else:
            raise ManualReviewRequired(f"shipping {label} cannot select {option!r}")
    if gateway._element_value(control) != option:
        raise ManualReviewRequired(f"shipping {label} did not read back as {option!r}")


def _gross(shipping: Any) -> Decimal:
    return (shipping.net_amount * (Decimal("1") + shipping.vat_rate_percent / 100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def _verify(gateway: Any, scope: Any, shipping: Any, vat_name: str, *,
            require_gross: bool = True) -> None:
    expected_gross = _gross(shipping)
    for label in ("Name", "Description"):
        observed = gateway._read_optional((label,), scope=scope)
        if observed != shipping.name:
            raise ManualReviewRequired(
                f"shipping {label} differs: expected {shipping.name!r}, observed {observed!r}"
            )
    if require_gross:
        observed_gross = _parse_decimal(gateway._read_optional(("Gross",), scope=scope) or "")
        if observed_gross != expected_gross:
            raise ManualReviewRequired(
                f"shipping Gross differs: expected {expected_gross}, observed {observed_gross}"
            )
    if _combo_value(gateway, scope, "VAT Calculation") != "Constant VAT":
        raise ManualReviewRequired("shipping VAT Calculation is not Constant VAT")
    if _combo_value(gateway, scope, "VAT") != vat_name:
        raise ManualReviewRequired("shipping VAT selection differs")


def _blank_draft(gateway: Any, scope: Any) -> bool:
    values = {}
    for label in ("Name", "Description", "Gross"):
        control = gateway._resolver(scope).resolve(ControlQuery.one_of(
            label, control_types=("Edit",), allow_ocr=False,
        )).element
        values[label] = gateway._raw_element_value(control)
    return (values["Name"] == "" and values["Description"] == ""
            and (values["Gross"] == "" or _parse_decimal(values["Gross"] or "") == 0))

def _set_gross(gateway: Any, scope: Any, amount: Decimal) -> None:
    control = gateway._resolver(scope).resolve(ControlQuery.one_of(
        "Gross", control_types=("Edit",), allow_ocr=False,
    )).element
    value = format(amount, ".2f")
    entry = gateway._numeric_entry_text(value, gateway._raw_element_value(control))
    gateway._type_field_text(control, entry, "Gross")
    if _parse_decimal(gateway._raw_element_value(control) or "") != amount:
        raise ManualReviewRequired("shipping Gross did not read back after native entry")


def ensure(gateway: Any, shipping: Any) -> None:
    """Reuse an exact fixed-VAT method or create and verify it once."""
    if not shipping.name.strip():
        raise ManualReviewRequired("shipping name is blank")
    vat_name = f"VAT {format(shipping.vat_rate_percent.normalize(), 'f')}%"
    vats = gateway.find_vats(shipping.vat_rate_percent)
    exact_vats = [vat for vat in vats if vat.name == vat_name]
    if exact_vats:
        if (len(exact_vats) != 1 or exact_vats[0].value_percent != shipping.vat_rate_percent
                or exact_vats[0].e_invoice_code != "S"):
            raise ManualReviewRequired("shipping VAT has duplicate or conflicting settings")
    else:
        vat = gateway.create_vat(shipping.vat_rate_percent)
        if (vat.name != vat_name or vat.value_percent != shipping.vat_rate_percent
                or vat.e_invoice_code != "S"):
            raise ManualReviewRequired("created shipping VAT could not be verified")
    if _find_exact(gateway, shipping.name):
        scope = editor(gateway)
        if element_name(scope).startswith("*"):
            raise ManualReviewRequired("the matching shipping method has unsaved changes")
        _verify(gateway, scope, shipping, vat_name)
        return
    matches = _editor_matches(gateway)
    if len(matches) > 1:
        raise ManualReviewRequired("multiple shipping editors are visible")
    if matches:
        scope = matches[0]
        if not _blank_draft(gateway, scope):
            raise ManualReviewRequired("an unrelated shipping editor is open")
    else:
        manager = gateway._data_manager_tab("Shippings")
        control = gateway._resolver(manager).resolve(ControlQuery.one_of(
            "Create a new shipping method", control_types=("Button",), allow_ocr=False,
        ))
        gateway._click_control(gateway._current_window(), control)
        gateway._wait_until("the new shipping editor", lambda: bool(editor(gateway)))
        scope = editor(gateway)
        if not _blank_draft(gateway, scope):
            raise ManualReviewRequired("new shipping editor is not blank")
    gateway._set_field(("Name",), shipping.name, scope=scope)
    gateway._set_field(("Description",), shipping.name, scope=scope)
    _select_combo(gateway, scope, "VAT Calculation", "Constant VAT")
    _select_combo(gateway, scope, "VAT", vat_name)
    _set_gross(gateway, scope, _gross(shipping))
    _verify(gateway, scope, shipping, vat_name)
    gateway._dispatch_save("save shipping method")
    try:
        if not _find_exact(gateway, shipping.name):
            raise ManualReviewRequired("saved shipping method is not visible")
        scope = editor(gateway)
        _verify(gateway, scope, shipping, vat_name)
        if element_name(scope).startswith("*"):
            raise ManualReviewRequired("shipping method still has unsaved changes")
    except Exception as exc:
        raise ActionOutcomeUnknown(f"shipping Save was activated; {exc}") from exc

