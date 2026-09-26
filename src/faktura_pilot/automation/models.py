from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


class GatewayError(RuntimeError):
    """Base class for Fakturama attachment and interaction failures."""


class ManualReviewRequired(GatewayError):
    """The visible UI state cannot be resolved safely without a human."""


class AmbiguousControl(ManualReviewRequired):
    """More than one UI element satisfies a semantic control query."""


class ElementNotFound(GatewayError):
    """No accessible UIA element or OCR target matched the semantic query."""


class TransitionTimeout(GatewayError):
    """The application did not reach the expected UI state before timeout."""


class PostconditionFailed(GatewayError):
    """An action completed but its required readback invariant did not hold."""


class ActionOutcomeUnknown(ManualReviewRequired):
    """A save/create action may have succeeded, but its outcome is uncertain."""


@dataclass(frozen=True)
class OrderEditorRef:
    """Ephemeral identity of the currently open Order editor."""

    token: str
    number: str | None = None


@dataclass(frozen=True)
class InvoiceEditorRef:
    """Ephemeral identity of the currently open Invoice editor."""

    token: str
    number: str | None = None
    linked_order_number: str | None = None
    proposed_invoice_date: str | None = None
    proposed_service_date: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    application_title: str
    process_id: int
    version: str | None
    language: str | None
    dpi_aware: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DebtorCandidate:
    token: str
    company: str
    first_name: str | None
    last_name: str | None
    zip_code: str
    city: str
    billing_address: str | None = None
    delivery_address: str | None = None
    number: str | None = None


@dataclass(frozen=True)
class PaymentMethodCandidate:
    token: str
    name: str
    code: str | None = None


@dataclass(frozen=True)
class VatCandidate:
    token: str
    name: str
    value_percent: Decimal
    e_invoice_code: str


@dataclass(frozen=True)
class ProductCandidate:
    token: str
    sku: str
    name: str | None = None
    vat_rate_percent: Decimal | None = None


@dataclass(frozen=True)
class DocumentRow:
    number: str
    type: str
    reference: str
    total: Decimal
    state: str
    linked_order_number: str | None = None
    document_date: date | None = None


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    observations: tuple[str, ...] = ()
    evidence_paths: tuple[Path, ...] = ()
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)

    def require_verified(self, step: str) -> VerificationResult:
        if not self.verified:
            raise PostconditionFailed(
                f"{step} verification failed: " + ("; ".join(self.observations) or "values differ")
            )
        return self
