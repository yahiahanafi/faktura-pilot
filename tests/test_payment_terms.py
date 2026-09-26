import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from faktura_pilot.automation import payment_terms
from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    ManualReviewRequired,
    PaymentMethodCandidate,
)
from faktura_pilot.automation.resolver import Bounds, OCRMatch


class PaymentTermsTests(unittest.TestCase):
    def make_gateway(self):
        manager = object()
        scope = SimpleNamespace(window_text=lambda: "Terms of payment")
        control = SimpleNamespace(element=object())
        gateway = SimpleNamespace(
            _data_manager_tab=Mock(return_value=manager),
            _resolver=Mock(return_value=SimpleNamespace(resolve=Mock(return_value=control))),
            _current_window=Mock(return_value=object()),
            _click_control=Mock(),
            _wait_until=Mock(),
            _set_field=Mock(),
            _select_named_option=Mock(),
            _raw_element_value=Mock(return_value=""),
            _write_element=Mock(),
            _dispatch_save=Mock(),
        )

        def read(labels, *, scope, control_types=None):
            del scope
            if control_types is not None:
                self.assertEqual(control_types, ("Edit",))
            if labels == ("Name",):
                return "Bank Transfer"
            if labels == ("Description",):
                return "Bank Transfer"
            if labels in payment_terms.ZERO_FIELDS:
                return "0"
            if labels in payment_terms.BLANK_FIELDS:
                return ""
            raise AssertionError(f"unexpected readback: {labels}")

        gateway._read_optional = Mock(side_effect=read)
        return gateway, manager, scope

    def test_exact_existing_code_is_reused_without_opening_or_saving_editor(self):
        gateway, _, _ = self.make_gateway()
        candidate = PaymentMethodCandidate("existing", "Bank Transfer", "Credit transfer")
        with patch.object(payment_terms, "find", return_value=[candidate]) as find:
            result = payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
        self.assertIs(result, candidate)
        find.assert_called_once_with(gateway, "Bank Transfer")
        gateway._click_control.assert_not_called()
        gateway._dispatch_save.assert_not_called()

    def test_conflict_or_duplicate_stops_before_create(self):
        for candidates in (
            [PaymentMethodCandidate("conflict", "Bank Transfer", "Cash")],
            [
                PaymentMethodCandidate("first", "Bank Transfer", "Credit transfer"),
                PaymentMethodCandidate("second", "Bank Transfer", "Credit transfer"),
            ],
        ):
            with self.subTest(candidates=candidates):
                gateway, _, _ = self.make_gateway()
                with patch.object(payment_terms, "find", return_value=candidates):
                    with self.assertRaises(ManualReviewRequired):
                        payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
                gateway._click_control.assert_not_called()
                gateway._dispatch_save.assert_not_called()

    def test_create_sets_exact_values_and_saves_once_without_setting_standard(self):
        gateway, manager, scope = self.make_gateway()
        candidate = PaymentMethodCandidate("saved", "Bank Transfer", "Credit transfer")
        with (
            patch.object(payment_terms, "find", side_effect=[[], [candidate]]) as find,
            patch.object(payment_terms, "editor", return_value=scope),
        ):
            result = payment_terms.create(gateway, "Bank Transfer", "Credit transfer")

        self.assertIs(result, candidate)
        self.assertEqual(find.call_count, 2)
        gateway._data_manager_tab.assert_called_with("terms of payment")
        self.assertEqual(gateway._resolver.call_args_list[0], call(manager))
        gateway._click_control.assert_called_once()
        self.assertEqual(
            gateway._set_field.call_args_list,
            [
                call(("Name",), "Bank Transfer", scope=scope),
                call(("Description",), "Bank Transfer", scope=scope),
                *(call(labels, "0", scope=scope) for labels in payment_terms.ZERO_FIELDS),
            ],
        )
        gateway._select_named_option.assert_called_once_with(
            payment_terms.CODE_LABELS, "Credit transfer", optional=False
        )
        self.assertIn("!editorPaymentPaymentcode!", payment_terms.CODE_LABELS)
        blank_queries = [
            item.args[0]
            for item in gateway._resolver.return_value.resolve.call_args_list
            if item.args[0].labels in payment_terms.BLANK_FIELDS
        ]
        self.assertEqual(
            [query.labels for query in blank_queries], list(payment_terms.BLANK_FIELDS)
        )
        self.assertTrue(all(query.control_types == ("Edit",) for query in blank_queries))
        gateway._write_element.assert_not_called()
        gateway._dispatch_save.assert_called_once_with("save payment terms")

    def test_account_targets_child_edit_and_clears_only_nonblank_value(self):
        gateway, _, scope = self.make_gateway()
        candidate = PaymentMethodCandidate("saved", "Bank Transfer", "Credit transfer")
        account_edit = object()
        other_edit = object()
        def resolve(query):
            return SimpleNamespace(
                element=account_edit if query.labels == ("Account",) else other_edit
            )
        gateway._resolver.return_value.resolve.side_effect = resolve
        reads = 0
        def raw_value(element):
            nonlocal reads
            if element is account_edit:
                reads += 1
                return "legacy account" if reads == 1 else ""
            return ""
        gateway._raw_element_value.side_effect = raw_value
        with (
            patch.object(payment_terms, "find", side_effect=[[], [candidate]]),
            patch.object(payment_terms, "editor", return_value=scope),
        ):
            payment_terms.create(gateway, "Bank Transfer", "Credit transfer")

        account_queries = [
            item.args[0]
            for item in gateway._resolver.return_value.resolve.call_args_list
            if item.args[0].labels == ("Account",)
        ]
        self.assertEqual(len(account_queries), 1)
        self.assertEqual(account_queries[0].control_types, ("Edit",))
        self.assertFalse(account_queries[0].allow_ocr)
        gateway._write_element.assert_called_once_with(account_edit, "")
        gateway._dispatch_save.assert_called_once_with("save payment terms")

    def test_matching_unsaved_draft_is_reused_without_create_click(self):
        gateway, _, _ = self.make_gateway()
        scope = SimpleNamespace(
            window_text=lambda: (
                "Terms of payment" if gateway._dispatch_save.called else "*Terms of payment"
            )
        )
        candidate = PaymentMethodCandidate("saved", "Bank Transfer", "Credit transfer")
        with (
            patch.object(payment_terms, "find", side_effect=[[], [candidate]]),
            patch.object(payment_terms, "editor", return_value=scope),
        ):
            payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
        gateway._data_manager_tab.assert_not_called()
        gateway._click_control.assert_not_called()
        gateway._dispatch_save.assert_called_once_with("save payment terms")

    def test_unrelated_unsaved_draft_fails_before_create_or_save(self):
        gateway, _, _ = self.make_gateway()
        scope = SimpleNamespace(window_text=lambda: "*Other payment term")
        gateway._read_optional = Mock(return_value="Other payment term")
        with (
            patch.object(payment_terms, "find", return_value=[]),
            patch.object(payment_terms, "editor", return_value=scope),
        ):
            with self.assertRaisesRegex(ManualReviewRequired, "unrelated unsaved"):
                payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
        gateway._click_control.assert_not_called()
        gateway._dispatch_save.assert_not_called()

    def test_post_save_readback_failure_is_uncertain_and_does_not_save_again(self):
        gateway, _, scope = self.make_gateway()
        with (
            patch.object(payment_terms, "find", side_effect=[[], []]),
            patch.object(payment_terms, "editor", return_value=scope),
        ):
            with self.assertRaises(ActionOutcomeUnknown):
                payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
        gateway._dispatch_save.assert_called_once_with("save payment terms")

    def test_exact_ocr_name_opens_selected_row_and_reads_code(self):
        class FakeOCR:
            def read_words(self, image):
                del image
                return [
                    OCRMatch("Standard", Bounds(10, 10, 90, 30)),
                    OCRMatch("Name", Bounds(110, 10, 180, 30)),
                    OCRMatch("Description", Bounds(410, 10, 520, 30)),
                    OCRMatch("Discount", Bounds(710, 10, 790, 30)),
                    # Live selected row: its left grid edge is OCR'd as a pipe.
                    OCRMatch("|", Bounds(102, 90, 104, 136)),
                    OCRMatch("Bank", Bounds(120, 100, 160, 120)),
                    OCRMatch("Transfer", Bounds(165, 100, 250, 120)),
                    OCRMatch("Bank", Bounds(120, 145, 160, 165)),
                    OCRMatch("Fee", Bounds(165, 145, 205, 165)),
                ]

        manager = SimpleNamespace(
            rectangle=lambda: SimpleNamespace(left=0, top=0, right=1000, bottom=800)
        )
        scope = SimpleNamespace(window_text=lambda: "Terms of payment")
        control = SimpleNamespace(element=object())
        gateway = SimpleNamespace(
            _close_dialog_if_present=Mock(),
            _open_data_manager=Mock(),
            _data_manager_tab=Mock(return_value=manager),
            _set_field=Mock(),
            _wait_stable_rows=Mock(),
            _capture_window=Mock(return_value=SimpleNamespace(width=1000, height=800)),
            _read_from_window=Mock(return_value="Bank"),
            _wait_until=Mock(side_effect=lambda description, predicate: predicate()),
            _read_optional=Mock(return_value="Bank Transfer"),
            _resolver=Mock(return_value=SimpleNamespace(resolve=Mock(return_value=control))),
            _element_value=Mock(return_value="Credit transfer"),
            _token=Mock(side_effect=lambda value: str(value)),
            ocr=FakeOCR(),
        )
        with (
            patch.object(payment_terms, "TesseractOCR", FakeOCR),
            patch.object(payment_terms, "verified_empty_selector", return_value=False),
            patch.object(payment_terms, "editor", return_value=scope),
            patch("pywinauto.mouse.double_click") as double_click,
        ):
            found = payment_terms.find(gateway, "Bank Transfer")
        gateway._set_field.assert_called_once_with(("Search",), "Bank", window=manager)
        self.assertEqual(
            [(item.name, item.code) for item in found],
            [("Bank Transfer", "Credit transfer")],
        )
        # Click the text rather than the OCR'd border at x=103.
        double_click.assert_called_once_with(coords=(140, 110))

    def test_truncated_ocr_name_fails_closed(self):
        gateway, _, _ = self.make_gateway()
        # A truncated row is not evidence of no exact match.
        with patch.object(payment_terms, "find", side_effect=ManualReviewRequired("truncated")):
            with self.assertRaises(ManualReviewRequired):
                payment_terms.create(gateway, "Bank Transfer", "Credit transfer")
        gateway._dispatch_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
