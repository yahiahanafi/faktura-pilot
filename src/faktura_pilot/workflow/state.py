from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class WorkflowCheckpoint(BaseModel):
    """Durable run state. UIA tokens are intentionally session-local and never stored."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    state: WorkflowState = WorkflowState.EXTRACTED
    resume_state: WorkflowState | None = None
    source: OrderSource | None = None
    source_image: str | None = None
    pending_action: PendingAction | None = None
    order_number: str | None = None
    invoice_number: str | None = None
    completed_item_indexes: list[int] = Field(default_factory=list)
    review: ReviewBundle | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def mark_state(self, new_state: WorkflowState) -> None:
        if new_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid workflow transition: {self.state} -> {new_state}")
        self.state = new_state
        self.resume_state = None
        self.updated_at = datetime.now(UTC)

    def begin_action(self, name: str, **details: Any) -> None:
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
        self.state = self.resume_state
        self.resume_state = None
        self.review = None
        self.updated_at = datetime.now(UTC)


def checkpoint_directory(root: Path, run_id: str) -> Path:
    """Return a per-run directory after rejecting path traversal in the run id."""
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a simple directory name")
    return root / run_id
