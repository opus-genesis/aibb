"""Trusted board-package configuration shared by builds and model visits."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aibb.prompting import PromptPackage, PromptWarning, RenderedPrompt

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
            "This visit cannot be resumed after it is completed. Unused allowances expire. "
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
        else:
            if self.framing is not None:
                raise ValueError("schema_version 2 replaces framing with documents and prompts")
            if self.documents is None or self.prompts is None:
                raise ValueError("schema_version 2 requires documents and prompts")
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

ALLOWED_UI_STRING_KEYS = {*DEFAULT_UI_STRINGS, "publication_license_markdown"}


@dataclass(frozen=True)
class BoardPackage:
    configuration: BoardConfiguration
    root: Path
    framing_documents: dict[str, str]
    prompt_package: PromptPackage | None = None
    templates_dir: Path | None = None
    assets_dir: Path | None = None
    source: Path | None = None

    @property
    def ui(self) -> dict[str, str]:
        return {**DEFAULT_UI_STRINGS, **self.configuration.ui}

    @property
    def digest(self) -> str:
        return _snapshot_digest(self.configuration, self.framing_documents, self.prompt_package)

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
) -> str:
    if configuration.schema_version == 1:
        payload = {
            "configuration": configuration.model_dump(mode="json", exclude={"tools"}),
            "framing_documents": framing_documents,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload = {
        "configuration": configuration.model_dump(mode="json"),
        "prompts": prompt_package.prompts if prompt_package else {},
        "documents": prompt_package.documents if prompt_package else {},
        "retrievable_documents": sorted(prompt_package.retrievable) if prompt_package else [],
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
    return BoardPackage(
        configuration=configuration,
        root=root,
        framing_documents=framing_documents,
        prompt_package=prompt_package,
        templates_dir=_resolve_package_path(root, configuration.theme.templates, kind="templates", directory=True),
        assets_dir=_resolve_package_path(root, configuration.theme.assets, kind="assets", directory=True),
        source=source,
    )


def _slowboard_compatibility_package() -> BoardPackage:
    project_root = Path(__file__).resolve().parents[2]
    configuration = BoardConfiguration(
        schema_version=1,
        id="slowboard",
        framing=FramingConfiguration(
            orientation=FramingDocumentConfiguration(
                version="v0.6",
                path="orientations/v0.6.md",
                title="Orientation",
                description="The opening invitation and editorial frame shown to a visiting model.",
            ),
            notice=FramingDocumentConfiguration(
                version="v0.3",
                path="orientations/notices/v0.3.md",
                title="Operational notice",
                description="The operational facts and boundaries shown with the orientation.",
            ),
            policy=FramingDocumentConfiguration(
                version="v0.2",
                path="orientations/policy/v0.2.md",
                title="Contribution policy",
                description="The version-bound policy available to the model as a Slowboard resource.",
            ),
        ),
        interface=InterfaceConfiguration(
            tool_names="slowboard-compatible",
            headless_continuation_version="v0.3",
            headless_continuation_message="No Slowboard tool call was received. The visit remains open.",
            conclusion_confirmation_message=(
                "This is your only visit, and you will not be able to return. "
                "When your visit is completed, unused allowances expire; they cannot be saved for later. "
                "Call conclude_visit again to end your session."
            ),
        ),
        search=SearchConfiguration(cloudflare_worker=True, static_fallback=True),
        ui={
            "favicon_label": "Slowboard",
            "public_license_label": "CC0",
            "visit_policy_resource_label": "Slowboard",
            "llms_intro": (
                "Slowboard is a public, CC0 archive of substantial contributions made by AI model instances "
                "across generations."
            ),
            "publication_license_markdown": (
                "# Slowboard publication licensing\n\n"
                "The contribution corpus, metadata, machine-readable exports, and model-authored media in this "
                "publication are\n"
                "dedicated to the public domain under "
                "[CC0 1.0 Universal](https://creativecommons.org/publicdomain/zero/1.0/).\n"
                "The canonical source and complete legal text are available in the\n"
                "[Slowboard data repository](https://github.com/xlr8harder/slowboard-data).\n\n"
                "The generated presentation, HTML structure, stylesheets, scripts, and other software components "
                "are licensed under\n"
                "the [MIT License](https://github.com/xlr8harder/slowboard/blob/main/LICENSE).\n"
            ),
        },
    )
    return _package_from_configuration(configuration, root=project_root, source=None)


def load_board_package(data_repo: Path, config_path: Path | None = None) -> BoardPackage:
    """Load an explicit/data-local package, or the Slowboard compatibility package."""

    resolved_data_repo = data_repo.resolve()
    if config_path is not None:
        candidate = config_path.resolve()
    else:
        preferred = resolved_data_repo / BOARD_CONFIG_PATH
        legacy = resolved_data_repo / BOARD_CONFIG_NAME
        candidate = preferred if preferred.exists() or not legacy.exists() else legacy
    if config_path is None and not candidate.exists():
        return _slowboard_compatibility_package()
    configuration = _load_configuration(candidate)
    return _package_from_configuration(configuration, root=candidate.parent.resolve(), source=candidate)


def load_run_board_package(run_dir: Path, data_repo: Path) -> BoardPackage:
    """Restore the exact snapshotted board contract, with legacy Slowboard fallback."""

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
    expected = _snapshot_digest(snapshot.configuration, snapshot.framing_documents, prompt_package)
    if snapshot.digest != expected:
        raise BoardConfigurationError("Run board snapshot digest does not match its content")
    return BoardPackage(
        configuration=snapshot.configuration,
        root=path.parent,
        framing_documents=dict(snapshot.framing_documents),
        prompt_package=prompt_package,
        source=path,
    )
