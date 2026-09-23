from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from faktura_pilot.domain.models import OrderSource


@runtime_checkable
class ExtractionService(Protocol):
    def extract(self, image_path: Path) -> OrderSource:
        """Extract and validate one order image into the canonical source model."""
