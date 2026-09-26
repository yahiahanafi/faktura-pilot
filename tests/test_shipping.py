import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from faktura_pilot.automation import shipping
from faktura_pilot.automation.models import ActionOutcomeUnknown, ManualReviewRequired, VatCandidate
from faktura_pilot.automation.resolver import Bounds, OCRMatch


class ShippingTests(unittest.TestCase):
    def setUp(self):
        self.item = SimpleNamespace(
            name="Standard Shipping", net_amount=Decimal("12.50"),
            vat_rate_percent=Decimal("19"),
        )
        self.vat = VatCandidate("vat", "VAT 19%", Decimal("19"), "S")
        self.gateway = SimpleNamespace(
            find_vats=Mock(return_value=[self.vat]),
            create_vat=Mock(),
            _data_manager_tab=Mock(return_value=object()),
            _resolver=Mock(return_value=SimpleNamespace(resolve=Mock(return_value=object()))),
            _current_window=Mock(return_value=object()),
            _click_control=Mock(),
            _wait_until=Mock(),
            _set_field=Mock(),
            _dispatch_save=Mock(),
        )

    def test_reuses_exact_saved_method_without_create_or_save(self):
        with (
            patch.object(shipping, "_find_exact", return_value=True) as find,
            patch.object(shipping, "editor", return_value=object()),
            patch.object(shipping, "_verify") as verify,
        ):
            shipping.ensure(self.gateway, self.item)
        self.gateway.find_vats.assert_called_once_with(Decimal("19"))
        find.assert_called_once_with(self.gateway, "Standard Shipping")
        verify.assert_called_once()
        self.gateway._click_control.assert_not_called()
        self.gateway._dispatch_save.assert_not_called()

    def test_missing_vat_is_created_before_shipping_search(self):
        self.gateway.find_vats.return_value = []
        self.gateway.create_vat.return_value = self.vat
        order = []
        self.gateway.create_vat.side_effect = lambda rate: (order.append("vat"), self.vat)[1]
        with (
            patch.object(shipping, "_find_exact",
                         side_effect=lambda *args: order.append("search") or True),
            patch.object(shipping, "editor", return_value=object()),
            patch.object(shipping, "_verify"),
        ):
            shipping.ensure(self.gateway, self.item)
        self.assertEqual(order, ["vat", "search"])
        self.gateway._dispatch_save.assert_not_called()

    def test_conflicting_vat_stops_before_shipping_search(self):
        self.gateway.find_vats.return_value = [
            VatCandidate("bad", "VAT 19%", Decimal("19"), "Z")
        ]
        with patch.object(shipping, "_find_exact") as find:
            with self.assertRaises(ManualReviewRequired):
                shipping.ensure(self.gateway, self.item)
        find.assert_not_called()
        self.gateway._dispatch_save.assert_not_called()

    def test_blank_draft_is_filled_and_saved_once(self):
        scope = SimpleNamespace(window_text=lambda: "Shipping Costs")
        with (
            patch.object(shipping, "_find_exact", side_effect=[False, True]) as find,
            patch.object(shipping, "editor", return_value=scope),
            patch.object(shipping, "_editor_matches", return_value=[scope]),
            patch.object(shipping, "_blank_draft", return_value=True),
            patch.object(shipping, "_select_combo") as select,
            patch.object(shipping, "_set_gross") as gross,
            patch.object(shipping, "_verify") as verify,
        ):
            shipping.ensure(self.gateway, self.item)
        self.assertEqual(find.call_count, 2)
        self.gateway._click_control.assert_not_called()
        self.assertEqual(self.gateway._set_field.call_args_list, [
            call(("Name",), "Standard Shipping", scope=scope),
            call(("Description",), "Standard Shipping", scope=scope),
        ])
        self.assertEqual(select.call_args_list, [
            call(self.gateway, scope, "VAT Calculation", "Constant VAT"),
            call(self.gateway, scope, "VAT", "VAT 19%"),
        ])
        gross.assert_called_once_with(self.gateway, scope, Decimal("14.88"))
        self.assertEqual(verify.call_count, 2)
        self.gateway._dispatch_save.assert_called_once_with("save shipping method")

    def test_uncertain_readback_after_save_does_not_retry(self):
        scope = SimpleNamespace(window_text=lambda: "Shipping Costs")
        with (
            patch.object(shipping, "_find_exact", side_effect=[False, False]),
            patch.object(shipping, "editor", return_value=scope),
            patch.object(shipping, "_editor_matches", return_value=[scope]),
            patch.object(shipping, "_blank_draft", return_value=True),
            patch.object(shipping, "_select_combo"),
            patch.object(shipping, "_set_gross"),
            patch.object(shipping, "_verify"),
        ):
            with self.assertRaises(ActionOutcomeUnknown):
                shipping.ensure(self.gateway, self.item)
        self.gateway._dispatch_save.assert_called_once()

    def test_nonempty_search_without_exact_name_fails_closed(self):
        class FakeOCR:
            def read_words(self, image, *, preprocess=False):
                return [
                    OCRMatch("Standard", Bounds(900, -60, 980, -40)),
                    OCRMatch("Standard", Bounds(10, 10, 90, 30)),
                    OCRMatch("Name", Bounds(110, 10, 180, 30)),
                    OCRMatch("Description", Bounds(410, 10, 530, 30)),
                    OCRMatch("Value", Bounds(710, 10, 790, 30)),
                    OCRMatch("Other", Bounds(120, 100, 170, 120)),
                    OCRMatch("Shipping", Bounds(175, 100, 240, 120)),
                ]

        manager = object()
        gateway = SimpleNamespace(
            _open_data_manager=Mock(),
            _data_manager_tab=Mock(return_value=manager),
            _set_field=Mock(),
            _wait_stable_rows=Mock(),
            _capture_window=Mock(return_value=object()),
            _read_from_window=Mock(return_value="Standard"),
            ocr=FakeOCR(),
        )
        with (
            patch.object(shipping, "TesseractOCR", FakeOCR),
            patch.object(shipping, "verified_empty_selector", return_value=False),
        ):
            with self.assertRaisesRegex(ManualReviewRequired, "nonempty or duplicate"):
                shipping._find_exact(gateway, "Standard Shipping")
        gateway._set_field.assert_called_once_with(("Search",), "Standard", window=manager)

    def test_proven_empty_search_returns_false(self):
        class FakeOCR:
            def read_words(self, image, *, preprocess=False):
                return [
                    OCRMatch("Standard", Bounds(900, -60, 980, -40)),
                    OCRMatch("Standard", Bounds(10, 10, 90, 30)),
                    OCRMatch("Name", Bounds(110, 10, 180, 30)),
                    OCRMatch("Description", Bounds(410, 10, 530, 30)),
                    OCRMatch("Value", Bounds(710, 10, 790, 30)),
                ]

        manager = object()
        gateway = SimpleNamespace(
            _open_data_manager=Mock(),
            _data_manager_tab=Mock(return_value=manager),
            _set_field=Mock(),
            _wait_stable_rows=Mock(),
            _capture_window=Mock(return_value=object()),
            _read_from_window=Mock(return_value="Standard"),
            ocr=FakeOCR(),
        )
        with (
            patch.object(shipping, "TesseractOCR", FakeOCR),
            patch.object(shipping, "verified_empty_selector", return_value=True) as empty,
        ):
            self.assertFalse(shipping._find_exact(gateway, "Standard Shipping"))
        self.assertTrue(empty.call_args.kwargs["search_verified"])

    def test_blank_draft_reads_raw_edits(self):
        edits = {label: object() for label in ("Name", "Description", "Gross")}
        gateway = SimpleNamespace(
            _resolver=Mock(return_value=SimpleNamespace(
                resolve=lambda query: SimpleNamespace(element=edits[query.labels[0]])
            )),
            _raw_element_value=lambda edit: "0,00" if edit is edits["Gross"] else "",
        )
        self.assertTrue(shipping._blank_draft(gateway, object()))
        gateway._raw_element_value = lambda edit: "Existing" if edit is edits["Name"] else ""
        self.assertFalse(shipping._blank_draft(gateway, object()))

    def test_existing_method_with_different_default_gross_requires_review(self):
        scope = SimpleNamespace(window_text=lambda: "Shipping Costs")
        values = {
            "Name": "Standard Shipping",
            "Description": "Standard Shipping",
            "Gross": "99.00",
        }
        combos = {"VAT Calculation": "Constant VAT", "VAT": "VAT 19%"}
        gateway = SimpleNamespace(
            _read_optional=lambda labels, *, scope: values[labels[0]],
            _resolver=lambda scope: SimpleNamespace(
                resolve=lambda query: SimpleNamespace(element=query.labels[0])
            ),
            _element_value=lambda label: combos[label],
        )
        with self.assertRaises(ManualReviewRequired):
            shipping._verify(gateway, scope, self.item, "VAT 19%")

    def test_ambiguous_editors_stop_before_create(self):
        with (
            patch.object(shipping, "_find_exact", return_value=False),
            patch.object(shipping, "_editor_matches", return_value=[object(), object()]),
        ):
            with self.assertRaisesRegex(ManualReviewRequired, "multiple shipping editors"):
                shipping.ensure(self.gateway, self.item)
        self.gateway._click_control.assert_not_called()
        self.gateway._dispatch_save.assert_not_called()

    def test_matching_unsaved_method_is_not_reused(self):
        scope = SimpleNamespace(window_text=lambda: "*Shipping Costs")
        with (
            patch.object(shipping, "_find_exact", return_value=True),
            patch.object(shipping, "editor", return_value=scope),
            patch.object(shipping, "_verify") as verify,
        ):
            with self.assertRaises(ManualReviewRequired):
                shipping.ensure(self.gateway, self.item)
        verify.assert_not_called()
        self.gateway._dispatch_save.assert_not_called()

    def test_gross_uses_cent_rounding(self):
        self.item.net_amount = Decimal("0.05")
        self.item.vat_rate_percent = Decimal("10")
        self.assertEqual(shipping._gross(self.item), Decimal("0.06"))


if __name__ == "__main__":
    unittest.main()



def test_clipped_shipping_name_requires_full_editor_identity():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from faktura_pilot.automation import shipping
    from faktura_pilot.automation.resolver import Bounds, OCRMatch, TesseractOCR
    g = SimpleNamespace(
        _open_data_manager=Mock(), _data_manager_tab=Mock(return_value=object()),
        _set_field=Mock(), _wait_stable_rows=Mock(),
        _capture_window=Mock(return_value=SimpleNamespace(width=1000, height=500)),
        _read_from_window=Mock(return_value='Standard'), ocr=TesseractOCR(),
    )
    g.ocr.read_words = Mock(return_value=[
        OCRMatch(label, Bounds(x, 10, x+70, 30))
        for label, x in [('Standard',10), ('Name',110), ('Description',410), ('Value',710)]
    ] + [OCRMatch('Standard',Bounds(120,100,200,120)),
         OCRMatch('Shippin...',Bounds(210,100,290,120))])
    g._wait_until = Mock(side_effect=lambda label, predicate: predicate() or
                        (_ for _ in ()).throw(RuntimeError('wrong editor')))
    with patch.object(shipping, 'verified_empty_selector', return_value=False), \
         patch.object(shipping, 'element_bounds', return_value=Bounds(0,0,1000,500)), \
         patch.object(shipping, '_editor_name', return_value='Standard Shipping'), \
         patch('pywinauto.mouse.double_click'):
        assert shipping._find_exact(g, 'Standard Shipping')
    with patch.object(shipping, 'verified_empty_selector', return_value=False), \
         patch.object(shipping, 'element_bounds', return_value=Bounds(0,0,1000,500)), \
         patch.object(shipping, '_editor_name', return_value='Standard Shipping Other'), \
         patch('pywinauto.mouse.double_click'):
        import pytest
        with pytest.raises(RuntimeError, match='wrong editor'):
            shipping._find_exact(g, 'Standard Shipping')
