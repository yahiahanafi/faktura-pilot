import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from faktura_pilot.workflow.state import (
    ReviewBundle,
    WorkflowCheckpoint,
    WorkflowState,
    checkpoint_directory,
)
from faktura_pilot.workflow.store import WorkflowStore, WorkflowStoreError
from tests.factories import sample_order


class WorkflowRecoveryTests(unittest.TestCase):
    def test_review_checkpoint_round_trip_keeps_pending_action_for_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "runs")
            checkpoint = WorkflowCheckpoint(
                run_id="resume-1",
                state=WorkflowState.ORDER_OPEN,
                source=sample_order(),
            )
            checkpoint.begin_action("save_debtor", company="Example GmbH")
            checkpoint.wait_for_review(
                ReviewBundle(
                    run_id="resume-1",
                    failed_step="save Debtor",
                    reason="The result needs reconciliation.",
                )
            )
            store.create(checkpoint)

            recovered = store.load("resume-1")
            self.assertEqual(recovered.state, WorkflowState.WAITING_FOR_REVIEW)
            self.assertEqual(recovered.resume_state, WorkflowState.ORDER_OPEN)
            self.assertEqual(recovered.pending_action.name, "save_debtor")

            recovered.prepare_resume()
            store.save(recovered)
            resumed = store.load("resume-1")

            self.assertEqual(resumed.state, WorkflowState.ORDER_OPEN)
            self.assertIsNone(resumed.review)
            self.assertIsNone(resumed.resume_state)
            self.assertEqual(resumed.pending_action.name, "save_debtor")

    def test_uncertain_action_blocks_workflow_transition_until_confirmed(self) -> None:
        checkpoint = WorkflowCheckpoint(run_id="pending-1", state=WorkflowState.ORDER_OPEN)
        checkpoint.begin_action("save_debtor")

        with self.assertRaisesRegex(RuntimeError, "still needs reconciliation"):
            checkpoint.mark_state(WorkflowState.DEBTOR_RESOLVED)

        checkpoint.confirm_action()
        checkpoint.mark_state(WorkflowState.DEBTOR_RESOLVED)
        self.assertEqual(checkpoint.state, WorkflowState.DEBTOR_RESOLVED)

    def test_review_bundle_must_belong_to_checkpoint(self) -> None:
        checkpoint = WorkflowCheckpoint(run_id="run-1")
        with self.assertRaisesRegex(ValueError, "run_id"):
            checkpoint.wait_for_review(
                ReviewBundle(run_id="another-run", failed_step="step", reason="review")
            )

    def test_persisted_review_checkpoint_requires_resume_state_and_bundle(self) -> None:
        with self.assertRaises(ValidationError):
            WorkflowCheckpoint(run_id="review-1", state=WorkflowState.WAITING_FOR_REVIEW)

    def test_completed_item_indexes_must_be_valid_and_unique(self) -> None:
        source = sample_order()
        with self.assertRaises(ValidationError):
            WorkflowCheckpoint(
                run_id="bad-index",
                source=source,
                completed_item_indexes=[0, 0],
            )
        with self.assertRaises(ValidationError):
            WorkflowCheckpoint(
                run_id="out-of-range",
                source=source,
                completed_item_indexes=[len(source.items)],
            )

    def test_store_refuses_to_persist_an_invalid_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "runs")
            checkpoint = WorkflowCheckpoint(run_id="invalid-snapshot")
            checkpoint.state = WorkflowState.ORDER_SAVED

            with self.assertRaisesRegex(WorkflowStoreError, "invalid checkpoint"):
                store.save(checkpoint)

    def test_checkpoint_directory_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            checkpoint_directory(Path("runs"), "../outside")

    def test_failed_atomic_replace_keeps_previous_checkpoint_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "runs")
            checkpoint = WorkflowCheckpoint(run_id="atomic-1", source=sample_order())
            store.create(checkpoint)
            review = ReviewBundle(
                run_id="atomic-1", failed_step="first", reason="original review"
            )
            store.save_review(review)
            folder = store.run_directory("atomic-1")
            checkpoint_path = folder / "checkpoint.json"
            review_path = folder / "review.json"
            original_checkpoint = checkpoint_path.read_bytes()
            original_review = review_path.read_bytes()

            checkpoint.mark_state(WorkflowState.VALIDATED)
            updated_review = review.model_copy(update={"failed_step": "second"})
            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(WorkflowStoreError, "persist workflow checkpoint"):
                    store.save(checkpoint)
                with self.assertRaisesRegex(WorkflowStoreError, "save review bundle"):
                    store.save_review(updated_review)

            self.assertEqual(checkpoint_path.read_bytes(), original_checkpoint)
            self.assertEqual(review_path.read_bytes(), original_review)
            self.assertEqual(list(folder.glob(".*.tmp")), [])

    def test_loading_unversioned_checkpoint_marks_it_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "runs")
            store.create(WorkflowCheckpoint(run_id="legacy-1", source=sample_order()))
            path = store.run_directory("legacy-1") / "checkpoint.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["format_version"]
            path.write_text(json.dumps(data), encoding="utf-8")

            loaded = store.load("legacy-1")
            self.assertEqual(loaded.format_version, 1)

    def test_load_rejects_non_object_and_future_checkpoint_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = WorkflowStore(Path(temporary) / "runs")
            store.create(WorkflowCheckpoint(run_id="invalid-json-shape", source=sample_order()))
            path = store.run_directory("invalid-json-shape") / "checkpoint.json"
            for payload in ("[]", "null", '"text"'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(WorkflowStoreError):
                        store.load("invalid-json-shape")
            data = WorkflowCheckpoint(
                run_id="invalid-json-shape", source=sample_order()
            ).model_dump(mode="json")
            data["format_version"] = 3
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(WorkflowStoreError):
                store.load("invalid-json-shape")
