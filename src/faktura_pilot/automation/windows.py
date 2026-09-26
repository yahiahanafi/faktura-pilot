from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    AmbiguousControl,
    DebtorCandidate,
    DocumentRow,
    ElementNotFound,
    GatewayError,
    InvoiceEditorRef,
    ManualReviewRequired,
    OrderEditorRef,
    PaymentMethodCandidate,
    PostconditionFailed,
    PreflightResult,
    ProductCandidate,
    TransitionTimeout,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.automation.native_edit import native_edit_text
from faktura_pilot.automation.native_focus import ensure_foreground, top_level_handle
from faktura_pilot.automation.native_table import verified_empty_selector
from faktura_pilot.automation.resolver import (
    Bounds,
    ControlQuery,
    ControlResolver,
    OCRMatch,
    OCRProvider,
    OCRUnavailable,
    ResolvedControl,
    TesseractOCR,
    element_bounds,
    element_name,
    element_type,
    normalize_label,
)
from faktura_pilot.domain.calculations import expected_line_net, product_gross_price
from faktura_pilot.domain.models import Address, Debtor, Item, OrderSource, PaymentStatus
from faktura_pilot.domain.policy import sku_matches

_ROW_TYPES = {"DataItem", "ListItem", "TreeItem", "Row"}
_CELL_TYPES = {"DataItem", "Text", "Edit", "ComboBox", "CheckBox", "Custom"}
_LOGGER = logging.getLogger("faktura_pilot.automation")
_MONEY_TOLERANCE = Decimal("0.01")


def _safe(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _is_fakturama_window_title(title: str) -> bool:
    """Match Fakturama's app title without matching package names in other windows."""
    if title.strip().casefold() in {
        "fakturama: automation complete", "fakturama: manual review required",
    }:
        return False
    return re.match(r"^\s*fakturama\b", title, flags=re.IGNORECASE) is not None


def _children(obj: Any) -> list[Any]:
    try:
        return list(obj.children())
    except Exception:
        return []


def _descendants(obj: Any) -> list[Any]:
    try:
        return list(obj.descendants())
    except Exception:
        return []


def _all_text(obj: Any) -> str:
    parts = [element_name(obj)]
    parts.extend(element_name(child) for child in _descendants(obj))
    return " ".join(part for part in parts if part).strip()


def _has_english_menu(window: Any, *, deep_fallback: bool = False) -> bool:
    """Inspect the shallow menu first; a full UIA tree is expensive in SWT."""
    pending = [(window, 0)]
    labels: set[str] = set()
    inspected = 0
    while pending and inspected < 200:
        element, depth = pending.pop(0)
        inspected += 1
        if element_type(element) in {"MenuItem", "MenuBarItem", "Menu", "Button"}:
            name = normalize_label(element_name(element))
            if name in {"file", "data"}:
                labels.add(name)
                if len(labels) == 2:
                    return True
        if depth < 4:
            pending.extend((child, depth + 1) for child in _children(element))
    if deep_fallback:
        _LOGGER.info("Checking deeper Fakturama controls to confirm the UI language")
        root_text = _all_text(window).casefold()
        return all(token in root_text for token in ("file", "data"))
    return False


def _date_text(value: date) -> str:
    return value.isoformat()


def _parse_date_text(value: str) -> date | None:
    cleaned = value.strip().replace("\u00a0", " ").replace(",", "")
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        pass

    # Fakturama's Windows date control commonly renders dates with an English
    # month name, while its numeric form follows the Windows locale. Accept
    # unambiguous numeric forms and month-name forms for readback verification.
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.IGNORECASE)
    formats = (
        "%d %m %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%m-%d-%Y",
    )
    parsed: set[date] = set()
    for format_string in formats:
        try:
            parsed.add(datetime.strptime(cleaned, format_string).date())
        except ValueError:
            continue
    return next(iter(parsed)) if len(parsed) == 1 else None


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _parse_decimal(text: str) -> Decimal | None:
    cleaned = text.strip().replace("\u00a0", " ")
    cleaned = re.sub(r"[^0-9,.-]", "", cleaned)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _normalized_equal(expected: str, actual: str) -> bool:
    return normalize_label(expected) == normalize_label(actual)


def _e_invoice_code(value: str) -> str:
    normalized = normalize_label(value)
    if normalized == "s" or normalized.startswith("sstandardrate"):
        return "S"
    return value.strip()


class WindowsFakturamaGateway:
    """UIA-first gateway for the Fakturama 2.2.0 English workflow.

    All controls are re-discovered from the current UIA tree at the time of each
    action. OCR targets are derived from the active window's current screenshot
    and bounds; this class does not store screen coordinates or layout templates.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        launch_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 0.25,
        ocr_engine: OCRProvider | None = None,
        evidence_directory: Path | None = None,
        app_factory: Callable[..., Any] | None = None,
        desktop_factory: Callable[..., Any] | None = None,
        process_checker: Callable[[Path], bool | None] | None = None,
        version_reader: Callable[[Path], str | None] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or launch_timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("wait timeouts and poll interval must be positive")
        self.timeout_seconds = timeout_seconds
        self.launch_timeout_seconds = max(timeout_seconds, launch_timeout_seconds)
        self.poll_interval_seconds = poll_interval_seconds
        self.ocr = ocr_engine if ocr_engine is not None else TesseractOCR()
        self.evidence_directory = evidence_directory
        self._app_factory = app_factory
        self._desktop_factory = desktop_factory
        self._process_checker = process_checker
        self._version_reader = version_reader
        self._application: Any | None = None
        self._main_window: Any | None = None
        self._configured_executable: Path | None = None
        self._order_ref: OrderEditorRef | None = None
        self._invoice_ref: InvoiceEditorRef | None = None
        self._last_source: OrderSource | None = None
        self._last_order_number: str | None = None
        self._last_invoice_number: str | None = None
        self._billing_address_tab_label: str | None = None
        self._delivery_address_tab_label: str | None = None
        self._save_timeout_seconds = max(timeout_seconds, 30.0)
        self._dpi_aware = False
        self._product_search_configured = False

    def attach_or_launch(self, executable: Path | None = None) -> None:
        _LOGGER.info("Looking for a running Fakturama window")
        app_factory, desktop_factory = self._automation_factories()
        self._dpi_aware = self._set_process_dpi_awareness()
        application = app_factory(backend="uia")
        if executable is not None:
            self._configured_executable = executable.expanduser().resolve()

        # Prefer the already-running, visible application. Connecting by path can fail for an
        # elevated Fakturama process even though its window is visible to UI Automation.
        windows = self._visible_fakturama_windows(desktop_factory)
        if len(windows) > 1:
            raise ManualReviewRequired(self._ambiguous_windows_message(windows))
        if windows:
            if self._configured_executable is None:
                self._remember_discovered_executable()
            self._attach_to_window(application, windows[0])
            _LOGGER.info("Attached to the running Fakturama window")
            return

        if executable is None:
            executable = (
                self._configured_executable or self._discover_fakturama_executable()
            )
            if executable is not None:
                self._configured_executable = executable

        started = False
        if executable is not None:
            executable = self._configured_executable
            if not executable.is_file():
                raise GatewayError(f"configured Fakturama executable does not exist: {executable}")
            try:
                application.connect(path=str(executable), timeout=self.timeout_seconds)
            except Exception as connect_error:
                # A path connection failure is ambiguous: the process may be elevated or its
                # window may have appeared between discovery and connection. Recheck windows,
                # then inspect processes before deciding that starting another instance is safe.
                windows = self._visible_fakturama_windows(desktop_factory)
                if len(windows) > 1:
                    raise ManualReviewRequired(
                        self._ambiguous_windows_message(windows)
                    ) from connect_error
                if windows:
                    self._attach_to_window(application, windows[0])
                    return
                process_running = self._process_exists_for_executable(executable)
                if process_running is not False:
                    reason = (
                        "a matching Fakturama process is running"
                        if process_running
                        else "could not confirm that Fakturama is stopped"
                    )
                    raise GatewayError(
                        f"could not attach to Fakturama at {executable}: {connect_error}; "
                        f"{reason}, so a second instance was not started"
                    ) from connect_error
                # A negative, successful process inventory plus a second empty window scan
                # distinguishes a stopped app from an inaccessible running instance.
                _LOGGER.info("Starting Fakturama and waiting for its window")
                try:
                    application.start(str(executable), timeout=self.timeout_seconds)
                except Exception as exc:
                    raise GatewayError(f"could not launch Fakturama: {exc}") from exc
                started = True
        else:
            raise GatewayError(
                "no visible Fakturama window or installed executable was found; start Fakturama, "
                "set FAKTURAMA_EXE, or provide --fakturama-exe"
            )

        if started:
            window = self._wait_for_visible_fakturama_window(desktop_factory, executable)
            self._attach_to_window(application, window)
            _LOGGER.info("Fakturama window is ready")
            return

        self._application = application
        try:
            self._main_window = application.top_window()
        except Exception as exc:
            raise GatewayError(
                f"connected to Fakturama but could not discover its main window: {exc}"
            ) from exc
        if self._main_window is None:
            raise GatewayError("connected to Fakturama but no top-level window was found")

    def diagnose(self, executable: Path | None = None) -> dict[str, str]:
        """Collect read-only diagnostics without launching or interacting with Fakturama."""
        discovery_issue = None
        if executable is not None:
            self._configured_executable = executable.expanduser().resolve()
        elif self._configured_executable is None:
            discovery_issue = self._remember_discovered_executable()

        checks: dict[str, str] = {}
        configured = self._configured_executable
        if configured is None:
            checks["Executable"] = (
                discovery_issue or "not specified; using the running process path if available"
            )
        else:
            checks["Executable"] = (
                f"{configured} ({'found' if configured.is_file() else 'not found'})"
            )

        try:
            app_factory, desktop_factory = self._automation_factories()
        except GatewayError as exc:
            checks["UI Automation"] = f"unavailable: {exc}"
            checks["Window"] = "not inspected"
            checks["Version"] = self._executable_version() or "unknown (no attached window)"
            checks["Language"] = "unknown (no attached window)"
            checks["DPI awareness"] = self._dpi_diagnostic()
            return checks

        checks["UI Automation"] = "pywinauto available"
        try:
            windows = self._visible_fakturama_windows(desktop_factory)
        except GatewayError as exc:
            checks["Window"] = f"could not inspect: {exc}"
            checks["Version"] = self._executable_version() or "unknown (window discovery failed)"
            checks["Language"] = "unknown (window discovery failed)"
            checks["DPI awareness"] = self._dpi_diagnostic()
            return checks

        if len(windows) > 1:
            checks["Window"] = self._ambiguous_windows_message(windows)
            checks["Version"] = self._executable_version() or "not checked (ambiguous windows)"
            checks["Language"] = "not checked (ambiguous windows)"
            checks["DPI awareness"] = self._dpi_diagnostic()
            return checks
        if not windows:
            checks["Window"] = "no visible Fakturama window found"
            checks["Version"] = self._executable_version() or "unknown (no attached window)"
            checks["Language"] = "unknown (no attached window)"
            checks["DPI awareness"] = self._dpi_diagnostic()
            return checks

        window = windows[0]
        try:
            self._attach_to_window(app_factory(backend="uia"), window)
        except GatewayError as exc:
            checks["Window"] = f"found but could not attach: {exc}"
            checks["Version"] = "unknown (window attachment failed)"
            checks["Language"] = "unknown (window attachment failed)"
            checks["DPI awareness"] = self._dpi_diagnostic()
            return checks

        title = self._window_title(window) or "(untitled)"
        handle = _safe(window, "handle")
        process_id = int(_safe(window, "process_id", 0) or 0)
        process_path = _safe(window, "process_path")
        identity = f"{title!r}, PID {process_id}"
        if handle:
            identity += f", HWND {handle}"
        checks["Window"] = identity
        if process_path:
            checks["Process image"] = str(process_path)

        version = self._executable_version()
        checks["Version"] = version or "could not determine from the running installation"
        checks["Language"] = (
            "English (visible File and Data labels found)"
            if _has_english_menu(window)
            else "could not confirm English from visible menu labels"
        )
        checks["DPI awareness"] = self._dpi_diagnostic()
        return checks

    def _automation_factories(self) -> tuple[Callable[..., Any], Callable[..., Any]]:
        app_factory = self._app_factory
        desktop_factory = self._desktop_factory
        if app_factory is None or desktop_factory is None:
            try:
                from pywinauto import Application, Desktop
            except Exception as exc:
                raise GatewayError(
                    "Windows Fakturama automation could not load pywinauto; "
                    "check the automation installation: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            app_factory = app_factory or Application
            desktop_factory = desktop_factory or Desktop
        return app_factory, desktop_factory

    def _visible_fakturama_windows(self, desktop_factory: Callable[..., Any]) -> list[Any]:
        if os.name == "nt" and self._desktop_factory is None:
            handles = self._native_visible_fakturama_handles()
            if handles:
                desktop = desktop_factory(backend="uia")
                windows = []
                for handle in handles:
                    try:
                        windows.append(desktop.window(handle=handle).wrapper_object())
                    except Exception as exc:
                        raise GatewayError(
                            f"found Fakturama window HWND {handle} but could not inspect it: {exc}"
                        ) from exc
                return windows
        try:
            windows = desktop_factory(backend="uia").windows()
        except Exception as exc:
            raise GatewayError(f"could not inspect desktop windows: {exc}") from exc

        matches: list[Any] = []
        seen_handles: set[Any] = set()
        for window in windows:
            title = self._window_title(window)
            if not _is_fakturama_window_title(title) or not _safe(window, "is_visible", True):
                continue
            handle = _safe(window, "handle")
            if handle is not None and handle in seen_handles:
                continue
            if handle is not None:
                seen_handles.add(handle)
            matches.append(window)
        return matches

    @staticmethod
    def _native_visible_fakturama_handles() -> list[int]:
        """Find title-matched HWNDs without enumerating unrelated UIA windows."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def collect(hwnd: int, _context: int) -> bool:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if _is_fakturama_window_title(title.value):
                    found.append(int(hwnd))
                return True

            callback = callback_type(collect)
            user32.EnumWindows(callback, 0)
            return found
        except Exception as exc:
            _LOGGER.debug("Native Fakturama window search failed: %s", exc)
            return []

    def _wait_for_visible_fakturama_window(
        self, desktop_factory: Callable[..., Any], executable: Path
    ) -> Any:
        deadline = time.monotonic() + self.launch_timeout_seconds
        while True:
            windows = self._visible_fakturama_windows(desktop_factory)
            if len(windows) > 1:
                raise ManualReviewRequired(self._ambiguous_windows_message(windows))
            if windows:
                return windows[0]

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError(
                    f"Fakturama was started from {executable}, but no visible Fakturama "
                    f"window appeared within {self.launch_timeout_seconds:g} seconds; "
                    "the process may be running without creating a window"
                )
            time.sleep(min(self.poll_interval_seconds, remaining))

    @staticmethod
    def _discover_fakturama_executable() -> Path | None:
        """Find one installed Fakturama executable without guessing between installs."""
        configured = os.environ.get("FAKTURAMA_EXE")
        if configured:
            executable = Path(configured.strip().strip('"')).expanduser().resolve()
            if not executable.is_file():
                raise GatewayError(
                    f"FAKTURAMA_EXE does not point to an existing file: {executable}"
                )
            return executable

        candidates: dict[str, Path] = {}

        def add_candidate(value: str | Path | None) -> None:
            if not value:
                return
            candidate = Path(str(value).strip().strip('"')).expanduser()
            try:
                candidate = candidate.resolve()
                if candidate.is_file():
                    candidates[str(candidate).casefold()] = candidate
            except (OSError, ValueError):
                return

        install_roots = {
            value
            for key in (
                "ProgramFiles",
                "ProgramFiles(x86)",
                "ProgramW6432",
                "LOCALAPPDATA",
                "APPDATA",
            )
            if (value := os.environ.get(key))
        }
        install_directories = (
            Path("Fakturama2"),
            Path("Fakturama"),
            Path("Sebulli") / "Fakturama2",
        )
        for root in install_roots:
            for directory in install_directories:
                add_candidate(Path(root) / directory / "Fakturama.exe")

        try:
            import winreg
        except ImportError:
            winreg = None
        if winreg is not None:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\App Paths\Fakturama.exe"
            hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
            views = (
                0,
                getattr(winreg, "KEY_WOW64_32KEY", 0),
                getattr(winreg, "KEY_WOW64_64KEY", 0),
            )
            for hive in hives:
                for view in dict.fromkeys(views):
                    try:
                        with winreg.OpenKey(
                            hive,
                            key_path,
                            0,
                            winreg.KEY_READ | view,
                        ) as app_path_key:
                            registered, _ = winreg.QueryValueEx(app_path_key, None)
                    except OSError:
                        continue
                    add_candidate(registered)

        if len(candidates) > 1:
            paths = sorted(str(candidate) for candidate in candidates.values())
            raise ManualReviewRequired(
                "multiple Fakturama installations were found; choose one with --fakturama-exe: "
                f"{paths!r}"
            )
        return next(iter(candidates.values()), None)

    def _remember_discovered_executable(self) -> str | None:
        if self._configured_executable is not None:
            return None
        try:
            executable = self._discover_fakturama_executable()
        except GatewayError as exc:
            return str(exc)
        if executable is not None:
            self._configured_executable = executable
        return None

    def _attach_to_window(self, application: Any, window: Any) -> None:
        handle = _safe(window, "handle")
        try:
            if handle is not None:
                application.connect(handle=handle, timeout=self.timeout_seconds)
            else:
                application.connect(
                    title_re=r"(?i).*fakturama.*", timeout=self.timeout_seconds
                )
        except Exception as exc:
            title = self._window_title(window)
            raise GatewayError(
                f"found existing Fakturama window {title!r} but could not attach: {exc}; "
                "a second instance was not started"
            ) from exc
        self._application = application
        self._main_window = window

    @staticmethod
    def _ambiguous_windows_message(windows: Sequence[Any]) -> str:
        titles = [WindowsFakturamaGateway._window_title(window) for window in windows]
        return f"multiple visible Fakturama windows found; select one manually: {titles!r}"

    def _process_exists_for_executable(self, executable: Path) -> bool | None:
        if self._process_checker is not None:
            try:
                return self._process_checker(executable)
            except Exception:
                return None
        return self._default_process_exists_for_executable(executable)

    @staticmethod
    def _default_process_exists_for_executable(executable: Path) -> bool | None:
        """Return None when the process inventory cannot prove the app is stopped."""
        target = executable.resolve()
        install_root = target.parent

        try:
            import psutil
        except ImportError:
            psutil = None

        if psutil is not None:
            uncertain = False
            try:
                # Read names first, then inspect only likely Fakturama runtimes. Asking
                # psutil for exe and command line on every process makes an unrelated
                # protected process turn the whole inventory into an unknown result.
                for process in psutil.process_iter(["name"]):
                    try:
                        name = str(process.info.get("name") or "").casefold()
                    except psutil.AccessDenied:
                        uncertain = True
                        continue

                    if name in {target.name.casefold(), "fakturama.exe"}:
                        return True
                    if name not in {"java.exe", "javaw.exe", "eclipse.exe", "eclipsec.exe"}:
                        continue

                    try:
                        process_path = process.exe()
                        command_line = process.cmdline()
                    except psutil.NoSuchProcess:
                        continue
                    except psutil.AccessDenied:
                        uncertain = True
                        continue

                    if process_path:
                        try:
                            resolved_process = Path(process_path).resolve()
                            if resolved_process == target:
                                return True
                            if resolved_process.is_relative_to(install_root):
                                return True
                        except (OSError, ValueError):
                            uncertain = True
                    normalized_args = [str(argument).casefold() for argument in command_line]
                    root_text = str(install_root).casefold()
                    if any(root_text in argument for argument in normalized_args):
                        return True
                    if any("com.sebulli.fakturama" in arg for arg in normalized_args):
                        return True
            except Exception:
                return None
            return None if uncertain else False

        try:
            import win32api
            import win32con
            import win32process

            uncertain = False

            # EnumProcesses returns only PIDs. Use tasklist to identify candidate images
            # before opening process handles, so unrelated protected services do not make
            # an otherwise complete Fakturama check uncertain.
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=True,
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
            )
            candidates: list[int] = []
            inventory_read = False
            for row in csv.reader(result.stdout.splitlines()):
                if len(row) < 2:
                    continue
                inventory_read = True
                name = row[0].strip().casefold()
                try:
                    process_id = int(row[1].replace(",", "").strip())
                except ValueError:
                    continue
                if name in {target.name.casefold(), "fakturama.exe"}:
                    return True
                if name in {"java.exe", "javaw.exe", "eclipse.exe", "eclipsec.exe"}:
                    candidates.append(process_id)

            if not inventory_read:
                return None

            for process_id in candidates:
                try:
                    handle = win32api.OpenProcess(
                        win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
                    )
                    try:
                        process_path = Path(
                            win32process.QueryFullProcessImageName(handle, 0)
                        ).resolve()
                    finally:
                        win32api.CloseHandle(handle)
                except Exception:
                    uncertain = True
                    continue
                if process_path == target or process_path.is_relative_to(install_root):
                    return True
            return None if uncertain else False
        except Exception:
            return None

    @staticmethod
    def _dpi_diagnostic() -> str:
        aware = WindowsFakturamaGateway._current_process_dpi_awareness()
        if aware is None:
            return "could not determine"
        return "process is DPI aware" if aware else "process is not DPI aware"

    @staticmethod
    def _current_process_dpi_awareness() -> bool | None:
        try:
            import ctypes

            if hasattr(ctypes, "windll"):
                return bool(ctypes.windll.user32.IsProcessDPIAware())
        except Exception:
            pass
        return None

    def preflight(
        self,
        expected_version: str = "2.2.0",
        expected_language: str = "English",
    ) -> PreflightResult:
        self._require_app()
        window = self._main_window
        if window is None:
            raise GatewayError("Fakturama main window is unavailable")
        title = self._window_title(window)
        if "fakturama" not in title.casefold():
            raise ManualReviewRequired(f"active window does not look like Fakturama: {title!r}")
        process_id = int(_safe(window, "process_id", 0) or 0)
        _LOGGER.info("Checking Fakturama version and visible menu language")
        language = "English" if _has_english_menu(window, deep_fallback=True) else None
        version = self._executable_version()
        warnings: list[str] = []
        if version is None:
            raise ManualReviewRequired(
                f"could not confirm Fakturama {expected_version} from the running installation"
            )
        elif not version.startswith(expected_version):
            raise ManualReviewRequired(
                f"expected Fakturama {expected_version}, found executable version {version}"
            )
        if language is None:
            raise ManualReviewRequired(
                f"could not confirm the {expected_language} UI from visible menu labels"
            )
        return PreflightResult(
            application_title=title,
            process_id=process_id,
            version=version,
            language=language,
            dpi_aware=self._dpi_aware,
            warnings=tuple(warnings),
        )

    def open_new_order(self) -> OrderEditorRef:
        # A selector left open by an earlier paused run covers the toolbar.
        # Cancel only this known selector; keep all editor forms untouched.
        selector = self._visible_dialog_root(("Select the address",), required=False)
        if selector is not None:
            _LOGGER.info("Closing the leftover address selector before opening an Order")
            self._close_selector_dialog(("Select the address",), selector)
        window = self._current_window()
        if self._new_order_tab_items(window):
            raise ManualReviewRequired(
                "an unsaved New Order is already open; resume or close that draft "
                "before creating another Order"
            )
        control = self._resolve_toolbar_order(window)
        self._click_control(window, control)
        self._wait_until("open New Order", lambda: (
            len(self._new_order_tab_items(self._current_window())) == 1
            and self._has_order_editor()
            and normalize_label(element_name(self._active_order_editor_tab())) == "neworder"
        ))
        self._order_ref = OrderEditorRef(token=uuid4().hex, number=self._read_optional(("No.",)))
        self._last_source = None
        self._last_order_number = None
        return self._order_ref

    def _resolve_toolbar_order(self, window: Any) -> ResolvedControl:
        """Resolve Order within the top toolbar, excluding document tabs."""
        _LOGGER.info("Bringing Fakturama forward to locate the Order toolbar button")
        self._focus_window_for_automation(window)
        rect = _safe(window, "rectangle")
        if rect is None:
            raise ManualReviewRequired("Fakturama window bounds are unavailable")
        elements = _descendants(window)
        tab_tops = [
            bounds.top
            for element in elements
            if element_type(element) == "TabItem"
            and _safe(element, "is_visible", None) is True
            and (bounds := element_bounds(element)) is not None
        ]
        toolbar_bottom = (
            min(tab_tops) - int(rect.top)
            if tab_tops
            else int((rect.bottom - rect.top) * 0.13)
        )
        ui_matches = [
            element
            for element in elements
            if element_type(element) in {"Button", "MenuItem", "SplitButton"}
            and _safe(element, "is_visible", None) is True
            and _safe(element, "is_enabled", None) is True
            and normalize_label(element_name(element)) in {"order", "createneworder", "createorder"}
            and (bounds := element_bounds(element)) is not None
            and bounds.center[1] - int(rect.top) <= toolbar_bottom
        ]
        query = ControlQuery.one_of(
            "Order", control_types=("Button", "MenuItem", "SplitButton")
        )
        if len(ui_matches) == 1:
            return ResolvedControl(query, element=ui_matches[0])
        if len(ui_matches) > 1:
            raise AmbiguousControl("multiple Order toolbar controls are visible")
        _LOGGER.info("Checking the visible Order toolbar button with OCR")
        try:
            ocr_matches = self.ocr.find_text(self._capture_window(window), ("Order",))
        except OCRUnavailable as exc:
            raise ManualReviewRequired(
                f"the Order toolbar button is not exposed by UI Automation: {exc}"
            ) from exc
        toolbar_matches = [
            match for match in ocr_matches
            if match.bounds.center[1] <= toolbar_bottom
        ]
        if len(toolbar_matches) != 1:
            raise AmbiguousControl(
                f"expected one Order toolbar label, found {len(toolbar_matches)}"
            )
        return ResolvedControl(query, ocr_match=toolbar_matches[0])

    @staticmethod
    def _new_order_tab_items(window: Any) -> list[Any]:
        return [
            element
            for element in _descendants(window)
            if element_type(element) == "TabItem"
            and normalize_label(element_name(element)) == "neworder"
        ]

    def _order_tab_ocr_matches(self, window: Any, count: int) -> list[OCRMatch]:
        """Pair tab labels by horizontal rank; SWT UIA coordinates can be DPI-scaled."""
        try:
            image = self._capture_window(window)
            matches = self.ocr.find_text(image, ("New Order",))
        except OCRUnavailable as exc:
            raise ManualReviewRequired(
                "cannot identify open New Order tabs without OCR"
            ) from exc
        height = getattr(image, "height", None)
        if height:
            matches = [
                match for match in matches
                if match.bounds.center[1] <= int(height * 0.35)
            ]
        matches.sort(key=lambda match: match.bounds.center[0])
        if len(matches) != count:
            raise ManualReviewRequired(
                f"found {count} New Order document tab(s) but {len(matches)} "
                "visible tab label(s); cannot select a draft safely"
            )
        return matches

    def _select_open_order_tab(
        self, window: Any, tab_item: Any, *, ocr_matches: list[OCRMatch]
    ) -> Any:
        """Activate a known tab by its OCR label and verify its exposed Order page."""
        tab_items = self._new_order_tab_items(window)
        tab_items.sort(
            key=lambda item: (
                element_bounds(item).center[0]
                if element_bounds(item) is not None else -1
            )
        )
        if any(element_bounds(item) is None for item in tab_items):
            raise ManualReviewRequired(
                "New Order tab bounds are unavailable for unique selection"
            )
        if len(tab_items) != len(ocr_matches):
            raise ManualReviewRequired(
                "New Order tab count changed while selecting a draft"
            )
        positions = [element_bounds(item).center[0] for item in tab_items]
        if len(set(positions)) != len(positions):
            raise ManualReviewRequired(
                "New Order tabs have indistinguishable horizontal positions"
            )
        target_bounds = element_bounds(tab_item)
        if target_bounds is None:
            raise ManualReviewRequired(
                "the New Order tab bounds changed before selection"
            )
        matching_indices = [
            index
            for index, item in enumerate(tab_items)
            if element_bounds(item) == target_bounds
        ]
        if len(matching_indices) != 1:
            raise ManualReviewRequired(
                "the New Order tab position changed before selection"
            )
        index = matching_indices[0]
        self._focus_window_for_automation(window)
        self._click_control(
            window,
            ResolvedControl(
                ControlQuery.one_of("New Order", allow_ocr=True),
                ocr_match=ocr_matches[index],
            ),
        )
        try:
            return self._active_order_editor_tab()
        except ManualReviewRequired as exc:
            raise ManualReviewRequired(
                "the selected New Order tab did not expose one active Order editor"
            ) from exc

    def discover_open_order(self, source: OrderSource) -> OrderEditorRef | None:
        if self._is_attached():
            for title in ("Select the address", "Select a product"):
                selector = self._visible_dialog_root((title,), required=False)
                if selector is not None:
                    _LOGGER.info("Closing %s to inspect the open Order", title)
                    self._close_selector_dialog((title,), selector)
        matches: list[tuple[Any, Any | None, str, list[OCRMatch] | None]] = []
        for window in self._application_windows():
            order_tabs = self._new_order_tab_items(window)
            if order_tabs:
                _LOGGER.info("Inspecting %s open Order draft tab(s)", len(order_tabs))
                ocr_matches = self._order_tab_ocr_matches(window, len(order_tabs))
                for tab_item in order_tabs:
                    editor = self._select_open_order_tab(
                        window, tab_item, ocr_matches=ocr_matches
                    )
                    reference = self._read_from_window(
                        editor, ("Cust.Ref.", "Customer reference")
                    )
                    number = self._read_from_window(editor, ("No.",))
                    if (
                        reference
                        and number
                        and _normalized_equal(source.external_reference, reference)
                    ):
                        matches.append((window, tab_item, number, ocr_matches))
                continue
            # A standalone Order window has no document TabItem. Scope its
            # readback to that window and retain the original uniqueness rule.
            title = self._window_title(window)
            if "order" not in title.casefold():
                continue
            reference = self._read_from_window(
                window, ("Cust.Ref.", "Customer reference")
            )
            number = self._read_from_window(window, ("No.",))
            if (
                reference
                and number
                and _normalized_equal(source.external_reference, reference)
            ):
                matches.append((window, None, number, None))
        if len(matches) > 1:
            raise ManualReviewRequired(
                f"multiple open Orders match customer reference {source.external_reference!r}"
            )
        if not matches:
            return None
        window, tab_item, number, selected_ocr_matches = matches[0]
        self._main_window = window
        if tab_item is not None:
            assert selected_ocr_matches is not None
            if len(selected_ocr_matches) == 1:
                # The only draft was just selected and checked above. Re-read
                # its fields below without a second OCR pass and tab click.
                editor = self._active_order_editor_tab()
            else:
                refreshed_ocr_matches = self._order_tab_ocr_matches(
                    window, len(selected_ocr_matches)
                )
                editor = self._select_open_order_tab(
                    window, tab_item, ocr_matches=refreshed_ocr_matches
                )
        else:
            editor = window
        active_reference = self._read_from_window(
            editor, ("Cust.Ref.", "Customer reference")
        )
        active_number = self._read_from_window(editor, ("No.",))
        if not (
            active_reference
            and _normalized_equal(source.external_reference, active_reference)
            and active_number == number
        ):
            raise ManualReviewRequired(
                "the selected New Order tab no longer matches the current "
                "customer reference and proposed Order number"
            )
        self._order_ref = OrderEditorRef(token=uuid4().hex, number=number)
        self._last_source = source
        return self._order_ref

    def discover_open_invoice(
        self,
        source: OrderSource,
        order_number: str,
    ) -> InvoiceEditorRef | None:
        try:
            editor, kind, number = self._document_editor_identity()
        except ManualReviewRequired:
            return None
        if kind != "Invoice":
            return None
        reference = self._read_from_window(editor, ("Cust.Ref.",), allow_ocr=False)
        if reference != source.external_reference:
            return None
        linked = self._invoice_link(number, editor)
        if linked != order_number:
            raise ManualReviewRequired("the open Invoice has no verified source Order provenance")
        self._last_source = source
        self._last_order_number = order_number
        if self._invoice_ref is None:
            self._invoice_ref = InvoiceEditorRef(
                token=uuid4().hex, number=number, linked_order_number=linked,
            )
        return InvoiceEditorRef(
            token=self._invoice_ref.token, number=number, linked_order_number=linked,
            proposed_invoice_date=self._read_optional(("Date",)),
            proposed_service_date=self._read_optional(("Service date",)),
        )

    def restore_invoice_context(self, provenance: Any, source: OrderSource) -> None:
        """Restore only provenance recorded after an observed follow-up creation."""
        self._last_source = source
        self._last_order_number = provenance.source_order_number
        self._invoice_ref = InvoiceEditorRef(
            token=uuid4().hex, number=provenance.invoice_number,
            linked_order_number=provenance.source_order_number,
            proposed_invoice_date=provenance.proposed_invoice_date,
            proposed_service_date=provenance.proposed_service_date,
        )

    def _invoice_link(self, number: str, editor: Any) -> str | None:
        if self._invoice_ref and self._invoice_ref.number == number:
            return self._invoice_ref.linked_order_number
        return self._read_optional(("Order No.", "Order number", "Follow-up from"), scope=editor)

    def order_is_open(self, ref: OrderEditorRef) -> bool:
        if not self._is_attached():
            return False
        if self._order_ref and self._order_ref.token == ref.token:
            return self._has_order_editor()
        if ref.number:
            return any(
                "order" in self._window_title(window).casefold()
                and _normalized_equal(ref.number, self._read_from_window(window, ("No.",)))
                for window in self._application_windows()
            )
        return self._has_order_editor()

    def ensure_currency(self, currency: str, *, allow_change: bool = False) -> None:
        from faktura_pilot.automation.currency import ensure_currency

        ensure_currency(self, currency, allow_change=allow_change)

    def fill_order_header(self, source: OrderSource) -> None:
        self._require_editor("order")
        self._set_date_field(("Date",), _date_text(source.order_date))
        self._set_field(("Cust.Ref.", "Customer reference"), source.external_reference)
        self._select_combo_by_current_value(
            {"Gross", "Net"},
            "Net",
            "Order price mode",
            labels=("Price mode", "Document price mode"),
        )
        self._select_combo_by_current_value(
            {"With VAT", "Without VAT"},
            "With VAT",
            "VAT mode",
            labels=("VAT mode", "Tax mode"),
        )
        self._last_source = source
        expected_fields = {
            "Date": _date_text(source.order_date),
            "Cust.Ref.": source.external_reference,
        }
        field_labels = {
            "Date": ("Date",),
            "Cust.Ref.": ("Cust.Ref.", "Customer reference"),
        }
        # No. is proposed by Fakturama when the editor opens. Leave it alone,
        # then verify that header entry did not change the proposed value.
        if self._order_ref is not None and self._order_ref.number:
            expected_fields["No."] = self._order_ref.number
            field_labels["No."] = ("No.",)
        self._verify_fields(
            expected_fields,
            labels=field_labels,
            step="Order header",
        ).require_verified("Order header")

    def open_debtor_selector(self) -> None:
        self._open_order_address_selector("Invoice address")

    def _open_order_address_selector(self, tab_label: str) -> None:
        selector = self._visible_dialog_root(("Select the address",), required=False)
        if selector is not None:
            self._close_selector_dialog(("Select the address",), selector)
        self._require_editor("order")
        self._activate_order_address_tab(tab_label)
        # The upper, unnamed Image inside the Order's Addresses pane opens the
        # selector for the address tab that was explicitly activated above.
        self._click_addresses_image("selector")
        self._wait_until(
            f"the {tab_label} address selector to open",
            lambda: self._visible_dialog_root(("Select the address",), required=False)
            is not None,
        )

    def find_debtors(self, query: str) -> list[DebtorCandidate]:
        dialog = self._visible_dialog_root(("Select the address",))
        search_term = query.strip()
        if not search_term:
            raise ManualReviewRequired(
                "cannot search the address selector because the extracted Company or customer "
                "name is blank"
            )
        self._set_field(("Search",), search_term, window=dialog)
        rows = self._wait_stable_rows(window=dialog)
        if not rows:
            return self._find_debtors_from_visible_table(dialog, search_term)

        candidates: list[DebtorCandidate] = []
        for row in rows:
            fields = self._debtor_candidate_fields(row)
            company = fields["company"]
            first_name = fields["first_name"]
            last_name = fields["last_name"]
            zip_code = fields["zip_code"]
            city = fields["city"]
            unreadable = [
                name
                for name in ("company", "first_name", "last_name", "zip_code", "city")
                if fields[name] is None
            ]
            if unreadable or not company or not zip_code or not city:
                if fields["row"]:
                    detail = (
                        f"missing visible identity columns: {', '.join(unreadable)}"
                        if unreadable
                        else "company, ZIP, or city is blank"
                    )
                    raise ManualReviewRequired(
                        "Debtor selector returned a row with unreadable identity data "
                        f"({detail}); resolve it manually instead of risking a wrong match "
                        "or duplicate"
                    )
                continue
            candidates.append(
                DebtorCandidate(
                    token=self._token(fields),
                    company=company,
                    first_name=first_name or None,
                    last_name=last_name or None,
                    zip_code=zip_code,
                    city=city,
                    billing_address=fields["billing_address"],
                    delivery_address=fields["delivery_address"],
                    number=fields["number"],
                )
            )
        return candidates

    def select_debtor(self, candidate: DebtorCandidate) -> None:
        _LOGGER.info("Selecting Invoice address for Debtor %s", candidate.company)
        self._select_address_candidate(candidate)
        _LOGGER.info("Searching Delivery address for Debtor %s", candidate.company)
        self._open_order_address_selector("Delivery address")
        candidates = self.find_debtors(candidate.company)
        matches = [
            current for current in candidates
            if self._same_debtor_identity(candidate, current)
        ]
        if len(matches) != 1:
            raise ManualReviewRequired(
                "the delivery selector did not expose one exact previously selected Debtor; "
                "the delivery address was not chosen"
            )
        _LOGGER.info("Selecting Delivery address for Debtor %s", candidate.company)
        self._select_address_candidate(matches[0])

    @staticmethod
    def _same_debtor_identity(left: DebtorCandidate, right: DebtorCandidate) -> bool:
        if left.number and right.number and left.number != right.number:
            return False
        return all(
            normalize_label(str(getattr(left, field) or ""))
            == normalize_label(str(getattr(right, field) or ""))
            for field in ("company", "first_name", "last_name", "zip_code", "city")
        )

    def _select_address_candidate(self, candidate: DebtorCandidate) -> None:
        dialog = self._visible_dialog_root(("Select the address",))
        token_data = json.loads(candidate.token)
        if token_data.get("source") == "ocr-debtor-row":
            visible_rows = self._ocr_debtor_table_rows(dialog, candidate.company)
            expected_company_prefix = normalize_label(
                token_data.get("visible_company", "").rstrip(" .…")
            )
            matches = [
                row
                for row in visible_rows
                if row["first_name"] == candidate.first_name
                and row["last_name"] == candidate.last_name
                and row["zip_code"] == candidate.zip_code
                and row["city"] == candidate.city
                and normalize_label(row["visible_company"].rstrip(" .…"))
                == expected_company_prefix
            ]
            if len(matches) != 1:
                raise ManualReviewRequired(
                    "the visible customer row changed or is no longer unique; refusing to select it"
                )
            target = ResolvedControl(
                ControlQuery.one_of("visible customer row", allow_ocr=False),
                ocr_match=OCRMatch("visible customer row", matches[0]["bounds"]),
            )
            self._click_control(dialog, target)
        else:
            row = self._find_row_by_token(
                candidate.token, self._debtor_row_token, window=dialog
            )
            self._click_element(row)
        self._click_if_present(
            ("OK", "Select", "Use"), control_types=("Button",), window=dialog
        )
        self._wait_until(
            "the address selector to close",
            lambda: self._visible_dialog_root(
                ("Select the address",), required=False
            ) is None,
        )

    def open_new_debtor(self) -> None:
        selector = self._visible_dialog_root(("Select the address",), required=False)
        if selector is not None:
            self._close_selector_dialog(("Select the address",), selector)

        if self._activate_existing_new_debtor():
            return

        self._require_editor("order")
        # The creation branch uses New Contact in the left New panel.
        self._click(ControlQuery.one_of("New Contact"))
        self._wait_until(
            "the new Debtor form to open",
            lambda: (
                self._has_edit_field(("Company", "Company name"))
                and self._has_any_labels("Main address", "Addresses")
            ),
        )

    def fill_debtor(self, debtor: Debtor) -> None:
        _LOGGER.info("Entering Debtor identity and contact fields")
        self._require_any_labels("Main address", "Addresses", "Payment")
        self._activate_debtor_tab("Main address")
        self._billing_address_tab_label = "Main address"
        editor = self._active_editor_tab()
        self._set_field(("Company", "Company name"), debtor.company, scope=editor)
        try:
            self._set_composite_row(
                "First Name Last Name",
                (debtor.first_name, debtor.last_name),
                scope=editor,
            )
        except ElementNotFound:
            for labels, value in (
                (("First Name", "First name"), debtor.first_name),
                (("Last Name", "Last name"), debtor.last_name),
            ):
                if value is not None:
                    self._set_field(labels, value)
        for labels, value in (
            (("Email", "E-mail"), debtor.email),
            (("Telephone", "Phone"), debtor.telephone),
        ):
            if value is not None:
                self._set_field(labels, value)

        _LOGGER.info("Entering the Debtor billing address")
        self._fill_address(debtor.billing_address, context=("Main address",))
        same_address = self._addresses_equal(debtor.billing_address, debtor.delivery_address)
        self._assign_address_roles(
            ("Invoice address", "Delivery address") if same_address else ("Invoice address",)
        )

        _LOGGER.info("Entering the Debtor delivery address")
        if same_address:
            self._delivery_address_tab_label = "Main address"
        else:
            self._delivery_address_tab_label = self._open_or_create_delivery_address()
            delivery_scope = self._fill_address(
                debtor.delivery_address, context=(self._delivery_address_tab_label,)
            )
            for labels, value in (
                (("Email", "E-mail"), debtor.email),
                (("Telephone", "Phone"), debtor.telephone),
            ):
                if value is not None:
                    self._set_field(labels, value, scope=delivery_scope)
            self._assign_address_roles(("Delivery address",))

        _LOGGER.info("Checking Debtor details and payment settings")
        self._activate_debtor_tab("Miscellaneous")
        if debtor.alias is not None:
            self._set_field(("Alias name", "Alias"), debtor.alias)
        self._set_field(("Discount", "Discount %", "Discount [%]"), "0")
        self._select_named_option(
            ("Net or Gross", "Price mode", "Pricing", "Price calculation"),
            "Net",
            optional=False,
        )

    def find_payment_methods(self, query: str) -> list[PaymentMethodCandidate]:
        from faktura_pilot.automation.payment_terms import find
        _LOGGER.info("Searching terms of payment for exact name %s", query)
        return find(self, query)

    def create_payment_method(self, name: str, code: str) -> PaymentMethodCandidate:
        from faktura_pilot.automation.payment_terms import create
        _LOGGER.info("Creating terms of payment %s with code %s", name, code)
        return create(self, name, code)

    def _debtor_payment_options(self) -> list[str] | None:
        control = self._scoped_resolver(self._active_editor_tab()).resolve_edit(
            ControlQuery.one_of("Payment", allow_ocr=False)
        ).element
        if element_type(control) != "ComboBox":
            raise ManualReviewRequired("Debtor Payment is not a dropdown")
        handle = _safe(control, "handle")
        if os.name != "nt" or not handle:
            return None
        try:
            from pywinauto import Desktop

            native = Desktop(backend="win32").window(handle=int(handle)).wrapper_object()
            # Win32 texts() includes the currently selected value before the list.
            values = list(native.texts())[1:]
            if len(values) != native.item_count() or any(not str(v).strip() for v in values):
                raise ManualReviewRequired("Payment dropdown options could not be read completely")
            return [str(value).strip() for value in values]
        except ManualReviewRequired:
            raise
        except Exception as exc:
            raise ManualReviewRequired(f"could not read Payment dropdown options: {exc}") from exc

    def select_payment_method(self, candidate: PaymentMethodCandidate) -> None:
        self._require_any_labels("Payment", "Payment method", "Terms of payment")
        selected = self._select_named_option(
            ("Payment", "Payment method", "Terms of payment", "Payment terms"),
            candidate.name,
            optional=False,
        )
        if not selected:
            raise PostconditionFailed(f"could not select payment method {candidate.name!r}")

    def save_debtor(self) -> DebtorCandidate:
        _LOGGER.info("Verifying Debtor Company and both addresses before Save")
        debtor = self._last_source.debtor if self._last_source else None
        if debtor is None:
            raise GatewayError(
                "Debtor save requires the source order to be associated with this run"
            )
        editor = self._active_editor_tab()
        company = self._read_optional(("Company", "Company name"), scope=editor)
        if company is None or not _normalized_equal(company, debtor.company):
            raise PostconditionFailed(
                f"Debtor Company before Save: expected {debtor.company!r}, observed {company!r}"
            )
        billing_tab = self._billing_address_tab_label or "Main address"
        delivery_tab = self._delivery_address_tab_label or billing_tab
        for tab_label in dict.fromkeys((billing_tab, delivery_tab)):
            scope = self._address_form_scope((tab_label,))
            for labels, expected in (
                (("Email", "E-mail"), debtor.email),
                (("Telephone", "Phone"), debtor.telephone),
            ):
                if expected is None:
                    continue
                observed = self._read_optional(labels, scope=scope)
                if observed is None or not _normalized_equal(expected, observed):
                    raise PostconditionFailed(
                        f"{tab_label} {labels[0]} before Save: expected {expected!r}, "
                        f"observed {observed!r}"
                    )
        self._verify_address_fields(debtor.billing_address, (billing_tab,)).require_verified(
            "Debtor invoice address before save"
        )
        self._verify_address_fields(
            debtor.delivery_address, (delivery_tab,)
        ).require_verified("Debtor delivery address before save")
        _LOGGER.info("Saving the Debtor with the top toolbar Save button")
        self._dispatch_save("save Debtor")
        try:
            self._wait_until(
                "the Debtor editor to close",
                lambda: (
                    self._dialog_is_open(("Select the address", "Search", "New Contact"))
                    or self._has_order_editor()
                ),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                "Debtor Save was activated but the original selector or Order was not restored; "
                "inspect before retrying"
            ) from exc
        address_selector = self._visible_dialog_root(
            ("Select the address",), required=False
        )
        if address_selector is not None:
            self._close_selector_dialog(("Select the address",), address_selector)
        return DebtorCandidate(
            token=self._token(
                {
                    "company": debtor.company,
                    "first_name": debtor.first_name,
                    "last_name": debtor.last_name,
                    "zip_code": debtor.billing_address.zip_code,
                    "city": debtor.billing_address.city,
                }
            ),
            company=debtor.company,
            first_name=debtor.first_name,
            last_name=debtor.last_name,
            zip_code=debtor.billing_address.zip_code,
            city=debtor.billing_address.city,
            billing_address=self._address_string(debtor.billing_address),
            delivery_address=self._address_string(debtor.delivery_address),
        )

    def return_to_order(self, ref: OrderEditorRef) -> None:
        if not self._order_ref or self._order_ref.token != ref.token:
            if ref.number is None:
                raise ManualReviewRequired(
                    "cannot rediscover the original open Order without its number"
                )
        self._activate_document_tab("New Order", ref.number)
        if not self.order_is_open(ref):
            raise PostconditionFailed("the original Order editor could not be restored")

    def verify_order_debtor(self, debtor: Debtor) -> VerificationResult:
        billing_display = self._read_order_address_display("Invoice address")
        delivery_display = self._read_order_address_display("Delivery address")
        expected = {
            "company": debtor.company,
            "billing_address": self._address_string(debtor.billing_address),
            "delivery_address": self._address_string(debtor.delivery_address),
        }
        observed = {
            "company": billing_display,
            "billing_address": billing_display,
            "delivery_address": delivery_display,
        }
        errors = []
        if observed["company"] and normalize_label(debtor.company) not in normalize_label(
            observed["company"]
        ):
            errors.append("selected Debtor company does not match the source")
        elif not observed["company"]:
            errors.append("selected Debtor company could not be read back")
        for key, address in (
            ("billing_address", debtor.billing_address),
            ("delivery_address", debtor.delivery_address),
        ):
            observed_address = observed[key]
            if observed_address is None:
                errors.append(f"selected {key.replace('_', ' ')} could not be read back")
            else:
                # The Order preview shows the debtor, street, postal locality, and
                # country, but omits the address's additional-name field. That field
                # is verified in the saved Debtor address editor.
                expected_components = (
                    address.street,
                    address.zip_code,
                    address.city,
                    address.country,
                    address.address_specification,
                    address.district,
                )
                if not all(
                    normalize_label(value) in normalize_label(observed_address)
                    for value in expected_components
                    if value
                ):
                    errors.append(f"selected {key.replace('_', ' ')} differs from the source")
        result = VerificationResult(
            verified=not errors,
            observations=tuple(errors) or ("Debtor and both addresses match",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence("debtor-verification-failed")
        return result

    def find_vats(self, rate: Decimal) -> list[VatCandidate]:
        self._open_data_manager("VATs")
        manager = self._data_manager_tab("VATs")
        if manager is None:
            raise ManualReviewRequired("the VAT manager is not active")
        expected_name = f"VAT {format(rate.normalize(), 'f')}%"
        _LOGGER.info("Searching VATs for exact name %s", expected_name)
        self._set_field(("Search",), expected_name, window=manager)
        rows = self._wait_stable_rows(window=manager)
        if not rows:
            return self._ocr_vat_candidates(manager, expected_name)
        candidates: list[VatCandidate] = []
        for row in rows:
            values, row_text = self._row_values(row)
            name = self._value(values, "Name")
            amount = _parse_decimal(self._value(values, "Value", "VAT", "Rate", "Percentage") or "")
            code = _e_invoice_code(
                self._value(values, "E-Invoice code", "E-Invoice Code", "Code") or ""
            )
            if name is None and amount is None:
                continue
            if name is None or amount is None or not code:
                self._close_dialog_if_present(("VAT", "VATs"))
                raise ManualReviewRequired(
                    "VAT manager did not expose name, percentage, and E-Invoice code for every row"
                )
            candidates.append(
                VatCandidate(
                    token=self._token(
                        {"name": name, "value_percent": str(amount), "code": code, "row": row_text}
                    ),
                    name=name,
                    value_percent=amount,
                    e_invoice_code=code,
                )
            )
        self._close_dialog_if_present(("VAT", "VATs"))
        return candidates

    def create_vat(self, rate: Decimal) -> VatCandidate:
        self._open_data_manager("VATs")
        expected_name = f"VAT {format(rate.normalize(), 'f')}%"
        existing = self.find_vats(rate)
        exact = [candidate for candidate in existing if candidate.name == expected_name]
        if exact:
            if (len(exact) == 1 and exact[0].value_percent == rate
                    and exact[0].e_invoice_code == "S"):
                return exact[0]
            raise ManualReviewRequired(
                f"VAT {expected_name!r} exists with a conflicting definition"
            )
        self._click_action(
            ControlQuery.one_of(
                "Create a new tax rate", "New VAT", "Add", control_types=("Button",)
            ),
            "create a VAT rate",
            lambda: (
                self._has_edit_field(("Name",))
                and self._has_edit_field(("Value", "VAT", "Percentage"))
            ),
        )
        self._set_field(("Name",), expected_name)
        self._set_field(("Description",), expected_name, optional=True)
        self._select_named_option(
            ("VAT code (E-Invoice)", "E-Invoice code", "E-Invoice Code", "Code"),
            "S (Standard rate)", optional=False
        )
        # Selecting a tax code can reset the percentage. Enter it last.
        self._set_field(("Value", "VAT", "Percentage"), _decimal_text(rate))
        self._dispatch_save("save VAT rate")
        try:
            self._wait_until(
                f"VAT {expected_name!r} to appear in the VAT manager",
                lambda: self._manager_contains_vat(expected_name, rate),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                f"VAT Save was activated but {expected_name!r} could not be confirmed "
                "in the manager"
            ) from exc
        self._close_dialog_if_present(("VAT", "VATs"))
        return VatCandidate(
            token=self._token({"name": expected_name, "rate": str(rate), "code": "S"}),
            name=expected_name,
            value_percent=rate,
            e_invoice_code="S",
        )

    def find_products(self, sku: str) -> list[ProductCandidate]:
        self._ensure_product_search_preferences()
        dialog = self._visible_dialog_root(("Select a product",), required=False)
        if dialog is None:
            self._require_editor("order")
            self.open_product_selector()
            dialog = self._visible_dialog_root(("Select a product",))

        _LOGGER.info("Searching the Product selector for exact SKU %s", sku)
        # Typing filters immediately; Enter would accept a row and must not be
        # used until an exact SKU and VAT have been verified.
        self._set_field(("Search",), sku, window=dialog)
        if self._visible_dialog_root(("Select a product",), required=False) is None:
            raise ActionOutcomeUnknown("Product selector closed unexpectedly while searching")
        rows = self._wait_stable_rows(window=dialog)
        candidates: list[ProductCandidate] = []
        for row in rows:
            values, row_text = self._row_values(row)
            item_number = self._value(
                values, "Item Number", "Item number", "SKU", "Product number", "Number"
            )
            if not item_number:
                if row_text:
                    raise ManualReviewRequired(
                        "Product selector does not expose an exact SKU through UIA"
                    )
                continue
            vat = _parse_decimal(self._value(values, "VAT", "VAT rate", "Tax rate") or "")
            name = self._value(values, "Name", "Description") or None
            candidates.append(
                ProductCandidate(
                    token=self._token(
                        {"sku": item_number, "name": name, "vat": str(vat), "row": row_text}
                    ),
                    sku=item_number,
                    name=name,
                    vat_rate_percent=vat,
                )
            )
        if not rows:
            candidate = self._ocr_product_candidate(dialog, sku)
            if candidate is not None:
                candidates.append(candidate)
        if not any(candidate.sku.strip() == sku.strip() for candidate in candidates):
            # Return to the same Order before consulting Data > VATs and opening
            # a Product form from New > New product.
            self._close_selector_dialog(("Select a product",), dialog)
        return candidates

    def _ocr_product_candidate(self, dialog: Any, sku: str) -> ProductCandidate | None:
        """Read one exact visible Product row when SWT exposes no UIA rows."""
        if not isinstance(self.ocr, TesseractOCR):
            raise ManualReviewRequired(
                "the Product selector has no accessible rows and the configured OCR engine "
                "cannot read its visible table"
            )

        self._focus_window_for_automation(dialog)
        image = self._capture_window(dialog)
        try:
            raw = self.ocr.read_text_data(image, config="--psm 11")
        except OCRUnavailable as exc:
            raise ManualReviewRequired(
                f"could not read the visible Product row for exact SKU {sku!r}: {exc}"
            ) from exc
        headers = [
            match
            for match in self.ocr.find_text_in_data(raw, ("Item No.",))
            if normalize_label(match.text) == normalize_label("Item No.")
        ]
        if len(headers) != 1:
            raise ManualReviewRequired(
                "the Product selector does not expose one visible Item No. column header"
            )
        header = headers[0]
        sku_matches = [
            match
            for match in self.ocr.find_text_in_data(raw, (sku,), exact=True)
            if match.bounds.top >= header.bounds.bottom
            and header.bounds.left - 30 <= match.bounds.center[0] <= header.bounds.right + 240
            and match.bounds.center[1] < image.height - 120
        ]
        if not sku_matches:
            return None
        if len(sku_matches) != 1:
            raise ManualReviewRequired(
                f"the Product selector shows multiple visible rows for exact SKU {sku!r}"
            )
        sku_match = sku_matches[0]
        item_header_center_y = header.bounds.center[1]
        vat_headers = [
            match
            for match in self.ocr.find_text_in_data(raw, ("VAT",))
            if match.bounds.center[0] > image.width * 0.70
            and abs(match.bounds.center[1] - item_header_center_y) <= 12
        ]
        if len(vat_headers) != 1:
            raise ManualReviewRequired(
                "the Product selector does not expose one visible VAT column header"
            )
        vat_column_left = vat_headers[0].bounds.left - 8
        row_center_y = sku_match.bounds.center[1]
        row_words: list[str] = []
        for index, value in enumerate(raw.get("text", [])):
            word = str(value).strip()
            if not word:
                continue
            left = int(raw.get("left", [0])[index])
            top = int(raw.get("top", [0])[index])
            height = int(raw.get("height", [0])[index])
            center_y = top + height // 2
            if left >= vat_column_left and abs(center_y - row_center_y) <= 14:
                row_words.append(word)
        percentages = re.findall(
            r"\(?\s*(\d+(?:[.,]\d+)?)\s*%\s*\)?",
            " ".join(row_words),
        )
        # The cell can display both the name and rate: VAT 19% (19.0%).
        # Repeated equivalent numbers agree; conflicting percentages do not.
        rates = {_parse_decimal(value) for value in percentages}
        if len(rates) != 1 or None in rates:
            raise ManualReviewRequired(
                f"the visible VAT rate for Product {sku!r} could not be read uniquely"
            )
        vat = next(iter(rates))
        if vat is None:
            raise ManualReviewRequired(
                f"the visible VAT rate for Product {sku!r} is unreadable"
            )

        return ProductCandidate(
            token=self._token(
                {
                    "source": "ocr-product-row",
                    "sku": sku,
                    "vat": str(vat),
                    "bounds": asdict(sku_match.bounds),
                }
            ),
            sku=sku,
            vat_rate_percent=vat,
        )

    def open_product_selector(self) -> None:
        self._require_editor("order")
        self._open_product_selector()

    def select_product(self, candidate: ProductCandidate) -> None:
        dialog = self._visible_dialog_root(("Select a product",))
        token_data = json.loads(candidate.token)
        if token_data.get("source") == "ocr-product-row":
            visible = self._ocr_product_candidate(dialog, candidate.sku)
            if (
                visible is None
                or visible.sku.strip() != candidate.sku.strip()
                or visible.vat_rate_percent != candidate.vat_rate_percent
            ):
                raise ManualReviewRequired(
                    "the visible Product row changed since lookup; refusing to select it"
                )
            visible_bounds = json.loads(visible.token)["bounds"]
            target = ResolvedControl(
                ControlQuery.one_of("visible exact Product SKU", allow_ocr=False),
                ocr_match=OCRMatch(candidate.sku, Bounds(**visible_bounds)),
            )
            self._click_control(dialog, target)
        else:
            row = self._find_row_by_token(
                candidate.token, self._product_row_token, window=dialog
            )
            self._click_element(row)
        self._click_if_present(
            ("OK", "Select", "Use"), control_types=("Button",), window=dialog
        )
        self._wait_until(
            "the Product selector to close",
            lambda: self._visible_dialog_root(
                ("Select a product",), required=False
            ) is None,
        )

    def open_new_product(self) -> None:
        window = self._main_window if self._main_window is not None else self._current_window()
        drafts = [element for element in _descendants(window)
                  if element_type(element) == "TabItem"
                  and normalize_label(element_name(element)) == "newproduct"]
        if len(drafts) > 1:
            raise ManualReviewRequired("multiple unsaved Product forms are open")
        if drafts:
            image = self._capture_window(window)
            matches = [match for match in self.ocr.find_text(image, ("New product",))
                       if match.bounds.top < image.height / 3]
            if len(matches) != 1:
                raise ManualReviewRequired("the existing Product draft tab is ambiguous")
            self._click_control(window, ResolvedControl(
                ControlQuery.one_of("New product"), ocr_match=matches[0]
            ))
            self._wait_until("the existing Product draft", lambda: self._has_edit_field(
                ("Item Number", "Item number")
            ))
            return
        selector = self._visible_dialog_root(("Select a product",), required=False)
        if selector is not None:
            self._close_selector_dialog(("Select a product",), selector)
        self._click_action(
            ControlQuery.one_of("Create a new product", control_types=("Button",), allow_ocr=False),
            "open a new Product form",
            lambda: any(
                element_type(element) == "Tab"
                and normalize_label(element_name(element)) == "newproduct"
                and self._visible_enabled(element)
                for element in _descendants(self._current_window())
            ),
        )

    def fill_product(self, item: Item, vat: VatCandidate) -> None:
        self._require_any_labels("Item Number", "Price (gross)", "cost price (net)")
        previous_sku = self._read_optional(("Item Number", "Item number"))
        if previous_sku and previous_sku != item.sku:
            raise ManualReviewRequired("the open Product draft belongs to a different SKU")
        price = product_gross_price(item.unit_net_price, item.vat_rate_percent)
        # VAT selection can recalculate the form, so snapshot only afterwards.
        self._select_named_option(("VAT", "VAT rate"), vat.name, optional=False)
        field_resolver = self._resolver().freeze()
        for labels, value in (
            (("Item Number", "Item number"), item.sku),
            (("Name",), item.description),
            (("Description",), item.description),
            (("Price (gross)", "Price gross"), _decimal_text(price)),
            (("cost price (net)", "Cost price (net)", "Cost price"), "0.00"),
            (("Stock",), "0.00"),
        ):
            self._set_field(labels, value, resolver=field_resolver)
        # Verify from a fresh tree after the writes, keeping readback independent.
        self._verify_fields(
            {
                "Item Number": item.sku,
                "Name": item.description,
                "Description": item.description,
                "Price (gross)": _decimal_text(price),
                "cost price (net)": "0.00",
                "VAT": vat.name,
                "Stock": "0.00",
            },
            labels={
                "Item Number": ("Item Number", "Item number"),
                "Price (gross)": ("Price (gross)", "Price gross"),
                "cost price (net)": (
                    "cost price (net)",
                    "Cost price (net)",
                    "Cost price",
                ),
                "VAT": ("VAT", "VAT rate"),
            },
            step=f"Product {item.sku}",
        ).require_verified(f"Product {item.sku}")

    def save_product(self) -> ProductCandidate:
        resolver = self._resolver().freeze()
        sku = self._read_with_resolver(resolver, ControlQuery.one_of("Item Number", "Item number"))
        name = self._read_with_resolver(resolver, ControlQuery.one_of("Name"))
        if not sku:
            raise PostconditionFailed("cannot save Product because Item Number is empty")
        expected_saved = {label: self._read_with_resolver(
            resolver, ControlQuery.one_of(label)
        ) for label in (
            "Item Number", "Name", "Description", "Price (gross)",
            "cost price (net)", "VAT", "Stock"
        )}
        if any(value is None for value in expected_saved.values()):
            raise PostconditionFailed("Product fields are not readable before Save")
        self._dispatch_save(f"save Product {sku}")
        self._verify_fields(expected_saved, step=f"saved Product {sku}").require_verified(
            f"saved Product {sku}"
        )
        try:
            self._wait_until(
                "the Product editor to close",
                lambda: (
                    self._dialog_is_open(("Select a product", "Search", "New product"))
                    or self._has_order_editor()
                ),
            )
        except TransitionTimeout as exc:
            raise ActionOutcomeUnknown(
                f"Product {sku} Save was activated but its editor did not close; "
                "inspect before retrying"
            ) from exc
        product_selector = self._visible_dialog_root(
            ("Select a product",), required=False
        )
        if product_selector is not None:
            self._close_selector_dialog(("Select a product",), product_selector)
        return ProductCandidate(
            token=self._token({"sku": sku, "name": name}),
            sku=sku,
            name=name,
        )

    def fill_order_line(self, item: Item) -> VerificationResult:
        self._require_editor("order")
        try:
            row = self._wait_for_order_line_row(item.sku)
        except ElementNotFound:
            # Fakturama's SWT table is custom-drawn and exposes only a Pane in
            # UIA. If the exact visible SKU is confirmed by OCR, edit its row
            # using the visible cell boundaries and verify every numeric cell.
            _LOGGER.info(
                "Order row %s is not exposed by UI Automation; checking the visible grid",
                item.sku,
            )
            return self._fill_order_line_ocr(item)
        expected_values = self._expected_line_values(item)
        observations: list[str] = []
        for column, value in expected_values.items():
            cell = self._row_cell(row, column)
            if cell is None:
                observations.append(
                    f"Order line column {column!r} is not exposed through UI Automation"
                )
                continue
            self._write_element(cell, value)
            actual = self._element_value(cell)
            if actual is None or not self._equivalent_value(column, value, actual):
                observations.append(f"{column}: expected {value}, observed {actual!r}")
        result = VerificationResult(
            verified=not observations,
            observations=tuple(observations) or (f"Product {item.sku} line fields match",),
            expected=expected_values,
            observed={
                column: self._element_value(cell)
                if (cell := self._row_cell(row, column)) is not None
                else None
                for column in expected_values
            },
        )
        if not result.verified:
            self.capture_evidence(f"line-verification-{item.sku}")
        return result

    def verify_order_line(self, item: Item, item_index: int) -> VerificationResult:
        if item_index < 0:
            raise ValueError("item_index must be zero or greater")
        self._require_editor("order")
        try:
            row = self._wait_for_order_line_row(item.sku)
        except ElementNotFound:
            _LOGGER.info(
                "Order row %s is not exposed by UI Automation; verifying the visible grid",
                item.sku,
            )
            return self._verify_order_line_ocr(item)
        expected_values = self._expected_line_values(item)
        observed: dict[str, str | None] = {}
        issues: list[str] = []
        for column, expected in expected_values.items():
            cell = self._row_cell(row, column)
            actual = self._element_value(cell) if cell is not None else None
            observed[column] = actual
            if actual is None or not self._equivalent_value(column, expected, actual):
                issues.append(f"{column}: expected {expected}, observed {actual!r}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or (f"Order line {item_index + 1} for {item.sku} matches",),
            expected=expected_values,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence(f"line-resume-verification-{item.sku}")
        return result

    @staticmethod
    def _expected_line_values(item: Item) -> dict[str, str]:
        return {
            "Qty.": _decimal_text(item.quantity),
            "U.Price": _decimal_text(item.unit_net_price),
            "VAT": _decimal_text(item.vat_rate_percent),
            "Discount": _decimal_text(item.discount_percent),
            "Price": _decimal_text(
                expected_line_net(item.quantity, item.unit_net_price, item.discount_percent)
            ),
        }

    def prepare_order_adjustments(self, source: OrderSource) -> None:
        if source.shipping:
            from faktura_pilot.automation.shipping import ensure
            ensure(self, source.shipping)

    def apply_order_adjustments(self, source: OrderSource) -> VerificationResult:
        from faktura_pilot.automation.adjustments import apply
        return apply(self, source)

    def verify_order_adjustments(self, source: OrderSource) -> VerificationResult:
        from faktura_pilot.automation.adjustments import verify
        return verify(self, source)

    def verify_order_totals(self, source: OrderSource) -> VerificationResult:
        expected = {
            # Fakturama's Total Net control is the goods subtotal before the
            # overall rebate and shipping, not DocumentSummary.totalNet.
            "net": sum((expected_line_net(item.quantity, item.unit_net_price,
                                          item.discount_percent) for item in source.items),
                       Decimal("0")),
            "vat": source.totals.vat,
            "gross": source.totals.gross,
        }
        labels = {
            "net": ("Total Net", "Net total", "Net"),
            "vat": ("VAT total", "VAT"),
            "gross": ("Gross total", "Total", "Amount due"),
        }
        observed: dict[str, Decimal | None] = {}
        issues: list[str] = []
        for key, amount in expected.items():
            raw = self._read_optional(labels[key], control_types=("Edit",))
            parsed = _parse_decimal(raw or "")
            observed[key] = parsed
            if parsed is None:
                issues.append(f"{key} total is not accessible for readback")
            elif abs(parsed - amount) > (
                Decimal("0") if source.order_discount_percent or source.shipping
                else _MONEY_TOLERANCE
            ):
                issues.append(f"{key} total expected {amount:.2f}, observed {parsed:.2f}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("Order totals match the source",),
            expected={key: _decimal_text(value) for key, value in expected.items()},
            observed={
                key: _decimal_text(value) if value is not None else None
                for key, value in observed.items()
            },
        )
        if not result.verified:
            self.capture_evidence("order-totals-verification-failed")
        return result

    def save_order(self) -> str:
        if self._is_attached():
            self._focus_order_header_for_grid(self._active_order_editor_tab())
        number = self._read_optional(("No.",))
        if not number:
            raise PostconditionFailed("Order has no visible generated No. before Save")
        self._dispatch_save(f"save Order {number}")
        self._confirm_saved_document(number, "Order")
        self._last_order_number = number
        return number

    def _confirm_saved_document(
        self, number: str, kind: str, linked_order_number: str | None = None,
    ) -> None:
        """Retry only fresh readback after the one dispatched Save, never Save itself."""
        source = self._require_source()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                if linked_order_number is None:
                    rows = self.find_documents(source, kind)
                else:
                    rows = self.find_documents(source, kind, linked_order_number)
                exact = [row for row in rows if row.number == number
                         and row.type == kind
                         and _normalized_equal(row.reference, source.external_reference)
                         and abs(row.total - source.totals.gross) <= _MONEY_TOLERANCE]
                if len(exact) == 1:
                    return
                raise ManualReviewRequired(
                    f"expected one saved {kind} {number!r}, found {len(exact)} exact rows"
                )
            except Exception as exc:
                last_error = exc
                _LOGGER.warning("Saved %s readback %s/3: %s", kind, attempt, exc)
                if attempt < 3:
                    time.sleep(self.poll_interval_seconds)
        raise ActionOutcomeUnknown(
            f"{kind} {number} Save was activated but its saved row could not be confirmed "
            f"after 3 read-only checks: {last_error}. Save will not be repeated."
        ) from last_error

    def verify_order_document(self, source: OrderSource, order_number: str) -> DocumentRow:
        matches = self.find_documents(source, "Order")
        exact = [
            row
            for row in matches
            if row.number == order_number
            and _normalized_equal(row.reference, source.external_reference)
            and abs(row.total - source.totals.gross) <= _MONEY_TOLERANCE
        ]
        if len(exact) != 1:
            raise ManualReviewRequired(
                f"expected one saved Order {order_number!r} for {source.external_reference!r}; "
                f"found {len(exact)} matching Documents rows"
            )
        if not _normalized_equal(exact[0].state, "Open"):
            raise PostconditionFailed(
                f"saved Order {order_number!r} is not Open: {exact[0].state!r}"
            )
        if exact[0].document_date is None:
            raise ManualReviewRequired(
                f"saved Order {order_number!r} date could not be read unambiguously"
            )
        if exact[0].document_date != source.order_date:
            raise PostconditionFailed(
                f"saved Order {order_number!r} date differs from the source: "
                f"expected {source.order_date.isoformat()}, "
                f"observed {exact[0].document_date.isoformat()}"
            )
        self._last_source = source
        self._last_order_number = order_number
        return exact[0]

    def create_linked_invoice(self, order_number: str) -> InvoiceEditorRef:
        if not order_number.strip():
            raise ValueError("order_number is required to create a linked Invoice")
        source = self._require_source()
        documents = self.find_documents(source, "Order")
        if len([row for row in documents if row.number == order_number]) != 1:
            raise ManualReviewRequired(f"could not establish one saved Order {order_number!r}")
        self._last_order_number = order_number
        editor, kind, number = self._document_editor_identity()
        if kind != "Order" or number != order_number:
            raise ManualReviewRequired("the saved source Order is not active")
        button = self._resolver(editor).resolve(
            ControlQuery.one_of("Invoice", control_types=("Button",), allow_ocr=False)
        )
        self._click_control(self._current_window(), button)
        self._wait_until("the linked Invoice editor", self._has_invoice_editor)
        number = self._read_optional(("No.",))
        self._invoice_ref = InvoiceEditorRef(
            token=uuid4().hex,
            number=number,
            linked_order_number=order_number,
            proposed_invoice_date=self._read_optional(("Date",)),
            proposed_service_date=self._read_optional(("Service date",)),
        )
        return self._invoice_ref

    def verify_invoice_copied_order(
        self,
        source: OrderSource,
        order_number: str,
    ) -> VerificationResult:
        date_text = self._read_optional(("Order Date",))
        expected = {
            "Cust.Ref.": source.external_reference,
            "Date": source.order_date.isoformat(),
            "VAT mode": "With VAT",
            "gross": _decimal_text(source.totals.gross),
            "linked_order_number": order_number,
        }
        observed: dict[str, Any] = {
            "Cust.Ref.": self._read_optional(("Cust.Ref.", "Customer reference")),
            "Date": date_text,
            "VAT mode": self._read_combo_optional(("VAT mode", "Tax mode", "VAT")),
            "gross": self._read_optional(("Gross total", "Total", "Amount due")),
            "linked_order_number": self._invoice_link(
                self._read_optional(("No.",)) or "", self._active_order_editor_tab()
            ),
        }
        issues: list[str] = []
        reference = observed["Cust.Ref."]
        if not reference or not _normalized_equal(source.external_reference, reference):
            issues.append("Invoice customer reference differs from the source Order")
        observed_date = _parse_date_text(date_text or "")
        observed["parsed_date"] = observed_date.isoformat() if observed_date else None
        if observed_date != source.order_date:
            issues.append("Invoice Order Date differs from the source Order or is unreadable")
        issues.extend(self._invoice_default_issues())
        if not _normalized_equal("With VAT", observed["VAT mode"] or ""):
            issues.append("Invoice VAT mode is not With VAT or could not be read back")
        gross = _parse_decimal(observed["gross"] or "")
        if gross is None or abs(gross - source.totals.gross) > _MONEY_TOLERANCE:
            issues.append("Invoice gross total differs from the source Order")
        linked = observed["linked_order_number"]
        if not linked:
            issues.append("Invoice's source Order number could not be read back")
        elif not _normalized_equal(order_number, linked):
            issues.append("Invoice's visible source Order number differs from the saved Order")

        if source.order_discount_percent or source.shipping:
            adjustments = self.verify_order_adjustments(source)
            if not adjustments.verified:
                issues.extend(adjustments.observations)
            observed["adjustments"] = adjustments.observed
        totals = self.verify_order_totals(source)
        if not totals.verified:
            issues.extend(f"Invoice copied totals: {issue}" for issue in totals.observations)
        observed["totals"] = totals.observed

        debtor = self.verify_order_debtor(source.debtor)
        if not debtor.verified:
            issues.extend(f"Invoice copied Debtor: {issue}" for issue in debtor.observations)
        observed["debtor"] = debtor.observed

        copied_lines: list[dict[str, Any]] = []
        for item_index, item in enumerate(source.items):
            line = self.verify_order_line(item, item_index)
            copied_lines.append({"sku": item.sku, "observed": line.observed})
            if not line.verified:
                issues.extend(
                    f"Invoice copied line {item.sku}: {issue}"
                    for issue in line.observations
                )
        observed["lines"] = copied_lines
        expected["lines"] = [
            {"sku": item.sku, **self._expected_line_values(item)} for item in source.items
        ]

        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues)
            or ("Invoice header, Debtor, totals, and copied lines match the saved Order",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence("invoice-copy-verification-failed")
        self._last_source = source
        self._last_order_number = order_number
        return result

    def apply_payment(self, source: OrderSource) -> VerificationResult:
        self._require_editor("invoice")
        method_control = self._invoice_payment_method_control()
        if not _normalized_equal(self._element_value(method_control) or "", source.payment.method):
            method_control.select(source.payment.method)
        paid_control = self._find_checkbox(("Paid", "Payment received", "Paid status"))
        if paid_control is None:
            raise ElementNotFound("Invoice paid status checkbox could not be resolved")
        is_paid = source.payment.status is PaymentStatus.PAID
        self._set_checkbox(paid_control, is_paid)
        if is_paid:
            assert source.payment.payment_date is not None
            self._set_date_field(
                ("Payment date", "Paid date", "at"), _date_text(source.payment.payment_date)
            )
            current = self._read_optional(("Value", "Payment value")) or ""
            if _parse_decimal(current) != source.totals.gross:
                entry = self._numeric_entry_text(_decimal_text(source.totals.gross), current)
                self._set_field(("Value", "Payment value"), entry)
        else:
            self._clear_field(("Payment date", "Paid date", "at"), optional=True)
            self._clear_field(("Value", "Payment value"), optional=True)

        return self.verify_invoice_payment(source)

    def _invoice_payment_method_control(self) -> Any:
        paid = self._find_checkbox(("paid",))
        bounds = element_bounds(paid) if paid is not None else None
        if bounds is None:
            raise ElementNotFound("Invoice paid checkbox cannot locate the payment dropdown")
        matches = [e for e in _descendants(self._active_order_editor_tab())
                   if element_type(e) == "ComboBox" and self._visible_enabled(e)
                   and (b := element_bounds(e)) is not None and b.left > bounds.right
                   and abs(b.center[1] - bounds.center[1]) < bounds.height / 2]
        if len(matches) != 1:
            raise ManualReviewRequired("Invoice payment dropdown is not unique beside paid")
        return matches[0]

    def _invoice_default_issues(self) -> list[str]:
        ref = self._invoice_ref
        if ref is None:
            return ["Invoice proposed defaults have not been recorded"]
        issues = []
        for label, expected in (("No.", ref.number),
                                ("Date", ref.proposed_invoice_date),
                                ("Service date", ref.proposed_service_date)):
            actual = self._read_optional((label,))
            if expected is None or actual != expected:
                issues.append(f"Invoice proposed {label} changed or cannot be verified")
        return issues

    def verify_invoice_payment(self, source: OrderSource) -> VerificationResult:
        if self._invoice_ref and self._invoice_ref.number:
            self._activate_document_tab("Invoice", self._invoice_ref.number)
        self._require_editor("invoice")
        is_paid = source.payment.status is PaymentStatus.PAID
        paid_control = self._find_checkbox(("paid", "Payment received", "Paid status"))
        method = self._element_value(self._invoice_payment_method_control())
        date_value = self._read_optional(("Payment date", "Paid date", "at"))
        value = _parse_decimal(self._read_optional(("Value", "Payment value")) or "")
        paid = self._checkbox_state(paid_control)
        expected_paid = is_paid
        issues: list[str] = self._invoice_default_issues()
        if source.order_discount_percent or source.shipping:
            adjustments = self.verify_order_adjustments(source)
            if not adjustments.verified:
                issues.extend(adjustments.observations)
        if not method or not _normalized_equal(source.payment.method, method):
            issues.append("payment method did not read back exactly")
        if paid is None or paid != expected_paid:
            issues.append("paid status did not read back as expected")
        if is_paid:
            if not date_value or not self._date_matches(source.payment.payment_date, date_value):
                issues.append("payment date did not read back as expected")
            if value is None or abs(value - source.totals.gross) > _MONEY_TOLERANCE:
                issues.append("payment value does not equal the full Invoice gross total")
        elif date_value or value is not None:
            issues.append("unpaid Invoice unexpectedly has a payment date or value")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("Invoice payment fields match the extracted status",),
            expected={
                "method": source.payment.method,
                "paid": expected_paid,
                "payment_date": _date_text(source.payment.payment_date)
                if source.payment.payment_date
                else None,
                "value": _decimal_text(source.totals.gross) if is_paid else None,
            },
            observed={
                "method": method,
                "paid": paid,
                "payment_date": date_value,
                "value": str(value) if value is not None else None,
            },
        )
        if not result.verified:
            self.capture_evidence("payment-verification-failed")
        return result

    def save_invoice(self) -> str:
        number = self._read_optional(("No.",))
        if not number:
            raise PostconditionFailed("Invoice has no visible generated No. before Save")
        self._dispatch_save(f"save Invoice {number}")
        self._confirm_saved_document(number, "Invoice", self._last_order_number)
        self._last_invoice_number = number
        return number

    def verify_final_documents(
        self,
        source: OrderSource,
        order_number: str,
        invoice_number: str,
    ) -> VerificationResult:
        # One fresh pass verifies both records without reopening every row twice.
        documents = self.find_documents(source, "All")
        orders = [row for row in documents if row.type == "Order"]
        invoices = [row for row in documents if row.type == "Invoice"
                    and row.linked_order_number == order_number]
        order_rows = [row for row in orders if row.number == order_number]
        invoice_rows = [row for row in invoices if row.number == invoice_number]
        issues: list[str] = []
        if len(order_rows) != 1:
            issues.append(f"expected one saved Order row, found {len(order_rows)}")
        else:
            if abs(order_rows[0].total - source.totals.gross) > _MONEY_TOLERANCE:
                issues.append("saved Order total differs from the source")
            if not _normalized_equal(order_rows[0].state, "Open"):
                issues.append(f"source Order is not Open (state {order_rows[0].state!r})")
            if order_rows[0].document_date is None:
                issues.append("saved Order date could not be read unambiguously")
            elif order_rows[0].document_date != source.order_date:
                issues.append("saved Order date differs from the source")
        if len(invoice_rows) != 1:
            issues.append(
                f"expected one Invoice row linked to Order {order_number}, "
                f"found {len(invoice_rows)}"
            )
        else:
            invoice = invoice_rows[0]
            if abs(invoice.total - source.totals.gross) > _MONEY_TOLERANCE:
                issues.append("saved Invoice total differs from source gross total")
            if invoice.document_date is None:
                issues.append("saved Invoice date could not be read unambiguously")
            elif (self._invoice_ref is None or invoice.document_date !=
                  _parse_date_text(self._invoice_ref.proposed_invoice_date or "")):
                issues.append("saved Invoice date differs from its proposed default")
            if source.payment.status is PaymentStatus.PAID and not _normalized_equal(
                invoice.state, "Paid"
            ):
                issues.append(f"Invoice is not Paid (state {invoice.state!r})")
            elif source.payment.status is PaymentStatus.UNPAID and _normalized_equal(
                invoice.state, "Paid"
            ):
                issues.append("unpaid source Invoice is marked Paid")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("saved Order and linked Invoice are verified",),
            expected={
                "order_number": order_number,
                "invoice_number": invoice_number,
                "reference": source.external_reference,
                "total": _decimal_text(source.totals.gross),
                "order_state": "Open",
                "invoice_state": source.payment.status.value.title(),
            },
            observed={
                "order": asdict(order_rows[0]) if len(order_rows) == 1 else None,
                "invoice": asdict(invoice_rows[0]) if len(invoice_rows) == 1 else None,
            },
        )
        if not result.verified:
            self.capture_evidence("final-verification-failed")
        return result

    def find_documents(
        self,
        source: OrderSource,
        document_type: str,
        linked_order_number: str | None = None,
    ) -> list[DocumentRow]:
        restore = self._active_editor_context()
        try:
            self._open_documents_view()
            manager = self._data_manager_tab("Documents")
            if manager is None:
                raise ManualReviewRequired("the Documents manager is not active")
            self._set_field(("Search",), source.external_reference, window=manager)
            rows = self._wait_stable_rows(window=manager)
            if not rows:
                return self._ocr_document_rows(manager, source, document_type, linked_order_number)
            documents: list[DocumentRow] = []
            readable_rows = 0
            for row in rows:
                values, row_text = self._row_values(row)
                number = self._value(values, "No.", "Number", "Document number", "Document No.")
                row_type = self._value(values, "Type", "Document type")
                document_date_text = self._value(
                    values, "Date", "Document date", "Document Date"
                )
                document_date = (
                    _parse_date_text(document_date_text) if document_date_text else None
                )
                reference = self._value(values, "Cust.Ref.", "Customer reference", "Reference")
                total = _parse_decimal(
                    self._value(values, "Total", "Gross", "Amount", "Value") or ""
                )
                state = self._value(values, "State", "Status", "Paid")
                linked = self._value(
                    values, "Order No.", "Order number", "Source Order", "Follow-up from"
                )
                if not number or not row_type or not reference or total is None or not state:
                    if row_text and source.external_reference.casefold() in row_text.casefold():
                        raise ManualReviewRequired(
                            "Documents contains the source reference but does not expose "
                            "all required columns"
                        )
                    continue
                readable_rows += 1
                if document_type != "All" and not _normalized_equal(row_type, document_type):
                    continue
                if not _normalized_equal(reference, source.external_reference):
                    continue
                if linked_order_number and (
                    not linked or not _normalized_equal(linked_order_number, linked)
                ):
                    continue
                documents.append(
                    DocumentRow(
                        number=number,
                        type=row_type,
                        reference=reference,
                        total=total,
                        state=state,
                        linked_order_number=linked,
                        document_date=document_date,
                    )
                )
            if not readable_rows:
                return self._ocr_document_rows(manager, source, document_type, linked_order_number)
            return documents
        finally:
            if restore is not None:
                self._restore_editor_context(*restore)

    def _ocr_document_rows(
        self, manager: Any, source: OrderSource, document_type: str,
        linked_order_number: str | None,
    ) -> list[DocumentRow]:
        if not isinstance(self.ocr, TesseractOCR):
            raise ManualReviewRequired("Documents rows need a visible-table reader")
        image = self._capture_window(manager)
        words = self.ocr.read_words(image, preprocess=False)
        headers = []
        for label in ("Document", "Date", "Name", "Cust.Ref.", "State", "Total", "Printed"):
            matches = [word.bounds for word in words if word.text == label]
            if len(matches) != 1:
                raise ManualReviewRequired(f"Documents has no unique {label} header")
            headers.append(matches[0])
        if verified_empty_selector(
            manager, image, headers,
            search_verified=self._read_from_window(manager, ("Search",), allow_ocr=False)
                == source.external_reference,
            allow_grid_lines=True,
        ):
            return []
        body = [word for word in words if word.bounds.top > max(h.bottom for h in headers)]
        def reference_locator(text: str) -> bool:
            text = text.strip("_| ")
            if text == source.external_reference:
                return True
            prefix = re.split(r"\.\.\.|…", text, maxsplit=1)
            return (len(prefix) == 2 and len(prefix[0]) >= 6
                    and source.external_reference.startswith(prefix[0].rstrip(")|_ ")))

        anchors = [word for word in body if reference_locator(word.text)
                   and headers[3].left - 12 <= word.bounds.center[0] < headers[4].left - 12]
        if not anchors:
            raise ManualReviewRequired("Documents has rows but their exact reference is unreadable")
        documents = []
        for anchor in anchors:
            row_words = [word for word in body
                         if abs(word.bounds.center[1] - anchor.bounds.center[1])
                         <= max(12, anchor.bounds.height)]
            def cell(index: int, row_words: list[OCRMatch] = row_words) -> str:
                selected = [word for word in row_words
                            if any(char.isalnum() for char in word.text)
                            if headers[index].left - 12 <= word.bounds.center[0]
                            < headers[index + 1].left - 12]
                return " ".join(word.text for word in sorted(
                    selected, key=lambda word: word.bounds.left
                ))
            document_text, date_text = cell(0), cell(1)
            reference, state, total_text = cell(3).strip("_| "), cell(4), cell(5)
            # Table-wide segmentation can omit a status beside its icon and lose
            # a decimal comma. Read these two bounded cells as individual lines.
            def read_cell_line(index: int, anchor: OCRMatch = anchor) -> str:
                box = (max(0, headers[index].left - 12), max(0, anchor.bounds.top - 10),
                       headers[index + 1].left - 12, anchor.bounds.bottom + 10)
                crop = image.crop(box)
                data = self.ocr.read_text_data(
                    crop.resize((crop.width * 2, crop.height * 2)), config="--psm 7"
                )
                return " ".join(str(text).strip() for text in data.get("text", [])
                                if str(text).strip())
            table_document_text = document_text
            document_text = read_cell_line(0)
            state, total_text = read_cell_line(4), read_cell_line(5)
            # The state cell starts with an icon; OCR may render it as one or
            # two glyphs. Require exactly one known status word, with only a
            # short leading icon and optional trailing grid punctuation.
            status = re.fullmatch(
                r"(?:\S{1,2}\s+)?(open|paid|unpaid)(?:\s+[^\w]+)?", state, re.IGNORECASE
            )
            if status:
                state = status.group(1)
            document_date, total = _parse_date_text(date_text), _parse_decimal(total_text)
            if (not document_text or not reference_locator(reference)
                    or document_date is None or total is None or not state):
                raise ManualReviewRequired(
                    f"Documents row is unreadable: number={document_text!r}, date={date_text!r}, "
                    f"reference={reference!r}, state={state!r}, total={total_text!r}"
                )
            # Open the observed row and read its authoritative number and type.
            # The list's type is an icon and OCR can confuse O/0 in document numbers.
            target = Bounds(headers[0].left, anchor.bounds.top,
                            headers[1].left - 15, anchor.bounds.bottom)
            from pywinauto.mouse import double_click
            rect = manager.rectangle()
            double_click(coords=(int(rect.left) + target.center[0],
                                 int(rect.top) + target.center[1]))
            def normalize_number(value: str) -> str:
                return re.sub(r"[OQ]", "0", value.upper().replace(" ", "").strip("|_"))
            number_candidates = {normalize_number(value) for value in
                                 (document_text, table_document_text) if value.strip()}
            def target_open(candidates: set[str] = number_candidates) -> bool:
                try:
                    return normalize_number(self._document_editor_identity()[2]) in candidates
                except ManualReviewRequired:
                    return False
            self._wait_until("the selected saved document number", target_open)
            editor, kind, number = self._document_editor_identity()
            if normalize_number(number) not in number_candidates:
                raise ManualReviewRequired(
                    f"opened document number {number!r} differs from row {document_text!r}"
                )
            actual_reference = self._read_from_window(editor, ("Cust.Ref.",), allow_ocr=False)
            if (actual_reference != source.external_reference
                    or element_name(editor).startswith("*")):
                raise ManualReviewRequired(
                    "document editor reference differs or contains unsaved changes"
                )
            linked = self._invoice_link(number, editor) if kind == "Invoice" else None
            if document_type != "All" and kind != document_type:
                continue
            if linked_order_number and linked != linked_order_number:
                raise ManualReviewRequired("the saved Invoice's source Order cannot be verified")
            documents.append(DocumentRow(number=number, type=kind, reference=actual_reference,
                                         total=total, state=state, document_date=document_date,
                                         linked_order_number=linked))
        return documents

    def capture_evidence(self, label: str) -> Path | None:
        if self.evidence_directory is None:
            return None
        window = self._require_window()
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "evidence"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.evidence_directory / f"{timestamp}-{safe_label}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image = window.capture_as_image()
            image.save(target)
        except Exception as exc:
            raise GatewayError(f"could not save Fakturama evidence screenshot: {exc}") from exc
        return target

    # Semantic resolution and guarded interactions

    def _capture_window(self, window: Any) -> Any:
        """Capture OCR input only after confirming the application's foreground window."""
        self._focus_window_for_automation(window)
        return window.capture_as_image()

    def _resolver(self, window: Any | None = None) -> ControlResolver:
        active = window if window is not None else self._current_window()
        self._focus_window_for_automation(active)
        return ControlResolver(
            active, screenshot=lambda: self._capture_window(active), ocr=self.ocr
        )

    def _scoped_resolver(self, scope: Any) -> ControlResolver:
        window = self._current_window()
        self._focus_window_for_automation(window)
        return ControlResolver(
            scope, screenshot=lambda: self._capture_window(window), ocr=self.ocr
        )

    @staticmethod
    def _foreground_matches(window: Any) -> bool:
        handle = _safe(window, "handle")
        if os.name != "nt" or not handle:
            return True
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        root = top_level_handle(int(handle))
        return root is not None and int(user32.GetForegroundWindow() or 0) == root

    @staticmethod
    def _focus_window_for_automation(window: Any) -> None:
        handle = _safe(window, "handle")
        if os.name == "nt" and handle:
            root = top_level_handle(int(handle))
            if root is None or not ensure_foreground(root):
                raise ManualReviewRequired(
                    "Fakturama could not become the foreground window. "
                    "Leave Fakturama visible and stop interacting with other apps during the run."
                )
            return
        focus = getattr(window, "set_focus", None)
        if not callable(focus):
            return
        try:
            if _safe(window, "is_minimized", False):
                window.restore()
            focus()
            if not WindowsFakturamaGateway._foreground_matches(window):
                raise ManualReviewRequired(
                    "Fakturama could not become the foreground window. "
                    "Leave Fakturama visible and stop interacting with other apps during the run."
                )
        except ManualReviewRequired:
            raise
        except Exception as exc:
            title = WindowsFakturamaGateway._window_title(window) or "(untitled)"
            raise ManualReviewRequired(
                f"could not bring Fakturama window {title!r} to the foreground "
                f"before control resolution: {exc}"
            ) from exc

    def _click_action(
        self,
        query: ControlQuery,
        description: str,
        postcondition: Callable[[], bool],
        *,
        save: bool = False,
        timeout: float | None = None,
    ) -> None:
        # Resolve immediately before the effect, so stale wrapper bounds are not reused.
        window = self._current_window()
        control = self._resolver(window).resolve(query)
        try:
            self._click_control(window, control)
        except Exception as exc:
            if save:
                raise ActionOutcomeUnknown(f"{description} may have been activated: {exc}") from exc
            raise
        try:
            self._wait_until(description, postcondition, timeout=timeout)
        except TransitionTimeout as exc:
            if save:
                raise ActionOutcomeUnknown(
                    f"{description} was activated, but the saved state could not be established; "
                    "inspect Fakturama before retrying"
                ) from exc
            raise

    def _dispatch_save(self, description: str) -> None:
        # Resolving the Save target is read-only. Once dispatched, any uncertainty
        # is surfaced as unknown and the caller must reconcile through Documents.
        window = self._current_window()
        control = self._resolver(window).resolve(
            ControlQuery.one_of(
                "Save the current contents", "Save", control_types=("Button", "MenuItem")
            )
        )
        try:
            self._click_control(window, control)
        except Exception as exc:
            raise ActionOutcomeUnknown(f"{description} may have taken effect: {exc}") from exc

    def _click(self, query: ControlQuery) -> None:
        window = self._current_window()
        self._click_control(window, self._resolver(window).resolve(query))

    @staticmethod
    def _click_control(window: Any, control: Any) -> None:
        WindowsFakturamaGateway._focus_window_for_automation(window)
        if control.element is not None:
            element = control.element
            try:
                element.click_input()
            except Exception:
                invoke = getattr(element, "invoke", None)
                if callable(invoke):
                    invoke()
                else:
                    raise
            return
        match = control.ocr_match
        if match is None:
            raise ElementNotFound("resolved control has neither UIA element nor OCR bounds")
        # OCR bounds are relative to capture_as_image(); convert through the live
        # window rectangle before sending a desktop click, including non-zero origins.
        rect = _safe(window, "rectangle")
        if rect is None:
            raise GatewayError("cannot convert OCR bounds without the current window rectangle")
        screen_x = int(rect.left) + match.bounds.center[0]
        screen_y = int(rect.top) + match.bounds.center[1]
        click_at_screen = getattr(window, "click_at_screen", None)
        if callable(click_at_screen):
            try:
                click_at_screen((screen_x, screen_y))
                return
            except (AttributeError, TypeError):
                pass
        try:
            from pywinauto.mouse import click
        except ImportError as exc:
            raise GatewayError("OCR clicking requires pywinauto on Windows") from exc
        click(coords=(screen_x, screen_y))

    def _native_date_picker(self, element: Any) -> Any | None:
        target = element_bounds(element)
        window_handle = _safe(self._current_window(), "handle")
        if target is None or window_handle is None:
            return None
        try:
            from pywinauto import Desktop

            root = Desktop(backend="win32").window(handle=int(window_handle))
            descendants = root.descendants()
        except Exception:
            return None

        matches = []
        target_area = max(1, target.width * target.height)
        for candidate in descendants:
            if str(_safe(candidate, "class_name", "")) != "SysDateTimePick32":
                continue
            rect = _safe(candidate, "rectangle")
            if rect is None:
                continue
            try:
                left, top, right, bottom = (
                    int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
                )
            except (AttributeError, TypeError, ValueError):
                continue
            overlap_width = max(0, min(target.right, right) - max(target.left, left))
            overlap_height = max(0, min(target.bottom, bottom) - max(target.top, top))
            if overlap_width * overlap_height < target_area * 0.8:
                continue
            if callable(getattr(candidate, "set_time", None)) and callable(
                getattr(candidate, "get_time", None)
            ):
                matches.append(candidate)

        if len(matches) > 1:
            raise ManualReviewRequired(
                "multiple native date controls overlap the labeled date field"
            )
        return matches[0] if matches else None

    def _set_date_field(
        self, labels: Sequence[str], value: str, *, optional: bool = False
    ) -> bool:
        try:
            expected = date.fromisoformat(value)
        except ValueError as exc:
            raise GatewayError(f"date value {value!r} is not ISO formatted") from exc
        try:
            control = self._resolver().resolve_edit(ControlQuery.one_of(*labels))
        except ElementNotFound:
            if optional:
                return False
            raise

        # A human may have corrected the visible date while resolving a review.
        # Preserve a correct value rather than overwriting it on resume.
        current = self._element_value(control.element)
        if current is not None and _parse_date_text(current) == expected:
            return True

        click_input = getattr(control.element, "click_input", None)
        type_keys = getattr(control.element, "type_keys", None)
        if callable(click_input) and callable(type_keys):
            try:
                bounds = element_bounds(control.element)
                if bounds is not None and bounds.width > 1 and bounds.height > 0:
                    day_x = max(1, min(bounds.width - 1, 30))
                    day_y = bounds.height // 2
                    try:
                        click_input(coords=(day_x, day_y))
                    except TypeError:
                        click_input()
                else:
                    click_input()
                type_keys(str(expected.day))
                type_keys(str(expected.month))
                type_keys("{RIGHT}")
                type_keys(str(expected.year))
                type_keys("{TAB}")
            except Exception as exc:
                raise ManualReviewRequired(
                    f"could not enter the {labels[0]!r} through keyboard input: {exc}"
                ) from exc
        else:
            picker = self._native_date_picker(control.element)
            if picker is None:
                # Keep a generic write fallback for controls without keyboard access
                # or a native date picker. Readback below catches rejected input.
                self._write_element(control.element, value)
            else:
                weekday_sunday_first = (expected.weekday() + 1) % 7
                try:
                    picker.set_time(
                        year=expected.year,
                        month=expected.month,
                        day_of_week=weekday_sunday_first,
                        day=expected.day,
                    )
                    observed_time = picker.get_time()
                    observed = date(
                        int(observed_time.wYear),
                        int(observed_time.wMonth),
                        int(observed_time.wDay),
                    )
                except Exception as exc:
                    raise ManualReviewRequired(
                        f"could not set the {labels[0]!r} through its native date control: {exc}"
                    ) from exc
                if observed != expected:
                    raise PostconditionFailed(
                        f"{labels[0]}: expected {expected.isoformat()}, "
                        f"observed {observed.isoformat()}"
                    )

        actual = self._element_value(control.element)
        if actual is None or _parse_date_text(actual) != expected:
            raise PostconditionFailed(
                f"date field {labels[0]!r}: expected {expected.isoformat()}, "
                f"observed {actual!r}"
            )
        return True

    def _set_field(
        self,
        labels: Sequence[str],
        value: str,
        *,
        optional: bool = False,
        window: Any | None = None,
        scope: Any | None = None,
        resolver: ControlResolver | None = None,
    ) -> bool:
        query = ControlQuery.one_of(*labels)
        try:
            if resolver is None:
                resolver = (
                    self._scoped_resolver(scope)
                    if scope is not None
                    else self._resolver(window)
                )
            control = resolver.resolve_edit(query)
        except ElementNotFound:
            if optional:
                return False
            raise
        if (normalize_label(labels[0]) in {"pricegross", "costpricenet", "stock", "discount"}
                and os.name == "nt" and _safe(control.element, "handle")
                and element_type(control.element) == "Edit"
                and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value)):
            entry = self._numeric_entry_text(value, self._raw_element_value(control.element))
            self._type_field_text(control.element, entry, labels[0])
        elif (normalize_label(labels[0]) == "description" and os.name == "nt"
              and _safe(control.element, "handle") and element_type(control.element) == "Edit"):
            self._type_field_text(control.element, value, labels[0])
        else:
            self._write_element(control.element, value)
        actual = (
            self._raw_element_value(control.element)
            if value == ""
            else self._element_value(control.element)
        )
        if value == "" and actual is None:
            raise PostconditionFailed(f"field {labels[0]!r} could not be verified as blank")
        if actual is None or not self._equivalent_value(labels[0], value, actual):
            raise PostconditionFailed(
                f"field {labels[0]!r} did not read back the entered value: "
                f"expected {value!r}, observed {actual!r}"
            )
        return True

    def _clear_field(self, labels: Sequence[str], *, optional: bool = False) -> bool:
        try:
            return self._set_field(labels, "", optional=optional)
        except ElementNotFound:
            if optional:
                return False
            raise

    @staticmethod
    def _numeric_entry_text(value: str, displayed: str | None) -> str:
        """Use the field's observed decimal mark without changing app preferences."""
        if not displayed or "." not in value:
            return value
        numeric = re.sub(r"[^0-9,.-]", "", displayed)
        # Formatted currency fields expose their fractional digits even at zero.
        # The rightmost separator is decimal when both grouping and decimals exist.
        mark = max(numeric.rfind(","), numeric.rfind("."))
        if mark >= 0 and numeric[mark] == "," and len(numeric) - mark - 1 == 2:
            return value.replace(".", ",")
        return value

    @staticmethod
    def _type_field_text(element: Any, value: str, label: str) -> None:
        WindowsFakturamaGateway._focus_window_for_automation(element)
        element.set_focus()
        if _safe(element, "has_keyboard_focus", False) is not True:
            raise ManualReviewRequired(f"field {label!r} did not receive keyboard focus")
        escaped = "".join("{" + char + "}" if char in "+^%~(){}" else char for char in value)
        element.type_keys("^a", set_foreground=False)
        element.type_keys(escaped, with_spaces=True, set_foreground=False, pause=0.02)

    @staticmethod
    def _write_element(element: Any, value: str) -> None:
        control_type = element_type(element)
        if (os.name == "nt" and _safe(element, "handle") and control_type == "Edit"
                and normalize_label(element_name(element)) == "value"
                and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value)):
            # SWT formatted numeric fields ignore SetValue in their bound model.
            WindowsFakturamaGateway._focus_window_for_automation(element)
            element.set_focus()
            if _safe(element, "has_keyboard_focus", False) is not True:
                raise ManualReviewRequired("the VAT Value field did not receive keyboard focus")
            element.type_keys("^a", set_foreground=False)
            element.type_keys(value, set_foreground=False, pause=0.02)
            return
        if (
            os.name == "nt" and _safe(element, "handle")
            and control_type == "Edit"
            and normalize_label(element_name(element)) == "company"
        ):
            # SWT Company can show UIA SetValue text without updating its bound
            # contact model. A real reversible edit emits the missing event.
            from pywinauto.keyboard import send_keys

            WindowsFakturamaGateway._focus_window_for_automation(element)
            element.set_edit_text(value)
            element.click_input()
            if _safe(element, "has_keyboard_focus", False) is not True:
                raise ManualReviewRequired("the contact field did not receive keyboard focus")
            send_keys("{END}{SPACE}{BACKSPACE}", pause=0.01, vk_packet=False)
            return
        if control_type == "ComboBox":
            for method_name in ("select", "select_by_text"):
                method = getattr(element, method_name, None)
                if callable(method):
                    try:
                        method(value)
                        return
                    except Exception:
                        continue
        set_text = getattr(element, "set_edit_text", None)
        if callable(set_text):
            try:
                set_text(value)
                return
            except Exception:
                pass
        click = getattr(element, "click_input", None)
        if not callable(click):
            raise GatewayError("resolved field cannot receive keyboard input")
        click()
        try:
            element.type_keys("^a", set_foreground=True)
            # Send keys with special characters escaped so customer data is literal.
            escaped = value.replace("{", "{{}").replace("}", "{}}").replace("+", "{+}")
            escaped = escaped.replace("^", "{^}").replace("%", "{%}").replace("~", "{~}")
            element.type_keys(escaped, with_spaces=True, set_foreground=True)
        except Exception as exc:
            raise GatewayError(f"could not enter text into the resolved field: {exc}") from exc

    def _read_combo_optional(self, labels: Sequence[str]) -> str | None:
        query = ControlQuery.one_of(
            *labels, control_types=("ComboBox",), allow_ocr=False
        )
        try:
            control = self._resolver().resolve(query)
            return self._element_value(control.element)
        except ElementNotFound:
            return None

    def _read_optional(
        self, labels: Sequence[str], *, scope: Any | None = None,
        control_types: Sequence[str] = (),
    ) -> str | None:
        query = ControlQuery.one_of(*labels, control_types=control_types)
        resolver = self._scoped_resolver(scope) if scope is not None else self._resolver()
        if control_types:
            try:
                return self._element_value(resolver.resolve(query).element)
            except ElementNotFound:
                return None
        return self._read_with_resolver(resolver, query)

    def _read_with_resolver(
        self, resolver: ControlResolver, query: ControlQuery
    ) -> str | None:
        try:
            control = resolver.resolve_edit(query)
            return self._element_value(control.element)
        except ElementNotFound:
            try:
                control = resolver.resolve(query)
                return self._element_value(control.element) if control.element is not None else None
            except ElementNotFound:
                return None

    def _read_from_window(
        self, window: Any, labels: Sequence[str], *, allow_ocr: bool = True
    ) -> str | None:
        try:
            control = self._resolver(window).resolve_edit(
                ControlQuery.one_of(*labels, allow_ocr=allow_ocr)
            )
            return self._element_value(control.element)
        except ElementNotFound:
            return None

    @staticmethod
    def _element_value(element: Any) -> str | None:
        if element is None:
            return None
        if element_type(element) == "Edit":
            native_text = native_edit_text(_safe(element, "handle"))
            if native_text is not None:
                return native_text.strip()
        for method_name in ("get_value", "selected_text", "window_text"):
            value = _safe(element, method_name)
            if value is not None and str(value).strip():
                return str(value).strip()
        name = element_name(element)
        return name or None

    @staticmethod
    def _raw_element_value(element: Any) -> str | None:
        if element is None:
            return None
        if element_type(element) == "Edit":
            native_text = native_edit_text(_safe(element, "handle"))
            if native_text is not None:
                return native_text.strip()
        for method_name in ("get_value", "selected_text", "window_text"):
            value = _safe(element, method_name)
            if value is not None:
                return str(value).strip()
        return None

    def _select_combo_by_current_value(
        self,
        current_values: set[str],
        option: str,
        description: str,
        *,
        labels: Sequence[str] = (),
    ) -> bool:
        accepted = {normalize_label(value) for value in current_values}
        element = None
        label_error: GatewayError | None = None
        if labels:
            try:
                element = self._resolver().resolve(
                    ControlQuery.one_of(
                        *labels,
                        control_types=("ComboBox",),
                        allow_ocr=False,
                    )
                ).element
            except (ElementNotFound, AmbiguousControl) as exc:
                label_error = exc

        if element is None:
            candidates = [
                candidate
                for candidate in _descendants(self._current_window())
                if element_type(candidate) == "ComboBox"
                and (current := self._element_value(candidate)) is not None
                and normalize_label(current) in accepted
            ]
            if not candidates:
                if label_error is not None:
                    raise label_error
                raise ElementNotFound(
                    f"could not identify the {description} dropdown by its current selection"
                )
            if len(candidates) > 1:
                raise AmbiguousControl(
                    f"multiple dropdowns match the current values for {description}"
                )
            element = candidates[0]

        if _normalized_equal(option, self._element_value(element) or ""):
            return True

        selected = False
        for method_name in ("select", "select_by_text"):
            method = getattr(element, method_name, None)
            if callable(method):
                try:
                    method(option)
                    selected = True
                    break
                except Exception:
                    continue
        if not selected:
            try:
                element.click_input()
                self._wait_until(
                    f"option {option!r} to appear", lambda: self._has_any_labels(option)
                )
                self._click(
                    ControlQuery.one_of(
                        option, control_types=("ListItem", "MenuItem", "DataItem")
                    )
                )
            except (GatewayError, TransitionTimeout) as exc:
                raise ManualReviewRequired(
                    f"could not select {option!r} for {description}: {exc}"
                ) from exc

        actual = self._element_value(element)
        if actual is None or not _normalized_equal(option, actual):
            raise PostconditionFailed(
                f"{description} expected {option!r}, observed {actual!r}"
            )
        return True

    def _select_named_option(
        self,
        labels: Sequence[str],
        option: str,
        *,
        optional: bool,
    ) -> bool:
        try:
            control = self._resolver().resolve_edit(ControlQuery.one_of(*labels))
        except ElementNotFound:
            if optional:
                return False
            raise
        element = control.element
        selected = False
        for method_name in ("select", "select_by_text"):
            method = getattr(element, method_name, None)
            if callable(method):
                try:
                    method(option)
                    selected = True
                    break
                except Exception:
                    continue
        if not selected:
            try:
                self._click_control(self._current_window(), control)
                self._wait_until(
                    f"option {option!r} to appear",
                    lambda: self._has_any_labels(option),
                )
                self._click(
                    ControlQuery.one_of(option, control_types=("ListItem", "MenuItem", "DataItem"))
                )
                selected = True
            except (GatewayError, TransitionTimeout):
                if optional:
                    return False
                raise
        actual = self._element_value(element)
        if actual is not None and not _normalized_equal(option, actual):
            raise PostconditionFailed(f"expected option {option!r}, observed {actual!r}")
        return selected

    def _find_checkbox(self, labels: Sequence[str]) -> Any | None:
        try:
            return (
                self._resolver()
                .resolve(
                    ControlQuery.one_of(
                        *labels, control_types=("CheckBox", "RadioButton"), allow_ocr=False
                    )
                )
                .element
            )
        except ElementNotFound:
            return None

    @staticmethod
    def _checkbox_state(checkbox: Any) -> bool | None:
        for method_name in ("is_checked", "get_toggle_state"):
            state = _safe(checkbox, method_name)
            if state is not None:
                if isinstance(state, bool):
                    return state
                try:
                    return int(state) != 0
                except (ValueError, TypeError):
                    continue
        return None

    def _set_checkbox(self, checkbox: Any, checked: bool) -> None:
        state = self._checkbox_state(checkbox)
        if state is None:
            raise ManualReviewRequired(
                f"cannot read the current state of checkbox {element_name(checkbox)!r}"
            )
        if state != checked:
            try:
                checkbox.click_input()
            except Exception:
                toggle = getattr(checkbox, "toggle", None)
                if not callable(toggle):
                    raise
                toggle()
        if self._checkbox_state(checkbox) != checked:
            raise PostconditionFailed(
                f"checkbox {element_name(checkbox)!r} did not change as expected"
            )

    @staticmethod
    def _visible_enabled(element: Any) -> bool:
        return _safe(element, "is_visible") is True and _safe(element, "is_enabled") is True

    @staticmethod
    def _scope_elements(scope: Any) -> list[Any]:
        return [scope, *_descendants(scope)]

    def _active_editor_tab(self) -> Any:
        window = self._main_window if self._main_window is not None else self._current_window()
        tabs = [element for element in _descendants(window)
                if element_type(element) == "Tab" and self._visible_enabled(element)]
        matches = [element for element in tabs
                   if normalize_label(element_name(element)) == "newdebtor"]
        if not matches:
            # Saved contacts are renamed, so use their distinct identity fields
            # only when the common New Debtor tab is absent.
            matches = [element for element in tabs
                       if {"customerid", "company"}.issubset({
                           normalize_label(element_name(child))
                           for child in _descendants(element)
                           if element_type(child) == "Edit" and self._visible_enabled(child)
                       })]
        if len(matches) != 1:
            raise ManualReviewRequired(
                "cannot uniquely identify the visible New Debtor editor tab"
            )
        return matches[0]

    def _active_debtor_tab(self, label: str) -> Any | None:
        editor = self._active_editor_tab()
        elements = _descendants(editor)
        wanted = normalize_label(label)
        tabs = [
            element for element in elements
            if element_type(element) == "Tab"
            and normalize_label(element_name(element)) == wanted
        ]
        if len(tabs) > 1:
            raise ManualReviewRequired(f"multiple active {label!r} tabs are visible")
        tab_items = [
            element for element in elements
            if element_type(element) == "TabItem"
            and normalize_label(element_name(element)) == wanted
        ]
        if len(tab_items) > 1:
            raise ManualReviewRequired(f"multiple {label!r} tab items are visible")
        selected = bool(
            tab_items and _safe(tab_items[0], "is_selected", None) in (True, 1)
        )
        if tabs:
            tab = tabs[0]
            if self._visible_enabled(tab):
                return tab
            # SWT can report its selected Tab and TabItem as invisible while
            # their child fields are visibly drawn. Check those children
            # instead, and retain the actual Tab as the field scope.
            visible_children = [
                child for child in _descendants(tab)
                if _safe(child, "is_visible", None) is True
            ]
            names = {normalize_label(element_name(child)) for child in visible_children}
            has_editor = any(
                element_type(child) in {"Edit", "ComboBox"} for child in visible_children
            )
            if wanted == "mainaddress" or wanted.startswith("additionaladdress"):
                if {"street", "country"}.issubset(names) and has_editor and (
                    selected or not tab_items
                ):
                    return tab
            elif selected and has_editor:
                return tab
        # Some builds expose the selected TabItem but no page Tab wrapper.
        # Keep the original editor fallback only when selection is explicit.
        if selected and not tabs:
            return editor
        return None

    def _scoped_ocr_matches(self, label: str, scope: Any) -> list[Any]:
        window = self._current_window()
        finder = getattr(self.ocr, "find_text", None)
        if not callable(finder):
            return []
        try:
            matches = finder(self._capture_window(window), (label,))
        except OCRUnavailable:
            return []
        window_bounds = _safe(window, "rectangle")
        scope_bounds = element_bounds(scope)
        if window_bounds is None or scope_bounds is None:
            raise ManualReviewRequired(
                f"cannot scope OCR text {label!r} to the active Debtor editor"
            )
        scoped = []
        for match in matches:
            if normalize_label(str(getattr(match, "text", ""))) != normalize_label(label):
                continue
            x, y = match.bounds.center
            x += int(window_bounds.left)
            y += int(window_bounds.top)
            if (
                scope_bounds.left <= x <= scope_bounds.right
                and scope_bounds.top <= y <= scope_bounds.bottom
            ):
                scoped.append(match)
        return scoped

    def _activate_debtor_tab(self, label: str) -> Any | None:
        active = self._active_debtor_tab(label)
        if active is not None:
            return active

        normalized = normalize_label(label)
        if normalized in {"mainaddress", "deliveryaddress"} or normalized.startswith(
            "additionaladdress"
        ):
            # Address pages are nested under Addresses and hidden by Miscellaneous.
            self._activate_debtor_tab("Addresses")
            active = self._active_debtor_tab(label)
            if active is not None:
                return active

        editor = self._active_editor_tab()
        matches = self._scoped_ocr_matches(label, editor)
        if len(matches) > 1:
            raise ManualReviewRequired(
                f"OCR found multiple {label!r} tabs inside the active Debtor editor"
            )
        if matches:
            self._click_control(
                self._current_window(),
                ResolvedControl(ControlQuery.one_of(label), ocr_match=matches[0]),
            )
            return self._active_debtor_tab(label)

        tab_items = [
            element
            for element in _descendants(editor)
            if element_type(element) == "TabItem"
            and normalize_label(element_name(element)) == normalize_label(label)
            and self._visible_enabled(element)
        ]
        if len(tab_items) > 1:
            raise ManualReviewRequired(f"multiple {label!r} tab items are visible")
        if tab_items:
            select = getattr(tab_items[0], "select", None)
            if callable(select):
                try:
                    select()
                    return self._active_debtor_tab(label)
                except Exception as exc:
                    raise ManualReviewRequired(
                        f"could not select the {label!r} tab through UI Automation: {exc}"
                    ) from exc
        raise ElementNotFound(f"cannot safely activate the {label!r} Debtor tab")

    @staticmethod
    def _row_aligned_right(label_element: Any, candidate: Any) -> bool:
        label_bounds = element_bounds(label_element)
        candidate_bounds = element_bounds(candidate)
        if label_bounds is None or candidate_bounds is None:
            return False
        y_tolerance = max(5, max(label_bounds.height, candidate_bounds.height) // 2)
        return (
            candidate_bounds.left >= label_bounds.right - 2
            and abs(
                (candidate_bounds.top + candidate_bounds.bottom)
                - (label_bounds.top + label_bounds.bottom)
            )
            <= y_tolerance * 2
        )

    def _composite_row_controls(self, label: str, *, scope: Any) -> tuple[Any, Any]:
        labels = [
            element
            for element in self._scope_elements(scope)
            if element_type(element) == "Text"
            and normalize_label(element_name(element)) == normalize_label(label)
            and self._visible_enabled(element)
        ]
        if not labels:
            raise ElementNotFound(f"composite row label {label!r} was not found")
        if len(labels) != 1:
            raise ManualReviewRequired(f"composite row label {label!r} is ambiguous")
        label_element = labels[0]
        label_bounds = element_bounds(label_element)
        if label_bounds is None:
            raise ManualReviewRequired(f"composite row label {label!r} has no usable bounds")

        ancestors = []
        scope_bounds = element_bounds(scope)
        parent = _safe(label_element, "parent")
        while parent is not None:
            parent_bounds = element_bounds(parent)
            same_scope = parent is scope or (
                scope_bounds is not None
                and parent_bounds == scope_bounds
                and element_type(parent) == element_type(scope)
                and normalize_label(element_name(parent))
                == normalize_label(element_name(scope))
            )
            if same_scope:
                break
            if (
                scope_bounds is None
                or parent_bounds is None
                or not self._bounds_within(parent_bounds, scope_bounds)
            ):
                break
            ancestors.append(parent)
            parent = _safe(parent, "parent")
        ancestors.append(scope)

        for container in ancestors:
            elements = self._scope_elements(container)
            following_labels = [
                element_bounds(element).left
                for element in elements
                if element_type(element) in {"Text", "Label", "Static"}
                and element_name(element).strip()
                and self._visible_enabled(element)
                and self._row_aligned_right(label_element, element)
                and element_bounds(element).left >= label_bounds.right
            ]
            right_boundary = min(following_labels) if following_labels else None
            candidates = [
                element
                for element in elements
                if element_type(element) == "Edit"
                and self._visible_enabled(element)
                and self._row_aligned_right(label_element, element)
                and (
                    right_boundary is None or element_bounds(element).right <= right_boundary
                )
            ]
            if len(candidates) == 2:
                candidates.sort(key=lambda element: element_bounds(element).left)
                return candidates[0], candidates[1]
            if len(candidates) > 2:
                raise ManualReviewRequired(
                    f"composite row {label!r} has more than two aligned Edit controls"
                )
        raise ManualReviewRequired(
            f"composite row {label!r} does not have exactly two visible, aligned Edit controls"
        )

    def _set_composite_row(
        self,
        label: str,
        values: tuple[str | None, str | None],
        *,
        scope: Any,
    ) -> bool:
        first, second = self._composite_row_controls(label, scope=scope)
        for element, value in zip((first, second), values, strict=True):
            if value is None:
                continue
            self._write_element(element, value)
            actual = (
                self._raw_element_value(element)
                if value == ""
                else self._element_value(element)
            )
            if actual is None or not _normalized_equal(value, actual):
                raise PostconditionFailed(
                    f"value {value!r} in composite row {label!r} did not read back"
                )
        return True

    def _read_composite_row(self, label: str, *, scope: Any) -> tuple[str | None, str | None]:
        first, second = self._composite_row_controls(label, scope=scope)
        return self._element_value(first), self._element_value(second)

    def _assign_address_roles(self, roles: Sequence[str]) -> None:
        wanted = {normalize_label(role) for role in roles}
        valid_roles = ("Invoice address", "Delivery address")
        if not wanted or not wanted.issubset({normalize_label(role) for role in valid_roles}):
            raise ValueError(f"unsupported address roles {roles!r}")
        editor = self._active_editor_tab()
        address_tabs = [
            tab
            for tab in _descendants(editor)
            if element_type(tab) == "Tab"
            and (
                normalize_label(element_name(tab)) in {"mainaddress", "deliveryaddress"}
                or normalize_label(element_name(tab)).startswith("additionaladdress")
            )
            and any(
                element_type(child) == "Text"
                and normalize_label(element_name(child)) == normalize_label("address type")
                and self._visible_enabled(child)
                for child in _descendants(tab)
            )
        ]
        if len(address_tabs) != 1:
            raise ManualReviewRequired(
                "cannot uniquely identify the active address Tab for role assignment"
            )
        address_tab = address_tabs[0]
        labels = [
            element
            for element in _descendants(address_tab)
            if element_type(element) == "Text"
            and normalize_label(element_name(element)) == normalize_label("address type")
            and self._visible_enabled(element)
        ]
        if len(labels) != 1:
            raise ManualReviewRequired("the active address Tab has an ambiguous address type row")
        row_label = labels[0]
        row_controls = [
            element
            for element in _descendants(address_tab)
            if element_type(element) in {"Edit", "Button"}
            and self._visible_enabled(element)
            and self._row_aligned_right(row_label, element)
        ]
        edits = [element for element in row_controls if element_type(element) == "Edit"]
        buttons = [element for element in row_controls if element_type(element) == "Button"]
        if len(edits) != 1 or len(buttons) != 1:
            raise ManualReviewRequired(
                "the address type row does not have one Edit and one associated Button"
            )
        edit_bounds = element_bounds(edits[0])
        button_bounds = element_bounds(buttons[0])
        if edit_bounds is None or button_bounds is None or button_bounds.left < edit_bounds.right:
            raise ManualReviewRequired("the address type selector Button is not right of its Edit")
        self._focus_window_for_automation(editor)
        buttons[0].click_input()

        role_controls: dict[str, Any] = {}

        def locate_role_options() -> bool:
            # SWT opens these checkboxes in a separate, unnamed foreground Pane.
            # The editor tree does not contain that popup.
            picker = self._current_window()
            elements = _descendants(picker)
            found: dict[str, Any] = {}
            for role in valid_roles:
                matches = [
                    element for element in elements
                    if element_type(element) == "CheckBox"
                    and normalize_label(element_name(element)) == normalize_label(role)
                    and self._visible_enabled(element)
                ]
                if len(matches) > 1:
                    raise ManualReviewRequired(f"multiple {role!r} address options are visible")
                if not matches:
                    return False
                found[role] = matches[0]
            role_controls.update(found)
            return True

        self._wait_until("the address type checkbox popup", locate_role_options)
        for role, checkbox in role_controls.items():
            self._set_checkbox(checkbox, normalize_label(role) in wanted)

        # Leaving the popup commits the selection. Close it before tab discovery,
        # where its Delivery address label could otherwise look like a tab.
        self._focus_window_for_automation(editor)
        row_label.click_input()

        def roles_committed() -> bool:
            value = normalize_label(self._element_value(edits[0]) or "")
            observed = {normalize_label(role) for role in valid_roles
                        if normalize_label(role) in value}
            return observed == wanted

        self._wait_until("the selected address types to be committed", roles_committed)

    @staticmethod
    def _bounds_within(bounds: Any | None, container: Any) -> bool:
        return (
            bounds is not None
            and container.left <= bounds.left
            and container.top <= bounds.top
            and bounds.right <= container.right
            and bounds.bottom <= container.bottom
        )

    def _verify_fields(
        self,
        expected: dict[str, str],
        *,
        labels: dict[str, Sequence[str]] | None = None,
        step: str,
    ) -> VerificationResult:
        observed: dict[str, str | None] = {}
        issues: list[str] = []
        resolver = self._resolver().freeze()
        for key, value in expected.items():
            query = ControlQuery.one_of(*(labels or {}).get(key, (key,)))
            raw = self._read_with_resolver(resolver, query)
            observed[key] = raw
            if raw is None or not self._equivalent_value(key, value, raw):
                issues.append(f"{key}: expected {value!r}, observed {raw!r}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or (f"{step} fields match",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence(f"{step}-readback-failed")
        return result

    @staticmethod
    def _equivalent_value(label: str, expected: str, actual: str) -> bool:
        if normalize_label(label) in {
            "date",
            "paymentdate",
            "paiddate",
            "documentdate",
        }:
            expected_date = _parse_date_text(expected)
            actual_date = _parse_date_text(actual)
            return expected_date is not None and expected_date == actual_date
        if normalize_label(label) in {
            "price",
            "pricegross",
            "costpricenet",
            "value",
            "total",
            "net",
            "gross",
            "qty",
            "uprice",
            "vat",
            "discount",
            "stock",
        }:
            left = _parse_decimal(expected)
            right = _parse_decimal(actual)
            if normalize_label(label) == "discount" and right is not None:
                # Fakturama renders a reduction as -10% after entering 10.
                # Line net and totals are verified separately against the source.
                right = abs(right)
            return left is not None and right is not None and abs(left - right) <= _MONEY_TOLERANCE
        return _normalized_equal(expected, actual)

    def _address_form_scope(self, context: Sequence[str]) -> Any:
        scope = None
        for label in context:
            scope = self._activate_debtor_tab(label)
        if not context:
            return self._current_window()
        if scope is None:
            scope = self._active_debtor_tab(context[-1])
        if scope is None:
            raise ManualReviewRequired(
                f"the {context[-1]!r} address tab is not visible after activation"
            )
        return scope

    def _fill_address(self, address: Address, *, context: Sequence[str]) -> Any:
        scope = self._address_form_scope(context)
        if address.additional_name is not None:
            self._set_field(("Additional name",), address.additional_name, scope=scope)
        self._set_field(("Street", "Street and number", "Address"), address.street, scope=scope)
        try:
            self._set_composite_row("ZIP - City", (address.zip_code, address.city), scope=scope)
        except ElementNotFound:
            self._set_field(("ZIP", "ZIP code", "Postal code", "Postcode"),
                            address.zip_code, scope=scope)
            self._set_field(("City", "Town"), address.city, scope=scope)
        self._set_field(("Country",), address.country, scope=scope)
        if address.address_specification is not None:
            self._set_field(
                ("Address specification", "Address line 2", "Additional address"),
                address.address_specification, scope=scope,
            )
        if address.district is not None:
            self._set_field(("District", "County"), address.district, scope=scope)
        return scope

    def _verify_address_fields(
        self, address: Address, context: Sequence[str]
    ) -> VerificationResult:
        scope = self._address_form_scope(context)
        expected = {
            "Street": address.street,
            "ZIP": address.zip_code,
            "City": address.city,
            "Country": address.country,
        }
        if address.additional_name is not None:
            expected["Additional name"] = address.additional_name
        if address.address_specification is not None:
            expected["Address specification"] = address.address_specification
        if address.district is not None:
            expected["District"] = address.district
        observed: dict[str, str | None] = {}
        issues: list[str] = []
        try:
            zip_code, city = self._read_composite_row("ZIP - City", scope=scope)
        except ElementNotFound:
            zip_code = self._read_optional(
                ("ZIP", "ZIP code", "Postal code", "Postcode"), scope=scope
            )
            city = self._read_optional(("City", "Town"), scope=scope)
        observed["ZIP"] = zip_code
        observed["City"] = city
        labels = {
            "ZIP": ("ZIP", "ZIP code", "Postal code", "Postcode"),
            "City": ("City", "Town"),
            "Address specification": (
                "Address specification",
                "Address line 2",
                "Additional address",
            ),
            "District": ("District", "County"),
        }
        for key, value in expected.items():
            if key not in observed:
                observed[key] = self._read_optional(labels.get(key, (key,)), scope=scope)
            actual = observed[key]
            if actual is None or not self._equivalent_value(key, value, actual):
                issues.append(f"{key}: expected {value!r}, observed {actual!r}")
        result = VerificationResult(
            verified=not issues,
            observations=tuple(issues) or ("address fields match",),
            expected=expected,
            observed=observed,
        )
        if not result.verified:
            self.capture_evidence("address-readback-failed")
        return result

    def _addresses_equal(self, first: Address, second: Address) -> bool:
        return all(
            normalize_label(str(getattr(first, field) or ""))
            == normalize_label(str(getattr(second, field) or ""))
            for field in (
                "street",
                "zip_code",
                "city",
                "country",
                "additional_name",
                "address_specification",
                "district",
            )
        )

    @staticmethod
    def _address_string(address: Address) -> str:
        return ", ".join(
            value
            for value in (
                address.street,
                getattr(address, "additional_name", None),
                address.address_specification,
                address.district,
                address.zip_code,
                address.city,
                address.country,
            )
            if value
        )

    def _active_order_editor_tab(self) -> Any:
        # Saved documents are renamed to their generated number. The header's
        # document type, not the transient "New Order" tab title, identifies them.
        window = self._current_window()
        matches = []
        for tab in _descendants(window):
            if element_type(tab) != "Tab" or not self._visible_enabled(tab):
                continue
            labels = {normalize_label(element_name(e)) for e in _descendants(tab)
                      if element_type(e) == "Text"}
            if {"no", "custref"}.issubset(labels) and labels.intersection({"order", "invoice"}):
                matches.append(tab)
            elif normalize_label(element_name(tab)) == "neworder":
                matches.append(tab)
        if len(matches) != 1:
            raise ManualReviewRequired("cannot uniquely identify the active document editor")
        return matches[0]

    def _document_editor_identity(self) -> tuple[Any, str, str]:
        editor = self._active_order_editor_tab()
        kinds = {element_name(e) for e in _descendants(editor)
                 if element_type(e) == "Text" and element_name(e) in {"Order", "Invoice"}}
        number = self._read_from_window(editor, ("No.",), allow_ocr=False)
        if len(kinds) != 1 or not number:
            raise ManualReviewRequired("the active document has no unique type and number")
        return editor, next(iter(kinds)), number

    def _order_address_tab(self, editor: Any, label: str) -> Any | None:
        matches = [
            element
            for element in _descendants(editor)
            if element_type(element) == "Tab"
            and normalize_label(element_name(element)) == normalize_label(label)
        ]
        if len(matches) > 1:
            raise ManualReviewRequired(
                f"the Order has multiple active {label!r} address panels"
            )
        return matches[0] if matches else None

    def _activate_order_address_tab(self, tab_label: str) -> Any:
        editor = self._active_order_editor_tab()
        address_tab = self._order_address_tab(editor, tab_label)
        if address_tab is None:
            tab_items = [
                element
                for element in _descendants(editor)
                if element_type(element) == "TabItem"
                and normalize_label(element_name(element)) == normalize_label(tab_label)
            ]
            if len(tab_items) != 1:
                raise ManualReviewRequired(
                    f"the Order does not expose one {tab_label!r} address tab"
                )
            window = self._current_window()
            editor_bounds = element_bounds(editor)
            if editor_bounds is None:
                raise ManualReviewRequired(
                    "cannot scope the address tab to the active Order editor"
                )
            self._focus_window_for_automation(window)
            try:
                matches = self.ocr.find_text(self._capture_window(window), (tab_label,))
            except OCRUnavailable as exc:
                raise ManualReviewRequired(
                    f"cannot safely activate the {tab_label!r} address tab "
                    "because OCR is unavailable"
                ) from exc
            window_bounds = _safe(window, "rectangle")
            if window_bounds is None:
                raise ManualReviewRequired(
                    "cannot map the address tab label to the active Order window"
                )
            scoped = []
            for match in matches:
                screen_x = match.bounds.center[0] + int(window_bounds.left)
                screen_y = match.bounds.center[1] + int(window_bounds.top)
                if (
                    editor_bounds.left <= screen_x <= editor_bounds.right
                    and editor_bounds.top <= screen_y <= editor_bounds.bottom
                ):
                    scoped.append(match)
            clusters: list[list[OCRMatch]] = []
            for match in scoped:
                overlaps = [
                    cluster
                    for cluster in clusters
                    if any(match.bounds.intersects(item.bounds) for item in cluster)
                ]
                if not overlaps:
                    clusters.append([match])
                else:
                    overlaps[0].append(match)
                    for duplicate in overlaps[1:]:
                        overlaps[0].extend(duplicate)
                        clusters.remove(duplicate)
            if len(clusters) != 1:
                raise ManualReviewRequired(
                    f"the {tab_label!r} address tab is not a unique visible label in the Order"
                )
            boxes = [match.bounds for match in clusters[0]]
            target = OCRMatch(
                tab_label,
                Bounds(
                    min(box.left for box in boxes),
                    min(box.top for box in boxes),
                    max(box.right for box in boxes),
                    max(box.bottom for box in boxes),
                ),
            )
            self._click_control(
                window,
                ResolvedControl(
                    ControlQuery.one_of(tab_label, allow_ocr=False),
                    ocr_match=target,
                ),
            )
            self._wait_until(
                f"the Order's {tab_label!r} address panel to activate",
                lambda: self._order_address_tab(self._active_order_editor_tab(), tab_label)
                is not None,
            )
            editor = self._active_order_editor_tab()
            address_tab = self._order_address_tab(editor, tab_label)
        if address_tab is None:
            raise ManualReviewRequired(
                f"the Order's {tab_label!r} address panel could not be read"
            )
        return address_tab

    def _read_order_address_display(self, tab_label: str) -> str | None:
        address_tab = self._activate_order_address_tab(tab_label)
        values = [
            value
            for element in _descendants(address_tab)
            if element_type(element) == "Edit"
            and (value := self._element_value(element))
        ]
        if len(values) != 1:
            raise ManualReviewRequired(
                f"the Order's {tab_label!r} address panel exposes {len(values)} readable summaries"
            )
        return values[0]

    # List, table, and record helpers

    def _search_if_available(self, query: str, *, window: Any | None = None) -> bool:
        try:
            self._set_field(("Search",), query, optional=True, window=window)
            return True
        except PostconditionFailed:
            return True

    def _wait_stable_rows(self, *, window: Any | None = None) -> list[Any]:
        previous: tuple[str, ...] | None = None
        stable = 0
        end = time.monotonic() + self.timeout_seconds
        latest: list[Any] = []
        while time.monotonic() < end:
            latest = self._list_rows(window=window) if window is not None else self._list_rows()
            signature = tuple(_all_text(row) for row in latest)
            if signature == previous:
                stable += 1
                if stable >= 2:
                    return latest
            else:
                stable = 0
                previous = signature
            time.sleep(self.poll_interval_seconds)
        raise TransitionTimeout("Fakturama selector results did not stabilize before timeout")

    def _list_rows(self, *, window: Any | None = None) -> list[Any]:
        root = window if window is not None else self._current_window()
        rows = [
            element
            for element in _descendants(root)
            if element_type(element) in _ROW_TYPES
        ]
        # Discard nested row wrappers when a data row itself contains row-like children.
        row_ids = {id(row) for row in rows}
        result = []
        for row in rows:
            nested = [child for child in _descendants(row) if id(child) in row_ids]
            if not nested:
                if _all_text(row):
                    result.append(row)
        return result

    def _has_edit_field(self, labels: Sequence[str]) -> bool:
        try:
            self._resolver().resolve_edit(ControlQuery.one_of(*labels, allow_ocr=False))
            return True
        except ElementNotFound:
            return False

    def _manager_contains_payment_method(self, name: str, code: str) -> bool:
        for row in self._list_rows():
            values, _ = self._row_values(row)
            observed_name = self._value(values, "Name", "Terms of payment", "Payment method")
            observed_code = self._value(values, "Code", "Payment code", "Type")
            if observed_name and _normalized_equal(observed_name, name):
                return observed_code is not None and _normalized_equal(observed_code, code)
        return False

    def _manager_contains_vat(self, name: str, rate: Decimal) -> bool:
        candidates = self.find_vats(rate)
        return (len(candidates) == 1 and candidates[0].name == name
                and candidates[0].value_percent == rate
                and candidates[0].e_invoice_code == "S")

    def _ocr_vat_candidates(self, manager: Any, expected_name: str) -> list[VatCandidate]:
        """Read SWT's painted VAT grid, then inspect the exact row's tax code."""
        if not isinstance(self.ocr, TesseractOCR):
            raise ManualReviewRequired("VAT rows require the visible table OCR reader")
        image = self._capture_window(manager)
        words = self.ocr.read_words(image)
        headers = []
        for label in ("Standard", "Name", "Description", "Value"):
            matches = [word for word in words if word.text == label]
            if len(matches) != 1:
                raise ManualReviewRequired(f"VAT grid has no unique {label} column")
            headers.append(matches[0].bounds)
        if ([box.left for box in headers] != sorted(box.left for box in headers)
                or max(box.top for box in headers) - min(box.top for box in headers) > 30):
            raise ManualReviewRequired("VAT grid headers are not aligned")
        if verified_empty_selector(
            manager, image, headers,
            search_verified=self._read_from_window(manager, ("Search",), allow_ocr=False)
                == expected_name,
            allow_grid_lines=True,
        ):
            return []
        body = [word for word in words if word.bounds.top > max(h.bottom for h in headers)
                and word.bounds.left >= headers[1].left - 10]
        groups: list[list[OCRMatch]] = []
        for word in sorted(body, key=lambda word: (word.bounds.center[1], word.bounds.left)):
            if not normalize_label(word.text):
                continue
            group = next((group for group in groups
                          if abs(group[0].bounds.center[1] - word.bounds.center[1]) <= 14), None)
            if group is None:
                groups.append([word])
            else:
                group.append(word)
        if not groups:
            raise ManualReviewRequired("VAT grid could not be verified as empty")
        candidates = []
        for group in groups:
            name_words = sorted([word for word in group
                                 if word.bounds.left < headers[2].left - 10],
                                key=lambda word: word.bounds.left)
            name = " ".join(word.text for word in name_words)
            if name != expected_name:
                raise ManualReviewRequired(f"VAT search returned an unexpected row {name!r}")
            values = sorted([word for word in group
                             if word.bounds.left >= headers[3].left - 10],
                            key=lambda word: word.bounds.left)
            amount = _parse_decimal(" ".join(word.text for word in values))
            if amount is None:
                raise ManualReviewRequired("VAT row percentage is unreadable")
            # Double-click the freshly observed name cell; no guessed row coordinates.
            from pywinauto import mouse

            self._focus_window_for_automation(manager)
            origin = element_bounds(manager)
            if origin is None:
                raise ManualReviewRequired("VAT manager bounds are unavailable")
            point = name_words[0].bounds.center
            mouse.double_click(coords=(origin.left + round(point[0] * origin.width / image.width),
                                       origin.top + round(point[1] * origin.height / image.height)))
            self._wait_until(
                "the selected VAT definition", lambda: self._has_edit_field(("Value",))
            )
            observed_name = self._read_optional(("Name",))
            observed_rate = _parse_decimal(self._read_optional(("Value",)) or "")
            code = _e_invoice_code(self._read_combo_optional(("VAT code (E-Invoice)",)) or "")
            if observed_name != name or observed_rate != amount or not code:
                raise ManualReviewRequired("VAT definition does not match the selected grid row")
            candidates.append(VatCandidate(
                token=self._token({"name": name, "value_percent": str(amount), "code": code}),
                name=name, value_percent=amount, e_invoice_code=code,
            ))
        return candidates

    def _row_values(self, row: Any) -> tuple[dict[str, str], str]:
        cells = self._row_cells(row)
        table = self._table_container(row)
        headers = self._table_headers(table) if table is not None else []
        values: dict[str, str] = {}
        for index, cell in enumerate(cells):
            value = self._element_value(cell)
            if value is None:
                value = element_name(cell)
            value = value or ""
            if index < len(headers):
                # Keep blank cells when a visible header identifies the column. This
                # distinguishes an observed empty name from an inaccessible field.
                values[headers[index]] = value.strip()
            if value:
                # Accessible cell names often already include the column name.
                info = _safe(cell, "element_info")
                automation_id = str(_safe(info, "automation_id", "") or "")
                if automation_id:
                    values[automation_id] = value
        if not values:
            row_name = element_name(row)
            if row_name:
                values["Name"] = row_name
        return values, _all_text(row)

    def _row_cells(self, row: Any) -> list[Any]:
        children = _children(row)
        cells = [child for child in children if element_type(child) in _CELL_TYPES]
        if cells:
            return sorted(
                cells,
                key=lambda cell: element_bounds(cell).left if element_bounds(cell) else 0,
            )
        descendants = [child for child in _descendants(row) if element_type(child) in _CELL_TYPES]
        # Prefer leaf values to parent containers with duplicate text.
        leaves = [
            child
            for child in descendants
            if not any(element_type(sub) in _CELL_TYPES for sub in _children(child))
        ]
        return sorted(
            leaves or descendants,
            key=lambda cell: element_bounds(cell).left if element_bounds(cell) else 0,
        )

    def _table_container(self, row: Any) -> Any | None:
        current = row
        for _ in range(8):
            parent = _safe(current, "parent")
            if parent is None:
                return None
            if element_type(parent) in {"DataGrid", "Table", "List", "Tree"}:
                return parent
            current = parent
        return None

    def _table_headers(self, table: Any) -> list[str]:
        headers = [
            element
            for element in _descendants(table)
            if element_type(element) in {"HeaderItem", "ColumnHeader"} and element_name(element)
        ]
        headers.sort(
            key=lambda element: element_bounds(element).left if element_bounds(element) else 0
        )
        return [element_name(element) for element in headers]

    @staticmethod
    def _value(values: dict[str, str], *labels: str) -> str | None:
        normalized = {normalize_label(key): value for key, value in values.items()}
        for label in labels:
            value = normalized.get(normalize_label(label))
            if value:
                return value.strip()
        # Header-less rows are mapped in visible left-to-right order at caller when safe.
        return None

    @staticmethod
    def _column_value(values: dict[str, str], *labels: str) -> str | None:
        """Read a named column while preserving a visible-but-empty cell."""
        normalized = {normalize_label(key): value for key, value in values.items()}
        for label in labels:
            key = normalize_label(label)
            if key in normalized:
                return normalized[key].strip()
        return None

    @staticmethod
    def _token(values: dict[str, Any]) -> str:
        return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _debtor_row_token(self, row: Any) -> str:
        return self._token(self._debtor_candidate_fields(row))

    def _debtor_candidate_fields(self, row: Any) -> dict[str, str | None]:
        values, row_text = self._row_values(row)
        company = self._column_value(values, "Company", "Company name", "Contact")
        first_name = self._column_value(values, "First name", "First Name", "Given name")
        last_name = self._column_value(values, "Last name", "Last Name", "Surname")
        # In Fakturama's address selector, the family-name column is captioned
        # "Name" and appears beside "First Name" and "Company".
        if last_name is None and first_name is not None and company is not None:
            last_name = self._column_value(values, "Name")
        return {
            "number": self._value(values, "No.", "Number", "Customer No.", "Debtor No."),
            "company": company,
            "first_name": first_name,
            "last_name": last_name,
            "zip_code": self._column_value(
                values, "ZIP", "Zip", "ZIP code", "Postal code", "Postcode"
            ),
            "city": self._column_value(values, "City", "Town"),
            "billing_address": self._value(values, "Invoice address", "Billing address"),
            "delivery_address": self._value(values, "Delivery address"),
            "row": row_text,
        }

    def _find_debtors_from_visible_table(
        self, dialog: Any, company_query: str
    ) -> list[DebtorCandidate]:
        rows = self._ocr_debtor_table_rows(dialog, company_query)
        return [
            DebtorCandidate(
                token=self._token(
                    {
                        "source": "ocr-debtor-row",
                        "visible_company": row["visible_company"],
                        "bounds": asdict(row["bounds"]),
                    }
                ),
                company=company_query,
                first_name=row["first_name"],
                last_name=row["last_name"],
                zip_code=row["zip_code"],
                city=row["city"],
            )
            for row in rows
        ]

    def _ocr_debtor_table_rows(
        self, dialog: Any, company_query: str
    ) -> list[dict[str, Any]]:
        # SWT paints the address selector's table without exposing UIA rows. Read
        # its visible columns, then re-read them immediately before clicking.
        if not isinstance(self.ocr, TesseractOCR):
            raise ManualReviewRequired(
                "the customer table has no accessible rows and the configured OCR engine "
                "cannot read its visible columns"
            )

        self._focus_window_for_automation(dialog)
        image = self._capture_window(dialog)
        labels = ("First Name", "Name", "Company", "ZIP", "City")
        try:
            data = self.ocr.read_text_data(image, config="--psm 6")
        except OCRUnavailable as exc:
            raise ManualReviewRequired(
                f"OCR could not read the visible customer table: {exc}"
            ) from exc
        matches = self.ocr.find_text_in_data(data, labels)
        footer_labels = self.ocr.find_text_in_data(data, ("OK", "Cancel"))
        footer_positions = [match.bounds.top for match in footer_labels]
        dialog_bounds = element_bounds(dialog)
        if dialog_bounds is not None and dialog_bounds.height > 0:
            scale_y = image.height / dialog_bounds.height
            footer_positions.extend(
                (bounds.top - dialog_bounds.top) * scale_y
                for element in _descendants(dialog)
                if element_type(element) == "Button"
                and normalize_label(element_name(element)) in {"ok", "cancel"}
                and self._visible_enabled(element)
                and (bounds := element_bounds(element)) is not None
            )
        if footer_positions:
            # Window captures may include a thin strip of the background table
            # beyond the dialog border. Its duplicate headers are below OK/Cancel.
            footer_top = min(footer_positions)
            matches = [match for match in matches if match.bounds.bottom < footer_top]
        first = [match for match in matches if normalize_label(match.text) == "firstname"]
        company = [match for match in matches if normalize_label(match.text) == "company"]
        zip_code = [match for match in matches if normalize_label(match.text) == "zip"]
        city = [match for match in matches if normalize_label(match.text) == "city"]
        if not (len(first) == len(company) == len(zip_code) == len(city) == 1):
            raise ManualReviewRequired(
                "the customer selector does not expose a unique set of First Name, Company, "
                "ZIP, and City headers to OCR"
            )
        first_header, company_header, zip_header, city_header = (
            first[0], company[0], zip_code[0], city[0]
        )
        family_name = [
            match
            for match in matches
            if normalize_label(match.text) == "name"
            and match.bounds.left >= first_header.bounds.right
            and match.bounds.center[0] < company_header.bounds.center[0]
        ]
        if len(family_name) != 1:
            raise ManualReviewRequired(
                "the customer selector's separate family-name column could not be identified"
            )
        last_header = family_name[0]
        headers = (first_header, last_header, company_header, zip_header, city_header)
        centers = [header.bounds.center[0] for header in headers]
        if centers != sorted(centers) or len(set(centers)) != len(centers):
            raise ManualReviewRequired(
                "the customer selector column positions are ambiguous"
            )
        boundaries = [
            (left + right) // 2
            for left, right in zip(centers, centers[1:], strict=False)
        ]

        # Native body geometry excludes border/background OCR noise. A stable
        # empty body is stronger evidence than stray OCR glyphs outside it.
        search_readback = self._read_from_window(dialog, ("Search",), allow_ocr=False)
        if verified_empty_selector(
            dialog,
            image,
            [header.bounds for header in headers],
            search_verified=(
                search_readback is not None
                and _normalized_equal(search_readback, company_query)
            ),
        ):
            # The selector can repaint while the second blank capture is
            # taken; confirm the searched identity did not change.
            final_search = self._read_from_window(dialog, ("Search",), allow_ocr=False)
            if final_search is not None and _normalized_equal(
                final_search, company_query
            ):
                _LOGGER.info("No matching Debtor rows are visible for %s", company_query)
                return []

        groups: dict[tuple[int, int, int], list[tuple[int, int, int, int, str]]] = {}
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
            left = int(data["left"][index])
            top = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            groups.setdefault(key, []).append((left, top, width, height, text))

        all_text = " ".join(
            word[4] for words in groups.values() for word in words
        )
        normalized_all_text = normalize_label(all_text)
        if any(
            phrase in normalized_all_text
            for phrase in ("noentries", "noresults", "nomatchingresults", "nodata")
        ):
            return []

        names = ("first_name", "last_name", "company", "zip_code", "city")
        header_bottom = max(header.bounds.bottom for header in headers)
        footer_tops = [
            match.bounds.top
            for match in footer_labels
            if match.bounds.top > header_bottom + 100
        ]
        table_footer = footer_positions or footer_tops
        table_bottom = min(table_footer) - 8 if table_footer else image.height - 150
        visible_rows: list[dict[str, Any]] = []
        incomplete_row_seen = False
        for words in groups.values():
            words.sort(key=lambda word: word[0])
            row_top = min(word[1] for word in words)
            if row_top <= header_bottom or row_top >= table_bottom:
                continue
            table_words = [
                word
                for word in words
                if first_header.bounds.left
                <= word[0] + word[2] // 2
                <= city_header.bounds.right
            ]
            if not table_words or not any(normalize_label(word[4]) for word in table_words):
                # Scrollbar arrows and grid borders do not constitute a data row.
                continue
            columns: dict[str, list[str]] = {name: [] for name in names}
            used_words = []
            for left, top, width, height, text in table_words:
                center = left + width // 2
                column_index = sum(center > boundary for boundary in boundaries)
                if column_index >= len(names):
                    continue
                columns[names[column_index]].append(text)
                used_words.append((left, top, width, height))
            values = {
                name: " ".join(parts).strip()
                for name, parts in columns.items()
            }
            if not any(values.values()):
                continue
            if not all(values.values()):
                incomplete_row_seen = True
                continue

            visible_company = values["company"].strip()
            normalized_company_prefix = normalize_label(
                visible_company.rstrip(" .…")
            )
            normalized_query = normalize_label(company_query)
            if not normalized_company_prefix or not normalized_query.startswith(
                normalized_company_prefix
            ):
                incomplete_row_seen = True
                continue
            visible_rows.append(
                {
                    **values,
                    "visible_company": visible_company,
                    "bounds": Bounds(
                        min(word[0] for word in used_words),
                        min(word[1] for word in used_words),
                        max(word[0] + word[2] for word in used_words),
                        max(word[1] + word[3] for word in used_words),
                    ),
                }
            )

        if incomplete_row_seen:
            raise ManualReviewRequired(
                "the customer selector shows a row whose company, person, ZIP, or city "
                "cannot be verified; refusing to create a possible duplicate"
            )
        if not visible_rows:
            raise ManualReviewRequired(
                "the customer selector exposes no UIA rows and could not verify "
                "a complete visible customer row or a stable empty result"
            )
        return visible_rows

    def _product_row_token(self, row: Any) -> str:
        values, row_text = self._row_values(row)
        return self._token(
            {
                "sku": self._value(
                    values, "Item Number", "Item number", "SKU", "Product number", "Number"
                ),
                "name": self._value(values, "Name", "Description"),
                "vat": str(
                    _parse_decimal(self._value(values, "VAT", "VAT rate", "Tax rate") or "")
                ),
                "row": row_text,
            }
        )

    def _find_row_by_token(
        self,
        token: str,
        token_for_row: Callable[[Any], str],
        *,
        window: Any | None = None,
    ) -> Any:
        rows = self._list_rows(window=window) if window is not None else self._list_rows()
        matches = [row for row in rows if token_for_row(row) == token]
        if not matches:
            raise ElementNotFound(
                "candidate disappeared from the current selector; re-run exact lookup"
            )
        if len(matches) > 1:
            raise ManualReviewRequired("candidate token matches multiple visible rows")
        return matches[0]

    def _row_cell(self, row: Any, header: str) -> Any | None:
        table = self._table_container(row)
        if table is None:
            return None
        headers = self._table_headers(table)
        indices = [
            index
            for index, title in enumerate(headers)
            if normalize_label(title) == normalize_label(header)
        ]
        if len(indices) != 1:
            if not indices:
                return None
            raise ManualReviewRequired(f"Order grid contains ambiguous {header!r} columns")
        cells = self._row_cells(row)
        return cells[indices[0]] if indices[0] < len(cells) else None

    def _find_order_line_row(self, sku: str) -> Any:
        order_editor = self._active_order_editor_tab()
        window = self._main_window if self._main_window is not None else self._current_window()
        editor_rows = self._list_rows(window=order_editor)
        self._order_line_uia_rows_in_editor = bool(editor_rows)
        roots = [(order_editor, editor_rows)]
        if window is not order_editor:
            # SWT's UIA provider exposes the Order's table as a sibling of the
            # document Tab page on some Fakturama builds. Keep the page-scoped
            # lookup first, then accept only a visible, Order-shaped grid row
            # whose screen bounds are inside the active Order page.
            roots.append((window, self._list_rows(window=window)))

        matches = []
        unreadable_matches = []
        seen_rows: set[int] = set()
        editor_bounds = element_bounds(order_editor)
        for root, rows in roots:
            for row in rows:
                if id(row) in seen_rows:
                    continue
                seen_rows.add(id(row))
                if root is not order_editor and not self._is_visible_order_grid_row(
                    row, editor_bounds
                ):
                    continue
                values, row_text = self._row_values(row)
                observed_sku = self._value(
                    values,
                    "Item Number",
                    "Item number",
                    "Item No.",
                    "SKU",
                    "Product number",
                    "Product No.",
                    "Article number",
                )
                if observed_sku is not None:
                    if sku_matches(sku, observed_sku):
                        matches.append(row)
                    continue
                # Row text is useful only to distinguish a missing exact identity
                # from an absent line. It never authorizes editing a row.
                if normalize_label(sku) in normalize_label(row_text):
                    unreadable_matches.append(row)
        if unreadable_matches:
            raise ManualReviewRequired(
                f"Order row mentions SKU {sku!r}, but its exact SKU cell is not accessible"
            )
        if len(matches) != 1:
            if not matches:
                raise ElementNotFound(
                    f"selected Product line with exact SKU {sku!r} is not visible in the Order grid"
                )
            raise ManualReviewRequired(f"multiple Order lines have exact SKU {sku!r}")
        return matches[0]

    def _is_visible_order_grid_row(self, row: Any, editor_bounds: Bounds | None) -> bool:
        row_bounds = element_bounds(row)
        if (
            editor_bounds is None
            or row_bounds is None
            or _safe(row, "is_visible", None) is not True
            or row_bounds.left < editor_bounds.left
            or row_bounds.top < editor_bounds.top
            or row_bounds.right > editor_bounds.right
            or row_bounds.bottom > editor_bounds.bottom
        ):
            return False

        table = self._table_container(row)
        if table is None:
            return False
        headers = {normalize_label(header) for header in self._table_headers(table)}
        header_groups = (
            {"itemnumber", "itemno", "sku", "productnumber", "productno", "articlenumber"},
            {"qty", "quantity"},
            {"uprice", "unitprice", "priceperunit"},
            {"vat"},
            {"discount"},
            {"price"},
        )
        return all(headers.intersection(aliases) for aliases in header_groups)

    def _fill_order_line_ocr(self, item: Item) -> VerificationResult:
        expected = self._expected_line_values(item)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                snapshot = self._capture_order_line_ocr(item.sku, select_row=True)
                break
            except ElementNotFound:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(self.poll_interval_seconds, remaining))
        vat = snapshot["observed"].get("VAT")
        if vat is None or not self._equivalent_value("VAT", expected["VAT"], vat):
            result = self._order_line_ocr_result(item, snapshot)
            self.capture_evidence(f"line-verification-{item.sku}")
            return result

        for column in ("Qty.", "U.Price", "Discount"):
            observed = snapshot["observed"].get(column)
            if observed is not None and self._equivalent_value(
                column, expected[column], observed
            ):
                continue
            self._write_visible_order_cell(
                snapshot["window"],
                snapshot["cells"][column],
                expected[column],
                column,
            )

        result = self._order_line_ocr_result(
            item, self._capture_order_line_ocr(item.sku, select_row=False)
        )
        if not result.verified:
            self.capture_evidence(f"line-verification-{item.sku}")
        return result

    def _verify_order_line_ocr(self, item: Item) -> VerificationResult:
        for attempt in range(4):
            try:
                snapshot = self._capture_order_line_ocr(item.sku, select_row=False)
                break
            except ElementNotFound:
                if attempt == 3 or not self._scroll_document_items_down():
                    raise
                _LOGGER.info("Revealing the next Items row to verify %s", item.sku)
        result = self._order_line_ocr_result(item, snapshot)
        if not result.verified:
            self.capture_evidence(f"line-resume-verification-{item.sku}")
        return result

    def _scroll_document_items_down(self) -> bool:
        editor = self._active_order_editor_tab()
        bounds = element_bounds(editor)
        remarks = self._resolver(editor).resolve_edit(
            ControlQuery.one_of("Remarks", allow_ocr=False)
        ).element
        remarks_bounds = element_bounds(remarks)
        if bounds is None or remarks_bounds is None:
            return False
        buttons = [e for e in _descendants(editor)
                   if element_type(e) == "Button" and element_name(e) == "Line down"
                   and self._visible_enabled(e) and (b := element_bounds(e)) is not None
                   and b.right > bounds.right - 70 and b.bottom < remarks_bounds.top]
        if len(buttons) != 1:
            return False
        buttons[0].click_input()
        return True

    def _order_line_ocr_result(
        self, item: Item, snapshot: dict[str, Any]
    ) -> VerificationResult:
        expected = self._expected_line_values(item)
        observed = snapshot["observed"]
        issues = [
            f"{column}: expected {value}, observed {observed.get(column)!r} "
            "from the visible Order row"
            for column, value in expected.items()
            if observed.get(column) is None
            or not self._equivalent_value(column, value, observed[column])
        ]
        return VerificationResult(
            verified=not issues,
            observations=tuple(issues) or (f"Product {item.sku} visible row fields match",),
            expected=expected,
            observed=observed,
        )

    def _capture_order_line_ocr(
        self, sku: str, *, select_row: bool
    ) -> dict[str, Any]:
        reader = getattr(self.ocr, "read_words", None)
        if not callable(reader):
            raise ManualReviewRequired(
                "Fakturama exposes no UIA grid rows and the configured OCR provider "
                "cannot read positioned cell values"
            )
        window = self._main_window if self._main_window is not None else self._current_window()
        editor = self._active_order_editor_tab()
        items_bounds = element_bounds(self._items_pane(editor=editor))
        editor_bounds = element_bounds(editor)
        rect = _safe(window, "rectangle")
        if items_bounds is None or editor_bounds is None or rect is None:
            raise ManualReviewRequired(
                "cannot scope the visible Product SKU to the active Order's Items grid"
            )
        self._focus_window_for_automation(window)
        self._focus_order_header_for_grid(editor)
        image = self._capture_window(window)
        words = reader(image)
        sku_word = self._visible_order_sku_word(
            sku, words, rect, editor_bounds, items_bounds
        )
        if select_row:
            self._click_control(
                window,
                ResolvedControl(
                    ControlQuery.one_of("visible exact Order Product SKU", allow_ocr=False),
                    ocr_match=sku_word,
                ),
            )
            time.sleep(self.poll_interval_seconds)
            self._focus_order_header_for_grid(editor)
            image = self._capture_window(window)
            words = reader(image)
            sku_word = self._visible_order_sku_word(
                sku, words, rect, editor_bounds, items_bounds
            )

        cells = self._visible_order_cell_bounds(image, sku_word.bounds)
        observed = self._visible_order_cell_values(words, sku_word.bounds, cells)
        return {
            "window": window,
            "image": image,
            "sku": sku_word,
            "cells": cells,
            "observed": observed,
        }

    def _focus_order_header_for_grid(self, editor: Any) -> None:
        if os.name != "nt" or not _safe(editor, "handle"):
            return
        # A caret in the SKU editor is sometimes recognized as a leading I.
        # Leaving the cell commits it and keeps the row readable without edits.
        field = self._resolver(editor).resolve_edit(
            ControlQuery.one_of("Cust.Ref.", "Customer reference", allow_ocr=False)
        ).element
        field.set_focus()
        if _safe(field, "has_keyboard_focus", False) is not True:
            field.click_input()
            field.set_focus()
        if _safe(field, "has_keyboard_focus", False) is not True:
            raise ManualReviewRequired("could not move focus out of the Order grid")

    def _visible_order_sku_word(
        self,
        sku: str,
        words: Sequence[OCRMatch],
        rect: Any,
        editor_bounds: Bounds,
        items_bounds: Bounds,
    ) -> OCRMatch:
        matches = []
        seen: set[tuple[int, int, int, int]] = set()
        for word in words:
            if not sku_matches(sku, word.text):
                continue
            bounds = word.bounds
            key = (bounds.left, bounds.top, bounds.right, bounds.bottom)
            if key in seen:
                continue
            seen.add(key)
            screen_bounds = Bounds(
                bounds.left + int(rect.left),
                bounds.top + int(rect.top),
                bounds.right + int(rect.left),
                bounds.bottom + int(rect.top),
            )
            if (
                screen_bounds.left > items_bounds.right
                and items_bounds.top <= screen_bounds.center[1] <= items_bounds.bottom
                and editor_bounds.left <= screen_bounds.left
                and screen_bounds.right <= editor_bounds.right
                and editor_bounds.top <= screen_bounds.top
                and screen_bounds.bottom <= editor_bounds.bottom
            ):
                matches.append(word)

        if not matches:
            raise ElementNotFound(
                f"exact SKU {sku!r} is not visible in the active Order's Items grid"
            )
        if len(matches) > 1:
            raise ManualReviewRequired(
                f"multiple visible Order rows contain exact SKU {sku!r}"
            )
        return matches[0]

    def _visible_order_cell_bounds(
        self, image: Any, sku_bounds: Bounds
    ) -> dict[str, Bounds]:
        try:
            width_px, height_px = image.size
            get_pixel = image.getpixel
        except Exception as exc:
            raise ManualReviewRequired(
                f"cannot inspect the selected Order row boundaries: {exc}"
            ) from exc
        sample_top = max(0, sku_bounds.top - 8)
        sample_bottom = min(height_px - 1, sku_bounds.top - 2)
        sample_y = list(range(sample_top, sample_bottom + 1))
        center_x = sku_bounds.center[0]
        if len(sample_y) < 3 or not (0 <= center_x < width_px):
            raise ManualReviewRequired("the selected Order row has no safe visible boundary strip")
        background_samples = [get_pixel((center_x, y)) for y in sample_y]
        background = max(set(background_samples), key=background_samples.count)
        if background_samples.count(background) < len(sample_y) - 1:
            raise ManualReviewRequired(
                "the selected Order row does not have a stable background for cell targeting"
            )

        def is_background(x: int) -> bool:
            if not 0 <= x < width_px:
                return False
            return sum(get_pixel((x, y)) == background for y in sample_y) >= len(sample_y) - 1

        def nearest_separator(direction: int) -> float:
            x = center_x + direction
            while 0 <= x < width_px:
                if is_background(x):
                    x += direction
                    continue
                run_start = x
                while 0 <= x < width_px and not is_background(x):
                    x += direction
                run_end = x - direction
                run_width = abs(run_end - run_start) + 1
                if run_width <= 8 and 0 <= x < width_px and is_background(x):
                    return (run_start + run_end) / 2
            raise ManualReviewRequired(
                "the selected Order row's Item No. cell boundaries are not visible"
            )

        item_left = nearest_separator(-1)
        item_right = nearest_separator(1)
        column_width = item_right - item_left
        if not 80 <= column_width <= 600:
            raise ManualReviewRequired(
                "the visible Item No. column width is not safe for Order cell targeting"
            )
        row_top = max(0, sku_bounds.top - 13)
        row_bottom = min(height_px, sku_bounds.bottom + 12)

        def column(index: int) -> Bounds:
            left = round(item_left + (index - 2) * column_width)
            right = round(item_left + (index - 1) * column_width)
            return Bounds(left, row_top, right, row_bottom)

        return {
            "Qty.": column(1),
            "VAT": column(6),
            "U.Price": column(7),
            "Discount": column(8),
            "Price": column(9),
        }

    @staticmethod
    def _visible_order_cell_values(
        words: Sequence[OCRMatch], sku_bounds: Bounds, cells: dict[str, Bounds]
    ) -> dict[str, str | None]:
        row_center = sku_bounds.center[1]
        row_tolerance = max(16, sku_bounds.height)
        observed: dict[str, str | None] = {}
        for label, bounds in cells.items():
            cell_words = [
                word
                for word in words
                if bounds.left + 3 <= word.bounds.center[0] <= bounds.right - 3
                and abs(word.bounds.center[1] - row_center) <= row_tolerance
                and normalize_label(word.text) not in {"", "|"}
            ]
            cell_words.sort(key=lambda word: word.bounds.left)
            text = " ".join(word.text for word in cell_words)
            if label == "VAT":
                rates = {_parse_decimal(value) for value in re.findall(
                    r"(\d+(?:[.,]\d+)?)\s*%", text
                )}
                value = next(iter(rates)) if len(rates) == 1 else None
            else:
                value = _parse_decimal(text)
            observed[label] = _decimal_text(value) if value is not None else None
        return observed

    def _write_visible_order_cell(
        self, window: Any, bounds: Bounds, value: str, label: str
    ) -> None:
        rect = _safe(window, "rectangle")
        if rect is None:
            raise ManualReviewRequired(
                f"cannot map the visible {label} cell to the screen"
            )
        try:
            from pywinauto.keyboard import send_keys
            from pywinauto.mouse import double_click
        except ImportError as exc:
            raise ManualReviewRequired(
                "keyboard editing of Fakturama's custom-drawn Order grid is unavailable"
            ) from exc

        self._focus_window_for_automation(window)
        screen_point = (
            int(rect.left) + bounds.center[0],
            int(rect.top) + bounds.center[1],
        )
        try:
            double_click(coords=screen_point)
            send_keys("^a")
            send_keys(value, with_spaces=True)
            send_keys("{ENTER}")
        except Exception as exc:
            raise ManualReviewRequired(
                f"could not enter {value!r} in the visible Order {label} cell: {exc}"
            ) from exc

    def _wait_for_order_line_row(self, sku: str) -> Any:
        """Wait briefly for the Order grid to publish a newly selected Product row."""
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                return self._find_order_line_row(sku)
            except ElementNotFound:
                if (
                    self._application is not None
                    and not getattr(self, "_order_line_uia_rows_in_editor", True)
                ):
                    # The live Order exposes its table as a custom-drawn Pane,
                    # so another UIA polling pass cannot make a row appear.
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(self.poll_interval_seconds, remaining))

    @staticmethod
    def _click_element(element: Any) -> None:
        element.click_input()

    def _ensure_product_search_preferences(self) -> None:
        if self._product_search_configured or not _safe(self._main_window, "handle"):
            return
        _LOGGER.info("Checking that Product search requires explicit selection")
        selector = self._visible_dialog_root(("Select a product",), required=False)
        if selector is not None:
            self._close_selector_dialog(("Select a product",), selector)
        self._click(ControlQuery.one_of("File", control_types=("MenuItem",), allow_ocr=False))
        self._click(ControlQuery.one_of(
            "Preferences", control_types=("MenuItem",), allow_ocr=False
        ))
        self._wait_until("Preferences", lambda: self._window_title(
            self._current_window()
        ) == "Preferences")
        dialog = self._current_window()
        self._click_control(dialog, self._resolver(dialog).resolve(ControlQuery.one_of(
            "Documents", control_types=("TreeItem",), allow_ocr=False
        )))
        checkbox = self._resolver(dialog).resolve(ControlQuery.one_of(
            "immediately take over a clearly found item number",
            control_types=("CheckBox",), allow_ocr=False,
        )).element
        state = _safe(checkbox, "get_toggle_state")
        if state not in (0, 1):
            raise ManualReviewRequired("the automatic Product selection setting is unreadable")
        if state == 1:
            _LOGGER.info("Disabling automatic acceptance of a single Product search result")
            checkbox.click_input()
        self._wait_until("automatic Product selection to be disabled",
                         lambda: _safe(checkbox, "get_toggle_state") == 0)
        self._click_control(dialog, self._resolver(dialog).resolve(ControlQuery.one_of(
            "Apply and Close", control_types=("Button",), allow_ocr=False
        )))
        self._wait_until("Preferences to close", lambda: self._visible_dialog_root(
            ("Preferences",), required=False
        ) is None)
        self._product_search_configured = True

    def _open_product_selector(self) -> None:
        self._ensure_product_search_preferences()
        if self._visible_dialog_root(("Select a product",), required=False) is not None:
            return
        order_window = self._current_window()
        self._require_editor("order")
        pane = self._items_pane()
        selector_image = self._items_pane_images(pane)[0]
        self._focus_window_for_automation(order_window)
        self._click_control(
            order_window,
            ResolvedControl(
                ControlQuery.one_of("upper Product selector icon", allow_ocr=False),
                element=selector_image,
            ),
        )
        self._wait_until(
            "open the Order's Product selector",
            lambda: self._visible_dialog_root(("Select a product",), required=False)
            is not None,
        )

    def _data_manager_tab(self, item_label: str) -> Any | None:
        window = self._main_window if self._main_window is not None else self._current_window()
        matches = [element for element in _descendants(window)
                   if element_type(element) == "Tab"
                   and normalize_label(element_name(element)) == normalize_label(item_label)
                   and (self._visible_enabled(element) or any(
                       element_type(child) in {"Edit", "Button"} and self._visible_enabled(child)
                       for child in _descendants(element)
                   ))]
        if len(matches) > 1:
            raise ManualReviewRequired(f"multiple active {item_label!r} manager tabs")
        return matches[0] if matches else None

    def _open_data_manager(self, item_label: str) -> None:
        # Sidebar and inactive tab labels do not mean the manager is active.
        if self._data_manager_tab(item_label) is not None:
            return
        self._click(ControlQuery.one_of("Data", control_types=("MenuItem",), allow_ocr=False))
        self._click(ControlQuery.one_of(item_label, control_types=("MenuItem",), allow_ocr=False))
        self._wait_until(f"the {item_label} manager tab",
                         lambda: self._data_manager_tab(item_label) is not None)

    def _open_documents_view(self) -> None:
        self._open_data_manager("Documents")

    def _documents_grid_visible(self) -> bool:
        return self._data_manager_tab("Documents") is not None

    def _find_document_row(self, number: str) -> Any:
        matches = []
        for row in self._list_rows():
            values, _ = self._row_values(row)
            row_number = self._value(values, "No.", "Number", "Document number", "Document No.")
            if row_number and _normalized_equal(row_number, number):
                matches.append(row)
        if not matches:
            raise ElementNotFound(f"Documents has no visible row for Order {number!r}")
        if len(matches) > 1:
            raise ManualReviewRequired(f"Documents shows more than one row for Order {number!r}")
        return matches[0]

    def _active_editor_context(self) -> tuple[str, str | None] | None:
        if self._has_invoice_editor():
            number = (
                self._invoice_ref.number if self._invoice_ref else self._read_optional(("No.",))
            )
            return ("Invoice", number)
        if self._has_order_editor():
            number = self._order_ref.number if self._order_ref else self._read_optional(("No.",))
            return ("New Order", number)
        return None

    def _restore_editor_context(self, title: str, number: str | None) -> None:
        if title == "Invoice":
            self._activate_document_tab("Invoice", number)
            self._wait_until("the original Invoice editor to return", self._has_invoice_editor)
        else:
            self._activate_document_tab(title, number)
            self._wait_until("the original Order editor to return", self._has_order_editor)

    def _open_or_create_delivery_address(self) -> str:
        editor = self._active_editor_tab()
        address_names: dict[str, str] = {}
        active_tabs: dict[str, Any] = {}
        for element in _descendants(editor):
            if (
                element_type(element) not in {"Tab", "TabItem"}
            ):
                continue
            name = element_name(element)
            normalized = normalize_label(name)
            if normalized == normalize_label("Delivery address") or normalized.startswith(
                normalize_label("additional address")
            ):
                address_names[normalized] = name
                if element_type(element) == "Tab" and self._active_debtor_tab(name) is not None:
                    active_tabs[normalized] = element

        if len(address_names) > 1:
            raise ManualReviewRequired(
                "multiple existing Delivery/additional address tabs are visible"
            )
        if address_names:
            normalized, label = next(iter(address_names.items()))
            if normalized not in active_tabs:
                self._activate_debtor_tab(label)
            return label

        # OCR-only address tabs are scoped to the visible New Debtor editor, not
        # to similarly named sidebar text elsewhere on the desktop.
        labels = ["Delivery address", *(f"additional address #{index}" for index in range(1, 21))]
        finder = getattr(self.ocr, "find_text", None)
        ocr_matches = []
        if callable(finder):
            try:
                raw_matches = finder(self._capture_window(self._current_window()), tuple(labels))
            except OCRUnavailable:
                raw_matches = []
            window_bounds = _safe(self._current_window(), "rectangle")
            editor_bounds = element_bounds(editor)
            if raw_matches and (window_bounds is None or editor_bounds is None):
                raise ManualReviewRequired(
                    "cannot scope OCR address tabs to the visible New Debtor editor"
                )
            for match in raw_matches:
                normalized = normalize_label(str(getattr(match, "text", "")))
                if normalized not in {normalize_label(label) for label in labels}:
                    continue
                x, y = match.bounds.center
                x += int(window_bounds.left)
                y += int(window_bounds.top)
                if (
                    editor_bounds.left <= x <= editor_bounds.right
                    and editor_bounds.top <= y <= editor_bounds.bottom
                ):
                    ocr_matches.append((normalized, match))
        distinct_ocr_tabs = {normalized for normalized, _ in ocr_matches}
        if len(distinct_ocr_tabs) > 1:
            raise ManualReviewRequired(
                "OCR found multiple existing Delivery/additional address tabs in the editor"
            )
        if ocr_matches:
            normalized = next(iter(distinct_ocr_tabs))
            label = next(label for label in labels if normalize_label(label) == normalized)
            selected = [match for name, match in ocr_matches if name == normalized]
            if len(selected) != 1:
                raise ManualReviewRequired(f"OCR found multiple matches for {label!r}")
            self._click_control(
                self._current_window(),
                ResolvedControl(ControlQuery.one_of(label), ocr_match=selected[0]),
            )
            return label

        self._activate_debtor_tab("Main address")
        main_tab = self._active_debtor_tab("Main address")
        if main_tab is None:
            raise ManualReviewRequired("the Main address tab is not active for address creation")
        main_bounds = element_bounds(main_tab)
        if main_bounds is None:
            raise ManualReviewRequired("the Main address tab has no usable bounds")
        plus_buttons = [
            element
            for element in _descendants(main_tab)
            if element_type(element) == "Button"
            and element_name(element) == "+"
            and self._visible_enabled(element)
            and self._bounds_within(element_bounds(element), main_bounds)
        ]
        if len(plus_buttons) != 1:
            raise ManualReviewRequired(
                f"Main address must expose exactly one scoped + button; found {len(plus_buttons)}"
            )
        self._focus_window_for_automation(editor)
        plus_buttons[0].click_input()

        def new_address_tab() -> bool:
            tabs = [
                tab
                for tab in _descendants(editor)
                if element_type(tab) == "Tab"
                and normalize_label(element_name(tab)).startswith(
                    normalize_label("additional address")
                )
                and self._active_debtor_tab(element_name(tab)) is not None
            ]
            return len(tabs) == 1

        self._wait_until("the new additional address tab to open", new_address_tab)
        tabs = [
            tab
            for tab in _descendants(editor)
            if element_type(tab) == "Tab"
            and normalize_label(element_name(tab)).startswith(
                normalize_label("additional address")
            )
            and self._active_debtor_tab(element_name(tab)) is not None
        ]
        if len(tabs) != 1:
            raise ManualReviewRequired(
                "cannot uniquely identify the newly active additional address tab"
            )
        return element_name(tabs[0])

    def _close_dialog_if_present(self, likely_labels: Sequence[str]) -> None:
        window = self._current_window()
        main = self._main_window
        if main is not None and (
            window is main or (
                _safe(window, "handle") and _safe(window, "handle") == _safe(main, "handle")
            )
        ):
            return
        if not self._dialog_is_open(likely_labels):
            return
        try:
            self._click(
                ControlQuery.one_of(
                    "Cancel", "Close", "Done", control_types=("Button", "MenuItem"), allow_ocr=True
                )
            )
        except (ElementNotFound, ManualReviewRequired):
            raise ManualReviewRequired(
                f"the {likely_labels[0]!r} dialog is open but has no unambiguous close action"
            ) from None
        self._wait_until(
            f"the {likely_labels[0]!r} dialog to close",
            lambda: not self._dialog_is_open(likely_labels),
        )

    def _activate_document_tab(self, title: str, number: str | None) -> None:
        if number:
            try:
                if self._document_editor_identity()[2] == number:
                    return
            except ManualReviewRequired:
                pass
            window = self._current_window()
            tabs = [e for e in _descendants(window) if element_type(e) == "TabItem"
                    and (element_name(e).lstrip("*") == number
                         or (title == "Invoice"
                             and normalize_label(element_name(e)) == "newinvoice"))]
            if len(tabs) == 1:
                # SWT tab bounds may cover the whole editor. Prefer its semantic
                # selection pattern; never click the centre of those bounds.
                selection = _safe(tabs[0], "iface_selection_item")
                if selection is not None:
                    try:
                        selection.Select()
                    except Exception as exc:
                        # SWT may advertise SelectionItem but reject Select.
                        # Recheck the result before using a fresh visual target.
                        _LOGGER.debug("Saved tab UIA selection unavailable: %s", exc)
                        if self._document_editor_identity()[2] == number:
                            return
                    else:
                        self._wait_until(
                            "the requested saved document tab",
                            lambda: self._document_editor_identity()[2] == number,
                        )
                        return
                tab_label = element_name(tabs[0]).lstrip("*")
                image = self._capture_window(window)
                bounds = element_bounds(self._active_order_editor_tab())
                rect = _safe(window, "rectangle")
                def in_tab_strip(match: OCRMatch) -> bool:
                    return bool(bounds and rect and bounds.top <=
                                match.bounds.center[1] + rect.top < bounds.top + 70)
                # Scope before deciding whether the fallback is needed: the
                # document number also appears in the form and Documents grid.
                matches = [m for m in self.ocr.find_text(image, (tab_label,))
                           if in_tab_strip(m)]
                if not matches and tab_label == number and isinstance(self.ocr, TesseractOCR):
                    def locator_text(value: str) -> str:
                        # OCR can read a final 5 as S. This only locates a tab;
                        # its authoritative No. is checked after activation.
                        return re.sub(r"[OQ]", "0", value.upper().lstrip("*")).replace("S", "5")
                    expected = locator_text(number)
                    matches = [m for m in self.ocr.read_words(image, preprocess=False)
                               if in_tab_strip(m) and locator_text(m.text) == expected]
                if len(matches) != 1:
                    raise ManualReviewRequired(
                        f"saved document tab {number!r}: found {len(matches)} labels in tab strip"
                    )
                self._click_control(window, ResolvedControl(query=ControlQuery.one_of(number),
                                                           ocr_match=matches[0]))
                if self._document_editor_identity()[2] != number:
                    raise ManualReviewRequired("the requested saved document did not activate")
                return
        if normalize_label(title) == normalize_label("New Order"):
            if self._has_order_editor(number):
                return
            window = self._main_window if self._main_window is not None else self._current_window()
            tabs = self._new_order_tab_items(window)
            if len(tabs) != 1:
                raise ManualReviewRequired(
                    f"cannot uniquely activate the existing {title} document tab"
                )
            editor = self._select_open_order_tab(
                window, tabs[0], ocr_matches=self._order_tab_ocr_matches(window, 1)
            )
            actual_number = self._read_from_window(editor, ("No.",))
            if number and actual_number != number:
                raise ManualReviewRequired("the selected Order number differs from this run")
            if self._last_source is not None:
                reference = self._read_from_window(editor, ("Cust.Ref.", "Customer reference"))
                if not reference or not _normalized_equal(
                    reference, self._last_source.external_reference
                ):
                    raise ManualReviewRequired("the selected Order reference differs from this run")
            return

        labels = (f"{title} {number}", title) if number else (title,)
        try:
            self._click(
                ControlQuery.one_of(*labels, control_types=("TabItem",), allow_ocr=False)
            )
        except ElementNotFound:
            # It may already be active. Do not synthesize a positional click.
            if not self._has_invoice_editor():
                raise

    def _record_appears_in_documents(self, number: str, doc_type: str) -> bool:
        if not self._is_attached():
            return False
        try:
            rows = self._list_rows()
            for row in rows:
                values, text = self._row_values(row)
                row_number = self._value(values, "No.", "Number", "Document number", "Document No.")
                row_type = self._value(values, "Type", "Document type")
                if (
                    row_number
                    and _normalized_equal(row_number, number)
                    and (not row_type or _normalized_equal(row_type, doc_type))
                ):
                    return True
                if _normalized_equal(number, text) and doc_type.casefold() in text.casefold():
                    return True
        except Exception:
            return False
        return False

    # Window state and wait helpers

    def _is_attached(self) -> bool:
        return self._application is not None and self._main_window is not None

    def _require_window(self) -> Any:
        if not self._is_attached():
            raise GatewayError("Fakturama is not attached; call attach_or_launch first")
        return self._current_window()

    def _require_editor(self, kind: str) -> None:
        if kind == "order" and not self._has_order_editor():
            self._raise_visible_error_dialog()
            raise ManualReviewRequired("the expected Order editor is not open")
        if kind == "invoice" and not self._has_invoice_editor():
            self._raise_visible_error_dialog()
            raise ManualReviewRequired("the expected Invoice editor is not open")

    def _raise_visible_error_dialog(self) -> None:
        """Report a blocking Fakturama error without clicking or dismissing it."""
        if not self._is_attached():
            return
        for window in self._application_windows():
            if _safe(window, "is_visible", None) is not True:
                continue
            title = self._window_title(window)
            if normalize_label(title) not in {"internalerror", "error"}:
                continue
            details = list(dict.fromkeys(
                element_name(element).strip()
                for element in _descendants(window)
                if element_type(element) in {"Text", "Static", "Edit"}
                and element_name(element).strip()
            ))
            detail = "; ".join(details)[:600]
            raise ManualReviewRequired(
                f"Fakturama displayed {title}: {detail}" if detail
                else f"Fakturama displayed {title}"
            )

    def _require_any_labels(self, *labels: str) -> None:
        if not self._has_any_labels(*labels):
            raise ManualReviewRequired(f"expected editor controls {labels!r} are not visible")

    def _require_dialog(self, description: str, labels: Sequence[str]) -> None:
        if not self._dialog_is_open(labels):
            raise ManualReviewRequired(f"expected {description} is not open")

    def _current_window(self) -> Any:
        self._require_app()
        main = self._main_window
        if main is None:
            raise GatewayError("Fakturama main window is unavailable")
        handle = _safe(main, "handle")
        process_id = int(_safe(main, "process_id", 0) or 0)
        if os.name == "nt" and handle and process_id:
            # Native foreground lookup is cheap. Resolve only a same-process
            # modal HWND instead of enumerating every Java UIA window.
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.GetForegroundWindow.restype = wintypes.HWND
                active_handle = user32.GetForegroundWindow()
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(
                    active_handle, ctypes.byref(owner)
                )
                if owner.value == process_id and int(active_handle) != int(handle):
                    _, desktop_factory = self._automation_factories()
                    return desktop_factory(backend="uia").window(
                        handle=int(active_handle)
                    ).wrapper_object()
            except Exception as exc:
                _LOGGER.debug("Foreground Fakturama window lookup failed: %s", exc)
            return main
        # Test and non-Windows backends can expose their active window cheaply.
        try:
            active = self._application.top_window()
            return active if active is not None else main
        except Exception:
            return main

    def _application_windows(self) -> list[Any]:
        """Find this process's top-level windows without a full UIA desktop scan."""
        self._require_app()
        main = self._current_window()
        handle = _safe(main, "handle")
        process_id = int(_safe(main, "process_id", 0) or 0)
        if os.name != "nt" or not handle or not process_id:
            try:
                windows = list(self._application.windows())
                return windows or [main]
            except Exception:
                return [main]
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found_handles: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def collect(hwnd: int, _context: int) -> bool:
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == process_id and user32.IsWindowVisible(hwnd):
                    found_handles.append(int(hwnd))
                return True

            callback = callback_type(collect)
            user32.EnumWindows(callback, 0)
            if not found_handles:
                return [main]
            _, desktop_factory = self._automation_factories()
            desktop = desktop_factory(backend="uia")
            windows = [main]
            for hwnd in found_handles:
                if hwnd == int(handle):
                    continue
                try:
                    windows.append(desktop.window(handle=hwnd).wrapper_object())
                except Exception:
                    _LOGGER.debug("Could not inspect Fakturama window handle %s", hwnd)
            return windows
        except Exception as exc:
            _LOGGER.debug("Native Fakturama window lookup failed: %s", exc)
            return [main]

    def _visible_dialog_root(
        self,
        title_labels: Sequence[str],
        *,
        required: bool = True,
    ) -> Any | None:
        """Return the unique visible top-level selector window matching its title."""
        current = self._current_window()
        windows = self._application_windows()
        current_handle = _safe(current, "handle")
        if all(
            window is not current
            and (
                not current_handle
                or _safe(window, "handle") != current_handle
            )
            for window in windows
        ):
            windows.append(current)
        normalized_labels = tuple(normalize_label(label) for label in title_labels)
        top_level = [
            window for window in windows
            if _safe(window, "is_visible", None) is True
            and any(
                label and label in normalize_label(self._window_title(window))
                for label in normalized_labels
            )
        ]
        unique_top_level: dict[tuple[str, int], Any] = {}
        for window in top_level:
            handle = _safe(window, "handle")
            key = ("handle", int(handle)) if handle else ("object", id(window))
            unique_top_level[key] = window
        if len(unique_top_level) > 1:
            raise ManualReviewRequired(
                f"multiple visible windows match selector title {tuple(title_labels)!r}"
            )
        if unique_top_level:
            return next(iter(unique_top_level.values()))
        # SWT modal windows can be UIA Window descendants of the application's
        # top-level window instead of separate native top-level windows.
        roots = list(windows)
        for root in roots:
            windows.extend(
                element
                for element in _descendants(root)
                if element_type(element) == "Window"
            )

        candidates: list[Any] = []
        seen: set[tuple[str, int]] = set()
        normalized_labels = tuple(normalize_label(label) for label in title_labels)
        for window in windows:
            handle = _safe(window, "handle")
            try:
                identity = ("handle", int(handle)) if handle else (
                    "object",
                    id(window),
                )
            except (TypeError, ValueError):
                identity = ("object", id(window))
            if identity in seen:
                continue
            seen.add(identity)
            if element_type(window) not in {"Window", ""}:
                continue
            if _safe(window, "is_visible", None) is not True:
                continue
            title = normalize_label(self._window_title(window))
            if any(label and label in title for label in normalized_labels):
                candidates.append(window)

        if len(candidates) > 1:
            raise ManualReviewRequired(
                f"multiple visible windows match selector title {tuple(title_labels)!r}"
            )
        if not candidates:
            if required:
                raise ManualReviewRequired(
                    f"could not find a unique visible selector window titled "
                    f"{tuple(title_labels)!r}"
                )
            return None
        return candidates[0]

    def _close_selector_dialog(self, title_labels: Sequence[str], dialog: Any) -> None:
        if not self._click_if_present(
            ("Cancel", "Close"),
            control_types=("Button", "MenuItem"),
            window=dialog,
        ):
            raise ManualReviewRequired(
                f"the {title_labels[0]!r} selector has no unambiguous Cancel or Close action"
            )
        self._wait_until(
            f"the {title_labels[0]!r} selector to close",
            lambda: self._visible_dialog_root(title_labels, required=False) is None,
        )

    def _require_app(self) -> None:
        if self._application is None:
            raise GatewayError("Fakturama is not attached; call attach_or_launch first")

    @staticmethod
    def _window_title(window: Any) -> str:
        title = _safe(window, "window_text", "")
        return str(title or element_name(window) or "").strip()

    def _has_order_editor(self, number: str | None = None) -> bool:
        try:
            editor = self._active_order_editor_tab()
        except ManualReviewRequired:
            return False
        observed_number = self._read_from_window(editor, ("No.",))
        if not observed_number:
            return False
        return number is None or _normalized_equal(number, observed_number)

    def _has_invoice_editor(self) -> bool:
        try:
            return self._document_editor_identity()[1] == "Invoice"
        except ManualReviewRequired:
            return False

    def _has_any_labels(self, *labels: str) -> bool:
        resolver = self._resolver().freeze()
        for label in labels:
            try:
                resolver.resolve(ControlQuery.one_of(label, allow_ocr=False))
                return True
            except AmbiguousControl:
                return True
            except ElementNotFound:
                continue
        return False

    def _has_control(self, label: str, control_types: Sequence[str]) -> bool:
        try:
            self._resolver().resolve(
                ControlQuery.one_of(label, control_types=control_types, allow_ocr=False)
            )
            return True
        except ElementNotFound:
            return False

    def _dialog_is_open(self, labels: Sequence[str]) -> bool:
        title = normalize_label(self._window_title(self._current_window()))
        if any(normalize_label(label) in title for label in labels):
            return True
        return sum(1 for label in labels if self._has_any_labels(label)) >= min(2, len(labels))

    def _items_pane(self, *, editor: Any | None = None) -> Any:
        editor = editor if editor is not None else self._active_order_editor_tab()
        labels = [
            element
            for element in _descendants(editor)
            if element_type(element) == "Text"
            and normalize_label(element_name(element)) == normalize_label("Items")
        ]
        if len(labels) != 1:
            raise ManualReviewRequired(
                f"the active Order must expose one Items label; found {len(labels)}"
            )
        current = labels[0]
        pane = None
        for _ in range(12):
            current = _safe(current, "parent")
            if current is None:
                break
            if element_type(current) == "Pane":
                pane = current
                break
        if pane is None:
            raise ManualReviewRequired("the Items label is not inside a unique UIA Pane")
        if element_bounds(pane) is None:
            raise ManualReviewRequired("the Items Pane has no accessible bounds")
        return pane

    def _items_pane_images(self, pane: Any) -> list[Any]:
        pane_bounds = element_bounds(pane)
        if pane_bounds is None:
            raise ManualReviewRequired("the Items Pane bounds are unavailable")
        eligible = []
        for image in _descendants(pane):
            if element_type(image) != "Image" or element_name(image):
                continue
            visible = _safe(image, "is_visible", None)
            enabled = _safe(image, "is_enabled", None)
            if visible is False or enabled is False:
                continue
            if visible is not True or enabled is not True:
                raise ManualReviewRequired(
                    "cannot establish visibility and enabled state for an unnamed Items icon"
                )
            bounds = element_bounds(image)
            if bounds is None:
                raise ManualReviewRequired("an unnamed Items icon has no accessible bounds")
            if (
                bounds.left >= pane_bounds.left
                and bounds.top >= pane_bounds.top
                and bounds.right <= pane_bounds.right
                and bounds.bottom <= pane_bounds.bottom
            ):
                eligible.append(image)
        if len(eligible) < 2:
            raise ManualReviewRequired(
                "the Items Pane must show separate Product-selection and add-item icons"
            )
        ordered = sorted(
            eligible,
            key=lambda image: element_bounds(image).top + element_bounds(image).bottom,
        )
        first_bounds = element_bounds(ordered[0])
        second_bounds = element_bounds(ordered[1])
        if (
            first_bounds is None
            or second_bounds is None
            or first_bounds.top + first_bounds.bottom
            == second_bounds.top + second_bounds.bottom
        ):
            raise ManualReviewRequired(
                "the upper Product-selection icon cannot be distinguished from the add-item icon"
            )
        return ordered

    def _addresses_pane(self) -> tuple[Any, Any]:
        order_window = self._current_window()
        label = self._resolver(order_window).resolve(
            ControlQuery.one_of("Addresses", control_types=("Text",), allow_ocr=False)
        ).element
        current = label
        pane = None
        for _ in range(12):
            current = _safe(current, "parent")
            if current is None:
                break
            if element_type(current) == "Pane":
                pane = current
                break
        if pane is None:
            raise ManualReviewRequired(
                "the Addresses label is not inside a unique UIA Pane"
            )
        if element_bounds(pane) is None:
            raise ManualReviewRequired(
                "the Addresses Pane has no accessible bounds for safe child identification"
            )
        return order_window, pane

    def _addresses_pane_images(self, pane: Any) -> tuple[Any, Any]:
        pane_bounds = element_bounds(pane)
        if pane_bounds is None:
            raise ManualReviewRequired("the Addresses Pane bounds are unavailable")
        eligible: list[Any] = []
        for image in _descendants(pane):
            if element_type(image) != "Image" or element_name(image):
                continue
            visible = _safe(image, "is_visible", None)
            enabled = _safe(image, "is_enabled", None)
            if visible is False or enabled is False:
                continue
            if visible is not True or enabled is not True:
                raise ManualReviewRequired(
                    "cannot establish visibility and enabled state for an unnamed Addresses icon"
                )
            bounds = element_bounds(image)
            if bounds is None:
                raise ManualReviewRequired(
                    "an unnamed Addresses icon has no accessible bounds"
                )
            if (
                bounds.left >= pane_bounds.left
                and bounds.top >= pane_bounds.top
                and bounds.right <= pane_bounds.right
                and bounds.bottom <= pane_bounds.bottom
            ):
                eligible.append(image)

        if len(eligible) != 2:
            raise ManualReviewRequired(
                "the Addresses Pane must contain exactly two visible, enabled unnamed Image "
                f"controls; found {len(eligible)}"
            )
        ordered = sorted(
            eligible,
            key=lambda image: (
                element_bounds(image).top + element_bounds(image).bottom
            ),
        )
        top_bounds = element_bounds(ordered[0])
        bottom_bounds = element_bounds(ordered[1])
        if top_bounds is None or bottom_bounds is None:
            raise ManualReviewRequired("the Addresses icon bounds changed during resolution")
        top_center = top_bounds.top + top_bounds.bottom
        bottom_center = bottom_bounds.top + bottom_bounds.bottom
        if top_center == bottom_center:
            raise ManualReviewRequired(
                "the two Addresses icons cannot be distinguished by vertical position"
            )
        return ordered[0], ordered[1]

    def _click_addresses_image(self, role: str) -> None:
        order_window, pane = self._addresses_pane()
        upper, lower = self._addresses_pane_images(pane)
        target = upper if role == "selector" else lower if role == "create" else None
        if target is None:
            raise ValueError(f"unsupported Addresses image role: {role}")
        self._click_control(
            order_window,
            ResolvedControl(ControlQuery.one_of("Addresses icon", allow_ocr=False), element=target),
        )

    def _click_named_address_create(self, order_window: Any, pane: Any) -> bool:
        names = {
            normalize_label(label)
            for label in ("New Contact", "Create a new contact", "Create New Contact")
        }
        actionable_types = {"Button", "MenuItem", "SplitButton", "Image", "Custom"}
        matches = [
            element
            for element in _descendants(pane)
            if element_type(element) in actionable_types
            and normalize_label(element_name(element)) in names
        ]
        if len(matches) > 1:
            raise ManualReviewRequired(
                "the Addresses Pane contains multiple named contact-creation actions"
            )
        if not matches:
            return False
        self._click_element(matches[0])
        return True

    def _activate_existing_new_debtor(self) -> bool:
        """Use OCR-only activation for an already-open New Debtor tab."""
        window = self._main_window if self._main_window is not None else self._current_window()
        query = ControlQuery.one_of("New Debtor", allow_ocr=False)
        self._focus_window_for_automation(window)
        try:
            matches = self.ocr.find_text(self._capture_window(window), query.labels)
        except OCRUnavailable as exc:
            raise ManualReviewRequired(
                "cannot safely check for an existing New Debtor tab because OCR is unavailable"
            ) from exc
        if len(matches) > 1:
            raise ManualReviewRequired(
                "OCR found multiple visible New Debtor tab labels; refusing to activate one"
            )
        if not matches:
            return False
        self._click_control(
            window,
            ResolvedControl(query=query, ocr_match=matches[0]),
        )
        self._wait_until(
            "the existing New Debtor editor to activate",
            lambda: (
                self._has_edit_field(("Company", "Company name"))
                and self._has_any_labels("Main address", "Addresses")
            ),
        )
        return True

    def _require_source(self) -> OrderSource:
        if self._last_source is None:
            raise GatewayError(
                "the source order has not been associated with the current UI workflow"
            )
        return self._last_source

    def _wait_until(
        self,
        description: str,
        predicate: Callable[[], bool],
        *,
        timeout: float | None = None,
    ) -> None:
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except ManualReviewRequired:
                raise
            except Exception as exc:
                last_error = exc
            time.sleep(self.poll_interval_seconds)
        self._raise_visible_error_dialog()
        detail = f"; last observed error: {last_error}" if last_error else ""
        raise TransitionTimeout(f"timed out waiting for {description}{detail}")

    @staticmethod
    def _date_matches(expected: date | None, actual: str) -> bool:
        return expected is not None and _parse_date_text(actual) == expected

    def _executable_version(self) -> str | None:
        paths: list[Path] = []
        if self._main_window is not None:
            process_path = _safe(self._main_window, "process_path")
            if process_path:
                paths.append(Path(str(process_path)))
            else:
                process_path = self._process_image_path(
                    int(_safe(self._main_window, "process_id", 0) or 0)
                )
                if process_path:
                    paths.append(Path(process_path))
        if self._configured_executable is not None:
            paths.append(self._configured_executable)

        unique_paths: list[Path] = []
        seen_paths: set[str] = set()
        for path in paths:
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                resolved = path.expanduser()
            key = str(resolved).casefold()
            if key not in seen_paths:
                seen_paths.add(key)
                unique_paths.append(resolved)

        for path in unique_paths:
            if self._version_reader is not None:
                try:
                    version = self._version_reader(path)
                    if version:
                        return str(version)
                except Exception:
                    pass
            version = self._windows_file_version(path)
            if version:
                return version

        # The visible UIA process may be the bundled javaw.exe rather than Fakturama.exe.
        # Walk a few ancestors from both the process image and configured launcher to find
        # the Eclipse installation's bundles.info metadata.
        roots: list[Path] = []
        for path in unique_paths:
            roots.extend(list(path.parents)[:7])
        unique_roots: list[Path] = []
        seen_roots: set[str] = set()
        for root in roots:
            key = str(root).casefold()
            if key not in seen_roots:
                seen_roots.add(key)
                unique_roots.append(root)

        for install_root in unique_roots:
            bundles_info = (
                install_root
                / "configuration"
                / "org.eclipse.equinox.simpleconfigurator"
                / "bundles.info"
            )
            try:
                if bundles_info.is_file():
                    for line in bundles_info.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines():
                        parts = line.split(",")
                        if len(parts) > 1 and parts[0] == "com.sebulli.fakturama.rcp":
                            return parts[1]
            except OSError:
                continue

            plugins = install_root / "plugins"
            try:
                if plugins.is_dir():
                    candidates = sorted(plugins.glob("com.sebulli.fakturama.rcp_*.jar"))
                    if candidates:
                        match = re.search(
                            r"com\.sebulli\.fakturama\.rcp_(\d+(?:\.\d+)+)",
                            candidates[0].name,
                        )
                        if match:
                            return match.group(1)
            except OSError:
                continue
        return None

    @staticmethod
    def _process_image_path(process_id: int) -> str | None:
        if not process_id:
            return None
        try:
            import win32api
            import win32con
            import win32process

            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
            )
            try:
                return win32process.QueryFullProcessImageName(handle, 0)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            try:
                import psutil

                return psutil.Process(process_id).exe()
            except Exception:
                return None

    @staticmethod
    def _windows_file_version(path: Path) -> str | None:
        # javaw.exe's file version describes Java, not Fakturama. Only trust an executable
        # resource when the image identifies itself as Fakturama.
        if "fakturama" not in path.name.casefold():
            return None
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(path), "\\")
            ms = info["FileVersionMS"]
            ls = info["FileVersionLS"]
            return (
                f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}."
                f"{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"
            )
        except Exception:
            return None

    @staticmethod
    def _set_process_dpi_awareness() -> bool:
        try:
            import ctypes

            if hasattr(ctypes, "windll"):
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
                    return True
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()
                    return True
        except Exception:
            return False
        return False

    def _click_if_present(
        self,
        labels: Sequence[str],
        *,
        control_types: Sequence[str] = (),
        window: Any | None = None,
    ) -> bool:
        try:
            root = window if window is not None else self._current_window()
            control = self._resolver(root).resolve(
                ControlQuery.one_of(*labels, control_types=control_types, allow_ocr=False)
            )
        except ElementNotFound:
            return False
        self._click_control(root, control)
        return True
