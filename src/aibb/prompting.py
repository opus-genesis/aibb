"""Deterministic board prompt composition over trusted local prompt and document files."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

SUPPORTED_TEXT_SUFFIXES = {".md", ".txt"}
MAX_SOURCE_BYTES = 1_000_000
MAX_RENDERED_BYTES = 2_000_000
MAX_PROMPT_DEPTH = 16

_PROMPT_DIRECTIVE = re.compile(r"\{\{\s*prompt\s*:\s*([A-Za-z0-9_./-]+)\s*\}\}")
_DOCUMENT_DIRECTIVE = re.compile(r"\{\{\s*doc\s*:\s*([A-Za-z0-9_./-]+)\s*\}\}")
_RUNVAR_DIRECTIVE = re.compile(r"\{\{\s*runvar\s*:\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_MALFORMED_DIRECTIVE = re.compile(r"\{\{\s*(?:prompt|doc|runvar)\s*:")


class PromptPackageError(ValueError):
    """Raised when a prompt package cannot be resolved or rendered safely."""


@dataclass(frozen=True)
class PromptWarning:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class RenderedPrompt:
    entrypoint: str
    text: str
    prompt_paths: tuple[str, ...]
    document_paths: tuple[str, ...]
    source_sha256: str
    rendered_sha256: str


class _PromptEnvironment(ImmutableSandboxedEnvironment):
    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:  # noqa: ARG002
        return False

    def is_safe_callable(self, obj: Any) -> bool:  # noqa: ARG002
        return False


def _relative_text_files(package_root: Path, root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise PromptPackageError(f"Prompt package directory does not exist: {root}")
    discovered: dict[str, str] = {}
    casefolded: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise PromptPackageError(f"Prompt package files must not be symbolic links: {path}")
        if path.suffix.casefold() not in SUPPORTED_TEXT_SUFFIXES:
            continue
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(package_root).as_posix()
        except (FileNotFoundError, ValueError) as error:
            raise PromptPackageError(f"Prompt package path escapes its root: {path}") from error
        folded = relative.casefold()
        if folded in casefolded and casefolded[folded] != relative:
            raise PromptPackageError(
                f"Prompt package paths collide when case-folded: {casefolded[folded]} and {relative}"
            )
        raw = resolved.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise PromptPackageError(f"Prompt package text file exceeds {MAX_SOURCE_BYTES} bytes: {relative}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PromptPackageError(f"Prompt package text file is not valid UTF-8: {relative}") from error
        if "\x00" in text:
            raise PromptPackageError(f"Prompt package text file contains NUL: {relative}")
        discovered[relative] = text.replace("\r\n", "\n").replace("\r", "\n")
        casefolded[folded] = relative
    return discovered


def _safe_relative_path(value: str, *, kind: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PromptPackageError(f"Invalid {kind} path: {value!r}")
    return path


class PromptPackage:
    """Discover prompt/document sources and render one model-visible prompt deterministically."""

    def __init__(
        self,
        package_root: Path,
        *,
        prompts_root: str = "prompts",
        documents_root: str = "documents",
        retrievable: list[str] | None = None,
    ) -> None:
        self.package_root = package_root.resolve()
        self.prompts_root = self._resolve_root(prompts_root, kind="prompts")
        self.documents_root = self._resolve_root(documents_root, kind="documents")
        self.prompts = _relative_text_files(self.package_root, self.prompts_root)
        self.documents = _relative_text_files(self.package_root, self.documents_root)
        self.retrievable = self._resolve_retrievable(retrievable or [])
        self._environment = _PromptEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._environment.globals.clear()
        self._environment.filters.clear()

    def _resolve_root(self, value: str, *, kind: str) -> Path:
        relative = _safe_relative_path(value, kind=kind)
        path = (self.package_root / relative.as_posix()).resolve()
        try:
            path.relative_to(self.package_root)
        except ValueError as error:
            raise PromptPackageError(f"Prompt package {kind} directory escapes its root: {value}") from error
        return path

    def _resolve_retrievable(self, values: list[str]) -> frozenset[str]:
        selected: set[str] = set()
        for value in values:
            relative = _safe_relative_path(value.rstrip("/"), kind="retrievable document").as_posix()
            if value.endswith("/"):
                prefix = relative + "/"
                matches = {path for path in self.documents if path.startswith(prefix)}
            else:
                matches = {relative} if relative in self.documents else set()
            if not matches:
                raise PromptPackageError(f"Retrievable document selector matches no documents: {value}")
            selected.update(matches)
        return frozenset(selected)

    def _prompt_path(self, value: str) -> str:
        relative = _safe_relative_path(value, kind="prompt").as_posix()
        root_relative = self.prompts_root.relative_to(self.package_root).as_posix()
        if relative.startswith(root_relative + "/"):
            candidate = relative
        else:
            candidate = f"{root_relative}/{relative}"
        if PurePosixPath(candidate).suffix:
            if candidate not in self.prompts:
                raise PromptPackageError(f"Unknown prompt: {value}")
            return candidate
        markdown = candidate + ".md"
        text = candidate + ".txt"
        matches = [path for path in (markdown, text) if path in self.prompts]
        if len(matches) != 1:
            raise PromptPackageError(f"Unknown or ambiguous prompt: {value}")
        return matches[0]

    def _document_path(self, value: str) -> str:
        relative = _safe_relative_path(value, kind="document").as_posix()
        root_relative = self.documents_root.relative_to(self.package_root).as_posix()
        candidate = relative if relative.startswith(root_relative + "/") else f"{root_relative}/{relative}"
        if candidate not in self.documents:
            raise PromptPackageError(f"Unknown document: {value}")
        return candidate

    def _expand_prompt(
        self,
        path: str,
        *,
        stack: tuple[str, ...],
        used_prompts: list[str],
    ) -> str:
        if path in stack:
            cycle = " -> ".join((*stack, path))
            raise PromptPackageError(f"Prompt partial cycle: {cycle}")
        if len(stack) >= MAX_PROMPT_DEPTH:
            raise PromptPackageError(f"Prompt partial depth exceeds {MAX_PROMPT_DEPTH}: {path}")
        if path not in used_prompts:
            used_prompts.append(path)
        source = self.prompts[path]

        def replace(match: re.Match[str]) -> str:
            included = self._prompt_path(match.group(1))
            return self._expand_prompt(included, stack=(*stack, path), used_prompts=used_prompts)

        return _PROMPT_DIRECTIVE.sub(replace, source)

    def _source_graph(self, entrypoint: str) -> tuple[str, list[str], list[str]]:
        used_prompts: list[str] = []
        source = self._expand_prompt(self._prompt_path(entrypoint), stack=(), used_prompts=used_prompts)
        documents: list[str] = []
        for match in _DOCUMENT_DIRECTIVE.finditer(source):
            path = self._document_path(match.group(1))
            if path not in documents:
                documents.append(path)
        return source, used_prompts, documents

    def warnings(self, entrypoints: list[str]) -> list[PromptWarning]:
        reachable_prompts: set[str] = set()
        referenced_documents: set[str] = set()
        for entrypoint in entrypoints:
            _, prompts, documents = self._source_graph(entrypoint)
            reachable_prompts.update(prompts)
            referenced_documents.update(documents)
        warnings = [
            PromptWarning(
                code="document-unreachable",
                path=path,
                message=(
                    f"{path} is neither referenced by a prompt nor exposed for retrieval, so no model can encounter it."
                ),
            )
            for path in sorted(self.documents.keys() - referenced_documents - set(self.retrievable))
        ]
        warnings.extend(
            PromptWarning(
                code="prompt-unreachable",
                path=path,
                message=f"{path} is not reachable from any configured prompt entrypoint.",
            )
            for path in sorted(self.prompts.keys() - reachable_prompts)
        )
        return warnings

    def source_paths(self, entrypoint: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return the prompt and document sources statically reachable from one entrypoint."""

        _, prompts, documents = self._source_graph(entrypoint)
        return tuple(prompts), tuple(documents)

    def render(self, entrypoint: str, *, runvar: dict[str, Any]) -> RenderedPrompt:
        try:
            normalized_runvar = json.loads(json.dumps(runvar, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise PromptPackageError("Prompt run variables must be finite JSON-compatible values") from error
        source, used_prompts, referenced_documents = self._source_graph(entrypoint)
        document_tokens: dict[str, tuple[str, str]] = {}

        def protect_document(match: re.Match[str]) -> str:
            path = self._document_path(match.group(1))
            index = len(document_tokens)
            digest = hashlib.sha256(path.encode()).hexdigest()[:16]
            token = f"\ue000AIBB_DOCUMENT_{index}_{digest}\ue001"
            document_tokens[token] = (path, self.documents[path])
            return token

        template_source = _DOCUMENT_DIRECTIVE.sub(protect_document, source)
        template_source = _RUNVAR_DIRECTIVE.sub(lambda match: "{{ runvar." + match.group(1) + " }}", template_source)
        if _MALFORMED_DIRECTIVE.search(template_source):
            raise PromptPackageError("Malformed prompt, document, or runvar directive")
        try:
            rendered = self._environment.from_string(template_source).render(runvar=normalized_runvar)
        except TemplateError as error:
            raise PromptPackageError(f"Prompt rendering failed: {error}") from error
        included_documents: list[str] = []
        for token, (path, body) in document_tokens.items():
            if token in rendered:
                included_documents.append(path)
                rendered = rendered.replace(token, body)
        if len(rendered.encode("utf-8")) > MAX_RENDERED_BYTES:
            raise PromptPackageError(f"Rendered prompt exceeds {MAX_RENDERED_BYTES} bytes")
        return RenderedPrompt(
            entrypoint=self._prompt_path(entrypoint),
            text=rendered,
            prompt_paths=tuple(used_prompts),
            document_paths=tuple(dict.fromkeys(included_documents)),
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            rendered_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
        )
