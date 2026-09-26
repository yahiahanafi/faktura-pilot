import hashlib
import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from faktura_pilot.domain.config import AppConfig
from faktura_pilot.extraction.fixture import FixtureExtractor
from faktura_pilot.extraction.openai_image import ExtractionError, OpenAIImageExtractor
from faktura_pilot.extraction.schemas import OrderExtractionPayload
from tests.factories import minimal_payload, sample_order


class FakeResponses:
    def __init__(self, *output_parsed):
        self.output_parsed_values = list(output_parsed)
        self.calls = []

    @property
    def call(self):
        return self.calls[-1] if self.calls else None

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.output_parsed_values) - 1)
        return SimpleNamespace(output_parsed=self.output_parsed_values[index])


class FakeClient:
    def __init__(self, *output_parsed):
        self.responses = FakeResponses(*output_parsed)


class ExtractionTests(unittest.TestCase):
    def test_openai_adapter_verifies_image_and_validates_all_canonical_fields(self) -> None:
        payload = OrderExtractionPayload.model_validate(minimal_payload())
        client = FakeClient(payload, payload)
        config = AppConfig(model="gpt-6-luna", reasoning_effort="low")
        extractor = OpenAIImageExtractor(config=config, client=client)

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.png")
            image_bytes = b"fixture image bytes"
            image_path.write_bytes(image_bytes)
            order = extractor.extract(image_path)

        self.assertEqual(len(client.responses.calls), 2)
        first_call, verification_call = client.responses.calls
        self.assertEqual(first_call["model"], "gpt-6-luna")
        self.assertEqual(first_call["reasoning"], {"effort": "low"})
        self.assertFalse(first_call["store"])
        self.assertIs(first_call["text_format"], OrderExtractionPayload)
        self.assertIn("independent verifier", verification_call["input"][0]["content"])
        image_part = verification_call["input"][1]["content"][1]
        self.assertEqual(image_part["type"], "input_image")
        self.assertTrue(image_part["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(order.extraction.image_sha256, hashlib.sha256(image_bytes).hexdigest())
        self.assertEqual(order.extraction.model, "gpt-6-luna")
        self.assertEqual(order.extraction.prompt_version, "2.0")
        self.assertEqual(order.extraction.source_values[0].text, "WEB-2026-0714-A17")
        self.assertIsNotNone(order.extraction.extracted_at.tzinfo)

        self.assertEqual(order.order_date, date(2026, 7, 14))
        self.assertEqual(order.external_reference, "WEB-2026-0714-A17")
        self.assertEqual(order.debtor.company, "Northstar Office GmbH")
        self.assertEqual((order.debtor.first_name, order.debtor.last_name), ("Mira", "Weber"))
        self.assertEqual(order.debtor.alias, "Northstar")
        self.assertEqual(order.debtor.email, "mira@example.de")
        self.assertEqual(order.debtor.telephone, "+49 30 555 0100")
        self.assertEqual(order.debtor.billing_address.zip_code, "20457")
        self.assertEqual(order.debtor.delivery_address.zip_code, "20095")
        self.assertEqual(order.payment.method, "Bank Transfer")
        self.assertEqual(order.payment.status, "PAID")
        self.assertEqual(order.payment.payment_date, date(2026, 7, 18))
        self.assertEqual(order.order_discount_percent, Decimal("0"))
        self.assertIsNone(order.shipping)
        self.assertEqual(
            [
                (
                    item.sku,
                    item.description,
                    str(item.quantity),
                    item.unit,
                    str(item.unit_net_price),
                    str(item.vat_rate_percent),
                    str(item.discount_percent),
                    str(item.source_line_net_total),
                )
                for item in order.items
            ],
            [
                (
                    "CHR-ERG-01",
                    "Ergonomic chair",
                    "2",
                    "piece",
                    "250.00",
                    "19",
                    "10",
                    "450.00",
                ),
                (
                    "MAT-DESK-02",
                    "Desk mat",
                    "3",
                    "piece",
                    "40.00",
                    "19",
                    "0",
                    "120.00",
                ),
            ],
        )
        self.assertEqual(
            (order.totals.net, order.totals.vat, order.totals.gross),
            (Decimal("570.00"), Decimal("108.30"), Decimal("678.30")),
        )

    def test_adjustment_schema_is_strict_and_legacy_payloads_default_to_absent(self) -> None:
        schema = OrderExtractionPayload.model_json_schema()
        for field in (
            "order_discount_present",
            "order_discount_percent",
            "shipping_present",
            "shipping",
        ):
            self.assertIn(field, schema["required"])
        self.assertEqual(
            set(schema["$defs"]["ExtractedShipping"]["required"]),
            {"name", "net_amount", "vat_rate_percent"},
        )
        legacy = OrderExtractionPayload.model_validate(minimal_payload())
        self.assertFalse(legacy.order_discount_present)
        self.assertIsNone(legacy.order_discount_percent)
        self.assertFalse(legacy.shipping_present)
        self.assertIsNone(legacy.shipping)

    def test_adjustments_convert_without_presence_flags(self) -> None:
        payload_data = minimal_payload()
        payload_data.update(
            order_discount_present=True,
            order_discount_percent="5",
            shipping_present=True,
            shipping={"name": "Express", "net_amount": "12.00", "vat_rate_percent": "19"},
        )
        payload = OrderExtractionPayload.model_validate(payload_data)
        extractor = OpenAIImageExtractor(config=AppConfig(), client=FakeClient(payload, payload))
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.jpg")
            image_path.write_bytes(b"image")
            with patch(
                "faktura_pilot.extraction.openai_image.OrderSource.model_validate",
                return_value=sample_order(),
            ) as canonical_validate:
                extractor.extract(image_path)
        converted = canonical_validate.call_args.args[0]
        self.assertEqual(converted["order_discount_percent"], "5")
        self.assertEqual(
            converted["shipping"],
            {"name": "Express", "net_amount": "12.00", "vat_rate_percent": "19"},
        )
        self.assertNotIn("order_discount_present", converted)
        self.assertNotIn("shipping_present", converted)
        self.assertNotIn("source_values", converted)

    def test_visible_unreadable_adjustments_stop_for_review(self) -> None:
        for adjustment in ("discount", "shipping"):
            with self.subTest(adjustment=adjustment):
                payload_data = minimal_payload()
                if adjustment == "discount":
                    payload_data["order_discount_present"] = True
                else:
                    payload_data["shipping_present"] = True
                    payload_data["shipping"] = {
                        "name": "Express",
                        "net_amount": None,
                        "vat_rate_percent": "19",
                    }
                payload = OrderExtractionPayload.model_validate(payload_data)
                extractor = OpenAIImageExtractor(
                    config=AppConfig(), client=FakeClient(payload, payload)
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    image_path = Path(temp_dir, "order.jpg")
                    image_path.write_bytes(b"image")
                    with self.assertRaisesRegex(ExtractionError, "unreadable"):
                        extractor.extract(image_path)

    def test_adjustment_values_cannot_be_marked_absent(self) -> None:
        for field, value in (
            ("order_discount_percent", "5"),
            (
                "shipping",
                {"name": "Express", "net_amount": "12.00", "vat_rate_percent": "19"},
            ),
        ):
            with self.subTest(field=field):
                payload_data = minimal_payload()
                payload_data[field] = value
                payload = OrderExtractionPayload.model_validate(payload_data)
                extractor = OpenAIImageExtractor(
                    config=AppConfig(), client=FakeClient(payload, payload)
                )
                with patch.object(Path, "open", return_value=io.BytesIO(b"image")):
                    with self.assertRaisesRegex(ExtractionError, "marked absent"):
                        extractor.extract(Path("order.jpg"))

    def test_identifier_conflict_with_source_transcription_requires_review(self) -> None:
        payload_data = minimal_payload()
        payload_data["debtor"]["billing_address"]["zip_code"] = "2045"
        payload_data["source_values"].append(
            {"field_path": "debtor.billing_address.zip_code", "text": "20457"}
        )
        payload = OrderExtractionPayload.model_validate(payload_data)
        extractor = OpenAIImageExtractor(config=AppConfig(), client=FakeClient(payload, payload))

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.jpg")
            image_path.write_bytes(b"image")
            with self.assertRaisesRegex(
                ExtractionError, "conflicts with its exact source transcription"
            ):
                extractor.extract(image_path)

    def test_non_german_postcode_and_exact_transcriptions_preserve_characters(self) -> None:
        payload_data = minimal_payload()
        payload_data["debtor"]["billing_address"]["country"] = "United Kingdom"
        payload_data["debtor"]["billing_address"]["zip_code"] = "SW1A 1AA"
        payload_data["debtor"]["telephone"] = "+44 (0)20 1234 5678"
        payload_data["source_values"].extend(
            [
                {
                    "field_path": "debtor.billing_address.zip_code",
                    "text": "  SW1A 1AA ",
                },
                {
                    "field_path": "debtor.telephone",
                    "text": " +44 (0)20 1234 5678  ",
                },
            ]
        )
        payload = OrderExtractionPayload.model_validate(payload_data)
        client = FakeClient(payload, payload)
        extractor = OpenAIImageExtractor(config=AppConfig(), client=client)

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.jpg")
            image_path.write_bytes(b"image")
            order = extractor.extract(image_path)

        self.assertEqual(order.debtor.billing_address.zip_code, "SW1A 1AA")
        self.assertEqual(order.debtor.telephone, "+44 (0)20 1234 5678")
        self.assertEqual(order.extraction.source_values[-2].text, "  SW1A 1AA ")
        self.assertEqual(order.extraction.source_values[-1].text, " +44 (0)20 1234 5678  ")

    def test_source_values_reject_duplicate_field_paths(self) -> None:
        payload_data = minimal_payload()
        payload_data["source_values"].append(
            {"field_path": "external_reference", "text": "WEB-2026-0714-A17"}
        )

        with self.assertRaisesRegex(ValidationError, "one transcription per field path"):
            OrderExtractionPayload.model_validate(payload_data)

    def test_fixture_extractor_returns_an_independent_copy(self) -> None:
        source = sample_order()
        extractor = FixtureExtractor(source)
        returned = extractor.extract(Path("unused.png"))
        returned.items[0].sku = "changed"

        self.assertEqual(source.items[0].sku, "CHR-ERG-01")
        self.assertEqual(returned.items[0].sku, "changed")

    def test_rejects_unsupported_empty_and_oversized_images_before_call(self) -> None:
        client = FakeClient(OrderExtractionPayload.model_validate(minimal_payload()))
        extractor = OpenAIImageExtractor(
            config=AppConfig(max_image_bytes=2),
            client=client,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            unsupported = Path(temp_dir, "order.gif")
            unsupported.write_bytes(b"x")
            with self.assertRaisesRegex(ExtractionError, "unsupported image type"):
                extractor.extract(unsupported)

            empty = Path(temp_dir, "empty.png")
            empty.write_bytes(b"")
            with self.assertRaisesRegex(ExtractionError, "empty"):
                extractor.extract(empty)

            oversized = Path(temp_dir, "large.png")
            oversized.write_bytes(b"123")
            with self.assertRaisesRegex(ExtractionError, "exceeds"):
                extractor.extract(oversized)

        self.assertIsNone(client.responses.call)

    def test_oversized_image_read_is_bounded_before_api_request(self) -> None:
        class TrackedImage(io.BytesIO):
            requested_size = None

            def read(self, size=-1):
                self.requested_size = size
                return super().read(size)

        image = TrackedImage(b"x" * 100)
        client = FakeClient(None)
        extractor = OpenAIImageExtractor(config=AppConfig(max_image_bytes=4), client=client)
        with patch.object(Path, "open", return_value=image):
            with self.assertRaisesRegex(ExtractionError, "exceeds"):
                extractor.extract(Path("large.png"))
        self.assertEqual(image.requested_size, 5)
        self.assertEqual(client.responses.calls, [])

    def test_progress_names_both_image_passes_without_logging_source_values(self) -> None:
        payload = OrderExtractionPayload.model_validate(minimal_payload())
        extractor = OpenAIImageExtractor(config=AppConfig(), client=FakeClient(payload, payload))
        with patch.object(Path, "open", return_value=io.BytesIO(b"private image bytes")):
            with self.assertLogs("faktura_pilot.extraction", level="INFO") as captured:
                extractor.extract(Path("private-customer.png"))
        messages = "\n".join(captured.output)
        self.assertIn("Starting image extraction", messages)
        self.assertIn("Completed independent image verification in", messages)
        self.assertNotIn("private image bytes", messages)
        self.assertNotIn("private-customer", messages)
        self.assertNotIn(payload.external_reference, messages)

    def test_rejects_missing_structured_output(self) -> None:
        client = FakeClient(None, OrderExtractionPayload.model_validate(minimal_payload()))
        extractor = OpenAIImageExtractor(config=AppConfig(), client=client)
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.jpg")
            image_path.write_bytes(b"image")
            with self.assertRaisesRegex(ExtractionError, "did not return"):
                extractor.extract(image_path)
        self.assertEqual(len(client.responses.calls), 1)

    def test_extraction_schema_has_required_nested_fields_and_forbids_extras(self) -> None:
        schema = OrderExtractionPayload.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("order_date", schema["required"])
        self.assertFalse(schema["$defs"]["ExtractedAddress"]["additionalProperties"])
        self.assertIn("street", schema["$defs"]["ExtractedAddress"]["required"])
        self.assertIn(
            "leading zeros",
            schema["$defs"]["ExtractedAddress"]["properties"]["zip_code"]["description"],
        )


if __name__ == "__main__":
    unittest.main()
