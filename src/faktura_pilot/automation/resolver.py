from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from faktura_pilot.automation.models import (
    AmbiguousControl,
    ElementNotFound,
    ManualReviewRequired,
)

_LOGGER = logging.getLogger("faktura_pilot.automation")


class OCRUnavailable(ManualReviewRequired):
    """The optional OCR engine could not be imported or initialized."""


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def intersects(self, other: Bounds) -> bool:
        return not (
            self.right < other.left
            or self.left > other.right
            or self.bottom < other.top
            or self.top > other.bottom
        )


def _same_ocr_location(left: Bounds, right: Bounds) -> bool:
    overlap_width = max(0, min(left.right, right.right) - max(left.left, right.left))
    overlap_height = max(0, min(left.bottom, right.bottom) - max(left.top, right.top))
    smaller_area = min(left.width * left.height, right.width * right.height)
    return bool(smaller_area and overlap_width * overlap_height * 2 >= smaller_area)


@dataclass(frozen=True)
class OCRMatch:
    text: str
    bounds: Bounds


@dataclass(frozen=True)
class ControlQuery:
    labels: tuple[str, ...]
    control_types: tuple[str, ...] = ()
    ancestor_labels: tuple[str, ...] = ()
    automation_ids: tuple[str, ...] = ()
    allow_ocr: bool = True

    @classmethod
    def one_of(
        cls,
        *labels: str,
        control_types: Sequence[str] = (),
        ancestor_labels: Sequence[str] = (),
        automation_ids: Sequence[str] = (),
        allow_ocr: bool = True,
    ) -> ControlQuery:
        return cls(
            labels=tuple(labels),
            control_types=tuple(control_types),
            ancestor_labels=tuple(ancestor_labels),
            automation_ids=tuple(automation_ids),
            allow_ocr=allow_ocr,
        )


class OCRProvider(Protocol):
    def find_text(self, image: Any, labels: Sequence[str]) -> list[OCRMatch]: ...


_TESSERACT_OVERRIDE_ENV = "FAKTURA_PILOT_TESSERACT_EXE"


def _existing_executable(value: str | Path) -> Path | None:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            return None
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def find_tesseract_executable(
    *,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    is_windows: bool | None = None,
) -> Path | None:
    """Find Tesseract on PATH or in an explicit/common Windows install location."""
    environment = os.environ if environ is None else environ
    path_lookup = shutil.which if which is None else which

    on_windows = os.name == "nt" if is_windows is None else is_windows
    path_candidate = path_lookup("tesseract")
    if path_candidate:
        executable = _existing_executable(path_candidate)
        if executable is not None:
            return executable

    override = environment.get(_TESSERACT_OVERRIDE_ENV)
    if override:
        executable = _existing_executable(override)
        if executable is not None:
            return executable

    if not on_windows:
        return None

    candidates: list[Path] = []
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = environment.get(variable)
        if root:
            candidates.append(Path(root) / "Tesseract-OCR" / "tesseract.exe")

    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe"
        )

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        executable = _existing_executable(candidate)
        if executable is not None:
            return executable
    return None


@dataclass(frozen=True)
class ResolvedControl:
    query: ControlQuery
    element: Any | None = None
    ocr_match: OCRMatch | None = None

    @property
    def is_ocr(self) -> bool:
        return self.element is None and self.ocr_match is not None


def normalize_label(text: str) -> str:
    compatible = unicodedata.normalize("NFKC", text)
    collapsed = " ".join(compatible.split()).casefold()
    return re.sub(r"[\W_]+", "", collapsed, flags=re.UNICODE)


def _safe_call(obj: Any, attribute: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, attribute)
        return value() if callable(value) else value
    except Exception:
        return default


def element_name(element: Any) -> str:
    info = _safe_call(element, "element_info")
    name = _safe_call(info, "name", "") if info is not None else ""
    if name:
        return str(name).strip()
    value = _safe_call(element, "window_text", "")
    if value:
        return str(value).strip()
    help_text = _safe_call(info, "help_text", "") if info is not None else ""
    return str(help_text or "").strip()


def element_type(element: Any) -> str:
    info = _safe_call(element, "element_info")
    value = _safe_call(info, "control_type", "") if info is not None else ""
    return str(value or "")


def element_bounds(element: Any) -> Bounds | None:
    info = _safe_call(element, "element_info")
    rect = _safe_call(info, "rectangle") if info is not None else None
    if rect is None:
        rect = _safe_call(element, "rectangle")
    if rect is None:
        return None
    try:
        return Bounds(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except (AttributeError, TypeError, ValueError):
        try:
            left, top, right, bottom = rect
            return Bounds(int(left), int(top), int(right), int(bottom))
        except (TypeError, ValueError):
            return None


def ancestor_names(element: Any, max_depth: int = 12) -> tuple[str, ...]:
    values: list[str] = []
    current = element
    for _ in range(max_depth):
        parent = _safe_call(current, "parent")
        if parent is None:
            break
        values.append(element_name(parent))
        current = parent
    return tuple(value for value in values if value)


class TesseractOCR:
    """Lazy pytesseract implementation; OCR coordinates are screenshot-relative."""

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable

    def read_text_data(
        self, image: Any, *, config: str = "", preprocess: bool = True
    ) -> dict[str, Any]:
        """Run Tesseract once; callers can inspect words and exact label matches."""
        try:
            import pytesseract
            from pytesseract import Output
        except ImportError as exc:
            raise OCRUnavailable(
                "OCR fallback requires the optional pytesseract package and Tesseract binary"
            ) from exc

        executable = (
            _existing_executable(self.executable)
            if self.executable is not None
            else find_tesseract_executable()
        )
        if executable is None:
            raise OCRUnavailable(
                "Tesseract executable was not found; set "
                f"{_TESSERACT_OVERRIDE_ENV} or add it to PATH"
            )
        self.executable = executable
        pytesseract.pytesseract.tesseract_cmd = str(executable)
        processed = self._preprocess(image) if preprocess else image
        try:
            return pytesseract.image_to_data(
                processed, output_type=Output.DICT, config=config
            )
        except Exception as exc:
            raise OCRUnavailable(
                f"OCR could not inspect the current Fakturama window: {exc}"
            ) from exc

    def find_text(self, image: Any, labels: Sequence[str]) -> list[OCRMatch]:
        return self.find_text_in_data(self.read_text_data(image), labels)

    @staticmethod
    def find_text_in_data(
        data: dict[str, Any], labels: Sequence[str], *, exact: bool = False
    ) -> list[OCRMatch]:
        groups: dict[tuple[int, int, int], list[tuple[int, str, Bounds]]] = {}
        count = len(data.get("text", []))
        for index in range(count):
            text = str(data["text"][index]).strip()
            if not text:
                continue
            key = (
                int(data.get("block_num", [0] * count)[index]),
                int(data.get("par_num", [0] * count)[index]),
                int(data.get("line_num", [0] * count)[index]),
            )
            left = int(data.get("left", [0] * count)[index])
            top = int(data.get("top", [0] * count)[index])
            width = int(data.get("width", [0] * count)[index])
            height = int(data.get("height", [0] * count)[index])
            groups.setdefault(key, []).append(
                (left, text, Bounds(left, top, left + width, top + height))
            )

        wanted = {
            label.strip() if exact else normalize_label(label): label
            for label in labels
        }
        matches: list[OCRMatch] = []
        for words in groups.values():
            words.sort(key=lambda word: word[0])
            for start in range(len(words)):
                for end in range(start + 1, len(words) + 1):
                    phrase = " ".join(word[1] for word in words[start:end])
                    key = phrase.strip() if exact else normalize_label(phrase)
                    if key in wanted:
                        boxes = [word[2] for word in words[start:end]]
                        matches.append(
                            OCRMatch(
                                text=phrase if exact else wanted[key],
                                bounds=Bounds(
                                    min(box.left for box in boxes),
                                    min(box.top for box in boxes),
                                    max(box.right for box in boxes),
                                    max(box.bottom for box in boxes),
                                ),
                            )
                        )
        # Identical matches from alternate spellings are one semantic location.
        unique: dict[tuple[int, int, int, int], OCRMatch] = {}
        for match in matches:
            key = (match.bounds.left, match.bounds.top, match.bounds.right, match.bounds.bottom)
            unique[key] = match
        # Tesseract can return both a word and an enclosing phrase with the
        # same label. They occupy one visible target, not two controls.
        distinct: list[OCRMatch] = []
        for match in unique.values():
            overlapping = [
                index for index, prior in enumerate(distinct)
                if normalize_label(prior.text) == normalize_label(match.text)
                and _same_ocr_location(prior.bounds, match.bounds)
            ]
            if overlapping:
                index = overlapping[0]
                if match.bounds.width * match.bounds.height < (
                    distinct[index].bounds.width * distinct[index].bounds.height
                ):
                    distinct[index] = match
            else:
                distinct.append(match)
        return distinct

    def read_words(self, image: Any, *, preprocess: bool = True) -> list[OCRMatch]:
        """Return individually recognized words with screenshot-relative bounds."""
        data = (self.read_text_data(image, config="--psm 6") if preprocess else
                self.read_text_data(image, config="--psm 6", preprocess=False))
        words: list[OCRMatch] = []
        count = len(data.get("text", []))
        for index in range(count):
            text = str(data["text"][index]).strip()
            if not text:
                continue
            left = int(data.get("left", [0] * count)[index])
            top = int(data.get("top", [0] * count)[index])
            width = int(data.get("width", [0] * count)[index])
            height = int(data.get("height", [0] * count)[index])
            words.append(
                OCRMatch(text=text, bounds=Bounds(left, top, left + width, top + height))
            )
        return words

    @staticmethod
    def _preprocess(image: Any) -> Any:
        # OpenCV is optional. PIL grayscale remains the portable fallback.
        try:
            import cv2
            import numpy as np
            from PIL import Image

            array = np.array(image.convert("RGB"))
            gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
            gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
            # pytesseract accepts PIL images and file paths, but not the NumPy
            # array returned by OpenCV. Wrap the processed pixels as a PIL image.
            return Image.fromarray(gray)
        except (ImportError, AttributeError, TypeError):
            try:
                return image.convert("L")
            except AttributeError:
                return image


class ControlResolver:
    """Resolve a live UIA element first, then a unique OCR text target."""

    def __init__(
        self,
        root: Any,
        screenshot: Callable[[], Any] | None = None,
        ocr: OCRProvider | None = None,
    ) -> None:
        self.root = root
        self.screenshot = screenshot
        self.ocr = ocr
        self._snapshot: list[Any] | None = None
        self._snapshot_metadata: dict[int, tuple[str, str, str]] = {}

    def freeze(self) -> ControlResolver:
        """Reuse one live tree for a short sequence of read-only field checks."""
        self._snapshot = self._scan_elements()
        self._snapshot_metadata.clear()
        return self

    def _metadata(self, element: Any) -> tuple[str, str, str]:
        key = id(element)
        if self._snapshot is not None and key in self._snapshot_metadata:
            return self._snapshot_metadata[key]
        info = _safe_call(element, "element_info")
        metadata = (
            str(_safe_call(info, "control_type", "") or ""),
            normalize_label(element_name(element)),
            str(_safe_call(info, "automation_id", "") or ""),
        )
        if self._snapshot is not None:
            self._snapshot_metadata[key] = metadata
        return metadata

    def resolve(self, query: ControlQuery) -> ResolvedControl:
        elements = self._uia_matches(query)
        if len(elements) == 1:
            return ResolvedControl(query=query, element=elements[0])
        if len(elements) > 1:
            narrowed = self._disambiguate_with_ocr(query, elements)
            if narrowed is not None:
                return ResolvedControl(query=query, element=narrowed)
            names = [element_name(element) for element in elements]
            raise AmbiguousControl(
                f"{query.labels!r} matched {len(elements)} UIA controls"
                + (f": {names!r}" if names else "")
            )
        if query.allow_ocr and self.ocr is not None and self.screenshot is not None:
            _LOGGER.debug("UI Automation could not resolve %s; trying OCR", query.labels)
            matches = self.ocr.find_text(self.screenshot(), query.labels)
            if len(matches) == 1:
                return ResolvedControl(query=query, ocr_match=matches[0])
            if len(matches) > 1:
                raise AmbiguousControl(
                    f"OCR found {len(matches)} visible matches for {query.labels!r}"
                )
        raise ElementNotFound(f"could not resolve Fakturama control {query.labels!r}")

    def resolve_edit(self, query: ControlQuery) -> ResolvedControl:
        """Find a named editor or associate a visible static label with an Edit."""
        elements = self._elements()
        edits = self._uia_matches(
            ControlQuery(
                labels=query.labels,
                control_types=("Edit", "ComboBox"),
                ancestor_labels=query.ancestor_labels,
                automation_ids=query.automation_ids,
                allow_ocr=False,
            ),
            elements=elements,
        )
        if len(edits) == 1:
            return ResolvedControl(query=query, element=edits[0])
        if len(edits) > 1:
            raise AmbiguousControl(f"multiple editable controls are named {query.labels!r}")

        labels = self._uia_matches(
            ControlQuery(
                labels=query.labels,
                control_types=("Text", "Label", "Static"),
                ancestor_labels=query.ancestor_labels,
                allow_ocr=False,
            ),
            elements=elements,
        )
        if len(labels) > 1:
            narrowed = self._disambiguate_with_ocr(query, labels)
            if narrowed is None:
                raise AmbiguousControl(f"multiple labels identify editable fields {query.labels!r}")
            labels = [narrowed]
        if len(labels) == 1:
            nearby = self._nearest_edit(labels[0], elements)
            if nearby is not None:
                return ResolvedControl(query=query, element=nearby)

        # OCR can locate the label, but text entry still requires an accessible
        # live editor; clicking guessed offsets to type into an unknown field is unsafe.
        if query.allow_ocr and self.ocr is not None and self.screenshot is not None:
            _LOGGER.debug("UI Automation could not resolve edit %s; trying OCR", query.labels)
            matches = self.ocr.find_text(self.screenshot(), query.labels)
            if len(matches) > 1:
                raise AmbiguousControl(
                    f"OCR found multiple labels for editable field {query.labels!r}"
                )
            if len(matches) == 1:
                nearby = self._nearest_edit_to_box(matches[0].bounds, elements)
                if nearby is not None:
                    return ResolvedControl(query=query, element=nearby)
        raise ElementNotFound(f"could not associate a live edit control with {query.labels!r}")

    def _elements(self) -> list[Any]:
        return self._snapshot if self._snapshot is not None else self._scan_elements()

    def _scan_elements(self) -> list[Any]:
        try:
            return [self.root, *self.root.descendants()]
        except Exception:
            return [self.root]

    def _uia_matches(self, query: ControlQuery, *, elements: list[Any] | None = None) -> list[Any]:
        auto_ids = set(query.automation_ids)
        allowed_types = set(query.control_types)
        wanted = tuple(normalize_label(label) for label in query.labels)
        required_ancestors = {normalize_label(label) for label in query.ancestor_labels}
        buckets: list[list[Any]] = [[] for _ in wanted]
        for element in elements if elements is not None else self._elements():
            control_type, name, automation_id = self._metadata(element)
            if allowed_types and control_type not in allowed_types:
                continue
            for index, label in enumerate(wanted):
                if name != label and automation_id not in auto_ids:
                    continue
                if required_ancestors:
                    observed = {normalize_label(name) for name in ancestor_names(element)}
                    if not required_ancestors.issubset(observed):
                        break
                buckets[index].append(element)
                break
        # Labels are prioritized synonyms. A later synonym is considered only
        # when the current label has no live UIA match.
        for matches in buckets:
            if matches:
                return matches
        return []

    def _disambiguate_with_ocr(self, query: ControlQuery, elements: list[Any]) -> Any | None:
        if self.ocr is None or self.screenshot is None:
            return None
        try:
            matches = self.ocr.find_text(self.screenshot(), query.labels)
        except OCRUnavailable:
            return None
        if len(matches) != 1:
            return None
        target = matches[0].bounds
        intersecting = [
            element
            for element in elements
            if (b := element_bounds(element)) and b.intersects(target)
        ]
        return intersecting[0] if len(intersecting) == 1 else None

    def _nearest_edit(self, label: Any, elements: list[Any]) -> Any | None:
        label_bounds = element_bounds(label)
        if label_bounds is None:
            return None
        candidates = [
            element for element in elements if element_type(element) in {"Edit", "ComboBox"}
        ]
        return self._choose_nearest(label_bounds, candidates, horizontal=True)

    def _nearest_edit_to_box(self, label_bounds: Bounds, elements: list[Any]) -> Any | None:
        candidates = [
            element for element in elements if element_type(element) in {"Edit", "ComboBox"}
        ]
        return self._choose_nearest(label_bounds, candidates, horizontal=True)

    @staticmethod
    def _choose_nearest(
        label_bounds: Bounds, candidates: list[Any], horizontal: bool
    ) -> Any | None:
        scored: list[tuple[int, Any]] = []
        label_y = (label_bounds.top + label_bounds.bottom) // 2
        max_horizontal_gap = max(label_bounds.height * 12, 240)
        max_vertical_gap = max(label_bounds.height * 4, 36)
        max_vertical_alignment = max(label_bounds.width * 2, 200)
        for element in candidates:
            bounds = element_bounds(element)
            if bounds is None or bounds.width == 0 or bounds.height == 0:
                continue
            center_y = (bounds.top + bounds.bottom) // 2
            dx = bounds.left - label_bounds.right
            dy = abs(center_y - label_y)
            if (
                horizontal
                and 0 <= dx <= max_horizontal_gap
                and dy <= max(label_bounds.height * 2, 18)
            ):
                score = dx + dy * 3
            elif (
                label_bounds.bottom <= bounds.top
                and bounds.top - label_bounds.bottom <= max_vertical_gap
                and abs(bounds.left - label_bounds.left) <= max_vertical_alignment
            ):
                score = (
                    bounds.top
                    - label_bounds.bottom
                    + abs(bounds.left - label_bounds.left) * 2
                    + 100
                )
            else:
                continue
            scored.append((score, element))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            raise AmbiguousControl("more than one editor is equally near to the visible label")
        return scored[0][1]
