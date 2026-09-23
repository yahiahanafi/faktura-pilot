from __future__ import annotations

import unittest
from dataclasses import dataclass

from faktura_pilot.automation.models import AmbiguousControl, ElementNotFound
from faktura_pilot.automation.resolver import (
    Bounds,
    ControlQuery,
    ControlResolver,
    OCRMatch,
)


@dataclass
class FakeRect:
    left: int
    top: int
    right: int
    bottom: int


class FakeElementInfo:
    def __init__(
        self,
        name: str,
        control_type: str,
        bounds: FakeRect | None = None,
        automation_id: str = "",
        help_text: str = "",
    ) -> None:
        self.name = name
        self.control_type = control_type
        self.rectangle = bounds or FakeRect(0, 0, 20, 12)
        self.automation_id = automation_id
        self.help_text = help_text


class FakeElement:
    def __init__(
        self,
        name: str,
        control_type: str,
        *,
        bounds: FakeRect | None = None,
        automation_id: str = "",
        help_text: str = "",
        value: str | None = None,
        on_click=None,
    ) -> None:
        self.element_info = FakeElementInfo(name, control_type, bounds, automation_id, help_text)
        self._value = value
        self._on_click = on_click
        self.click_count = 0
        self._children: list[FakeElement] = []
        self._parent: FakeElement | None = None

    def add(self, *children: FakeElement) -> None:
        for child in children:
            child._parent = self
            self._children.append(child)

    def children(self):
        return list(self._children)

    def descendants(self):
        result = []
        for child in self._children:
            result.append(child)
            result.extend(child.descendants())
        return result

    def parent(self):
        return self._parent

    def window_text(self):
        return self.element_info.name

    def get_value(self):
        return self._value

    def set_edit_text(self, value: str):
        self._value = value

    def select(self, value: str):
        self._value = value

    def click_input(self, *args, **kwargs):
        self.click_count += 1
        if self._on_click:
            self._on_click()


class FakeWindow(FakeElement):
    def __init__(self, children=(), *, bounds: FakeRect | None = None) -> None:
        super().__init__("Fakturama - New Order", "Window", bounds=bounds)
        self.screen_clicks: list[tuple[int, int]] = []
        self.add(*children)

    def rectangle(self):
        return self.element_info.rectangle

    def capture_as_image(self):
        return object()

    def click_at_screen(self, point):
        self.screen_clicks.append(point)


class FakeOCR:
    def __init__(self, matches):
        self.matches = matches
        self.calls = []

    def find_text(self, image, labels):
        self.calls.append((image, tuple(labels)))
        return self.matches


class ResolverTests(unittest.TestCase):
    def test_resolver_prefers_unique_semantic_uia_control(self):
        button = FakeElement("Select the address", "Button")
        window = FakeWindow([button])
        ocr = FakeOCR([OCRMatch("Select the address", Bounds(1, 2, 10, 10))])

        resolved = ControlResolver(window, screenshot=window.capture_as_image, ocr=ocr).resolve(
            ControlQuery.one_of("Select the address", control_types=("Button",))
        )

        self.assertIs(resolved.element, button)
        self.assertEqual(ocr.calls, [])

    def test_resolver_uses_ancestor_context_to_disambiguate_controls(self):
        address_button = FakeElement("Select", "Button")
        product_button = FakeElement("Select", "Button")
        addresses = FakeElement("Addresses", "Group")
        products = FakeElement("Products", "Group")
        addresses.add(address_button)
        products.add(product_button)
        window = FakeWindow([addresses, products])

        resolved = ControlResolver(window).resolve(
            ControlQuery.one_of("Select", control_types=("Button",), ancestor_labels=("Addresses",))
        )

        self.assertIs(resolved.element, address_button)

    def test_duplicate_semantic_controls_raise_manual_review_error(self):
        first = FakeElement("Save", "Button")
        second = FakeElement("Save", "Button")
        window = FakeWindow([first, second])

        with self.assertRaises(AmbiguousControl):
            ControlResolver(window).resolve(ControlQuery.one_of("Save", control_types=("Button",)))

    def test_resolver_falls_back_to_unique_ocr_match(self):
        window = FakeWindow()
        match = OCRMatch("Select the address", Bounds(12, 30, 98, 50))
        ocr = FakeOCR([match])

        resolved = ControlResolver(window, screenshot=window.capture_as_image, ocr=ocr).resolve(
            ControlQuery.one_of("Select the address")
        )

        self.assertIsNone(resolved.element)
        self.assertEqual(resolved.ocr_match, match)
        self.assertEqual(len(ocr.calls), 1)

    def test_multiple_ocr_matches_raise_manual_review_error(self):
        window = FakeWindow()
        ocr = FakeOCR(
            [
                OCRMatch("Save", Bounds(0, 0, 20, 15)),
                OCRMatch("Save", Bounds(50, 0, 70, 15)),
            ]
        )

        with self.assertRaises(AmbiguousControl):
            ControlResolver(window, screenshot=window.capture_as_image, ocr=ocr).resolve(
                ControlQuery.one_of("Save")
            )

    def test_missing_control_raises_typed_error(self):
        window = FakeWindow()
        with self.assertRaises(ElementNotFound):
            ControlResolver(window).resolve(ControlQuery.one_of("Invoice"))


if __name__ == "__main__":
    unittest.main()
