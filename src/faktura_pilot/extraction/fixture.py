from __future__ import annotations

from pathlib import Path

from faktura_pilot.domain.models import OrderSource


class FixtureExtractor:
    """Deterministic extractor used by tests and offline CLI runs."""

    def __init__(self, source: OrderSource | Path) -> None:
        if isinstance(source, Path):
            self.source = OrderSource.model_validate_json(source.read_text(encoding="utf-8"))
        else:
            self.source = source

    def extract(self, image_path: Path) -> OrderSource:
        del image_path  # The canonical fixture is intentionally independent of external services.
        return self.source.model_copy(deep=True)
