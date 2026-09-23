import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from faktura_pilot.domain.config import AppConfig
from faktura_pilot.extraction.fixture import FixtureExtractor
from faktura_pilot.extraction.openai_image import ExtractionError, OpenAIImageExtractor
from faktura_pilot.extraction.schemas import OrderExtractionPayload
from tests.factories import minimal_payload, sample_order


class FakeResponses:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed
        self.call = None

    def parse(self, **kwargs):
        self.call = kwargs
        return SimpleNamespace(output_parsed=self.output_parsed)


class FakeClient:
    def __init__(self, output_parsed):
        self.responses = FakeResponses(output_parsed)


class ExtractionTests(unittest.TestCase):
    def test_openai_adapter_sends_image_with_strict_pydantic_output_and_validates_domain(
        self,
    ) -> None:
        payload = OrderExtractionPayload.model_validate(minimal_payload())
        client = FakeClient(payload)
        config = AppConfig(model="gpt-6-luna", reasoning_effort="low")
        extractor = OpenAIImageExtractor(config=config, client=client)

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.png")
            image_bytes = b"fixture image bytes"
            image_path.write_bytes(image_bytes)
            order = extractor.extract(image_path)

        call = client.responses.call
        self.assertEqual(call["model"], "gpt-6-luna")
        self.assertEqual(call["reasoning"], {"effort": "low"})
        self.assertFalse(call["store"])
        self.assertIs(call["text_format"], OrderExtractionPayload)
        image_part = call["input"][1]["content"][1]
        self.assertEqual(image_part["type"], "input_image")
        self.assertTrue(image_part["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(order.extraction.image_sha256, hashlib.sha256(image_bytes).hexdigest())
        self.assertEqual(order.extraction.model, "gpt-6-luna")
        self.assertEqual(order.extraction.source_values[0].text, "WEB-2026-0714-A17")
        self.assertIsNotNone(order.extraction.extracted_at.tzinfo)
        self.assertEqual(order.external_reference, "WEB-2026-0714-A17")

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

    def test_rejects_missing_structured_output(self) -> None:
        client = FakeClient(None)
        extractor = OpenAIImageExtractor(config=AppConfig(), client=client)
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.jpg")
            image_path.write_bytes(b"image")
            with self.assertRaisesRegex(ExtractionError, "did not return"):
                extractor.extract(image_path)

    def test_extraction_schema_has_required_nested_fields_and_forbids_extras(self) -> None:
        schema = OrderExtractionPayload.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("order_date", schema["required"])
        self.assertFalse(schema["$defs"]["ExtractedAddress"]["additionalProperties"])
        self.assertIn("street", schema["$defs"]["ExtractedAddress"]["required"])


if __name__ == "__main__":
    unittest.main()
