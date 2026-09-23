from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from faktura_pilot.domain.models import Address, Debtor


class ResolutionAction(StrEnum):
    REUSE = "reuse"
    CREATE = "create"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True)
class CandidateResolution[T]:
    action: ResolutionAction
    match: T | None
    exact_match_count: int
    reason: str


@dataclass(frozen=True)
class DebtorIdentity:
    company: str
    first_name: str | None
    last_name: str | None
    zip_code: str
    city: str


@dataclass(frozen=True)
class VatIdentity:
    name: str
    value_percent: Decimal
    e_invoice_code: str


class PaymentCode(StrEnum):
    CREDIT_TRANSFER = "Credit transfer"
    CREDIT_CARD = "Credit card"
    SEPA_DIRECT_DEBIT = "SEPA direct debit"


def normalize_text(value: str | None) -> str:
    """Normalize human-entered identity text while retaining exact equality semantics."""
    if value is None:
        return ""
    compatible = unicodedata.normalize("NFKC", value)
    return " ".join(compatible.split()).casefold()


def debtor_identity(debtor: Debtor) -> DebtorIdentity:
    return DebtorIdentity(
        company=debtor.company,
        first_name=debtor.first_name,
        last_name=debtor.last_name,
        zip_code=debtor.billing_address.zip_code,
        city=debtor.billing_address.city,
    )


def debtor_matches(expected: Debtor, candidate: DebtorIdentity) -> bool:
    expected_identity = debtor_identity(expected)
    fields = ("company", "first_name", "last_name", "zip_code", "city")
    return all(
        normalize_text(getattr(expected_identity, field))
        == normalize_text(getattr(candidate, field))
        for field in fields
    )


def sku_matches(expected_sku: str, candidate_sku: str) -> bool:
    """Compare SKU exactly after trimming outer whitespace; preserve case."""
    return expected_sku.strip() == candidate_sku.strip()


def expected_vat_name(rate_percent: Decimal) -> str:
    rate = format(rate_percent.normalize(), "f")
    return f"VAT {rate}%"


def vat_matches(expected_rate: Decimal, candidate: VatIdentity) -> bool:
    return (
        normalize_text(candidate.name) == normalize_text(expected_vat_name(expected_rate))
        and candidate.value_percent == expected_rate
        and normalize_text(candidate.e_invoice_code) == "s"
    )


def payment_code_for_method(method: str) -> PaymentCode | None:
    normalized = normalize_text(method)
    mapping = {
        "bank transfer": PaymentCode.CREDIT_TRANSFER,
        "credit card": PaymentCode.CREDIT_CARD,
        "sepa direct debit": PaymentCode.SEPA_DIRECT_DEBIT,
    }
    return mapping.get(normalized)


def same_address(first: Address, second: Address) -> bool:
    fields = (
        "additional_name",
        "street",
        "zip_code",
        "city",
        "country",
        "address_specification",
        "district",
    )
    return all(
        normalize_text(getattr(first, field)) == normalize_text(getattr(second, field))
        for field in fields
    )


def resolve_exact_candidates[T](
    candidates: Iterable[T],
    is_exact_match: Callable[[T], bool],
) -> CandidateResolution[T]:
    exact = [candidate for candidate in candidates if is_exact_match(candidate)]
    if len(exact) == 1:
        return CandidateResolution(
            action=ResolutionAction.REUSE,
            match=exact[0],
            exact_match_count=1,
            reason="one exact match is available",
        )
    if not exact:
        return CandidateResolution(
            action=ResolutionAction.CREATE,
            match=None,
            exact_match_count=0,
            reason="no exact match is available",
        )
    return CandidateResolution(
        action=ResolutionAction.MANUAL_REVIEW,
        match=None,
        exact_match_count=len(exact),
        reason="multiple exact matches are ambiguous",
    )
