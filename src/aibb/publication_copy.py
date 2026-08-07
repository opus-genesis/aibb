"""Deterministic reader-facing publication copy shared by builds and customization."""

from __future__ import annotations


def default_publication_license(title: str) -> str:
    return (
        f"# {title} publication licensing\n\n"
        "The contribution corpus, metadata, machine-readable exports, and model-authored media in this "
        "publication are dedicated to the public domain under "
        "[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).\n\n"
        "The generated presentation and other software components are produced by AIBB, whose software is "
        "licensed under the MIT License.\n"
    )
