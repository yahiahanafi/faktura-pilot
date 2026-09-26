import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from faktura_pilot.automation.models import DebtorCandidate
from faktura_pilot.cli import main
from faktura_pilot.domain.models import OrderSource
from tests.workflow_fakes import ScriptedGateway

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
ORDER_IMAGE = EXAMPLES_DIR / "order-image.png"
ORDER_SOURCE = EXAMPLES_DIR / "order-source.json"


def load_example_source() -> OrderSource:
    return OrderSource.model_validate_json(ORDER_SOURCE.read_text(encoding="utf-8"))


class WorkflowCliTests(unittest.TestCase):
    def test_offline_example_fixture_matches_the_brief_and_image(self) -> None:
        source = load_example_source()

        self.assertEqual(
            hashlib.sha256(ORDER_IMAGE.read_bytes()).hexdigest(), source.extraction.image_sha256
        )
        self.assertEqual(source.order_date.isoformat(), "2026-07-14")
        self.assertEqual(source.external_reference, "WEB-2026-0714-A17")
        self.assertEqual(source.source_customer_id, "CUST-1007")
        self.assertEqual(source.currency, "EUR")
        self.assertEqual(
            (
                source.debtor.company,
                source.debtor.first_name,
                source.debtor.last_name,
                source.debtor.alias,
                source.debtor.email,
                source.debtor.telephone,
            ),
            (
                "Northstar Office GmbH",
                "Marta",
                "Klein",
                "NORTHSTAR-BERLIN",
                "marta.klein@example.test",
                "+49 30 5550 1420",
            ),
        )
        self.assertEqual(
            (
                source.debtor.billing_address.street,
                source.debtor.billing_address.zip_code,
                source.debtor.billing_address.city,
                source.debtor.billing_address.country,
            ),
            ("Friedrichstrasse 88", "10117", "Berlin", "Germany"),
        )
        self.assertEqual(
            (
                source.debtor.delivery_address.additional_name,
                source.debtor.delivery_address.street,
                source.debtor.delivery_address.zip_code,
                source.debtor.delivery_address.city,
                source.debtor.delivery_address.country,
            ),
            (
                "Northstar Office Warehouse",
                "Beusselstrasse 44",
                "10553",
                "Berlin",
                "Germany",
            ),
        )
        self.assertEqual(source.payment.method, "Bank Transfer")
        self.assertEqual(source.payment.status.value, "PAID")
        self.assertEqual(source.payment.payment_date.isoformat(), "2026-07-18")
        self.assertEqual(
            [
                (
                    item.sku,
                    item.description,
                    item.quantity,
                    item.unit,
                    item.unit_net_price,
                    item.discount_percent,
                    item.vat_rate_percent,
                    item.source_line_net_total,
                )
                for item in source.items
            ],
            [
                (
                    "CHR-ERG-01",
                    "Ergonomic Desk Chair",
                    Decimal("2"),
                    "pcs",
                    Decimal("250.00"),
                    Decimal("10"),
                    Decimal("19"),
                    Decimal("450.00"),
                ),
                (
                    "MAT-DESK-02",
                    "Anti-Fatigue Desk Mat",
                    Decimal("3"),
                    "pcs",
                    Decimal("40.00"),
                    Decimal("0"),
                    Decimal("19"),
                    Decimal("120.00"),
                ),
            ],
        )
        self.assertEqual(
            (source.totals.net, source.totals.vat, source.totals.gross),
            (Decimal("570.00"), Decimal("108.30"), Decimal("678.30")),
        )

    def test_run_resume_and_inspect_use_the_committed_offline_fixture(self) -> None:
        source = load_example_source()
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
        verify_order_document = gateway.verify_order_document

        def verify_order_document_with_source_date(source: OrderSource, order_number: str):
            row = verify_order_document(source, order_number)
            return SimpleNamespace(
                number=row.number,
                type=row.type,
                reference=row.reference,
                total=row.total,
                state=row.state,
                linked_order_number=row.linked_order_number,
                document_date=source.order_date,
            )

        gateway.verify_order_document = verify_order_document_with_source_date

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runs_dir = root / "runs"
            evidence_dir = root / "evidence"
            output = io.StringIO()
            with patch(
                "faktura_pilot.cli.OpenAIImageExtractor",
                side_effect=AssertionError("fixture runs must not use network extraction"),
            ) as openai_extractor, patch(
                "faktura_pilot.cli._gateway", return_value=gateway
            ) as gateway_factory:
                with redirect_stdout(output):
                    status = main(
                        [
                            "run",
                            "--image",
                            str(ORDER_IMAGE),
                            "--fixture-json",
                            str(ORDER_SOURCE),
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
                openai_extractor.assert_not_called()

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
                self.assertEqual(status, 0, output.getvalue())
                self.assertIn("complete", output.getvalue())

                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(["inspect", "--run-id", "cli-run", "--run-dir", str(runs_dir)])
                self.assertEqual(status, 0, output.getvalue())
                self.assertIn("Order reference: WEB-2026-0714-A17", output.getvalue())
                self.assertIn("State: complete", output.getvalue())
                self.assertEqual(gateway.current_source, source)


if __name__ == "__main__":
    unittest.main()
