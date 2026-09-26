from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from faktura_pilot.domain.models import OrderSource


class WorkflowState(StrEnum):
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    ORDER_OPEN = "order_open"
    DEBTOR_RESOLVED = "debtor_resolved"
    ITEMS_RESOLVED = "items_resolved"
    ORDER_SAVED = "order_saved"
    ORDER_VERIFIED = "order_verified"
    INVOICE_OPEN = "invoice_open"
    PAYMENT_APPLIED = "payment_applied"
    INVOICE_SAVED = "invoice_saved"
    COMPLETE = "complete"
    WAITING_FOR_REVIEW = "waiting_for_review"


_ALLOWED_TRANSITIONS = {
    WorkflowState.EXTRACTED: {WorkflowState.VALIDATED},
    WorkflowState.VALIDATED: {WorkflowState.ORDER_OPEN},
    WorkflowState.ORDER_OPEN: {WorkflowState.DEBTOR_RESOLVED},
    WorkflowState.DEBTOR_RESOLVED: {WorkflowState.ITEMS_RESOLVED},
    WorkflowState.ITEMS_RESOLVED: {WorkflowState.ORDER_SAVED},
    WorkflowState.ORDER_SAVED: {WorkflowState.ORDER_VERIFIED},
    WorkflowState.ORDER_VERIFIED: {WorkflowState.INVOICE_OPEN},
    WorkflowState.INVOICE_OPEN: {WorkflowState.PAYMENT_APPLIED},
    WorkflowState.PAYMENT_APPLIED: {WorkflowState.INVOICE_SAVED},
    WorkflowState.INVOICE_SAVED: {WorkflowState.COMPLETE},
    WorkflowState.COMPLETE: set(),
    WorkflowState.WAITING_FOR_REVIEW: set(),
}


class PendingAction(BaseModel):
    """A side effect started but not yet confirmed in the checkpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    failed_step: str
    reason: str
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_path: str | None = None
    permitted_resolutions: list[str] = Field(
        default_factory=lambda: [
            "correct the source data and restart with a new run",
            "correct Fakturama master data or the open document, then resume",
            "abort this run",
        ]
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InvoiceProvenance(BaseModel):
    """Durable context for an Invoice created from a verified saved Order."""

    model_config = ConfigDict(extra="forbid")

    source_order_number: str = Field(min_length=1)
    invoice_number: str | None = None
    proposed_invoice_date: str | None = None
    proposed_service_date: str | None = None
    evidence_path: str | None = None
    creation_method: Literal["order_followup_button"] = "order_followup_button"


class WorkflowCheckpoint(BaseModel):
    """Durable run state. UIA tokens are intentionally session-local and never stored."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    format_version: int = Field(default=2, ge=1, le=2)
    state: WorkflowState = WorkflowState.EXTRACTED
    resume_state: WorkflowState | None = None
    source: OrderSource | None = None
    source_image: str | None = None
    pending_action: PendingAction | None = None
    draft_order_number: str | None = None
    order_number: str | None = None
    invoice_number: str | None = None
    invoice_provenance: InvoiceProvenance | None = None
    completed_item_indexes: list[int] = Field(default_factory=list)
    review: ReviewBundle | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_recovery_fields(self) -> WorkflowCheckpoint:
        if self.state is WorkflowState.WAITING_FOR_REVIEW:
            if self.resume_state is None:
                raise ValueError("a review checkpoint must retain its resume state")
            if self.resume_state is WorkflowState.WAITING_FOR_REVIEW:
                raise ValueError("a review checkpoint cannot resume to the review state")
            if self.review is None:
                raise ValueError("a review checkpoint must include its review bundle")
            if self.review.run_id != self.run_id:
                raise ValueError("review bundle run_id must match checkpoint run_id")
        elif self.resume_state is not None or self.review is not None:
            raise ValueError(
                "resume state and review bundle are only valid while waiting for review"
            )

        if (
            self.invoice_provenance is not None
            and self.invoice_provenance.source_order_number != self.order_number
        ):
            raise ValueError("Invoice provenance must name the saved source Order")
        if (
            self.invoice_provenance is not None
            and self.invoice_number is not None
            and self.invoice_provenance.invoice_number is not None
            and self.invoice_provenance.invoice_number != self.invoice_number
        ):
            raise ValueError("Invoice provenance number must match the saved Invoice")

        if len(self.completed_item_indexes) != len(set(self.completed_item_indexes)):
            raise ValueError("completed item indexes must be unique")
        if any(index < 0 for index in self.completed_item_indexes):
            raise ValueError("completed item indexes cannot be negative")
        if self.source is not None and any(
            index >= len(self.source.items) for index in self.completed_item_indexes
        ):
            raise ValueError("completed item index is outside the source order")

        effective_state = (
            self.resume_state
            if self.state is WorkflowState.WAITING_FOR_REVIEW
            else self.state
        )
        if effective_state in {
            WorkflowState.ORDER_SAVED,
            WorkflowState.ORDER_VERIFIED,
            WorkflowState.INVOICE_OPEN,
            WorkflowState.PAYMENT_APPLIED,
            WorkflowState.INVOICE_SAVED,
            WorkflowState.COMPLETE,
        } and not self.order_number:
            raise ValueError("workflow state requires a saved Order number")
        if (
            effective_state in {WorkflowState.INVOICE_SAVED, WorkflowState.COMPLETE}
            and not self.invoice_number
        ):
            raise ValueError("workflow state requires a saved Invoice number")
        return self

    def mark_state(self, new_state: WorkflowState) -> None:
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid workflow transition: {self.state} -> {new_state}")
        if self.pending_action is not None:
            raise RuntimeError(
                f"cannot advance to {new_state}; "
                f"{self.pending_action.name} still needs reconciliation"
            )
        self.state = new_state
        self.resume_state = None
        self.review = None
        self.updated_at = datetime.now(UTC)

    def begin_action(self, name: str, **details: Any) -> None:
        if self.state in {WorkflowState.WAITING_FOR_REVIEW, WorkflowState.COMPLETE}:
            raise RuntimeError(f"cannot begin {name} while workflow is {self.state}")
        if self.pending_action is not None:
            raise RuntimeError(
                f"cannot begin {name}; {self.pending_action.name} still needs reconciliation"
            )
        self.pending_action = PendingAction(name=name, details=details)
        self.updated_at = datetime.now(UTC)

    def confirm_action(self) -> None:
        self.pending_action = None
        self.updated_at = datetime.now(UTC)

    def wait_for_review(self, bundle: ReviewBundle) -> None:
        if bundle.run_id != self.run_id:
            raise ValueError("review bundle run_id must match checkpoint run_id")
        if self.state is not WorkflowState.WAITING_FOR_REVIEW:
            self.resume_state = self.state
        self.state = WorkflowState.WAITING_FOR_REVIEW
        self.review = bundle
        self.updated_at = datetime.now(UTC)

    def prepare_resume(self) -> None:
        if self.state is not WorkflowState.WAITING_FOR_REVIEW:
            return
        if self.resume_state is None:
            raise RuntimeError("review checkpoint has no resume state")
        if self.review is None or self.review.run_id != self.run_id:
            raise RuntimeError("review checkpoint has no matching review bundle")
        self.state = self.resume_state
        self.resume_state = None
        self.review = None
        self.updated_at = datetime.now(UTC)


def checkpoint_directory(root: Path, run_id: str) -> Path:
    """Return a per-run directory after rejecting path traversal in the run id."""
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a simple directory name")
    return root / run_id
