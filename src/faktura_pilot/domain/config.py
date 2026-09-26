from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ConfigurationError(ValueError):
    pass


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai_api_key: SecretStr | None = None
    model: str = "gpt-6-luna"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "low"
    request_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    max_image_bytes: int = Field(default=20_000_000, gt=0)
    prompt_version: str = "2.0"

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> AppConfig:
        values: dict[str, object] = {}
        if config_path is not None:
            try:
                with config_path.open("rb") as config_file:
                    loaded = tomllib.load(config_file)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigurationError(f"could not read configuration file: {exc}") from exc
            if "image_to_cash" in loaded:
                unexpected = set(loaded) - {"image_to_cash"}
                if unexpected:
                    names = ", ".join(sorted(unexpected))
                    raise ConfigurationError(
                        "configuration values must be inside the [image_to_cash] table; "
                        f"found top-level key(s): {names}"
                    )
                loaded = loaded["image_to_cash"]
            if not isinstance(loaded, dict):
                raise ConfigurationError("configuration must be a TOML table")
            values.update(loaded)

        environment = os.environ if environ is None else environ
        env_fields = {
            "OPENAI_API_KEY": "openai_api_key",
            "IMAGE_TO_CASH_MODEL": "model",
            "IMAGE_TO_CASH_REASONING_EFFORT": "reasoning_effort",
            "IMAGE_TO_CASH_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
            "IMAGE_TO_CASH_MAX_IMAGE_BYTES": "max_image_bytes",
            "IMAGE_TO_CASH_PROMPT_VERSION": "prompt_version",
        }
        for env_name, field_name in env_fields.items():
            if env_name in environment:
                values[field_name] = environment[env_name]
        try:
            return cls.model_validate(values)
        except Exception as exc:
            raise ConfigurationError(f"invalid configuration: {exc}") from exc
