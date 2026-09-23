from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExtractionPayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExtractedAddress(ExtractionPayloadModel):
    additional_name: str | None
    street: str | None
    zip_code: str | None
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
    telephone: str | None
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


class ExtractedOriginalValue(ExtractionPayloadModel):
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
    totals: ExtractedTotals
    source_values: list[ExtractedOriginalValue]
