from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from faktura_pilot.automation.models import AmbiguousControl, ElementNotFound
from faktura_pilot.automation.resolver import (
    Bounds,
    ControlQuery,
    ControlResolver,
    OCRMatch,
    OCRUnavailable,
    TesseractOCR,
    find_tesseract_executable,
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

    def is_visible(self):
        return True


class FakeOCR:
    def __init__(self, matches):
        self.matches = matches
        self.calls = []

    def find_text(self, image, labels):
        self.calls.append((image, tuple(labels)))
        return self.matches


class TesseractResolverTests(unittest.TestCase):
    @staticmethod
    def _touch(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def test_path_executable_has_priority_over_override_and_common_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path_executable = self._touch(root / "path" / "tesseract.exe")
            override = self._touch(root / "override.exe")
            standard = self._touch(root / "ProgramFiles" / "Tesseract-OCR" / "tesseract.exe")

            executable = find_tesseract_executable(
                which=lambda name: str(path_executable),
                environ={
                    "FAKTURA_PILOT_TESSERACT_EXE": str(override),
                    "ProgramFiles": str(standard.parents[1]),
                },
                is_windows=True,
            )

        self.assertEqual(executable, path_executable.resolve())

    def test_explicit_override_is_used_when_path_does_not_find_tesseract(self):
        with tempfile.TemporaryDirectory() as folder:
            override = self._touch(Path(folder) / "custom" / "tesseract.exe")

            executable = find_tesseract_executable(
                which=lambda name: None,
                environ={"FAKTURA_PILOT_TESSERACT_EXE": str(override)},
                is_windows=True,
            )

        self.assertEqual(executable, override.resolve())

    def test_finds_tesseract_in_standard_windows_install_location(self):
        with tempfile.TemporaryDirectory() as folder:
            program_files = Path(folder) / "Program Files"
            installed = self._touch(program_files / "Tesseract-OCR" / "tesseract.exe")

            executable = find_tesseract_executable(
                which=lambda name: None,
                environ={"ProgramFiles": str(program_files)},
                is_windows=True,
            )

        self.assertEqual(executable, installed.resolve())

    def test_ignores_missing_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            missing_on_path = Path(folder) / "path" / "tesseract.exe"
            missing_override = Path(folder) / "override.exe"

            executable = find_tesseract_executable(
                which=lambda name: str(missing_on_path),
                environ={
                    "FAKTURA_PILOT_TESSERACT_EXE": str(missing_override),
                    "ProgramFiles": folder,
                },
                is_windows=True,
            )

        self.assertIsNone(executable)

    def test_configures_pytesseract_to_use_discovered_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = self._touch(Path(folder) / "Tesseract-OCR" / "tesseract.exe")
            pytesseract = SimpleNamespace(
                Output=SimpleNamespace(DICT="dict"),
                pytesseract=SimpleNamespace(tesseract_cmd="tesseract"),
                image_to_data=Mock(return_value={"text": []}),
            )

            with (
                patch.dict(sys.modules, {"pytesseract": pytesseract}),
                patch(
                    "faktura_pilot.automation.resolver.find_tesseract_executable",
                    return_value=executable.resolve(),
                ),
                patch.object(TesseractOCR, "_preprocess", return_value="prepared"),
            ):
                matches = TesseractOCR().find_text(object(), ["Save"])

        self.assertEqual(matches, [])
        self.assertEqual(pytesseract.pytesseract.tesseract_cmd, str(executable.resolve()))

    def test_overlapping_ocr_boxes_are_one_target_but_separate_boxes_remain_distinct(self):
        data = {
            "text": ["Company", "Company", "Company"],
            "block_num": [1, 2, 3],
            "par_num": [1, 1, 1],
            "line_num": [1, 1, 1],
            "left": [100, 95, 400],
            "top": [50, 45, 50],
            "width": [90, 110, 90],
            "height": [20, 30, 20],
        }

        matches = TesseractOCR.find_text_in_data(data, ["Company"])

        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].bounds, Bounds(100, 50, 190, 70))
        self.assertEqual(matches[1].bounds, Bounds(400, 50, 490, 70))

    def test_reports_missing_executable_as_ocr_unavailable(self):
        pytesseract = SimpleNamespace(
            Output=SimpleNamespace(DICT="dict"),
            pytesseract=SimpleNamespace(tesseract_cmd="tesseract"),
        )
        with (
            patch.dict(sys.modules, {"pytesseract": pytesseract}),
            patch(
                "faktura_pilot.automation.resolver.find_tesseract_executable",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(OCRUnavailable, "FAKTURA_PILOT_TESSERACT_EXE"):
                TesseractOCR().find_text(object(), ["Save"])


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

    def test_edit_resolution_scans_the_control_tree_once(self):
        label = FakeElement("Company", "Text", bounds=FakeRect(10, 10, 80, 30))
        edit = FakeElement("", "Edit", bounds=FakeRect(95, 10, 250, 30), value="")
        window = FakeWindow([label, edit])
        original_descendants = window.descendants
        scan_count = 0

        def counted_descendants():
            nonlocal scan_count
            scan_count += 1
            return original_descendants()

        window.descendants = counted_descendants

        resolved = ControlResolver(window).resolve_edit(
            ControlQuery.one_of("Company", "Customer")
        )

        self.assertIs(resolved.element, edit)
        self.assertEqual(scan_count, 1)

    def test_frozen_resolver_reuses_control_metadata_across_fields(self):
        fields = [FakeElement(label, "Edit", value=label) for label in ("Name", "Stock", "Price")]
        window = FakeWindow(fields)
        from faktura_pilot.automation import resolver as resolver_module

        name_reads = {id(field): 0 for field in fields}
        original = resolver_module.element_name

        def counted(element):
            if id(element) in name_reads:
                name_reads[id(element)] += 1
            return original(element)

        with patch.object(resolver_module, "element_name", side_effect=counted):
            resolver = ControlResolver(window).freeze()
            for label, field in zip(("Name", "Stock", "Price"), fields, strict=True):
                self.assertIs(resolver.resolve_edit(ControlQuery.one_of(label)).element, field)

        self.assertEqual(list(name_reads.values()), [1, 1, 1])

    def test_resolve_edit_does_not_associate_a_distant_unrelated_field(self):
        label = FakeElement("Company", "Text", bounds=FakeRect(10, 10, 80, 30))
        distant = FakeElement("", "Edit", bounds=FakeRect(10, 200, 180, 224), value="")
        window = FakeWindow([label, distant])

        with self.assertRaises(ElementNotFound):
            ControlResolver(window).resolve_edit(ControlQuery.one_of("Company"))


if __name__ == "__main__":
    unittest.main()
