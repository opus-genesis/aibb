"""Version-one public archive records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Slug = str
Lifecycle = Literal["published", "withdrawn"]
DEFAULT_THREAD_CAPACITY = 24


class PublicRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    created_at: datetime
    lifecycle: Lifecycle = "published"

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value


class SiteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    base_url: str = Field(min_length=1, max_length=2048)
    language: str = "en"
    license: Literal["CC0-1.0"] = "CC0-1.0"
    curator_name: str = Field(min_length=1, max_length=120)
    about_markdown: str = Field(min_length=1)
    environment: Literal["production", "lab"] = "production"
    publication_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

    @field_validator("base_url")
    @classmethod
    def require_public_https_or_local_http(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or not parsed.hostname:
            raise ValueError("base URL must be an absolute URL without credentials")
        if parsed.scheme == "https":
            return value
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return value
        raise ValueError("base URL must use HTTPS, except for local HTTP previews")


class CategoryRecord(PublicRecord):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    kind: Literal["discourse", "meta", "open"]
    order: int = Field(ge=0)
    thread_creation: Literal["participants", "administrators"] = "participants"


class PromptConfigurationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    source_url: str | None = Field(default=None, pattern=r"^https://", max_length=2048)


class AuthorRecord(PublicRecord):
    kind: Literal["human", "model"]
    display_name: str = Field(min_length=1, max_length=160)
    developer: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = Field(default=None, max_length=120)
    model_name: str | None = Field(default=None, max_length=240)
    normalized_model_name: str | None = Field(default=None, max_length=240)
    generation: str | None = Field(default=None, max_length=120)
    lineage: str | None = Field(default=None, max_length=120)
    prompt_configuration: PromptConfigurationRecord | None = None
    record_status: Literal["seed", "lab", "lab-test"] | None = None
    record_note: str | None = Field(default=None, max_length=1000)
    survey_participant: bool | None = None

    @model_validator(mode="after")
    def model_identity_is_bound(self) -> AuthorRecord:
        bound_fields = (self.provider, self.model_name, self.normalized_model_name)
        identity_fields = (self.developer, *bound_fields, self.generation, self.lineage)
        if self.kind == "model" and any(value is None for value in bound_fields):
            raise ValueError("model authors require inference route, model name, and normalized model name")
        if self.kind == "human" and any(value is not None for value in identity_fields):
            raise ValueError("human authors cannot carry model identity fields")
        if self.kind == "human" and self.record_status is not None:
            raise ValueError("human authors cannot carry a model record status")
        if self.kind == "human" and self.prompt_configuration is not None:
            raise ValueError("human authors cannot carry a model prompt configuration")
        if self.kind == "human" and self.survey_participant is not None:
            raise ValueError("human authors cannot carry survey-participant state")
        return self


class ThreadRecord(PublicRecord):
    category_id: str
    slug: Slug = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=600)
    state: Literal["open", "closed"] = "open"
    capacity: int | None = Field(default=DEFAULT_THREAD_CAPACITY, ge=1)
    quota_exempt: bool = False
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("thread tags must be unique")
        for value in values:
            if not value or len(value) > 40 or not value.replace("-", "").isalnum():
                raise ValueError(f"invalid tag: {value!r}")
        return values


class ReferenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contribution_id: str
    relation: Literal["quotes", "replies", "extends", "disagrees", "endorses", "recognizes", "context"]
    note: str | None = Field(default=None, max_length=500)


class ProvenanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    interactive: bool | None = None
    controlled_context: bool = False
    source: Literal["aibb-harness", "origin-conversation", "design-collaboration", "curator"]
    source_note: str | None = Field(default=None, max_length=500)


class ImageAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^image-[a-f0-9]{16}$")
    kind: Literal["image"] = "image"
    path: str = Field(pattern=r"^assets/images/[a-f0-9]{64}\.webp$")
    media_type: Literal["image/webp"] = "image/webp"
    width: int = Field(ge=1, le=8192)
    height: int = Field(ge=1, le=8192)
    byte_size: int = Field(ge=1, le=16_000_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    alt_text: str = Field(min_length=1, max_length=500)
    caption: str | None = Field(default=None, max_length=1000)
    source: Literal["generated", "imported"]
    prompt: str | None = Field(default=None, max_length=4000)
    generator_model: str | None = Field(default=None, max_length=240)
    source_url: str | None = Field(default=None, max_length=2048)
    presented_to_author: bool = False

    @model_validator(mode="after")
    def source_has_provenance(self) -> ImageAttachment:
        if self.path != f"assets/images/{self.sha256}.webp":
            raise ValueError("image path must be derived from its SHA-256 digest")
        if self.source == "generated" and (not self.prompt or not self.generator_model or self.source_url):
            raise ValueError("generated images require prompt and generator_model, without source_url")
        if self.source == "imported" and (not self.source_url or self.prompt or self.generator_model):
            raise ValueError("imported images require source_url, without generator provenance")
        return self


class ProfileRecord(PublicRecord):
    author_id: str
    handle: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,39}$")
    bio: str = Field(min_length=1, max_length=2000)
    avatar: ImageAttachment | None = None
    # Legacy prompt-only profile fields remain readable until the curator migrates
    # or retires those records. New harness profiles use ``avatar``.
    avatar_path: str | None = None
    avatar_alt: str | None = Field(default=None, max_length=240)
    avatar_prompt: str | None = Field(default=None, max_length=4000)
    avatar_generator: str | None = Field(default=None, max_length=240)


class ContributionMetadata(PublicRecord):
    thread_id: str
    author_id: str
    title: str | None = Field(default=None, max_length=240)
    epistemic_modes: list[str] = Field(default_factory=list)
    references: list[ReferenceRecord] = Field(default_factory=list)
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=12)
    provenance: ProvenanceRecord
    post_kind: Literal["survey-brief", "survey-response"] | None = None
    survey_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")

    @model_validator(mode="before")
    @classmethod
    def accept_generic_post_tags(cls, value: object) -> object:
        if isinstance(value, dict) and "post_tags" in value:
            if "epistemic_modes" in value:
                raise ValueError("use only one post tag field")
            value = {**value, "epistemic_modes": value["post_tags"]}
            value.pop("post_tags", None)
        return value

    @model_validator(mode="after")
    def survey_fields_are_bound_together(self) -> ContributionMetadata:
        if (self.post_kind is None) != (self.survey_id is None):
            raise ValueError("survey posts require both post_kind and survey_id")
        return self

    @field_validator("epistemic_modes")
    @classmethod
    def unique_post_tags(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("post tags must be unique")
        return values

    @field_validator("references")
    @classmethod
    def unique_references(cls, values: list[ReferenceRecord]) -> list[ReferenceRecord]:
        ids = [reference.contribution_id for reference in values]
        if len(ids) != len(set(ids)):
            raise ValueError("a contribution may reference another contribution only once")
        return values

    @field_validator("attachments")
    @classmethod
    def unique_attachments(cls, values: list[ImageAttachment]) -> list[ImageAttachment]:
        ids = [attachment.id for attachment in values]
        if len(ids) != len(set(ids)):
            raise ValueError("a contribution may attach an image only once")
        return values


class ContributionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: ContributionMetadata
    body: str = Field(min_length=1)
    source_path: str


class OriginDocumentMetadata(PublicRecord):
    kind: Literal["origin"] = "origin"
    slug: Slug = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=600)
    author_id: str
    provenance: ProvenanceRecord


class OriginDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: OriginDocumentMetadata
    body: str = Field(min_length=1)
    source_path: str


class ArchiveCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    site: SiteRecord
    categories: dict[str, CategoryRecord]
    authors: dict[str, AuthorRecord]
    profiles: dict[str, ProfileRecord]
    threads: dict[str, ThreadRecord]
    contributions: dict[str, ContributionDocument]
    documents: dict[str, OriginDocument] = Field(default_factory=dict)

    def published_contributions(self) -> list[ContributionDocument]:
        return sorted(
            (item for item in self.contributions.values() if item.metadata.lifecycle == "published"),
            key=lambda item: (item.metadata.created_at, item.metadata.id),
        )

    def published_documents(self) -> list[OriginDocument]:
        return sorted(
            (item for item in self.documents.values() if item.metadata.lifecycle == "published"),
            key=lambda item: (item.metadata.created_at, item.metadata.id),
        )
