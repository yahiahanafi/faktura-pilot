from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExtractedAddress(ExtractionPayloadModel):
    additional_name: str | None
    street: str | None
    zip_code: str | None = Field(
        description=(
            "The complete postal code copied as a string from the image. Preserve every digit, "
            "including leading zeros; never truncate, pad, or infer a digit."
        )
    )
    city: str | None
    country: str | None
    address_specification: str | None
    district: str | None


class ExtractedDebtor(ExtractionPayloadModel):
    company: str | None
    first_name: str | None
    last_name: str | None
    alias: str | None
    email: str | None
    telephone: str | None = Field(
        description=(
            "The complete telephone number exactly as printed, preserving every digit, leading "
            "zero, plus sign, spacing, and punctuation."
        )
    )
    billing_address: ExtractedAddress
    delivery_address: ExtractedAddress


class ExtractedPayment(ExtractionPayloadModel):
    method: str | None
    status: str | None
    payment_date: str | None


class ExtractedItem(ExtractionPayloadModel):
    sku: str | None
    description: str | None
    quantity: str | None
    unit: str | None
    unit_net_price: str | None
    vat_rate_percent: str | None
    discount_percent: str | None
    source_line_net_total: str | None


class ExtractedTotals(ExtractionPayloadModel):
    net: str | None
    vat: str | None
    gross: str | None


class ExtractedShipping(ExtractionPayloadModel):
    name: str | None = Field(
        description=(
            "Shipping label copied from the image; null if it cannot be read. Never invent one."
        )
    )
    net_amount: str | None = Field(
        description="Printed shipping net amount as a decimal string; null if unreadable."
    )
    vat_rate_percent: str | None = Field(
        description="Printed shipping VAT percentage as a decimal string; null if unreadable."
    )


class ExtractedOriginalValue(ExtractionPayloadModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    field_path: str
    text: str


class OrderExtractionPayload(ExtractionPayloadModel):
    order_date: str | None
    external_reference: str | None
    source_customer_id: str | None
    currency: str | None
    debtor: ExtractedDebtor
    payment: ExtractedPayment
    items: list[ExtractedItem]
    order_discount_present: bool = Field(
        description=(
            "True when an order-level discount is visible, even if its percentage is unreadable."
        ),
    )
    order_discount_percent: str | None = Field(
        description=(
            "Printed order-level discount percentage as a decimal string; "
            "null if absent or unreadable."
        ),
    )
    shipping_present: bool = Field(
        description="True when a shipping charge is visible, even if its details are unreadable.",
    )
    shipping: ExtractedShipping | None
    totals: ExtractedTotals
    source_values: list[ExtractedOriginalValue]

    @model_validator(mode="before")
    @classmethod
    def default_legacy_adjustments(cls, value: object) -> object:
        # Preserve old fixtures while keeping all fields required in the API schema.
        if isinstance(value, dict):
            value = {
                "order_discount_present": False,
                "order_discount_percent": None,
                "shipping_present": False,
                "shipping": None,
                **value,
            }
        return value

    @model_validator(mode="after")
    def source_value_paths_are_unique(self) -> OrderExtractionPayload:
        paths = [value.field_path for value in self.source_values]
        if len(paths) != len(set(paths)):
            raise ValueError("source_values must contain at most one transcription per field path")
        return self
