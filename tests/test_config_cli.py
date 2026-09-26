import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from faktura_pilot.cli import main
from faktura_pilot.domain.config import AppConfig, ConfigurationError
from tests.factories import sample_order


class ConfigAndCliTests(unittest.TestCase):
    def test_config_defaults_and_environment_overrides(self) -> None:
        config = AppConfig.load(environ={})
        self.assertEqual(config.model, "gpt-6-luna")
        self.assertEqual(config.reasoning_effort, "low")
        self.assertEqual(config.prompt_version, "2.0")

        config = AppConfig.load(
            environ={"OPENAI_API_KEY": "secret", "IMAGE_TO_CASH_REQUEST_TIMEOUT_SECONDS": "45"}
        )
        self.assertEqual(config.openai_api_key.get_secret_value(), "secret")
        self.assertEqual(config.request_timeout_seconds, 45.0)
        self.assertNotIn("secret", repr(config))

        max_reasoning = AppConfig.load(environ={"IMAGE_TO_CASH_REASONING_EFFORT": "max"})
        self.assertEqual(max_reasoning.reasoning_effort, "max")

    def test_config_reads_toml_then_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "settings.toml")
            config_path.write_text(
                '[image_to_cash]\nmodel = "gpt-6-luna"\nreasoning_effort = "low"\n',
                encoding="utf-8",
            )
            config = AppConfig.load(config_path, environ={"IMAGE_TO_CASH_REASONING_EFFORT": "high"})

        self.assertEqual(config.reasoning_effort, "high")

    def test_config_reports_invalid_toml_and_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "bad.toml")
            config_path.write_text("not = [", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                AppConfig.load(config_path, environ={})

        with self.assertRaises(ConfigurationError):
            AppConfig.load(environ={"IMAGE_TO_CASH_REQUEST_TIMEOUT_SECONDS": "0"})

    def test_config_rejects_values_outside_the_named_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir, "misplaced.toml")
            config_path.write_text(
                'model = "gpt-6-luna"\n[image_to_cash]\nreasoning_effort = "low"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigurationError, "top-level key.*model"):
                AppConfig.load(config_path, environ={})

    def test_validate_cli_accepts_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir, "order.json")
            source_path.write_text(sample_order().model_dump_json(), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["validate", "--source-json", str(source_path)])

        self.assertEqual(code, 0)
        self.assertIn("WEB-2026-0714-A17", output.getvalue())
        self.assertIn("EUR 678.30 gross", output.getvalue())

    def test_validate_cli_uses_fixture_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.png")
            image_path.write_bytes(b"image")
            fixture_path = Path(temp_dir, "source.json")
            fixture_path.write_text(sample_order().model_dump_json(), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "validate",
                        "--image",
                        str(image_path),
                        "--fixture-json",
                        str(fixture_path),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("2 item(s)", output.getvalue())

    def test_extract_cli_serializes_fixture_to_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir, "order.png")
            image_path.write_bytes(b"image")
            fixture_path = Path(temp_dir, "source.json")
            fixture_path.write_text(sample_order().model_dump_json(), encoding="utf-8")
            output_path = Path(temp_dir, "out", "order.json")
            message = io.StringIO()
            with redirect_stdout(message):
                code = main(
                    [
                        "extract",
                        "--image",
                        str(image_path),
                        "--fixture-json",
                        str(fixture_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertTrue(output_path.exists())

        self.assertEqual(code, 0)
        self.assertIn("Validated order JSON", message.getvalue())

    def test_cli_returns_clean_error_for_invalid_source_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir, "missing.json")
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = main(["validate", "--source-json", str(missing)])

        self.assertEqual(code, 2)
        self.assertIn("could not read source JSON", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
