from __future__ import annotations

from decimal import Decimal

from faktura_pilot.automation.models import (
    DebtorCandidate,
    PaymentMethodCandidate,
    ProductCandidate,
    VatCandidate,
)
from faktura_pilot.domain.models import Debtor
from faktura_pilot.domain.policy import (
    CandidateResolution,
    DebtorIdentity,
    PaymentCode,
    ResolutionAction,
    VatIdentity,
    debtor_matches,
    normalize_text,
    payment_code_for_method,
    resolve_exact_candidates,
    sku_matches,
    vat_matches,
)


def resolve_debtor(
    expected: Debtor,
    candidates: list[DebtorCandidate],
) -> CandidateResolution[DebtorCandidate]:
    resolution = resolve_exact_candidates(
        candidates,
        lambda candidate: debtor_matches(
            expected,
            DebtorIdentity(
                company=candidate.company,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                zip_code=candidate.zip_code,
                city=candidate.city,
            ),
        ),
    )
    expected_person = (normalize_text(expected.first_name), normalize_text(expected.last_name))
    has_expected_person = all(expected_person)
    conflicts = [
        candidate
        for candidate in candidates
        if (
            normalize_text(candidate.company)
            and normalize_text(candidate.company) == normalize_text(expected.company)
        )
        or (
            has_expected_person
            and (normalize_text(candidate.first_name), normalize_text(candidate.last_name))
            == expected_person
        )
    ]
    conflicting_candidates = [
        candidate
        for candidate in conflicts
        if not debtor_matches(
            expected,
            DebtorIdentity(
                company=candidate.company,
                first_name=candidate.first_name,
                last_name=candidate.last_name,
                zip_code=candidate.zip_code,
                city=candidate.city,
            ),
        )
    ]
    if conflicting_candidates:
        return CandidateResolution(
            action=ResolutionAction.MANUAL_REVIEW,
            match=None,
            exact_match_count=resolution.exact_match_count,
            reason=(
                "a result shares the debtor company or customer name but conflicts with one or "
                "more required identity fields"
            ),
        )
    return resolution


def resolve_product(
    expected_sku: str,
    candidates: list[ProductCandidate],
) -> CandidateResolution[ProductCandidate]:
    return resolve_exact_candidates(
        candidates,
        lambda candidate: sku_matches(expected_sku, candidate.sku),
    )


def resolve_vat(
    rate: Decimal,
    candidates: list[VatCandidate],
) -> CandidateResolution[VatCandidate]:
    conflicts = conflicting_vat(rate, candidates)
    if conflicts:
        return CandidateResolution(
            action=ResolutionAction.MANUAL_REVIEW,
            match=None,
            exact_match_count=0,
            reason="a VAT result shares the requested name or rate but has conflicting settings",
        )
    return resolve_exact_candidates(
        candidates,
        lambda candidate: vat_matches(
            rate,
            VatIdentity(
                name=candidate.name,
                value_percent=candidate.value_percent,
                e_invoice_code=candidate.e_invoice_code,
            ),
        ),
    )


def payment_code(method: str) -> PaymentCode:
    code = payment_code_for_method(method)
    if code is None:
        raise ValueError(f"unsupported payment method {method!r}")
    return code


def resolve_payment_method(
    source_method: str,
    code: PaymentCode,
    candidates: list[PaymentMethodCandidate],
) -> CandidateResolution[PaymentMethodCandidate]:
    named_candidates = [
        candidate
        for candidate in candidates
        if normalize_text(candidate.name) == normalize_text(source_method)
    ]
    conflicting_definitions = [
        candidate
        for candidate in named_candidates
        if candidate.code is not None
        and normalize_text(candidate.code) != normalize_text(code.value)
    ]
    if conflicting_definitions:
        return CandidateResolution(
            action=ResolutionAction.MANUAL_REVIEW,
            match=None,
            exact_match_count=0,
            reason=(
                "a payment method with the requested name has a conflicting payment-code "
                "definition"
            ),
        )

    def exact(candidate: PaymentMethodCandidate) -> bool:
        if normalize_text(candidate.name) != normalize_text(source_method):
            return False
        # The UI may expose only the visible method name. If it exposes a
        # definition code as well, it must agree with the deterministic mapping.
        return candidate.code is None or normalize_text(candidate.code) == normalize_text(
            code.value
        )

    return resolve_exact_candidates(candidates, exact)


def conflicting_vat(rate: Decimal, candidates: list[VatCandidate]) -> list[VatCandidate]:
    """Return rate/name collisions that must be reviewed instead of duplicated."""
    expected_name = f"VAT {format(rate.normalize(), 'f')}%"
    expected_normalized = normalize_text(expected_name)
    return [
        candidate
        for candidate in candidates
        if (
            normalize_text(candidate.name) == expected_normalized or candidate.value_percent == rate
        )
        and not vat_matches(
            rate,
            VatIdentity(
                name=candidate.name,
                value_percent=candidate.value_percent,
                e_invoice_code=candidate.e_invoice_code,
            ),
        )
    ]
