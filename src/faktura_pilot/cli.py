from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from faktura_pilot.automation.resolver import find_tesseract_executable
from faktura_pilot.completion_alert import launch_completion_alert
from faktura_pilot.domain.config import AppConfig, ConfigurationError
from faktura_pilot.domain.models import OrderSource
from faktura_pilot.extraction.fixture import FixtureExtractor
from faktura_pilot.extraction.openai_image import ExtractionError, OpenAIImageExtractor
from faktura_pilot.reporting import ProgressReporter
from faktura_pilot.review_alert import launch_review_alert, should_alert
from faktura_pilot.workflow.orchestrator import WorkflowInputError, WorkflowOrchestrator
from faktura_pilot.workflow.state import WorkflowCheckpoint, WorkflowState
from faktura_pilot.workflow.store import WorkflowStore, WorkflowStoreError


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("progress interval must be a number") from exc
    if not 0 < seconds < float("inf"):
        raise argparse.ArgumentTypeError("progress interval must be positive and finite")
    return seconds


def _add_progress_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--quiet", action="store_true", help="suppress live progress on stderr")
    command.add_argument(
        "--progress-interval",
        type=_positive_seconds,
        default=20.0,
        metavar="SECONDS",
        help="seconds between idle progress updates (default: 20)",
    )


def _add_review_alert_options(command: argparse.ArgumentParser) -> None:
    alerts = command.add_mutually_exclusive_group()
    alerts.add_argument(
        "--review-alert",
        action="store_true",
        dest="review_alert",
        help="show a Windows popup and play a sound if manual review is required",
    )
    alerts.add_argument(
        "--no-review-alert",
        action="store_false",
        dest="review_alert",
        help="disable the Windows popup and sound",
    )
    command.set_defaults(review_alert=None)


def _add_completion_alert_options(command: argparse.ArgumentParser) -> None:
    alerts = command.add_mutually_exclusive_group()
    alerts.add_argument(
        "--completion-alert",
        action="store_true",
        dest="completion_alert",
        help="show a Windows popup when the full automation completes",
    )
    alerts.add_argument(
        "--no-completion-alert",
        action="store_false",
        dest="completion_alert",
        help="disable the Windows completion popup",
    )
    command.set_defaults(completion_alert=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="image-to-cash")
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="extract and validate an order image")
    extract.add_argument("--image", type=Path, required=True)
    extract.add_argument("--output", type=Path)
    extract.add_argument("--config", type=Path)
    extract.add_argument("--fixture-json", type=Path)
    _add_progress_options(extract)

    validate = commands.add_parser("validate", help="validate an image or canonical order JSON")
    source = validate.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path)
    source.add_argument("--source-json", type=Path)
    validate.add_argument("--config", type=Path)
    validate.add_argument("--fixture-json", type=Path)
    _add_progress_options(validate)

    run = commands.add_parser(
        "run", help="create a verified Order and linked Invoice from an image"
    )
    run.add_argument("--image", type=Path, required=True)
    run.add_argument("--config", type=Path)
    run.add_argument(
        "--fixture-json", type=Path, help="use validated fixture JSON instead of image extraction"
    )
    run.add_argument(
        "--run-dir",
        "--runs-dir",
        dest="runs_dir",
        type=Path,
        default=Path("run-data"),
        help="base directory for checkpoint and evidence subdirectories",
    )
    run.add_argument("--run-id")
    run.add_argument("--evidence-dir", type=Path)
    run.add_argument("--fakturama-exe", type=Path)
    _add_progress_options(run)
    _add_review_alert_options(run)
    _add_completion_alert_options(run)

    resume = commands.add_parser("resume", help="resume a paused workflow after review")
    resume.add_argument("--run-id", required=True)
    resume.add_argument(
        "--run-dir",
        "--runs-dir",
        dest="runs_dir",
        type=Path,
        default=Path("run-data"),
        help="base directory for checkpoint and evidence subdirectories",
    )
    resume.add_argument("--evidence-dir", type=Path)
    resume.add_argument("--fakturama-exe", type=Path)
    resume.add_argument(
        "--continue-after-review",
        action="store_true",
        help="verify manually resolved work, skip completed items, and continue",
    )
    _add_progress_options(resume)
    _add_review_alert_options(resume)
    _add_completion_alert_options(resume)

    inspect = commands.add_parser(
        "inspect", help="show a saved workflow checkpoint and review status"
    )
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument(
        "--run-dir",
        "--runs-dir",
        dest="runs_dir",
        type=Path,
        default=Path("run-data"),
        help="base directory for checkpoint and evidence subdirectories",
    )

    doctor = commands.add_parser(
        "doctor", help="read-only diagnostics for the Fakturama desktop setup"
    )
    doctor.add_argument("--fakturama-exe", type=Path)
    return parser


def _extractor(config_path: Path | None, fixture_path: Path | None):
    if fixture_path is not None:
        return FixtureExtractor(fixture_path)
    return OpenAIImageExtractor(AppConfig.load(config_path))


def _write_json(source: OrderSource, output: Path | None) -> None:
    rendered = json.dumps(source.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"Validated order JSON written to {output}")


def _validate_json(path: Path) -> OrderSource:
    try:
        return OrderSource.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read source JSON: {exc}") from exc


class _ResumeOnlyExtractor:
    def extract(self, image_path: Path) -> OrderSource:
        del image_path
        raise RuntimeError("resume does not run image extraction")


def _gateway(evidence_directory: Path):
    from faktura_pilot.automation.windows import WindowsFakturamaGateway

    return WindowsFakturamaGateway(evidence_directory=evidence_directory)


def _print_run_result(result) -> int:
    print(f"Run {result.run_id}: {result.state.value}")
    if result.order_number:
        print(f"Order: {result.order_number}")
    if result.invoice_number:
        print(f"Invoice: {result.invoice_number}")
    if result.waiting_for_review:
        print(f"Review bundle: {result.review_path}")
        if result.checkpoint.review is not None:
            print(f"Reason: {result.checkpoint.review.reason}")
        return 3
    return 0 if result.complete else 1


def _report_run_result(
    result,
    *,
    alert_choice: bool | None,
    quiet: bool,
    completion_alert_choice: bool | None = None,
    completion_is_new: bool = True,
) -> int:
    code = _print_run_result(result)
    if result.waiting_for_review and should_alert(alert_choice, quiet):
        reason = result.checkpoint.review.reason if result.checkpoint.review else "Review needed"
        try:
            launch_review_alert(result.run_id, reason, result.review_path)
        except Exception as exc:
            print(f"Warning: could not show review alert: {exc}", file=sys.stderr)
    elif result.complete and completion_is_new and should_alert(completion_alert_choice, quiet):
        try:
            launch_completion_alert(result.run_id, result.order_number, result.invoice_number)
        except Exception as exc:
            print(f"Warning: could not show completion alert: {exc}", file=sys.stderr)
    return code


def _print_checkpoint(checkpoint: WorkflowCheckpoint, runs_dir: Path) -> None:
    print(f"Run: {checkpoint.run_id}")
    print(f"State: {checkpoint.state.value}")
    if checkpoint.source is not None:
        print(f"Order reference: {checkpoint.source.external_reference}")
        print(f"Source image: {checkpoint.source_image or '(not recorded)'}")
    if checkpoint.order_number:
        print(f"Order: {checkpoint.order_number}")
    if checkpoint.invoice_number:
        print(f"Invoice: {checkpoint.invoice_number}")
    if checkpoint.pending_action:
        print(f"Pending action: {checkpoint.pending_action.name}")
    if checkpoint.review is not None:
        print(f"Review step: {checkpoint.review.failed_step}")
        print(f"Reason: {checkpoint.review.reason}")
        print(f"Review bundle: {runs_dir / checkpoint.run_id / 'review.json'}")


def _doctor_dependency(module: str, distribution: str | None = None) -> str:
    try:
        imported = importlib.import_module(module)
    except ImportError:
        return "not installed"
    except Exception as exc:
        return f"unavailable ({type(exc).__name__}: {exc})"
    if distribution is not None:
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            pass
    return str(getattr(imported, "__version__", "installed"))


def _doctor(executable: Path | None) -> int:
    from faktura_pilot.automation.windows import WindowsFakturamaGateway

    print("Fakturama doctor (read-only)")
    pywinauto = _doctor_dependency("pywinauto")
    pytesseract = _doctor_dependency("pytesseract")
    opencv = _doctor_dependency("cv2", "opencv-python-headless")
    tesseract = find_tesseract_executable()
    print(f"pywinauto: {pywinauto}")
    print(f"pytesseract: {pytesseract}")
    print(f"OpenCV: {opencv}")
    if tesseract is not None:
        print(f"Tesseract: {tesseract}")
    if pytesseract == "not installed":
        print("OCR: WARNING - pytesseract is missing; OCR fallback is unavailable")
    elif pytesseract.startswith("unavailable"):
        print("OCR: WARNING - pytesseract could not be initialized; OCR fallback is unavailable")
    elif tesseract is None:
        print("OCR: WARNING - Tesseract executable is missing; OCR fallback is unavailable")

    checks = WindowsFakturamaGateway().diagnose(executable)
    for name, detail in checks.items():
        print(f"{name}: {detail}")

    window = checks.get("Window", "")
    version = checks.get("Version", "")
    language = checks.get("Language", "")
    automation_status = checks.get("UI Automation", "")
    ui_automation_ready = (
        pywinauto != "not installed"
        and not pywinauto.startswith("unavailable")
        and automation_status == "pywinauto available"
        and "multiple visible" not in window.casefold()
        and "could not attach" not in window.casefold()
        and "PID " in window
        and version.startswith("2.2.0")
        and language.startswith("English")
    )
    if not ui_automation_ready:
        print("Result: Fakturama is not ready for the configured UIA workflow")
        return 1
    pytesseract_ready = pytesseract != "not installed" and not pytesseract.startswith(
        "unavailable"
    )
    if not pytesseract_ready or tesseract is None:
        print("Result: UI Automation is ready; actions requiring OCR will stop safely")
    else:
        print("Result: UI Automation is ready")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args.fakturama_exe)

        if args.command == "run":
            run_id = args.run_id or uuid.uuid4().hex
            with ProgressReporter(
                "run", run_id=run_id, quiet=args.quiet, interval=args.progress_interval
            ) as progress:
                progress.step("extract and validate source")
                store = WorkflowStore(args.runs_dir)
                evidence_directory = args.evidence_dir or store.run_directory(run_id) / "evidence"
                gateway = _gateway(evidence_directory)
                runner = WorkflowOrchestrator(
                    _extractor(args.config, args.fixture_json),
                    gateway,
                    store,
                    executable=args.fakturama_exe,
                    progress=progress.event,
                )
                result = runner.run(args.image, run_id=run_id)
                progress.finish(result.state.value)
            return _report_run_result(
                result,
                alert_choice=args.review_alert,
                quiet=args.quiet,
                completion_alert_choice=args.completion_alert,
            )

        if args.command == "resume":
            with ProgressReporter(
                "resume", run_id=args.run_id, quiet=args.quiet, interval=args.progress_interval
            ) as progress:
                progress.step("load checkpoint and reconnect")
                store = WorkflowStore(args.runs_dir)
                completion_is_new = store.load(args.run_id).state is not WorkflowState.COMPLETE
                evidence_directory = (
                    args.evidence_dir or store.run_directory(args.run_id) / "evidence"
                )
                gateway = _gateway(evidence_directory)
                runner = WorkflowOrchestrator(
                    _ResumeOnlyExtractor(),
                    gateway,
                    store,
                    executable=args.fakturama_exe,
                    progress=progress.event,
                )
                result = runner.resume(
                    args.run_id, continue_after_review=args.continue_after_review
                )
                progress.finish(result.state.value)
            return _report_run_result(
                result,
                alert_choice=args.review_alert,
                quiet=args.quiet,
                completion_alert_choice=args.completion_alert,
                completion_is_new=completion_is_new,
            )

        if args.command == "inspect":
            store = WorkflowStore(args.runs_dir)
            _print_checkpoint(store.load(args.run_id), args.runs_dir)
            return 0

        if args.command == "extract":
            with ProgressReporter(
                "extract", quiet=args.quiet, interval=args.progress_interval
            ) as progress:
                progress.step("extract and validate image")
                source = _extractor(args.config, args.fixture_json).extract(args.image)
                progress.step("write validated order")
                _write_json(source, args.output)
                return 0

        with ProgressReporter(
            "validate", quiet=args.quiet, interval=args.progress_interval
        ) as progress:
            if args.source_json is not None:
                progress.step("read and validate order JSON")
                source = _validate_json(args.source_json)
            else:
                progress.step("extract and validate image")
                source = _extractor(args.config, args.fixture_json).extract(args.image)
            item_count = len(source.items)
            print(
                f"Valid order {source.external_reference}: {item_count} item(s), "
                f"{source.currency} {source.totals.gross:.2f} gross"
            )
            return 0
    except (
        ConfigurationError,
        ExtractionError,
        ValidationError,
        WorkflowInputError,
        WorkflowStoreError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def entrypoint() -> None:
    raise SystemExit(main())
