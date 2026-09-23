import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from faktura_pilot.automation.models import DebtorCandidate
from faktura_pilot.cli import main
from tests.factories import sample_order
from tests.workflow_fakes import ScriptedGateway


class WorkflowCliTests(unittest.TestCase):
    def test_run_resume_and_inspect_use_injected_gateway_and_configured_directories(self) -> None:
        source = sample_order()
        debtor = source.debtor
        candidate = DebtorCandidate(
            "debtor-1",
            debtor.company,
            debtor.first_name,
            debtor.last_name,
            debtor.billing_address.zip_code,
            debtor.billing_address.city,
        )
        gateway = ScriptedGateway(debtors=[candidate, candidate])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_path = root / "source.json"
            source_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")
            runs_dir = root / "runs"
            evidence_dir = root / "evidence"

            output = io.StringIO()
            with patch("faktura_pilot.cli._gateway", return_value=gateway) as gateway_factory:
                with redirect_stdout(output):
                    status = main(
                        [
                            "run",
                            "--image",
                            str(root / "order.png"),
                            "--fixture-json",
                            str(source_path),
                            "--run-id",
                            "cli-run",
                            "--run-dir",
                            str(runs_dir),
                            "--evidence-dir",
                            str(evidence_dir),
                            "--fakturama-exe",
                            str(root / "Fakturama.exe"),
                        ]
                    )
                self.assertEqual(status, 3)
                self.assertIn("waiting_for_review", output.getvalue())
                gateway_factory.assert_called_once_with(evidence_dir)

                gateway.debtors = [candidate]
                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(
                        [
                            "resume",
                            "--run-id",
                            "cli-run",
                            "--run-dir",
                            str(runs_dir),
                            "--evidence-dir",
                            str(evidence_dir),
                            "--fakturama-exe",
                            str(root / "Fakturama.exe"),
                        ]
                    )
                self.assertEqual(status, 0)
                self.assertIn("complete", output.getvalue())

                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(["inspect", "--run-id", "cli-run", "--run-dir", str(runs_dir)])
                self.assertEqual(status, 0)
                self.assertIn("Order reference: WEB-2026-0714-A17", output.getvalue())
                self.assertIn("State: complete", output.getvalue())
