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
        try:
            checkpoint = WorkflowCheckpoint.model_validate(checkpoint.model_dump())
        except ValueError as exc:
            raise WorkflowStoreError(f"refusing to save an invalid checkpoint: {exc}") from exc
        folder = self.run_directory(checkpoint.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        self._write_json_atomically(
            folder / "checkpoint.json",
            checkpoint.model_dump(mode="json"),
            prefix=".checkpoint-",
            operation="persist workflow checkpoint",
        )

    def load(self, run_id: str) -> WorkflowCheckpoint:
        path = self.run_directory(run_id) / "checkpoint.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "format_version" not in data:
                data["format_version"] = 1
            return WorkflowCheckpoint.model_validate(data)
        except FileNotFoundError as exc:
            raise WorkflowStoreError(f"run {run_id!r} was not found") from exc
        except (OSError, ValueError) as exc:
            raise WorkflowStoreError(f"could not load run {run_id!r}: {exc}") from exc

    def save_review(self, review: ReviewBundle) -> Path:
        folder = self.run_directory(review.run_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "review.json"
        self._write_json_atomically(
            path,
            review.model_dump(mode="json"),
            prefix=".review-",
            operation="save review bundle",
        )
        return path

    @staticmethod
    def _write_json_atomically(
        path: Path, value: dict[str, Any], *, prefix: str, operation: str
    ) -> None:
        rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=prefix,
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
                temporary.write(rendered)
                temporary.flush()
                os.fsync(temporary.fileno())
            temp_path.replace(path)
        except OSError as exc:
            raise WorkflowStoreError(f"could not {operation}: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

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
