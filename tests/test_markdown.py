from __future__ import annotations

import pytest

from aibb.markdown import MarkdownValidationError, normalize_contribution_markdown, render_contribution_markdown


def test_markdown_normalization_strips_trailing_whitespace_except_in_fences() -> None:
    source = "Outside.  \n  \n```text  \ncode  \n  \n```\nAfter.\t \n"
    expected = "Outside.\n\n```text  \ncode  \n  \n```\nAfter.\n"

    assert normalize_contribution_markdown(source) == expected


def test_constrained_markdown_renders_allowed_profile_deterministically() -> None:
    source = """## A heading

A paragraph with *emphasis*, **strength**, and [a source](https://example.com/x?q=1).

---

> A quoted line.

1. first
2. second

- one
- two

```text
## [5.0.0] — unreleased
  whitespace stays
```
"""

    first = render_contribution_markdown(source)
    second = render_contribution_markdown(source)

    assert first == second
    assert "<h2>A heading</h2>" in first
    assert "<hr" in first
    assert "<em>emphasis</em>" in first
    assert "<strong>strength</strong>" in first
    assert '<a href="https://example.com/x?q=1">a source</a>' in first
    assert "<blockquote>" in first
    assert "<ol>" in first and "<ul>" in first
    assert '<pre><code class="language-text">' in first
    assert "  whitespace stays\n" in first


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("![alt](https://example.com/image.png)", "image"),
        ("[file](ftp://example.com/file)", "HTTP"),
    ],
)
def test_constrained_markdown_rejects_syntax_outside_profile(source: str, message: str) -> None:
    with pytest.raises(MarkdownValidationError, match=message):
        render_contribution_markdown(source)


def test_inline_code_renders() -> None:
    html = render_contribution_markdown("A `path/to/file` reference.")
    assert "<code>path/to/file</code>" in html


def test_raw_html_becomes_literal_text() -> None:
    html = render_contribution_markdown("A <b>tag</b> stays text.")
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
