"""Fakturama UI automation interfaces and implementations.

The Windows dependencies are imported lazily by :class:`WindowsFakturamaGateway`,
so the domain model and fake gateways remain usable on non-Windows hosts.
"""

from faktura_pilot.automation.gateway import FakturamaGateway
from faktura_pilot.automation.models import (
    ActionOutcomeUnknown,
    AmbiguousControl,
    DebtorCandidate,
    DocumentRow,
    ElementNotFound,
    GatewayError,
    InvoiceEditorRef,
    ManualReviewRequired,
    OrderEditorRef,
    PaymentMethodCandidate,
    PostconditionFailed,
    PreflightResult,
    ProductCandidate,
    TransitionTimeout,
    VatCandidate,
    VerificationResult,
)
from faktura_pilot.automation.windows import WindowsFakturamaGateway

__all__ = [
    "ActionOutcomeUnknown",
    "AmbiguousControl",
    "DebtorCandidate",
    "DocumentRow",
    "ElementNotFound",
    "FakturamaGateway",
    "GatewayError",
    "InvoiceEditorRef",
    "ManualReviewRequired",
    "OrderEditorRef",
    "PaymentMethodCandidate",
    "PostconditionFailed",
    "PreflightResult",
    "ProductCandidate",
    "TransitionTimeout",
    "VatCandidate",
    "VerificationResult",
    "WindowsFakturamaGateway",
]
