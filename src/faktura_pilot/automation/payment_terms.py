"""Search-first payment-term preparation before opening a missing Debtor."""
from __future__ import annotations

from typing import Any

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    ManualReviewRequired,
    PaymentMethodCandidate,
)
from faktura_pilot.automation.native_table import verified_empty_selector
from faktura_pilot.automation.resolver import (
    ControlQuery,
    OCRMatch,
    TesseractOCR,
    element_bounds,
    element_name,
    element_type,
)

CODE_LABELS = ("Payment code", "!editorPaymentPaymentcode!", "Code", "Type")
ZERO_FIELDS = (("Cash discount",), ("Discount Days",), ("Net Days",))
BLANK_FIELDS = (("Account",), ("Text 'unpaid'",), ("Text 'deposit'",), ("Text 'paid'",))


def editor(gateway: Any) -> Any:
    matches = []
    for tab in gateway._current_window().descendants():
        if element_type(tab) != "Tab" or not gateway._visible_enabled(tab):
            continue
        labels = {element_name(e) for e in tab.descendants() if element_type(e) == "Text"}
        if {"Name", "Cash discount", "Net Days"}.issubset(labels):
            matches.append(tab)
    if len(matches) != 1:
        raise ManualReviewRequired("the payment-term editor is not uniquely visible")
    return matches[0]


def find(gateway: Any, query: str) -> list[PaymentMethodCandidate]:
    gateway._close_dialog_if_present(("Select the address",))
    gateway._open_data_manager("terms of payment")
    manager = gateway._data_manager_tab("terms of payment")
    if manager is None:
        raise ManualReviewRequired("the terms-of-payment manager is not visible")
    # This SWT filter drops an existing multiword name for a full-name query.
    # Search broadly, but reuse only a complete exact name from the result grid.
    search = query.split()[0]
    gateway._set_field(("Search",), search, window=manager)
    gateway._wait_stable_rows(window=manager)
    if not isinstance(gateway.ocr, TesseractOCR):
        raise ManualReviewRequired("payment terms require the visible table OCR reader")
    image = gateway._capture_window(manager)
    words = gateway.ocr.read_words(image)
    headers = []
    for label in ("Standard", "Name", "Description", "Discount"):
        found = [word.bounds for word in words if word.text == label]
        if len(found) != 1:
            raise ManualReviewRequired(f"payment terms have no unique {label} column")
        headers.append(found[0])
    if ([h.left for h in headers] != sorted(h.left for h in headers)
            or max(h.top for h in headers) - min(h.top for h in headers) > 30):
        raise ManualReviewRequired("payment-term table headers are not aligned")
    if verified_empty_selector(manager, image, headers, search_verified=(
        gateway._read_from_window(manager, ("Search",), allow_ocr=False) == search
    ), allow_grid_lines=True):
        return []
    # Selected SWT rows expose vertical grid borders as standalone OCR words.
    # Exclude only those borders, preserving all actual name characters.
    names = [w for w in words if w.bounds.top > max(h.bottom for h in headers)
             and headers[1].left - 10 <= w.bounds.left < headers[2].left - 10
             and w.text.strip() not in {"|", "¦"}]
    groups: list[list[OCRMatch]] = []
    for word in sorted(names, key=lambda w: (w.bounds.center[1], w.bounds.left)):
        group = next((g for g in groups
                      if abs(g[0].bounds.center[1] - word.bounds.center[1]) <= 14), None)
        if group is None:
            groups.append([word])
        else:
            group.append(word)
    if not groups:
        raise ManualReviewRequired("payment-term results are unreadable, not proven empty")
    matches = []
    for group in groups:
        name = " ".join(w.text for w in sorted(group, key=lambda w: w.bounds.left))
        if "…" in name or "..." in name:
            raise ManualReviewRequired("a payment-term name is truncated")
        if name.casefold() == query.casefold():
            matches.append((name, group[0].bounds.center))
    if not matches:
        raise ManualReviewRequired(
            "payment-term search has nonempty results but no readable exact name; "
            "absence cannot be established safely"
        )
    if len(matches) != 1:
        return [PaymentMethodCandidate(token=gateway._token({"name": name}), name=name)
                for name, _ in matches]
    name, point = matches[0]
    from pywinauto.mouse import double_click
    bounds = element_bounds(manager)
    if bounds is None:
        raise ManualReviewRequired("payment-term manager bounds are unavailable")
    double_click(coords=(bounds.left + round(point[0] * bounds.width / image.width),
                         bounds.top + round(point[1] * bounds.height / image.height)))
    def selected() -> bool:
        try:
            return gateway._read_optional(("Name",), scope=editor(gateway)) == name
        except ManualReviewRequired:
            return False
    gateway._wait_until("the selected payment-term definition", selected)
    scope = editor(gateway)
    control = gateway._resolver(scope).resolve(
        ControlQuery.one_of(*CODE_LABELS, control_types=("ComboBox",), allow_ocr=False)
    )
    code = gateway._element_value(control.element)
    if not code:
        raise ManualReviewRequired("the existing payment-term code is unreadable")
    return [PaymentMethodCandidate(token=gateway._token({"name": name, "code": code}),
                                   name=name, code=code)]


def create(gateway: Any, name: str, code: str) -> PaymentMethodCandidate:
    existing = find(gateway, name)
    if existing:
        if len(existing) == 1 and existing[0].code == code:
            return existing[0]
        raise ManualReviewRequired(
            "payment terms already exist with duplicate/conflicting settings"
        )
    try:
        scope = editor(gateway)
    except ManualReviewRequired:
        scope = None
    if scope is not None and element_name(scope).startswith("*"):
        if gateway._read_optional(("Name",), scope=scope) != name:
            raise ManualReviewRequired("an unrelated unsaved payment term is open")
    else:
        manager = gateway._data_manager_tab("terms of payment")
        control = gateway._resolver(manager).resolve(ControlQuery.one_of(
            "Create a new term of payment", control_types=("Button",), allow_ocr=False
        ))
        gateway._click_control(gateway._current_window(), control)
        def ready() -> bool:
            try:
                return bool(editor(gateway))
            except ManualReviewRequired:
                return False
        gateway._wait_until("the new payment-term editor", ready)
        scope = editor(gateway)
    for label in ("Name", "Description"):
        gateway._set_field((label,), name, scope=scope)
    gateway._select_named_option(CODE_LABELS, code, optional=False)
    for labels in ZERO_FIELDS:
        gateway._set_field(labels, "0", scope=scope)
    for labels in BLANK_FIELDS:
        control = gateway._resolver(scope).resolve(ControlQuery.one_of(
            *labels, control_types=("Edit",), allow_ocr=False
        )).element
        if gateway._raw_element_value(control) != "":
            gateway._write_element(control, "")
        if gateway._raw_element_value(control) != "":
            raise ManualReviewRequired(f"{labels[0]} did not read back as blank")
    # Do not touch the Set as standard control.
    gateway._dispatch_save("save payment terms")
    try:
        saved = find(gateway, name)
        if len(saved) != 1 or saved[0].code != code:
            raise ManualReviewRequired("saved payment terms are not uniquely visible")
        scope = editor(gateway)
        for labels in ZERO_FIELDS:
            from faktura_pilot.automation.windows import _parse_decimal
            if _parse_decimal(gateway._read_optional(labels, scope=scope) or "") != 0:
                raise ManualReviewRequired(f"saved {labels[0]} is not zero")
        for labels in BLANK_FIELDS:
            if gateway._read_optional(labels, scope=scope, control_types=("Edit",)) != "":
                raise ManualReviewRequired(f"saved {labels[0]} is not blank")
        if gateway._read_optional(("Description",), scope=scope) != name:
            raise ManualReviewRequired("saved payment-term Description differs")
        if element_name(scope).startswith("*"):
            raise ManualReviewRequired("payment terms still have unsaved changes")
        return saved[0]
    except Exception as exc:
        raise ActionOutcomeUnknown(f"payment-term Save was activated; {exc}") from exc
