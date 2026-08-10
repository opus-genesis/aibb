from pathlib import Path

import pytest

from aibb import __version__
from aibb.config import (
    CompatibilityError,
    compatible_builder_requirement,
    load_archive_config,
    verify_archive_compatibility,
)


def write_config(root: Path, text: str) -> None:
    (root / "aibb.toml").write_text(text, encoding="utf-8")


def test_current_builder_and_schema_are_compatible(tmp_path: Path) -> None:
    write_config(tmp_path, f'schema_version = 1\n\n[builder]\nrequirement = "aibb=={__version__}"\n')

    config = load_archive_config(tmp_path)
    verify_archive_compatibility(config)


def test_current_compatibility_range_uses_the_release_as_a_bounded_floor(tmp_path: Path) -> None:
    requirement = compatible_builder_requirement()
    write_config(tmp_path, f'schema_version = 1\n\n[builder]\nrequirement = "{requirement}"\n')

    verify_archive_compatibility(load_archive_config(tmp_path))
    assert requirement == f"aibb>={__version__},<0.2"


def test_unknown_schema_fails_clearly(tmp_path: Path) -> None:
    write_config(tmp_path, f'schema_version = 99\n\n[builder]\nrequirement = "aibb=={__version__}"\n')

    with pytest.raises(CompatibilityError, match="Unsupported data schema 99"):
        verify_archive_compatibility(load_archive_config(tmp_path))


def test_incompatible_builder_fails_clearly(tmp_path: Path) -> None:
    write_config(tmp_path, 'schema_version = 1\n\n[builder]\nrequirement = "aibb==9.9.9"\n')

    with pytest.raises(CompatibilityError, match="requires aibb==9.9.9"):
        verify_archive_compatibility(load_archive_config(tmp_path))
