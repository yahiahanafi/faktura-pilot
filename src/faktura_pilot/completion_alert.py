"""Best-effort Windows desktop alert for a completed workflow."""

from __future__ import annotations

import subprocess
import sys


def launch_completion_alert(
    run_id: str, order_number: str | None, invoice_number: str | None
) -> None:
    """Return promptly while a separate process owns the dismissible dialog."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "faktura_pilot.completion_alert",
            run_id,
            order_number or "",
            invoice_number or "",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def show_completion_alert(run_id: str, order_number: str, invoice_number: str) -> None:
    """Play a success sound and show the completed document numbers."""
    import ctypes
    import winsound

    details = [f"Run ID: {run_id}"]
    if order_number:
        details.append(f"Order: {order_number}")
    if invoice_number:
        details.append(f"Invoice: {invoice_number}")
    message = "Fakturama automation completed successfully.\n\n" + "\n".join(details)
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except RuntimeError:
        # A muted or unavailable system sound must not suppress the dialog.
        pass
    user32 = ctypes.windll.user32
    user32.MessageBoxW(
        None,
        message,
        "Fakturama: automation complete",
        0x00000040 | 0x00040000,
    )


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) != 4:
        return 1
    show_completion_alert(sys.argv[1], sys.argv[2], sys.argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
