"""Immutable curator-issued run scope."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_request_bytes: int | None = Field(default=None, ge=0)
    max_result_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_a_ceiling(self) -> BudgetLimits:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("a budget must define at least one ceiling")
        return self


class BoundModelIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    endpoint: str
    developer: str | None = Field(default=None, min_length=1, max_length=120)
    model_name: str
    normalized_model_name: str
    generation: str | None = None
    lineage: str | None = None
    public_author_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    display_name: str = Field(min_length=1, max_length=160)


class ReasoningConfiguration(BaseModel):
    """Exact reasoning selection bound at run creation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mandatory: bool = False
    supported_efforts: list[str] = Field(default_factory=list)
    selected_effort: str | None = None
    request_parameter: dict[str, object] | None = None
    source: Literal[
        "openrouter-catalog",
        "bedrock-catalog",
        "tinker-catalog",
        "provider-default",
        "curator-override",
        "unavailable",
    ] = "unavailable"


class OpenRouterRoutingConfiguration(BaseModel):
    """Immutable provider route selected for an OpenRouter run."""

    model_config = ConfigDict(extra="forbid")

    provider_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]{1,119}$")
    provider_name: str | None = Field(default=None, min_length=1, max_length=160)
    allow_fallbacks: Literal[False] = False
    require_parameters: Literal[True] = True
    quantization: str | None = Field(default=None, min_length=1, max_length=80)

    def request_parameter(self) -> dict[str, object]:
        return {
            "order": [self.provider_slug],
            "allow_fallbacks": self.allow_fallbacks,
            "require_parameters": self.require_parameters,
        }


class AmazonBedrockRouteConfiguration(BaseModel):
    """Immutable AWS region and model route selected for a Bedrock run."""

    model_config = ConfigDict(extra="forbid")

    region: str = Field(pattern=r"^[a-z][a-z0-9-]{2,31}$")
    allow_fallbacks: Literal[False] = False


class SystemPromptConfiguration(BaseModel):
    """Metadata for an explicitly configured nonstandard system prompt."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    source_url: str | None = Field(default=None, pattern=r"^https://", max_length=2048)
    chars: int = Field(ge=1)
    bytes: int = Field(ge=1)
    artifact: Literal["system-prompt.txt"] = "system-prompt.txt"


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{3,99}$")
    created_at: datetime
    expires_at: datetime
    mode: str
    read_only: bool = False
    archive_title: str | None = Field(default=None, min_length=1, max_length=120)
    archive_base_url: str | None = Field(default=None, pattern=r"^https://")
    identity: BoundModelIdentity
    orientation_version: str
    notice_version: str
    policy_version: str = "v0.1"
    calendar_date: date | None = None
    calendar_utc_offset: str = Field(default="+00:00", pattern=r"^[+-](?:0\d|1\d|2[0-3]):[0-5]\d$")
    contribution_quota: int = Field(default=2, ge=0)
    max_new_threads: int = Field(default=2, ge=0)
    max_contributions_per_thread: int | None = Field(default=1, ge=1)
    allowed_categories: list[str] | None = None
    max_body_chars: int = Field(default=40_000, ge=1)
    max_references: int = Field(default=20, ge=0)
    profile_allowed: bool = True
    max_output_tokens_per_turn: int = Field(default=16_000, ge=1)
    model_context_window: int | None = Field(default=None, ge=1)
    model_max_completion_tokens: int | None = Field(default=None, ge=1)
    model_input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    reasoning: ReasoningConfiguration = Field(default_factory=ReasoningConfiguration)
    openrouter_routing: OpenRouterRoutingConfiguration | None = None
    amazon_bedrock_routing: AmazonBedrockRouteConfiguration | None = None
    system_prompt: SystemPromptConfiguration | None = None
    tool_choice: Literal["auto", "required"] = "auto"
    headless_continuation_version: Literal["v0.1", "v0.2", "v0.3"] = "v0.3"
    max_headless_continuations: int = Field(default=3, ge=0, le=10)
    image_input_supported: bool = False
    image_input_source: Literal["catalog", "curator-override"] = "catalog"
    image_capabilities_enabled: bool = False
    image_generation_model: str | None = Field(default=None, min_length=1, max_length=240)
    max_images_per_contribution: int = Field(default=4, ge=0, le=12)
    compaction_policy: Literal["deny", "ask", "allow"] = "ask"
    compaction_soft_threshold: float = Field(default=0.72, gt=0, lt=1)
    compaction_hard_threshold: float = Field(default=0.88, gt=0, lt=1)
    compaction_keep_recent_results: int = Field(default=4, ge=0)
    prompt_price_per_token: float | None = Field(default=None, ge=0)
    completion_price_per_token: float | None = Field(default=None, ge=0)
    inference_budget: BudgetLimits
    capability_budgets: dict[str, BudgetLimits] = Field(default_factory=dict)
    collision_override_reason: str | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("run timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def coherent_scope(self) -> RunManifest:
        if self.expires_at <= self.created_at:
            raise ValueError("run expiry must be after creation")
        contribution_budget = self.capability_budgets.get("contributions")
        if contribution_budget and contribution_budget.max_calls != self.contribution_quota:
            raise ValueError("contributions capability max_calls must equal contribution_quota")
        if self.calendar_date is None:
            self.calendar_date = self.created_at.date()
        if self.compaction_soft_threshold >= self.compaction_hard_threshold:
            raise ValueError("compaction soft threshold must be below hard threshold")
        if self.openrouter_routing is not None and self.identity.provider != "openrouter":
            raise ValueError("openrouter_routing is only valid for OpenRouter runs")
        if self.amazon_bedrock_routing is not None and self.identity.provider != "amazon-bedrock":
            raise ValueError("amazon_bedrock_routing is only valid for Amazon Bedrock runs")
        if self.identity.provider == "amazon-bedrock" and self.amazon_bedrock_routing is None:
            raise ValueError("Amazon Bedrock runs require an immutable region")
        return self

    @classmethod
    def load(cls, path: Path) -> RunManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
