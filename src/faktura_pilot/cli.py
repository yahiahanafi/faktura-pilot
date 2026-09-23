from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from faktura_pilot.domain.config import AppConfig, ConfigurationError
from faktura_pilot.domain.models import OrderSource
from faktura_pilot.extraction.fixture import FixtureExtractor
from faktura_pilot.extraction.openai_image import ExtractionError, OpenAIImageExtractor
from faktura_pilot.workflow.orchestrator import WorkflowInputError, WorkflowOrchestrator
from faktura_pilot.workflow.state import WorkflowCheckpoint
from faktura_pilot.workflow.store import WorkflowStore, WorkflowStoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="image-to-cash")
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="extract and validate an order image")
    extract.add_argument("--image", type=Path, required=True)
    extract.add_argument("--output", type=Path)
    extract.add_argument("--config", type=Path)
    extract.add_argument("--fixture-json", type=Path)

    validate = commands.add_parser("validate", help="validate an image or canonical order JSON")
    source = validate.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path)
    source.add_argument("--source-json", type=Path)
    validate.add_argument("--config", type=Path)
    validate.add_argument("--fixture-json", type=Path)

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            run_id = args.run_id or uuid.uuid4().hex
            store = WorkflowStore(args.runs_dir)
            evidence_directory = args.evidence_dir or store.run_directory(run_id) / "evidence"
            gateway = _gateway(evidence_directory)
            runner = WorkflowOrchestrator(
                _extractor(args.config, args.fixture_json),
                gateway,
                store,
                executable=args.fakturama_exe,
            )
            return _print_run_result(runner.run(args.image, run_id=run_id))

        if args.command == "resume":
            store = WorkflowStore(args.runs_dir)
            evidence_directory = args.evidence_dir or store.run_directory(args.run_id) / "evidence"
            gateway = _gateway(evidence_directory)
            runner = WorkflowOrchestrator(
                _ResumeOnlyExtractor(),
                gateway,
                store,
                executable=args.fakturama_exe,
            )
            return _print_run_result(runner.resume(args.run_id))

        if args.command == "inspect":
            store = WorkflowStore(args.runs_dir)
            _print_checkpoint(store.load(args.run_id), args.runs_dir)
            return 0

        if args.command == "extract":
            source = _extractor(args.config, args.fixture_json).extract(args.image)
            _write_json(source, args.output)
            return 0

        if args.source_json is not None:
            source = _validate_json(args.source_json)
        else:
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
