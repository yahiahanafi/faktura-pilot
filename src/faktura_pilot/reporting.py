"""Live command-line progress without changing stdout results."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from datetime import datetime
from typing import Any, TextIO


class _ProgressLogHandler(logging.Handler):
    def __init__(self, reporter: ProgressReporter) -> None:
        super().__init__(level=logging.INFO)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.reporter.log_record(record)
        except Exception:
            # Reporting must not change extraction or UI automation outcomes.
            pass


class ProgressReporter:
    """Show workflow stages and periodic status during blocking UI or network calls."""

    def __init__(
        self,
        command: str,
        *,
        run_id: str | None = None,
        quiet: bool = False,
        interval: float = 20.0,
        stream: TextIO | None = None,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("progress interval must be positive and finite")
        self.command = command
        self.run_id = run_id
        self.quiet = quiet
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = time.monotonic()
        self._last_update = self._started
        self._current_step = "starting"
        self._finished = False
        self._output_broken = False
        self._loggers = (
            logging.getLogger("faktura_pilot.extraction"),
            logging.getLogger("faktura_pilot.automation"),
        )
        self._previous_log_levels: dict[logging.Logger, int] = {}
        self._log_handler = _ProgressLogHandler(self)

    def __enter__(self) -> ProgressReporter:
        label = f"{self.command} {self.run_id}" if self.run_id else self.command
        self._write(f"Started {label}")
        if not self.quiet:
            try:
                for logger in self._loggers:
                    self._previous_log_levels[logger] = logger.level
                    if logger.getEffectiveLevel() > logging.INFO:
                        logger.setLevel(logging.INFO)
                    logger.addHandler(self._log_handler)
                thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                thread.start()
                self._thread = thread
            except Exception:
                self._cleanup()
                raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        del exc_type, traceback
        try:
            if exc is not None:
                self.fail(exc)
            elif not self._finished:
                self.finish("complete")
        finally:
            self._cleanup()

    def event(self, name: str, details: dict[str, Any]) -> None:
        """Render the workflow events relevant to the operator."""
        if name == "step_started":
            step = str(details.get("step", "working"))
            self._current_step = step
            self._write(f"Step: {step}")
        elif name == "action_started":
            action = str(details.get("name", "action"))
            self._current_step = action
            self._write(f"Action started: {action}")
        elif name == "action_confirmed":
            self._write(f"Action confirmed: {details.get('name', 'action')}")
        elif name == "state_verified":
            self._write(f"State verified: {details.get('state', 'unknown')}")
        elif name == "waiting_for_review":
            self._write(
                f"Waiting for review at {details.get('step', 'workflow')}: "
                f"{details.get('reason', 'review needed')}"
            )
        elif name == "evidence_capture_failed":
            self._write(f"Evidence capture failed: {details.get('error', 'unknown error')}")

    def step(self, description: str) -> None:
        self.event("step_started", {"step": description})

    def log_record(self, record: logging.LogRecord) -> None:
        with self._lock:
            stage = getattr(record, "progress_step", None)
            if stage:
                self._current_step = str(stage)
            self._write_locked(record.getMessage())

    def heartbeat(self) -> None:
        """Report the current step after an interval without another update."""
        with self._lock:
            if self.quiet or self._finished:
                return
            idle = time.monotonic() - self._last_update
            if idle < self.interval:
                return
            self._write_locked(f"Still working: {self._current_step} ({idle:.0f}s since update)")

    def finish(self, state: str) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._write_locked(f"Finished: {state}")
        self._stop.set()

    def fail(self, error: BaseException) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._write_locked(f"Stopped at {self._current_step}: {error}")
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.heartbeat()

    def _cleanup(self) -> None:
        self._stop.set()
        for logger, previous_level in self._previous_log_levels.items():
            logger.removeHandler(self._log_handler)
            logger.setLevel(previous_level)
        self._previous_log_levels.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _write(self, message: str) -> None:
        with self._lock:
            self._write_locked(message)

    def _write_locked(self, message: str) -> None:
        if self.quiet or self._output_broken:
            return
        now = time.monotonic()
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.stream.write(f"[{stamp} +{now - self._started:.1f}s] {message}\n")
            self.stream.flush()
        except (OSError, ValueError):
            self._output_broken = True
            self._stop.set()
        self._last_update = now
