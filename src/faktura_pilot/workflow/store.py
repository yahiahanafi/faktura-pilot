from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from faktura_pilot.workflow.state import (
    ReviewBundle,
    WorkflowCheckpoint,
    checkpoint_directory,
)


class WorkflowStoreError(RuntimeError):
    pass


class WorkflowStore:
    """Persists checkpoints and a compact append-only event trail per run."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def run_directory(self, run_id: str) -> Path:
        return checkpoint_directory(self.root, run_id)

    def create(self, checkpoint: WorkflowCheckpoint) -> None:
        folder = self.run_directory(checkpoint.run_id)
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise WorkflowStoreError(f"run {checkpoint.run_id!r} already exists") from exc
        self.save(checkpoint)
        self.event(checkpoint.run_id, "run_created", {"state": checkpoint.state.value})

    def save(self, checkpoint: WorkflowCheckpoint) -> None:
        folder = self.run_directory(checkpoint.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "checkpoint.json"
        rendered = (
            json.dumps(checkpoint.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        )
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=folder,
                prefix=".checkpoint-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
                temporary.write(rendered)
                temporary.flush()
                os.fsync(temporary.fileno())
            temp_path.replace(target)
        except OSError as exc:
            raise WorkflowStoreError(f"could not persist workflow checkpoint: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def load(self, run_id: str) -> WorkflowCheckpoint:
        path = self.run_directory(run_id) / "checkpoint.json"
        try:
            return WorkflowCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowStoreError(f"run {run_id!r} was not found") from exc
        except (OSError, ValueError) as exc:
            raise WorkflowStoreError(f"could not load run {run_id!r}: {exc}") from exc

    def save_review(self, review: ReviewBundle) -> Path:
        folder = self.run_directory(review.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "review.json"
        try:
            path.write_text(
                json.dumps(review.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise WorkflowStoreError(f"could not save review bundle: {exc}") from exc
        return path

    def event(self, run_id: str, event: str, details: dict[str, Any] | None = None) -> None:
        path = self.run_directory(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "details": details or {},
        }
        try:
            with path.open("a", encoding="utf-8", newline="\n") as event_file:
                event_file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                event_file.flush()
        except OSError as exc:
            raise WorkflowStoreError(f"could not append workflow event: {exc}") from exc
