"""Configuration shared by the code and public-data repositories."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from aibb import __version__

SUPPORTED_DATA_SCHEMA_VERSIONS = frozenset({1})
DATA_CONFIG_NAME = "aibb.toml"


class BuilderPin(BaseModel):
    """Compatible package requirement used to operate a data revision."""

    model_config = ConfigDict(extra="forbid")

    requirement: str


class ArchiveConfig(BaseModel):
    """Version handshake stored at the root of the public data repository."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    builder: BuilderPin


class CompatibilityError(ValueError):
    """Raised when a code checkout cannot safely operate on a data checkout."""


def compatible_builder_requirement(version: str = __version__) -> str:
    """Allow compatible releases while retaining the creating release as the floor."""

    parsed = Version(version)
    upper = f"0.{parsed.minor + 1}" if parsed.major == 0 else str(parsed.major + 1)
    return f"aibb>={parsed},<{upper}"


def load_archive_config(data_repo: Path) -> ArchiveConfig:
    config_path = data_repo.resolve() / DATA_CONFIG_NAME
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CompatibilityError(f"Missing data-repository configuration: {config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise CompatibilityError(f"Invalid TOML in {config_path}: {error}") from error
    return ArchiveConfig.model_validate(payload)


def verify_archive_compatibility(config: ArchiveConfig) -> None:
    if config.schema_version not in SUPPORTED_DATA_SCHEMA_VERSIONS:
        supported = ", ".join(str(version) for version in sorted(SUPPORTED_DATA_SCHEMA_VERSIONS))
        raise CompatibilityError(
            f"Unsupported data schema {config.schema_version}; this AIBB build supports: {supported}"
        )

    requirement = Requirement(config.builder.requirement)
    if requirement.name.lower() != "aibb":
        raise CompatibilityError(
            f"Builder requirement must name the aibb package, not {requirement.name!r}"
        )
    if not requirement.specifier.contains(__version__, prereleases=True):
        raise CompatibilityError(
            f"Data repository requires {requirement}; running AIBB version is {__version__}"
        )
