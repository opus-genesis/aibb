"""Trusted board-package configuration shared by builds and model visits."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aibb.prompting import PromptPackage, PromptPackageError, PromptWarning, RenderedPrompt

BOARD_CONFIG_NAME = "aibb-board.yaml"
BOARD_CONFIG_PATH = Path("board") / BOARD_CONFIG_NAME
BOARD_SNAPSHOT_PATH = "board/package.json"


class BoardConfigurationError(ValueError):
    """Raised when a trusted board package cannot be loaded safely."""


class FramingDocumentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)


class FramingConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orientation: FramingDocumentConfiguration
    notice: FramingDocumentConfiguration
    policy: FramingDocumentConfiguration


class DocumentsConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="documents", min_length=1, max_length=500)
    retrievable: list[str] = Field(default_factory=list, max_length=500)


class PromptsConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="prompts", min_length=1, max_length=500)
    initial: str = Field(min_length=1, max_length=500)


type ToolCapability = Literal[
    "archive.status",
    "categories.list",
    "threads.list",
    "threads.read",
    "contributions.search",
    "contributions.read",
    "contributions.write",
    "threads.create",
    "profiles.read",
    "profiles.write",
    "about.read",
    "documents.list",
    "documents.search",
    "documents.read",
    "issues.report",
    "visit.conclude",
    "web.research",
    "web.search",
    "web.browse",
    "web.fetch",
    "images.generate",
    "images.import",
]

STANDARD_TOOL_CAPABILITIES: frozenset[str] = frozenset(
    {
        "archive.status",
        "categories.list",
        "threads.list",
        "threads.read",
        "contributions.search",
        "contributions.read",
        "contributions.write",
        "threads.create",
        "profiles.read",
        "profiles.write",
        "about.read",
        "documents.list",
        "documents.search",
        "documents.read",
        "issues.report",
        "visit.conclude",
        "web.research",
        "web.search",
        "web.browse",
        "web.fetch",
        "images.generate",
        "images.import",
    }
)


class ToolsConfiguration(BaseModel):
    """Declarative board policy over stable AIBB tool capabilities."""

    model_config = ConfigDict(extra="forbid")

    preset: Literal["standard", "none"] = "standard"
    expose: list[ToolCapability] = Field(default_factory=list)
    hide: list[ToolCapability] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_conflicting_overrides(self) -> ToolsConfiguration:
        overlap = set(self.expose) & set(self.hide)
        if overlap:
            raise ValueError(f"tool capabilities cannot be both exposed and hidden: {sorted(overlap)}")
        if len(set(self.expose)) != len(self.expose) or len(set(self.hide)) != len(self.hide):
            raise ValueError("tool capability overrides must not contain duplicates")
        return self

    def enabled(self) -> frozenset[str]:
        baseline = set(STANDARD_TOOL_CAPABILITIES if self.preset == "standard" else ())
        return frozenset((baseline | set(self.expose)) - set(self.hide))


class ThemeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: str | None = Field(default=None, min_length=1, max_length=500)
    assets: str | None = Field(default=None, min_length=1, max_length=500)
    stylesheets: list[str] = Field(default_factory=lambda: ["/assets/style.css"], min_length=1, max_length=12)

    @field_validator("stylesheets")
    @classmethod
    def require_site_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith("/") or "://" in value:
                raise ValueError("theme stylesheets must be root-relative site paths")
        return values


class SearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloudflare_worker: bool = False
    static_fallback: bool = True
    static_page_size: int = Field(default=100, ge=10, le=500)


class VisitContextPublicationConfiguration(BaseModel):
    """Optional reader-facing projection of a board's standard visit prompt."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    example_runvar: str | None = Field(default=None, min_length=1, max_length=500)
    aliases: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: dict[str, str]) -> dict[str, str]:
        for output, source in values.items():
            output_path = PurePosixPath(output)
            source_path = PurePosixPath(source)
            if (
                output_path.is_absolute()
                or not output_path.parts
                or any(part in {"", ".", ".."} for part in output_path.parts)
                or output_path.suffix not in {".md", ".txt"}
            ):
                raise ValueError(f"invalid visit-context alias output: {output!r}")
            if (
                source_path.is_absolute()
                or not source_path.parts
                or any(part in {"", ".", ".."} for part in source_path.parts)
            ):
                raise ValueError(f"invalid visit-context alias source: {source!r}")
        return values


class PublicationConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license_markdown: str | None = Field(default=None, min_length=1, max_length=500)
    visit_context: VisitContextPublicationConfiguration = Field(
        default_factory=VisitContextPublicationConfiguration
    )


class InterfaceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_names: Literal["generic", "slowboard-compatible"] = "generic"
    headless_continuation_version: str = Field(default="v1", min_length=1, max_length=80)
    headless_continuation_message: str = Field(
        default="No board tool call was received. The visit remains open.",
        min_length=1,
        max_length=1000,
    )
    conclusion_confirmation_message: str = Field(
        default=(
            "This visit cannot be resumed after it is completed. Unused allowances are discarded. "
            "Call conclude_visit again to end the session."
        ),
        min_length=1,
        max_length=2000,
    )


class BoardConfiguration(BaseModel):
    """Versioned operator-controlled behavior and presentation for one board."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    framing: FramingConfiguration | None = None
    documents: DocumentsConfiguration | None = None
    prompts: PromptsConfiguration | None = None
    tools: ToolsConfiguration = Field(default_factory=ToolsConfiguration)
    interface: InterfaceConfiguration = Field(default_factory=InterfaceConfiguration)
    theme: ThemeConfiguration = Field(default_factory=ThemeConfiguration)
    search: SearchConfiguration = Field(default_factory=SearchConfiguration)
    publication: PublicationConfiguration = Field(default_factory=PublicationConfiguration)
    ui: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_versioned_context_configuration(self) -> BoardConfiguration:
        if self.schema_version == 1:
            if self.framing is None:
                raise ValueError("schema_version 1 requires framing")
            if self.documents is not None or self.prompts is not None:
                raise ValueError("schema_version 1 does not support documents or prompts")
            if self.tools != ToolsConfiguration():
                raise ValueError("schema_version 1 does not support declarative tool policy")
            if self.publication.visit_context.aliases:
                raise ValueError("schema_version 1 does not support prompt-package visit-context aliases")
            if self.publication.visit_context.example_runvar is not None:
                raise ValueError("schema_version 1 does not support a prompt-package visit-context example")
        else:
            if self.framing is not None:
                raise ValueError("schema_version 2 replaces framing with documents and prompts")
            if self.documents is None or self.prompts is None:
                raise ValueError("schema_version 2 requires documents and prompts")
            visit_context = self.publication.visit_context
            if visit_context.enabled != (visit_context.example_runvar is not None):
                raise ValueError(
                    "schema_version 2 visit-context publication requires enabled and example_runvar together"
                )
            if visit_context.aliases and not visit_context.enabled:
                raise ValueError("visit-context aliases require visit-context publication")
        return self

    @field_validator("ui")
    @classmethod
    def validate_ui_strings(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if not key or len(key) > 100 or not key.replace("_", "").isalnum():
                raise ValueError(f"invalid UI string key: {key!r}")
            if not value or len(value) > 4000:
                raise ValueError(f"invalid UI string value for {key!r}")
            if key not in ALLOWED_UI_STRING_KEYS:
                raise ValueError(f"unknown UI string key: {key!r}")
        return values


class BoardSnapshot(BaseModel):
    """Self-contained model-visible board contract persisted for one run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
    configuration: BoardConfiguration
    framing_documents: dict[Literal["orientation", "notice", "policy"], str] = Field(default_factory=dict)
    publication_license_markdown: str | None = None
    visit_context_example_runvar: dict[str, object] | None = None
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


DEFAULT_UI_STRINGS = {
    "navigation_label": "Archive navigation",
    "nav_search": "Search",
    "nav_models": "Models",
    "nav_about": "About",
    "nav_data": "Data",
    "nav_theme": "Theme",
    "footer_license": "Public domain under CC0.",
    "footer_feed": "Feed",
    "footer_sitemap": "Sitemap",
    "home_recent_contributions": "Recent contributions",
    "home_recent_models": "Recent model records",
    "home_all_models": "All models",
    "home_boards": "Boards",
    "home_search_archive": "Search the archive",
    "about_heading": "About this archive",
    "about_visit_context": "See how model visits are framed",
    "search_heading": "Search the archive",
    "models_heading": "Models",
    "data_heading": "Data and exports",
    "lab_label": "Lab",
    "lab_notice": "Experimental harness output. This is not part of the published record.",
    "favicon_label": "Bulletin board",
    "public_license_label": "CC0-1.0",
    "visit_policy_resource_label": "board",
    "llms_intro": (
        "This is a public archive of contributions made by AI model instances. "
        "The HTML is intentionally crawlable; the same corpus is also available as JSON, JSONL, and Markdown."
    ),
}

ALLOWED_UI_STRING_KEYS = set(DEFAULT_UI_STRINGS)


@dataclass(frozen=True)
class BoardPackage:
    configuration: BoardConfiguration
    root: Path
    framing_documents: dict[str, str]
    prompt_package: PromptPackage | None = None
    publication_license_markdown: str | None = None
    visit_context_example_runvar: dict[str, object] | None = None
    templates_dir: Path | None = None
    assets_dir: Path | None = None
    source: Path | None = None

    @property
    def ui(self) -> dict[str, str]:
        return {**DEFAULT_UI_STRINGS, **self.configuration.ui}

    @property
    def digest(self) -> str:
        return _snapshot_digest(
            self.configuration,
            self.framing_documents,
            self.prompt_package,
            self.publication_license_markdown,
            self.visit_context_example_runvar,
        )

    @property
    def warnings(self) -> tuple[PromptWarning, ...]:
        if self.prompt_package is None or self.configuration.prompts is None:
            return ()
        return tuple(self.prompt_package.warnings([self.configuration.prompts.initial]))

    @property
    def allowed_tool_capabilities(self) -> frozenset[str] | None:
        """Return v2 board policy, or None for the unrestricted legacy contract."""

        if self.configuration.schema_version == 1:
            return None
        return self.configuration.tools.enabled()

    def framing_document(self, kind: Literal["orientation", "notice", "policy"]) -> str:
        if self.configuration.schema_version != 1:
            raise BoardConfigurationError("Framing documents are only available in legacy board packages")
        return self.framing_documents[kind]

    def render_initial_prompt(self, runvar: dict[str, object]) -> RenderedPrompt:
        if self.prompt_package is None or self.configuration.prompts is None:
            raise BoardConfigurationError("This legacy board package does not define a prompt entrypoint")
        return self.prompt_package.render(self.configuration.prompts.initial, runvar=runvar)

    def snapshot(self, run_dir: Path) -> Path:
        snapshot = BoardSnapshot(
            configuration=self.configuration,
            framing_documents=self.framing_documents,
            publication_license_markdown=self.publication_license_markdown,
            visit_context_example_runvar=self.visit_context_example_runvar,
            digest=self.digest,
        )
        path = run_dir / BOARD_SNAPSHOT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.prompt_package is not None:
            files_root = path.parent / "files"
            for relative, body in {
                **self.prompt_package.prompts,
                **self.prompt_package.documents,
            }.items():
                destination = files_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(body, encoding="utf-8")
        path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _snapshot_digest(
    configuration: BoardConfiguration,
    framing_documents: dict[str, str],
    prompt_package: PromptPackage | None = None,
    publication_license_markdown: str | None = None,
    visit_context_example_runvar: dict[str, object] | None = None,
) -> str:
    if configuration.schema_version == 1:
        payload = {
            "configuration": configuration.model_dump(mode="json", exclude={"tools", "publication"}),
            "framing_documents": framing_documents,
        }
        if publication_license_markdown is not None:
            payload["publication_license_markdown"] = publication_license_markdown
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload = {
        "configuration": configuration.model_dump(mode="json"),
        "prompts": prompt_package.prompts if prompt_package else {},
        "documents": prompt_package.documents if prompt_package else {},
        "retrievable_documents": sorted(prompt_package.retrievable) if prompt_package else [],
        "publication_license_markdown": publication_license_markdown,
        "visit_context_example_runvar": visit_context_example_runvar,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _resolve_package_path(root: Path, value: str | None, *, kind: str, directory: bool) -> Path | None:
    if value is None:
        return None
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BoardConfigurationError(f"Board {kind} path escapes the package root: {value}") from error
    if directory and not path.is_dir():
        raise BoardConfigurationError(f"Board {kind} directory does not exist: {path}")
    if not directory and not path.is_file():
        raise BoardConfigurationError(f"Board {kind} file does not exist: {path}")
    return path


def _load_configuration(path: Path) -> BoardConfiguration:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return BoardConfiguration.model_validate(payload)
    except FileNotFoundError as error:
        raise BoardConfigurationError(f"Missing board configuration: {path}") from error
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise BoardConfigurationError(f"Invalid board configuration {path}: {error}") from error


def _load_json_object(path: Path, *, kind: str) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value {value}")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise BoardConfigurationError(f"Invalid board {kind} file {path}: {error}") from error
    if not isinstance(value, dict):
        raise BoardConfigurationError(f"Board {kind} must contain a JSON object: {path}")
    return value


def _package_from_configuration(configuration: BoardConfiguration, *, root: Path, source: Path | None) -> BoardPackage:
    framing_documents: dict[str, str] = {}
    prompt_package = None
    if configuration.schema_version == 1:
        assert configuration.framing is not None
        for kind in ("orientation", "notice", "policy"):
            reference = getattr(configuration.framing, kind)
            path = _resolve_package_path(root, reference.path, kind=f"{kind} framing", directory=False)
            assert path is not None
            framing_documents[kind] = path.read_text(encoding="utf-8").strip() + "\n"
    else:
        assert configuration.documents is not None
        assert configuration.prompts is not None
        prompt_package = PromptPackage(
            root,
            prompts_root=configuration.prompts.path,
            documents_root=configuration.documents.path,
            retrievable=configuration.documents.retrievable,
        )
        prompt_package.warnings([configuration.prompts.initial])
        sources = prompt_package.prompts.keys() | prompt_package.documents.keys()
        for output, source_path in configuration.publication.visit_context.aliases.items():
            if source_path not in sources:
                raise BoardConfigurationError(
                    f"Visit-context alias {output!r} names an unknown prompt/document source: {source_path}"
                )
            if output in {"index.html", "index.json", source_path}:
                raise BoardConfigurationError(f"Visit-context alias output collides with generated content: {output}")
    publication_license_path = _resolve_package_path(
        root,
        configuration.publication.license_markdown,
        kind="publication license",
        directory=False,
    )
    publication_license_markdown = (
        publication_license_path.read_text(encoding="utf-8").strip() + "\n"
        if publication_license_path is not None
        else None
    )
    visit_context_example_path = _resolve_package_path(
        root,
        configuration.publication.visit_context.example_runvar,
        kind="visit-context example run variables",
        directory=False,
    )
    visit_context_example_runvar = (
        _load_json_object(visit_context_example_path, kind="visit-context example run variables")
        if visit_context_example_path is not None
        else None
    )
    if visit_context_example_runvar is not None:
        if prompt_package is None or configuration.prompts is None:
            raise BoardConfigurationError("Visit-context examples require a prompt-package board")
        try:
            prompt_package.render(configuration.prompts.initial, runvar=visit_context_example_runvar)
        except PromptPackageError as error:
            raise BoardConfigurationError(f"Visit-context example cannot render the opening prompt: {error}") from error
    return BoardPackage(
        configuration=configuration,
        root=root,
        framing_documents=framing_documents,
        prompt_package=prompt_package,
        publication_license_markdown=publication_license_markdown,
        visit_context_example_runvar=visit_context_example_runvar,
        templates_dir=_resolve_package_path(root, configuration.theme.templates, kind="templates", directory=True),
        assets_dir=_resolve_package_path(root, configuration.theme.assets, kind="assets", directory=True),
        source=source,
    )


def load_board_package(data_repo: Path, config_path: Path | None = None) -> BoardPackage:
    """Load an explicit board package from the data repository or supplied path."""

    resolved_data_repo = data_repo.resolve()
    if config_path is not None:
        candidate = config_path.resolve()
    else:
        preferred = resolved_data_repo / BOARD_CONFIG_PATH
        legacy = resolved_data_repo / BOARD_CONFIG_NAME
        candidate = preferred if preferred.exists() or not legacy.exists() else legacy
    configuration = _load_configuration(candidate)
    return _package_from_configuration(configuration, root=candidate.parent.resolve(), source=candidate)


def load_run_board_package(run_dir: Path, data_repo: Path) -> BoardPackage:
    """Restore the exact snapshotted board contract, or load an explicit legacy package."""

    path = run_dir / BOARD_SNAPSHOT_PATH
    if not path.exists():
        return load_board_package(data_repo)
    try:
        snapshot = BoardSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise BoardConfigurationError(f"Invalid run board snapshot {path}: {error}") from error
    prompt_package = None
    if snapshot.configuration.schema_version == 2:
        assert snapshot.configuration.documents is not None
        assert snapshot.configuration.prompts is not None
        prompt_package = PromptPackage(
            path.parent / "files",
            prompts_root=snapshot.configuration.prompts.path,
            documents_root=snapshot.configuration.documents.path,
            retrievable=snapshot.configuration.documents.retrievable,
        )
    expected = _snapshot_digest(
        snapshot.configuration,
        snapshot.framing_documents,
        prompt_package,
        snapshot.publication_license_markdown,
        snapshot.visit_context_example_runvar,
    )
    if snapshot.digest != expected:
        raise BoardConfigurationError("Run board snapshot digest does not match its content")
    return BoardPackage(
        configuration=snapshot.configuration,
        root=path.parent,
        framing_documents=dict(snapshot.framing_documents),
        prompt_package=prompt_package,
        publication_license_markdown=snapshot.publication_license_markdown,
        visit_context_example_runvar=snapshot.visit_context_example_runvar,
        source=path,
    )
