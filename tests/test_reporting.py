import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from faktura_pilot.cli import main
from faktura_pilot.reporting import ProgressReporter
from tests.factories import sample_order


class ProgressReporterTests(unittest.TestCase):
    def test_events_and_heartbeat_are_flushed_and_keep_the_current_step(self) -> None:
        output = io.StringIO()
        with ProgressReporter("run", run_id="test-run", interval=100, stream=output) as progress:
            progress.event("step_started", {"step": "resolve Products"})
            progress.event("action_started", {"name": "save Order"})
            progress._last_update -= 101
            progress.heartbeat()
            progress.event("state_verified", {"state": "order_saved"})
            progress.finish("complete")
        lines = output.getvalue().splitlines()
        self.assertIn("Started run test-run", lines[0])
        self.assertIn("Step: resolve Products", output.getvalue())
        self.assertIn("Action started: save Order", output.getvalue())
        self.assertIn("Still working: save Order", output.getvalue())
        self.assertIn("State verified: order_saved", output.getvalue())
        self.assertIn("Finished: complete", lines[-1])
        self.assertTrue(output.getvalue().endswith("\n"))

    def test_extract_cli_keeps_stdout_json_and_sends_progress_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder, "source.json")
            fixture.write_text(sample_order().model_dump_json(), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "extract",
                        "--image",
                        str(Path(folder, "unused.png")),
                        "--fixture-json",
                        str(fixture),
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn('"external_reference"', stdout.getvalue())
        self.assertNotIn("Step:", stdout.getvalue())
        self.assertIn("Step: extract and validate image", stderr.getvalue())
        self.assertIn("Finished: complete", stderr.getvalue())

    def test_quiet_suppresses_progress_and_extraction_log_but_retains_json(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder, "source.json")
            fixture.write_text(sample_order().model_dump_json(), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "extract",
                        "--image",
                        str(Path(folder, "unused.png")),
                        "--fixture-json",
                        str(fixture),
                        "--quiet",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn('"external_reference"', stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_extraction_logs_are_reported_and_logger_configuration_is_restored(self) -> None:
        logger = logging.getLogger("faktura_pilot.extraction")
        level_before = logger.level
        handlers_before = list(logger.handlers)
        output = io.StringIO()
        with ProgressReporter("extract", interval=100, stream=output):
            logger.getChild("openai_image").info("Completed verification stage in 1.0s")
        self.assertIn("Completed verification stage in 1.0s", output.getvalue())
        self.assertEqual(logger.level, level_before)
        self.assertEqual(logger.handlers, handlers_before)

    def test_extraction_and_automation_logs_include_actual_idle_stage(self) -> None:
        extraction = logging.getLogger("faktura_pilot.extraction.openai_image")
        automation = logging.getLogger("faktura_pilot.automation")
        previous_levels = (extraction.parent.level, automation.level)
        previous_handlers = (list(extraction.parent.handlers), list(automation.handlers))
        output = io.StringIO()
        with ProgressReporter("run", interval=100, stream=output) as progress:
            extraction.info(
                "Starting independent image verification",
                extra={"progress_step": "independent image verification"},
            )
            progress._last_update -= 101
            progress.heartbeat()
            automation.info("Waiting for exact Order row")
        self.assertIn(
            "Still working: independent image verification", output.getvalue()
        )
        self.assertIn("Waiting for exact Order row", output.getvalue())
        self.assertEqual((extraction.parent.level, automation.level), previous_levels)
        self.assertEqual(
            (extraction.parent.handlers, automation.handlers), previous_handlers
        )

    def test_progress_interval_rejects_nonfinite_values(self) -> None:
        for value in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProgressReporter("run", interval=value)

    def test_broken_stderr_does_not_interrupt_run_or_leave_log_handlers(self) -> None:
        class BrokenStream:
            def write(self, value: str) -> None:
                del value
                raise OSError("console closed")

            def flush(self) -> None:
                raise OSError("console closed")

        logger = logging.getLogger("faktura_pilot.automation")
        handlers_before = list(logger.handlers)
        level_before = logger.level
        result = SimpleNamespace(
            run_id="broken-console",
            state=SimpleNamespace(value="complete"),
            order_number=None,
            invoice_number=None,
            waiting_for_review=False,
            complete=True,
        )
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder, "source.json")
            fixture.write_text(sample_order().model_dump_json(), encoding="utf-8")
            output = io.StringIO()
            with (
                redirect_stdout(output),
                redirect_stderr(BrokenStream()),
                patch("faktura_pilot.cli._gateway"),
                patch("faktura_pilot.cli.WorkflowOrchestrator") as orchestrator,
            ):
                orchestrator.return_value.run.return_value = result
                code = main(
                    [
                        "run",
                        "--image",
                        str(Path(folder, "unused.png")),
                        "--fixture-json",
                        str(fixture),
                        "--run-id",
                        "broken-console",
                        "--run-dir",
                        str(Path(folder, "runs")),
                    ]
                )
                orchestrator.return_value.run.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("Run broken-console: complete", output.getvalue())
        self.assertEqual(logger.handlers, handlers_before)
        self.assertEqual(logger.level, level_before)


if __name__ == "__main__":
    unittest.main()
