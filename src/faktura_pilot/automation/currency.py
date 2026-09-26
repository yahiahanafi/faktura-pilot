from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from faktura_pilot.automation.models import ManualReviewRequired
from faktura_pilot.automation.resolver import ControlQuery

if TYPE_CHECKING:
    from faktura_pilot.automation.windows import WindowsFakturamaGateway

_LOGGER = logging.getLogger("faktura_pilot.automation")
# Exact English country labels exposed by Fakturama's Currency locale combo.
# A locale controls formatting as well as the currency; never guess an unknown code.
CURRENCY_LOCALES = {
    "EUR": "Germany",
    "GBP": "United Kingdom",
    "USD": "United States",
    "CHF": "Switzerland",
    "CAD": "Canada",
    "AUD": "Australia",
    "JPY": "Japan",
}


def ensure_currency(
    gateway: WindowsFakturamaGateway, currency: str, *, allow_change: bool
) -> None:
    """Apply currency before creating an Order; never convert an existing draft."""
    country = CURRENCY_LOCALES.get(currency.upper())
    if country is None:
        raise ManualReviewRequired(
            f"currency {currency!r} has no verified Fakturama Currency locale mapping"
        )
    _LOGGER.info("Checking Fakturama currency locale for %s", currency)
    gateway._click(ControlQuery.one_of("File", control_types=("MenuItem",), allow_ocr=False))
    gateway._click(ControlQuery.one_of("Preferences", control_types=("MenuItem",), allow_ocr=False))
    gateway._wait_until(
        "Preferences to open",
        lambda: gateway._visible_dialog_root(("Preferences",), required=False) is not None,
    )
    dialog = gateway._visible_dialog_root(("Preferences",))
    general = gateway._resolver(dialog).resolve(
        ControlQuery.one_of("General", control_types=("TreeItem",), allow_ocr=False)
    )
    gateway._click_control(dialog, general)
    control = gateway._resolver(dialog).resolve(
        ControlQuery.one_of("Currency locale", control_types=("ComboBox",), allow_ocr=False)
    ).element
    from pywinauto.controls.win32_controls import ComboBoxWrapper

    combo = ComboBoxWrapper(control.handle)
    options = combo.item_texts()
    current = combo.selected_text()
    if country not in options:
        gateway._click_if_present(("Cancel",), control_types=("Button",), window=dialog)
        raise ManualReviewRequired(f"Currency locale {country!r} is unavailable in Fakturama")
    if current != country and not allow_change:
        gateway._click_if_present(("Cancel",), control_types=("Button",), window=dialog)
        raise ManualReviewRequired(
            f"existing run requires {currency} ({country}), but Currency locale is {current!r}; "
            "changing currency on an existing draft can mix amounts. Start a fresh Order "
            "after correcting the currency."
        )
    # Selecting and applying even the existing locale initializes Fakturama's
    # formatter before an Order constructor captures its zero/deposit currency.
    combo.select(country)
    if combo.selected_text() != country:
        raise ManualReviewRequired(f"Currency locale did not read back as {country!r}")
    gateway._click_if_present(("Apply and Close",), control_types=("Button",), window=dialog)
    gateway._wait_until(
        "currency Preferences to close",
        lambda: gateway._visible_dialog_root(("Preferences",), required=False) is None,
    )
    _LOGGER.info("Currency locale verified: %s (%s)", currency, country)
