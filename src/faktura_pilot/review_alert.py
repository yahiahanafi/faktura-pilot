"""Best-effort Windows desktop alert for a workflow paused for review."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO


def should_alert(
    choice: bool | None,
    quiet: bool,
    *,
    platform: str | None = None,
    input_stream: TextIO | None = None,
) -> bool:
    """Enable alerts by default only for interactive Windows runs."""
    if choice is False or (platform or sys.platform) != "win32":
        return False
    if choice is True:
        return True
    if quiet:
        return False
    stream = input_stream if input_stream is not None else sys.stdin
    try:
        return bool(stream is not None and stream.isatty())
    except OSError:
        return False


def launch_review_alert(run_id: str, reason: str, review_path: Path) -> None:
    """Return promptly while a separate process owns the dismissible dialog."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "faktura_pilot.review_alert",
            run_id,
            reason,
            str(review_path.resolve()),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _launch_resume(run_id: str, review_path: str) -> None:
    run_directory = Path(review_path).resolve().parent.parent
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "faktura_pilot",
            "resume",
            "--run-id",
            run_id,
            "--run-dir",
            str(run_directory),
            "--continue-after-review",
            "--review-alert",
            "--completion-alert",
        ],
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        close_fds=True,
    )


def _show_review_dialog(run_id: str, reason: str, review_path: str) -> int:
    """Show a native review dialog with open, resume, and close actions."""
    import ctypes

    class TaskDialogButton(ctypes.Structure):
        _fields_ = [("nButtonID", ctypes.c_int), ("pszButtonText", ctypes.c_wchar_p)]

    class TaskDialogConfig(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hwndParent", ctypes.c_void_p),
            ("hInstance", ctypes.c_void_p),
            ("dwFlags", ctypes.c_uint),
            ("dwCommonButtons", ctypes.c_uint),
            ("pszWindowTitle", ctypes.c_wchar_p),
            ("pszMainIcon", ctypes.c_void_p),
            ("pszMainInstruction", ctypes.c_wchar_p),
            ("pszContent", ctypes.c_wchar_p),
            ("cButtons", ctypes.c_uint),
            ("pButtons", ctypes.POINTER(TaskDialogButton)),
            ("nDefaultButton", ctypes.c_int),
            ("cRadioButtons", ctypes.c_uint),
            ("pRadioButtons", ctypes.POINTER(TaskDialogButton)),
            ("nDefaultRadioButton", ctypes.c_int),
            ("pszVerificationText", ctypes.c_wchar_p),
            ("pszExpandedInformation", ctypes.c_wchar_p),
            ("pszExpandedControlText", ctypes.c_wchar_p),
            ("pszCollapsedControlText", ctypes.c_wchar_p),
            ("pszFooter", ctypes.c_wchar_p),
            ("pszFooterIcon", ctypes.c_void_p),
            ("pfCallback", ctypes.c_void_p),
            ("lpCallbackData", ctypes.c_ssize_t),
            ("cxWidth", ctypes.c_uint),
        ]

    buttons = (TaskDialogButton * 3)(
        TaskDialogButton(1001, "Open review bundle"),
        TaskDialogButton(1002, "Yes, continue"),
        TaskDialogButton(1003, "Close"),
    )
    details = (
        f"Run ID: {run_id}\n"
        f"Reason: {reason[:500]}{'...' if len(reason) > 500 else ''}\n"
        f"Review bundle: {review_path}\n\n"
        "Resolve the review item in Fakturama before choosing Yes, continue. "
        "The workflow verifies resolved work, skips completed items, and continues "
        "in a new console."
    )
    config = TaskDialogConfig(
        cbSize=ctypes.sizeof(TaskDialogConfig),
        hwndParent=None,
        hInstance=None,
        dwFlags=0x0008,  # TDF_ALLOW_DIALOG_CANCELLATION
        dwCommonButtons=0,
        pszWindowTitle="Fakturama: manual review required",
        pszMainIcon=ctypes.c_void_p(0xFFFF),  # TD_WARNING_ICON
        pszMainInstruction="Automation stopped for manual review",
        pszContent=details,
        cButtons=len(buttons),
        pButtons=buttons,
        nDefaultButton=1001,
        cRadioButtons=0,
        pRadioButtons=None,
        nDefaultRadioButton=0,
        pszVerificationText=None,
        pszExpandedInformation=None,
        pszExpandedControlText=None,
        pszCollapsedControlText=None,
        pszFooter=None,
        pszFooterIcon=None,
        pfCallback=None,
        lpCallbackData=0,
        cxWidth=0,
    )
    clicked = ctypes.c_int(0)
    result = ctypes.windll.comctl32.TaskDialogIndirect(
        ctypes.byref(config), ctypes.byref(clicked), None, None
    )
    if result != 0:
        raise OSError(f"TaskDialogIndirect failed with HRESULT {result}")
    return clicked.value


def show_review_alert(run_id: str, reason: str, review_path: str) -> None:
    """Sound the warning and show actions for reviewing or resuming the run."""
    import ctypes
    import winsound

    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except RuntimeError:
        # A muted or unavailable system sound must not suppress the dialog.
        pass

    try:
        clicked = _show_review_dialog(run_id, reason, review_path)
    except Exception:
        # Keep a usable warning prompt if the native task dialog is unavailable.
        message = (
            "Fakturama automation stopped for manual review.\n\n"
            f"Run ID: {run_id}\n"
            f"Reason: {reason[:500]}{'...' if len(reason) > 500 else ''}\n\n"
            f"Review bundle: {review_path}\n\n"
            "Choose Yes after resolving the review item in Fakturama. "
            "The workflow verifies completed work before skipping it."
        )
        clicked = ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Fakturama: manual review required",
            0x00000034 | 0x00040000,  # MB_YESNO | MB_ICONWARNING | MB_TOPMOST
        )
        if clicked == 6:  # IDYES
            try:
                _launch_resume(run_id, review_path)
            except OSError as exc:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"Could not start the resume workflow: {exc}",
                    "Fakturama: resume failed",
                    0x00000010 | 0x00040000,  # MB_ICONERROR | MB_TOPMOST
                )
        return

    if clicked == 1001:  # Open review bundle
        try:
            os.startfile(review_path)
        except OSError as exc:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Could not open the review bundle: {exc}",
                "Fakturama: could not open review bundle",
                0x00000010 | 0x00040000,  # MB_ICONERROR | MB_TOPMOST
            )
    elif clicked == 1002:  # Yes, continue
        try:
            _launch_resume(run_id, review_path)
        except OSError as exc:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Could not start the resume workflow: {exc}",
                "Fakturama: resume failed",
                0x00000010 | 0x00040000,  # MB_ICONERROR | MB_TOPMOST
            )


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) != 4:
        return 1
    show_review_alert(sys.argv[1], sys.argv[2], sys.argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
