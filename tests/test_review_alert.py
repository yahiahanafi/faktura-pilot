import ctypes
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from faktura_pilot.cli import _report_run_result, build_parser
from faktura_pilot.review_alert import launch_review_alert, should_alert, show_review_alert


class ReviewAlertTests(unittest.TestCase):
    def _result(self, *, waiting: bool = True):
        return SimpleNamespace(
            run_id="review-42",
            state=SimpleNamespace(value="waiting_for_review" if waiting else "complete"),
            order_number=None,
            invoice_number=None,
            waiting_for_review=waiting,
            complete=not waiting,
            review_path=Path("run-data/review-42/review.json") if waiting else None,
            checkpoint=SimpleNamespace(
                review=SimpleNamespace(reason="Ambiguous debtor match") if waiting else None
            ),
        )

    def test_default_requires_interactive_windows_and_quiet_disables_it(self) -> None:
        terminal = SimpleNamespace(isatty=lambda: True)
        pipe = SimpleNamespace(isatty=lambda: False)
        self.assertTrue(should_alert(None, False, platform="win32", input_stream=terminal))
        self.assertFalse(should_alert(None, False, platform="win32", input_stream=pipe))
        self.assertFalse(should_alert(None, True, platform="win32", input_stream=terminal))
        self.assertFalse(should_alert(None, False, platform="linux", input_stream=terminal))
        self.assertTrue(should_alert(True, True, platform="win32", input_stream=pipe))
        self.assertFalse(should_alert(False, False, platform="win32", input_stream=terminal))

    def test_cli_flags_are_available_for_run_and_resume(self) -> None:
        parser = build_parser()
        for command in ("run", "resume"):
            required = ["--image", "unused.png"] if command == "run" else ["--run-id", "review-42"]
            self.assertIsNone(parser.parse_args([command, *required]).review_alert)
            self.assertTrue(parser.parse_args([command, *required, "--review-alert"]).review_alert)
            self.assertFalse(
                parser.parse_args([command, *required, "--no-review-alert"]).review_alert
            )

    def test_only_review_result_launches_alert_without_changing_output_or_exit_code(self) -> None:
        output = io.StringIO()
        with (
            patch("faktura_pilot.cli.should_alert", return_value=True),
            patch("faktura_pilot.cli.launch_review_alert") as launch,
            patch("faktura_pilot.cli.launch_completion_alert"),
            redirect_stdout(output),
        ):
            self.assertEqual(
                _report_run_result(self._result(), alert_choice=None, quiet=False), 3
            )
            self.assertEqual(
                _report_run_result(self._result(waiting=False), alert_choice=None, quiet=False), 0
            )
        launch.assert_called_once_with(
            "review-42", "Ambiguous debtor match", Path("run-data/review-42/review.json")
        )
        self.assertIn("Review bundle: run-data", output.getvalue())
        self.assertNotIn("Fakturama automation stopped", output.getvalue())

    def test_alert_launch_failure_keeps_review_exit_code(self) -> None:
        with (
            patch("faktura_pilot.cli.should_alert", return_value=True),
            patch("faktura_pilot.cli.launch_review_alert", side_effect=OSError("unavailable")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(
                _report_run_result(self._result(), alert_choice=True, quiet=False), 3
            )
        self.assertIn("could not show review alert", error.getvalue())

    def test_launch_uses_detached_stdio_and_same_python_environment(self) -> None:
        with patch("faktura_pilot.review_alert.subprocess.Popen") as process:
            launch_review_alert("review-42", "Need review", Path("run-data/review-42/review.json"))
        args, kwargs = process.call_args
        self.assertEqual(args[0][:3], [sys.executable, "-m", "faktura_pilot.review_alert"])
        self.assertEqual(args[0][3:5], ["review-42", "Need review"])
        self.assertTrue(Path(args[0][5]).is_absolute())
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_dialog_sounds_and_identifies_review_bundle(self) -> None:
        sound = SimpleNamespace(MessageBeep=Mock(), MB_ICONEXCLAMATION=48)
        user32 = SimpleNamespace(MessageBoxW=Mock())
        with (
            patch.dict(sys.modules, {"winsound": sound}),
            patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True),
        ):
            show_review_alert("review-42", "Ambiguous debtor match", r"C:\\runs\\review.json")
        sound.MessageBeep.assert_called_once()
        message = user32.MessageBoxW.call_args.args[1]
        self.assertIn("review-42", message)
        self.assertIn("Ambiguous debtor match", message)
        self.assertIn(r"C:\\runs\\review.json", message)


if __name__ == "__main__":
    unittest.main()
