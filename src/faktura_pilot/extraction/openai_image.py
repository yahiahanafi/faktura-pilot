from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from faktura_pilot.domain.config import AppConfig, ConfigurationError
from faktura_pilot.domain.models import ExtractionMetadata, OrderSource
from faktura_pilot.extraction.schemas import OrderExtractionPayload

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You extract purchase-order data from one supplied image.
Return only fields supported by visible source text. Never infer a date, currency, payment status,
address component, identifier, discount, tax rate, quantity, or amount. Use null for an unreadable
or absent scalar. Return dates as YYYY-MM-DD. Return quantities, prices, percentages, and totals as
decimal strings with a dot separator and no currency/percent symbols. Keep postal codes, telephone
numbers, SKUs, order references, and customer IDs as strings: copy every character and digit
exactly,
including leading zeros, plus signs, spaces, and punctuation. Never shorten, pad, or reformat those
identifiers. Read both complete addresses and do not split a postal code from its city incorrectly.
Keep the printed payment-method wording. Preserve every legible source value in source_values with
its field path and exact printed text. Do not calculate or correct totals: transcribe the visible
source amounts so deterministic validation can compare them. For order-level discount and shipping,
set each *_present flag true whenever the adjustment is visible, even if a value is unreadable.
When an adjustment is absent, set its flag false and its value null. Never turn an unreadable
visible adjustment into zero or null shipping. Transcribe the printed order discount percentage,
shipping label, shipping net amount, and shipping VAT rate as strings when legible. Never infer
a shipping label or VAT rate from item lines or totals. For PAID, return the printed payment
date; for UNPAID, payment_date must be null.
"""

USER_PROMPT = """Read this order image and populate the complete schema. Include the external order
reference, source customer ID, and order date; debtor company, contact name, alias, email,
telephone, billing address, and delivery address. Preserve each address's additional
name/addressee when printed. Include payment method, PAID/UNPAID status and payment date.
Copy every character of postal codes, telephone numbers, SKUs, order references, and customer IDs
exactly as printed, including leading zeros, spaces, punctuation, and separators. Do not apply
country-specific formatting rules or fill in a missing character. Double-check each value against
the image.
Include every item in source order with SKU, description, quantity, unit, unit net price, VAT
percent, discount percent, and source line net total; plus order-level discount percentage,
shipping label/net amount/VAT percentage, and source net, VAT, and gross totals. Set
order_discount_present and shipping_present based on what is visibly printed. If one is visible
but unreadable, set its present flag true and its unreadable value null so review can stop.
If it is absent, set its present flag false and value null. An
item row must correspond to one printed row. Do not merge repeated SKUs or omit a row. Add one
source_values entry per legible field using a precise path such as
debtor.billing_address.zip_code or items.0.unit_net_price; the text must be copied exactly as
printed. Use null only for a value that is absent or genuinely unreadable. Never guess a missing
digit.
Make sure that the Zip codes copied or extracted are exactlty as printed, including leading zeros
and any punctuation. Do not infer or correct them.
"""

VERIFICATION_SYSTEM_PROMPT = """You are an independent verifier for purchase-order image extraction.
The image is the source of truth. Audit the draft against the image field by field; do not assume
the draft is correct or simply repeat it. Re-read both addresses and count every postal-code digit.
Check every telephone digit, identifier character, order date, payment value, item row, SKU,
quantity, unit price, VAT percentage, item discount, order-level discount, shipping
label, shipping net amount, shipping VAT rate, line total, and order total. Verify both
adjustment presence flags against the image, including visible but unreadable adjustments.
Correct any mismatch
you see. Preserve leading zeros and all characters in postal codes, telephone numbers, SKUs, order
references, and customer IDs. Return dates as YYYY-MM-DD and monetary or percentage values as
decimal strings. Keep source_values exact and field-specific. Do not calculate or infer values; use
null only when a field is absent or genuinely unreadable.
"""

VERIFICATION_USER_PROMPT = """Re-read the attached order image and verify the complete draft below.
Return a complete corrected payload in the same schema, including every printed line item and
field. Pay particular attention to digits that may have been dropped from postal codes or the
telephone number, Make sure that the zipcode number of digits is the correct number for the country
if not extract it again.

Draft extraction:
{draft_json}
"""

PROMPT_VERSION = "2.1"
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
            # Bound memory use even if the input file is unexpectedly large.
            with image_path.open("rb") as image_file:
                image_bytes = image_file.read(self.config.max_image_bytes + 1)
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
        payload = self._parse_image(
            data_url,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            stage="image extraction",
        )
        draft_json = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
        verified_payload = self._parse_image(
            data_url,
            system_prompt=VERIFICATION_SYSTEM_PROMPT,
            user_prompt=VERIFICATION_USER_PROMPT.format(draft_json=draft_json),
            stage="independent image verification",
        )
        self._validate_exact_identifier_transcriptions(verified_payload)

        source_values = [value.model_dump() for value in verified_payload.source_values]
        raw_order = verified_payload.model_dump(
            exclude={"source_values", "order_discount_present", "shipping_present"}
        )
        if verified_payload.order_discount_present:
            if not verified_payload.order_discount_percent:
                raise ExtractionError("visible order discount percentage is unreadable")
        elif verified_payload.order_discount_percent is not None:
            raise ExtractionError("order discount has a value but is marked absent")
        else:
            raw_order["order_discount_percent"] = "0"

        if verified_payload.shipping_present:
            shipping = verified_payload.shipping
            if shipping is None or not all(
                (shipping.name, shipping.net_amount, shipping.vat_rate_percent)
            ):
                raise ExtractionError("visible shipping details are unreadable")
        elif verified_payload.shipping is not None:
            raise ExtractionError("shipping has details but is marked absent")
        raw_order["extraction"] = ExtractionMetadata(
            image_sha256=image_hash,
            extracted_at=datetime.now(UTC),
            model=self.config.model,
            prompt_version=self.config.prompt_version or PROMPT_VERSION,
            source_values=source_values,
        )
        try:
            return OrderSource.model_validate(raw_order)
        except ValidationError as exc:
            raise ExtractionError(
                f"the verified extraction failed canonical validation: {exc}"
            ) from exc

    def _parse_image(
        self,
        data_url: str,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
    ) -> OrderExtractionPayload:
        started = time.monotonic()
        logger.info("Starting %s", stage, extra={"progress_step": stage})
        try:
            response = self.client.responses.parse(
                model=self.config.model,
                reasoning={"effort": self.config.reasoning_effort},
                store=False,
                input=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    },
                ],
                text_format=OrderExtractionPayload,
            )
        except Exception as exc:
            logger.warning("%s failed after %.1fs", stage.capitalize(), time.monotonic() - started)
            raise ExtractionError(f"OpenAI {stage} failed: {exc}") from exc

        payload = getattr(response, "output_parsed", None)
        if payload is None:
            raise ExtractionError(f"the model did not return a structured payload during {stage}")
        if not isinstance(payload, OrderExtractionPayload):
            try:
                payload = OrderExtractionPayload.model_validate(payload)
            except ValidationError as exc:
                raise ExtractionError(
                    f"the model response did not match the extraction schema during {stage}: {exc}"
                ) from exc
        logger.info("Completed %s in %.1fs", stage, time.monotonic() - started)
        return payload

    @staticmethod
    def _validate_exact_identifier_transcriptions(payload: OrderExtractionPayload) -> None:
        printed = {value.field_path: value.text for value in payload.source_values}
        extracted = {
            "external_reference": payload.external_reference,
            "source_customer_id": payload.source_customer_id,
            "debtor.telephone": payload.debtor.telephone,
            "debtor.billing_address.zip_code": payload.debtor.billing_address.zip_code,
            "debtor.delivery_address.zip_code": payload.debtor.delivery_address.zip_code,
        }
        extracted.update(
            {f"items.{index}.sku": item.sku for index, item in enumerate(payload.items)}
        )

        for field_path, value in extracted.items():
            source_text = printed.get(field_path)
            if source_text is not None and (value is None or value != source_text.strip()):
                raise ExtractionError(
                    f"the verified {field_path} conflicts with its exact source transcription"
                )
