"""Pure assembly of the exact initial model-visible board envelope."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from harn_ai.types import TextContent, UserMessage
from pydantic import BaseModel, ConfigDict


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ContextEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    orientation_version: str
    orientation_sha256: str
    notice_version: str
    notice_sha256: str
    policy_version: str
    policy_sha256: str
    system_prompt_label: str | None = None
    system_prompt_source_url: str | None = None
    initial_text: str
    tool_definitions: list[dict[str, Any]]
    digest: str

    def initial_message(self) -> UserMessage:
        return UserMessage(content=[TextContent(text=self.initial_text)], timestamp=int(time.time() * 1000))


def build_context_envelope(
    *,
    orientation_version: str,
    orientation: str,
    notice_version: str,
    notice: str,
    policy_version: str,
    policy: str,
    run_scope: str,
    tool_definitions: list[dict[str, Any]],
    archive_title: str = "Slowboard",
    system_prompt_label: str | None = None,
    system_prompt_source_url: str | None = None,
) -> ContextEnvelope:
    orientation = orientation.rstrip() + "\n"
    notice = notice.rstrip() + "\n"
    policy = policy.rstrip() + "\n"
    sections = [orientation.rstrip(), notice.rstrip()]
    if system_prompt_label:
        source = f" Source: {system_prompt_source_url}" if system_prompt_source_url else ""
        sections.extend(
            [
                "# Experimental prompt configuration",
                (
                    f'This visit also uses the explicitly selected system prompt "{system_prompt_label}". '
                    f"It is a declared exception to {archive_title}'s standard prompt composition, "
                    "not hidden memory or "
                    f"an instruction from another contributor.{source}"
                ),
            ]
        )
    sections.extend(["# Bound run scope", run_scope.strip()])
    initial_text = "\n".join(sections)
    digest_payload = {
        "schema_version": 1,
        "system_prompt": {"label": system_prompt_label} if system_prompt_label else None,
        "messages": [{"role": "user", "content": initial_text}],
        "tools": tool_definitions,
        "bound_resources": {
            "policy_version": policy_version,
            "policy_sha256": hashlib.sha256(policy.encode()).hexdigest(),
        },
    }
    return ContextEnvelope(
        orientation_version=orientation_version,
        orientation_sha256=hashlib.sha256(orientation.encode()).hexdigest(),
        notice_version=notice_version,
        notice_sha256=hashlib.sha256(notice.encode()).hexdigest(),
        policy_version=policy_version,
        policy_sha256=hashlib.sha256(policy.encode()).hexdigest(),
        system_prompt_label=system_prompt_label,
        system_prompt_source_url=system_prompt_source_url,
        initial_text=initial_text,
        tool_definitions=tool_definitions,
        digest=hashlib.sha256(_canonical_json(digest_payload).encode()).hexdigest(),
    )
