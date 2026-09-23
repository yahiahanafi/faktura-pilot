from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from faktura_pilot.domain.config import AppConfig, ConfigurationError
from faktura_pilot.domain.models import ExtractionMetadata, OrderSource
from faktura_pilot.extraction.schemas import OrderExtractionPayload

SYSTEM_PROMPT = """You extract purchase-order data from one supplied image.
Return only fields supported by visible source text. Never infer a date, currency, payment status,
address component, identifier, discount, tax rate, quantity, or amount. Use null for an unreadable
or absent scalar. Return all dates as YYYY-MM-DD. Return all numbers as decimal strings with a dot
separator and no currency/percent symbols. Keep the printed payment-method wording. Preserve every
legible source value in source_values using a concise field_path and its exact printed text. Do not
calculate or correct totals: transcribe the visible source amounts so deterministic validation can
compare them. For PAID, return the printed payment date; for UNPAID, payment_date must be null.
"""

USER_PROMPT = """Read this order image and populate the complete schema. Include the external order
reference, source customer ID, and order date; debtor company, contact name, alias, email,
telephone, billing address, and delivery address. Preserve each address's additional
name/addressee when printed. Include payment method, PAID/UNPAID status and payment date.
Include every item in source order with SKU, description, quantity, unit, unit net price, VAT
percent, discount percent, and
source line net total; plus source net, VAT, and gross totals. An item row must correspond to one
printed row. Do not merge repeated SKUs. Use null for unavailable scalar values and an empty list
for source_values only when no printed values are legible.
"""

PROMPT_VERSION = "1.0"
SUPPORTED_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ExtractionError(RuntimeError):
    pass


class OpenAIImageExtractor:
    def __init__(self, config: AppConfig | None = None, client: Any | None = None) -> None:
        self.config = config or AppConfig.load()
        if client is None:
            if self.config.openai_api_key is None:
                raise ConfigurationError(
                    "OPENAI_API_KEY is required for image extraction; set it in the environment"
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigurationError(
                    "the OpenAI client is not installed; install the project dependencies"
                ) from exc
            client = OpenAI(
                api_key=self.config.openai_api_key.get_secret_value(),
                timeout=self.config.request_timeout_seconds,
            )
        self.client = client

    def extract(self, image_path: Path) -> OrderSource:
        image_path = Path(image_path)
        try:
            mime_type = SUPPORTED_IMAGE_TYPES[image_path.suffix.lower()]
        except KeyError as exc:
            supported = ", ".join(sorted(SUPPORTED_IMAGE_TYPES))
            raise ExtractionError(
                f"unsupported image type; supported extensions: {supported}"
            ) from exc

        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            raise ExtractionError(f"could not read image: {exc}") from exc
        if not image_bytes:
            raise ExtractionError("image file is empty")
        if len(image_bytes) > self.config.max_image_bytes:
            raise ExtractionError(
                f"image exceeds the configured {self.config.max_image_bytes}-byte limit"
            )

        image_hash = hashlib.sha256(image_bytes).hexdigest()
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        try:
            response = self.client.responses.parse(
                model=self.config.model,
                reasoning={"effort": self.config.reasoning_effort},
                store=False,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": USER_PROMPT},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    },
                ],
                text_format=OrderExtractionPayload,
            )
        except Exception as exc:
            raise ExtractionError(f"OpenAI image extraction failed: {exc}") from exc

        payload = getattr(response, "output_parsed", None)
        if payload is None:
            raise ExtractionError("the model did not return a structured order payload")
        if not isinstance(payload, OrderExtractionPayload):
            try:
                payload = OrderExtractionPayload.model_validate(payload)
            except ValidationError as exc:
                raise ExtractionError(
                    f"the model response did not match the extraction schema: {exc}"
                ) from exc

        source_values = [value.model_dump() for value in payload.source_values]
        raw_order = payload.model_dump(exclude={"source_values"})
        raw_order["extraction"] = ExtractionMetadata(
            image_sha256=image_hash,
            extracted_at=datetime.now(UTC),
            model=self.config.model,
            prompt_version=self.config.prompt_version or PROMPT_VERSION,
            source_values=source_values,
        )
        try:
            return OrderSource.model_validate(raw_order)
        except ValidationError:
            raise
