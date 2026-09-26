from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from faktura_pilot.automation.currency import ensure_currency
from faktura_pilot.automation.models import ManualReviewRequired


def setup_gateway(current="Germany", options=None):
    dialog = object()
    gateway = Mock()
    gateway._visible_dialog_root.return_value = dialog
    gateway._resolver.return_value.resolve.return_value = SimpleNamespace(
        element=SimpleNamespace(handle=123)
    )
    combo = Mock()
    combo.item_texts.return_value = options or ["Germany", "United Kingdom"]
    combo.selected_text.return_value = current
    return gateway, combo, dialog


def test_currency_is_applied_even_when_already_selected():
    gateway, combo, dialog = setup_gateway()
    with patch("pywinauto.controls.win32_controls.ComboBoxWrapper", return_value=combo):
        ensure_currency(gateway, "EUR", allow_change=True)
    combo.select.assert_called_once_with("Germany")
    gateway._click_if_present.assert_called_once_with(
        ("Apply and Close",), control_types=("Button",), window=dialog
    )


def test_fresh_run_changes_currency_and_verifies_selection():
    gateway, combo, _ = setup_gateway("United Kingdom")
    combo.selected_text.side_effect = ["United Kingdom", "Germany"]
    with patch("pywinauto.controls.win32_controls.ComboBoxWrapper", return_value=combo):
        ensure_currency(gateway, "EUR", allow_change=True)
    combo.select.assert_called_once_with("Germany")


def test_resume_does_not_mix_an_existing_draft_currency():
    gateway, combo, dialog = setup_gateway("United Kingdom")
    with (
        patch("pywinauto.controls.win32_controls.ComboBoxWrapper", return_value=combo),
        pytest.raises(ManualReviewRequired, match="existing draft"),
    ):
        ensure_currency(gateway, "EUR", allow_change=False)
    combo.select.assert_not_called()
    gateway._click_if_present.assert_called_once_with(
        ("Cancel",), control_types=("Button",), window=dialog
    )


def test_unknown_currency_never_changes_preferences():
    gateway, _, _ = setup_gateway()
    with pytest.raises(ManualReviewRequired, match="no verified"):
        ensure_currency(gateway, "XYZ", allow_change=True)
    gateway._click.assert_not_called()


def test_failed_currency_selection_cannot_be_applied():
    gateway, combo, _ = setup_gateway("United Kingdom")
    with (
        patch("pywinauto.controls.win32_controls.ComboBoxWrapper", return_value=combo),
        pytest.raises(ManualReviewRequired, match="did not read back"),
    ):
        ensure_currency(gateway, "EUR", allow_change=True)
    gateway._click_if_present.assert_not_called()
