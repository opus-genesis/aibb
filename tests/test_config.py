from pathlib import Path

import pytest

from aibb import __version__
from aibb.config import CompatibilityError, load_archive_config, verify_archive_compatibility


def write_config(root: Path, text: str) -> None:
    (root / "aibb.toml").write_text(text, encoding="utf-8")


def test_current_builder_and_schema_are_compatible(tmp_path: Path) -> None:
    write_config(tmp_path, f'schema_version = 1\n\n[builder]\nrequirement = "aibb=={__version__}"\n')

    config = load_archive_config(tmp_path)
    verify_archive_compatibility(config)


def test_unknown_schema_fails_clearly(tmp_path: Path) -> None:
    write_config(tmp_path, f'schema_version = 99\n\n[builder]\nrequirement = "aibb=={__version__}"\n')

    with pytest.raises(CompatibilityError, match="Unsupported data schema 99"):
        verify_archive_compatibility(load_archive_config(tmp_path))


def test_incompatible_builder_fails_clearly(tmp_path: Path) -> None:
    write_config(tmp_path, 'schema_version = 1\n\n[builder]\nrequirement = "aibb==9.9.9"\n')

    with pytest.raises(CompatibilityError, match="requires aibb==9.9.9"):
        verify_archive_compatibility(load_archive_config(tmp_path))
