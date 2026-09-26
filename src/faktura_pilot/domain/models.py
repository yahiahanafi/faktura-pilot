from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid numbers")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("value must be a decimal number using a dot separator") from exc
    if not parsed.is_finite():
        raise ValueError("value must be finite")
    return parsed


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PaymentStatus(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"


class Address(DomainModel):
    additional_name: str | None
    street: str = Field(min_length=1)
    zip_code: str = Field(min_length=1)
    city: str = Field(min_length=1)
    country: str = Field(min_length=1)
    address_specification: str | None
    district: str | None

class Debtor(DomainModel):
    company: str = Field(min_length=1)
    first_name: str | None
    last_name: str | None
    alias: str | None
    email: str | None
    telephone: str | None
    billing_address: Address
    delivery_address: Address

    @field_validator("email")
    @classmethod
    def validate_email_if_present(cls, value: str | None) -> str | None:
        if (
            value is not None
            and value
            and ("@" not in value or value.startswith("@") or value.endswith("@"))
        ):
            raise ValueError("email must contain a local part and a domain")
        return value


class Payment(DomainModel):
    method: str = Field(min_length=1)
    status: PaymentStatus
    payment_date: date | None

    @model_validator(mode="after")
    def payment_date_matches_status(self) -> Payment:
        if self.status is PaymentStatus.PAID and self.payment_date is None:
            raise ValueError("a paid order requires an explicit payment date")
        if self.status is PaymentStatus.UNPAID and self.payment_date is not None:
            raise ValueError("an unpaid order must not have a payment date")
        return self


class Item(DomainModel):
    sku: str = Field(min_length=1)
    description: str = Field(min_length=1)
    quantity: Decimal
    unit: str = Field(min_length=1)
    unit_net_price: Decimal
    vat_rate_percent: Decimal
    discount_percent: Decimal
    source_line_net_total: Decimal

    @field_validator("quantity", "vat_rate_percent", "discount_percent", mode="before")
    @classmethod
    def parse_decimal_values(cls, value: Any) -> Decimal:
        return _decimal(value)

    @field_validator("unit_net_price", "source_line_net_total", mode="before")
    @classmethod
    def parse_money_values(cls, value: Any) -> Decimal:
        return _money(value)

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, value: Decimal) -> Decimal:
        if value <= ZERO:
            raise ValueError("quantity must be greater than zero")
        return value

    @field_validator("unit_net_price", "source_line_net_total")
    @classmethod
    def money_must_not_be_negative(cls, value: Decimal) -> Decimal:
        if value < ZERO:
            raise ValueError("amount must not be negative")
        return value

    @field_validator("vat_rate_percent")
    @classmethod
    def vat_rate_must_be_in_range(cls, value: Decimal) -> Decimal:
        if value < ZERO or value > Decimal("100"):
            raise ValueError("VAT percentage must be between 0 and 100")
        return value

    @field_validator("discount_percent")
    @classmethod
    def discount_must_be_in_range(cls, value: Decimal) -> Decimal:
        if value < ZERO or value > Decimal("100"):
            raise ValueError("discount percentage must be between 0 and 100")
        return value


class OrderTotals(DomainModel):
    net: Decimal
    vat: Decimal
    gross: Decimal

    @field_validator("net", "vat", "gross", mode="before")
    @classmethod
    def parse_money_values(cls, value: Any) -> Decimal:
        return _money(value)

    @field_validator("net", "vat", "gross")
    @classmethod
    def totals_must_not_be_negative(cls, value: Decimal) -> Decimal:
        if value < ZERO:
            raise ValueError("total must not be negative")
        return value


class Shipping(DomainModel):
    name: str = Field(min_length=1)
    net_amount: Decimal
    vat_rate_percent: Decimal

    @field_validator("net_amount", mode="before")
    @classmethod
    def parse_net_amount(cls, value: Any) -> Decimal:
        return _money(value)

    @field_validator("vat_rate_percent", mode="before")
    @classmethod
    def parse_vat_rate(cls, value: Any) -> Decimal:
        return _decimal(value)

    @field_validator("net_amount")
    @classmethod
    def net_amount_must_not_be_negative(cls, value: Decimal) -> Decimal:
        if value < ZERO:
            raise ValueError("shipping net amount must not be negative")
        return value

    @field_validator("vat_rate_percent")
    @classmethod
    def vat_rate_must_be_in_range(cls, value: Decimal) -> Decimal:
        if value < ZERO or value > Decimal("100"):
            raise ValueError("shipping VAT percentage must be between 0 and 100")
        return value


class OriginalValue(DomainModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
    )

    field_path: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ExtractionMetadata(DomainModel):
    image_sha256: str
    extracted_at: datetime
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    source_values: list[OriginalValue] = Field(default_factory=list)

    @field_validator("image_sha256")
    @classmethod
    def image_hash_must_be_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("image_sha256 must be a 64-character SHA-256 hex digest")
        return value.lower()

    @field_validator("extracted_at")
    @classmethod
    def extraction_time_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extracted_at must include a timezone")
        return value.astimezone(UTC)


class OrderSource(DomainModel):
    order_date: date
    external_reference: str = Field(min_length=1)
    source_customer_id: str | None
    currency: str = Field(min_length=3, max_length=3)
    debtor: Debtor
    payment: Payment
    items: list[Item] = Field(min_length=1)
    order_discount_percent: Decimal = Decimal("0")
    shipping: Shipping | None = None
    totals: OrderTotals
    extraction: ExtractionMetadata

    @field_validator("order_discount_percent", mode="before")
    @classmethod
    def parse_order_discount(cls, value: Any) -> Decimal:
        return _decimal(value)

    @field_validator("order_discount_percent")
    @classmethod
    def order_discount_must_be_in_range(cls, value: Decimal) -> Decimal:
        if value < ZERO or value > Decimal("100"):
            raise ValueError("order discount percentage must be between 0 and 100")
        return value

    @field_validator("currency")
    @classmethod
    def currency_is_iso_code(cls, value: str) -> str:
        code = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("currency must be a three-letter ISO currency code")
        return code

    @model_validator(mode="after")
    def verify_source_calculations(self) -> OrderSource:
        from faktura_pilot.domain.calculations import calculate_order_totals, expected_line_net

        for index, item in enumerate(self.items, start=1):
            calculated = expected_line_net(
                item.quantity,
                item.unit_net_price,
                item.discount_percent,
            )
            if abs(calculated - item.source_line_net_total) > CENT:
                raise ValueError(
                    f"item {index} ({item.sku}) source line net total "
                    f"{item.source_line_net_total} does not match calculated {calculated}"
                )

        calculated_totals = calculate_order_totals(
            self.items, self.order_discount_percent, self.shipping
        )
        for name in ("net", "vat", "gross"):
            source_value = getattr(self.totals, name)
            calculated_value = getattr(calculated_totals, name)
            if abs(source_value - calculated_value) > CENT:
                raise ValueError(
                    f"source {name} total {source_value} does not match "
                    f"calculated {calculated_value}"
                )
        return self
