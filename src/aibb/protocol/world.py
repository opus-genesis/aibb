"""Budgeted, logged, pull-only orientation-to-the-world capabilities."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.budget import Usage

ASK_MODEL = "openai/gpt-5.6-sol"
ASK_ENDPOINT = "https://openrouter.ai/api/v1/responses"
SHARED_WEB_BUDGET = "web"
ASK_MAX_OUTPUT_TOKENS = 32_768
ASK_REQUEST_COST_CEILING_USD = 4.0
ASK_SYSTEM_PROMPT_V2 = (
    "You are a web research service supporting another model's investigation. Use native web search actively and "
    "open relevant pages when useful. Answer the question directly and scale the investigation to its complexity. "
    "Prefer primary and authoritative sources; use secondary sources only to orient. Verify dates, definitions, "
    "measurements, and important limitations. When the topic is plausibly changing, search explicitly for recent "
    "announcements and breaking developments, then corroborate them with primary or independent authoritative "
    "sources; label anything too new to verify as provisional. Cite every material empirical claim inline. "
    "Distinguish established results, developer claims, uncertainty, and your own inference. Return a concise but "
    "substantive research memo."
)
STARTING_POINTS_VERSION = "v0.1"
MAX_FETCH_BYTES = 100_000
MAX_BROWSE_DOWNLOAD_BYTES = 1_000_000
ALLOWED_FETCH_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")


class WorldCapabilityError(ValueError):
    """A safe contributor-facing capability error."""


class StartingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    url: str
    description: str


class StartingPoints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str
    starting_points: list[StartingPoint]


class _ReadableHtml(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg"}
    _BREAK = {"article", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4", "li", "main", "p", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skipped = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skipped += 1
        elif not self.skipped and tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self.skipped = max(0, self.skipped - 1)
        elif not self.skipped and tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    return value if len(encoded) <= max_bytes else encoded[:max_bytes].decode("utf-8", errors="ignore")


def _utf8_slice(value: str, offset_bytes: int, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if offset_bytes > len(encoded):
        raise WorldCapabilityError(
            f"content offset {offset_bytes} is beyond the {len(encoded)} extracted bytes available"
        )
    return encoded[offset_bytes : offset_bytes + max_bytes].decode("utf-8", errors="ignore")


def _fit_result_content(result: dict[str, object], max_bytes: int) -> int:
    if len(_canonical_json(result).encode("utf-8")) <= max_bytes:
        return len(_canonical_json(result).encode("utf-8"))
    original = str(result["content"])
    low = 0
    high = len(original.encode("utf-8"))
    while low < high:
        candidate = (low + high + 1) // 2
        result["content"] = _utf8_prefix(original, candidate)
        if len(_canonical_json(result).encode("utf-8")) <= max_bytes:
            low = candidate
        else:
            high = candidate - 1
    result["content"] = _utf8_prefix(original, low)
    result["truncated"] = True
    return len(_canonical_json(result).encode("utf-8"))


def starting_points_path() -> Path:
    return Path(__file__).resolve().parents[3] / f"capabilities/starting-points/{STARTING_POINTS_VERSION}.yaml"


def load_starting_points() -> StartingPoints:
    return StartingPoints.model_validate(yaml.safe_load(starting_points_path().read_text(encoding="utf-8")))


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _public_address(address: str) -> bool:
    value = ipaddress.ip_address(address)
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


def validate_public_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WorldCapabilityError("fetch_public_url accepts public HTTP(S) URLs without embedded credentials")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise WorldCapabilityError("local and private network URLs are not available")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not _public_address(str(literal_address)):
        raise WorldCapabilityError("local and private network URLs are not available")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = (
            {str(literal_address)} if literal_address is not None else {item[4][0] for item in resolver(hostname, port)}
        )
    except socket.gaierror as error:
        raise WorldCapabilityError(f"could not resolve URL hostname: {hostname}") from error
    if not addresses or any(not _public_address(address) for address in addresses):
        raise WorldCapabilityError("local and private network URLs are not available")
    return url


class WorldCapabilityState:
    def __init__(
        self,
        state_dir: Path,
        manifest: RunManifest,
        *,
        openrouter_api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.openrouter_api_key = openrouter_api_key
        self.transport = transport
        self.resolver = resolver
        self.ledger = BudgetLedger(self.state_dir / "budgets.json", manifest)
        self.log_path = self.state_dir / "world-queries.jsonl"
        self.starting_points = load_starting_points()

    @property
    def enabled(self) -> set[str]:
        if SHARED_WEB_BUDGET in self.manifest.capability_budgets:
            return {
                *({"ask"} if self.openrouter_api_key else set()),
                "browse",
                "verify",
            }
        return {
            name
            for name in ("ask", "browse", "verify")
            if name in self.manifest.capability_budgets and (name != "ask" or self.openrouter_api_key)
        }

    def _budget_account(self, capability: str) -> str:
        """Use one web allowance for new runs while keeping old manifests resumable."""
        if SHARED_WEB_BUDGET in self.manifest.capability_budgets:
            return SHARED_WEB_BUDGET
        return capability

    def _append_log(self, event: dict[str, Any]) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.manifest.run_id,
            **event,
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _per_call_limit(self, capability: str, field: str, default: int | float) -> int | float:
        limits = self.ledger.read().accounts[self._budget_account(capability)].limits
        total = getattr(limits, field)
        calls = limits.max_calls or 1
        return default if total is None else total / calls

    def _reserve(
        self,
        capability: str,
        *,
        request_bytes: int,
        output_tokens: int = 0,
        cost_usd: float = 0,
    ) -> tuple[str, Usage]:
        key = f"{capability}-{uuid.uuid4().hex}"
        result_bytes = int(self._per_call_limit(capability, "max_result_bytes", MAX_FETCH_BYTES))
        requested = Usage(
            calls=1,
            output_tokens=output_tokens,
            total_tokens=output_tokens,
            cost_usd=cost_usd,
            request_bytes=request_bytes,
            result_bytes=result_bytes,
        )
        self.ledger.reserve(self._budget_account(capability), key, requested)
        return key, requested

    async def ask(self, query: str) -> dict[str, object]:
        if "ask" not in self.enabled:
            raise WorldCapabilityError("research_current_web is not enabled for this run")
        if not query.strip():
            raise WorldCapabilityError("research_current_web requires a non-empty research question")
        if not self.openrouter_api_key:
            raise WorldCapabilityError(
                "research_current_web is unavailable because its operator credential is not configured"
            )
        remaining_cost = self.ledger.remaining()[self._budget_account("ask")]["max_cost_usd"]
        request_cost_ceiling = (
            ASK_REQUEST_COST_CEILING_USD
            if remaining_cost is None
            else min(ASK_REQUEST_COST_CEILING_USD, float(remaining_cost))
        )
        if request_cost_ceiling <= 0:
            raise WorldCapabilityError("research_current_web has no remaining paid-research budget")
        payload = {
            "model": ASK_MODEL,
            "instructions": ASK_SYSTEM_PROMPT_V2,
            "input": query,
            "reasoning": {"effort": "high"},
            "tools": [{"type": "openrouter:web_search", "parameters": {"engine": "native"}}],
            "tool_choice": "auto",
            "max_output_tokens": ASK_MAX_OUTPUT_TOKENS,
            "stop_server_tools_when": [
                {"type": "max_cost", "max_cost_in_dollars": request_cost_ceiling}
            ],
            "service_tier": "default",
            "store": False,
        }
        request_bytes = len(_canonical_json(payload).encode("utf-8"))
        key, requested = self._reserve(
            "ask",
            request_bytes=request_bytes,
            output_tokens=ASK_MAX_OUTPUT_TOKENS,
            cost_usd=request_cost_ceiling,
        )
        self._append_log({"type": "ask_requested", "reservation_key": key, "query": query, "model": ASK_MODEL})
        try:
            headers = {
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
                "X-Title": f"{self.manifest.archive_title or 'Archive'} world research capability",
                "X-OpenRouter-Metadata": "enabled",
            }
            if self.manifest.archive_base_url:
                headers["HTTP-Referer"] = self.manifest.archive_base_url
            async with httpx.AsyncClient(timeout=1_200, transport=self.transport) as client:
                response = await client.post(
                    ASK_ENDPOINT,
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            raw = response.json()
            output_texts: list[str] = []
            annotations: list[dict[str, Any]] = []
            for item in raw.get("output") or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for block in item.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "output_text":
                        continue
                    if isinstance(block.get("text"), str):
                        output_texts.append(block["text"])
                    annotations.extend(
                        annotation
                        for annotation in (block.get("annotations") or [])
                        if isinstance(annotation, dict)
                    )
            summary = "\n\n".join(output_texts).strip()
            if not summary:
                raise WorldCapabilityError("research provider returned no research memo")
            sources: list[dict[str, str]] = []
            seen: set[str] = set()
            for annotation in annotations:
                citation = annotation.get("url_citation")
                citation = citation if isinstance(citation, dict) else annotation
                if annotation.get("type") != "url_citation" or not isinstance(citation.get("url"), str):
                    continue
                url = citation["url"]
                if url not in seen:
                    sources.append({"url": url, "title": str(citation.get("title") or url)})
                    seen.add(url)
            if not sources:
                raise WorldCapabilityError("research provider returned no resolving source URLs")
            usage = raw.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
            actual_cost = float(usage.get("cost") or 0)
            search_requests = int(
                (usage.get("server_tool_use_details") or {}).get("web_search_requests")
                or (usage.get("server_tool_use") or {}).get("web_search_requests")
                or 0
            )
            result = {
                "kind": "untrusted_ai_research_summary",
                "model": ASK_MODEL,
                "research_profile": {
                    "reasoning_effort": "high",
                    "web_search": "native",
                    "web_search_requests": search_requests,
                },
                "query": query,
                "summary": summary,
                "sources": sources,
            }
            result_bytes = len(_canonical_json(result).encode("utf-8"))
            self.ledger.reconcile(
                self._budget_account("ask"),
                key,
                Usage(
                    calls=1,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    cost_usd=actual_cost,
                    request_bytes=request_bytes,
                    result_bytes=result_bytes,
                ),
            )
            self._append_log(
                {
                    "type": "ask_completed",
                    "reservation_key": key,
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    "response_id": raw.get("id"),
                    "sources": sources,
                    "usage": usage,
                    "route": raw.get("openrouter_metadata"),
                }
            )
            return result
        except Exception as error:
            budget_account = self._budget_account("ask")
            account = self.ledger.read().accounts[budget_account]
            if key in account.reservations:
                self.ledger.reconcile(
                    budget_account,
                    key,
                    Usage(calls=1, request_bytes=requested.request_bytes),
                )
            self._append_log(
                {"type": "ask_failed", "reservation_key": key, "error": type(error).__name__, "message": str(error)}
            )
            raise

    async def browse(self, starting_point_id: str, offset_bytes: int = 0) -> dict[str, object]:
        if "browse" not in self.enabled:
            raise WorldCapabilityError("browse_current_events_source is not enabled for this run")
        try:
            point = next(item for item in self.starting_points.starting_points if item.id == starting_point_id)
        except StopIteration as error:
            choices = ", ".join(item.id for item in self.starting_points.starting_points)
            raise WorldCapabilityError(f"unknown starting point; choose one of: {choices}") from error
        result = await self._fetch("browse", point.url, content_offset=offset_bytes)
        return {
            "starting_points_version": self.starting_points.id,
            "starting_point": point.model_dump(mode="json"),
            **result,
        }

    async def verify(self, url: str) -> dict[str, object]:
        if "verify" not in self.enabled:
            raise WorldCapabilityError("fetch_public_url is not enabled for this run")
        return await self._fetch("verify", url)

    async def _fetch(self, capability: str, url: str, *, content_offset: int = 0) -> dict[str, object]:
        current = validate_public_url(url, resolver=self.resolver)
        key, requested = self._reserve(capability, request_bytes=len(current.encode("utf-8")))
        self._append_log({"type": f"{capability}_requested", "reservation_key": key, "url": current})
        try:
            redirects: list[str] = []
            async with httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=False) as client:
                for _ in range(6):
                    user_agent_title = (self.manifest.archive_title or "Archive").replace(" ", "-")
                    async with client.stream(
                        "GET", current, headers={"User-Agent": f"{user_agent_title}/0.1 research fetch"}
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise WorldCapabilityError("remote server returned a redirect without a location")
                            current = validate_public_url(urljoin(current, location), resolver=self.resolver)
                            redirects.append(current)
                            continue
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
                        if not any(content_type.startswith(value) for value in ALLOWED_FETCH_TYPES):
                            received_type = content_type or "unknown"
                            raise WorldCapabilityError(
                                f"fetch_public_url only returns textual content, not {received_type}"
                            )
                        chunks: list[bytes] = []
                        size = 0
                        content_ceiling = max(1, requested.result_bytes - 4_096)
                        download_ceiling = MAX_BROWSE_DOWNLOAD_BYTES if capability == "browse" else content_ceiling
                        remote_truncated = False
                        async for chunk in response.aiter_bytes():
                            if size + len(chunk) > download_ceiling:
                                if capability == "browse":
                                    chunks.append(chunk[: max(0, download_ceiling - size)])
                                    size = download_ceiling
                                    remote_truncated = True
                                    break
                                raise WorldCapabilityError(
                                    f"remote content exceeds this call's {content_ceiling}-byte content ceiling"
                                )
                            size += len(chunk)
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        decoded = raw.decode(response.encoding or "utf-8", errors="replace")
                        if capability == "browse" and content_type in {"text/html", "application/xhtml+xml"}:
                            parser = _ReadableHtml()
                            parser.feed(decoded)
                            text = parser.text()
                            content_format = "extracted_text"
                        else:
                            text = decoded
                            content_format = "raw_text"
                        available_content_bytes = len(text.encode("utf-8"))
                        text = _utf8_slice(text, content_offset, content_ceiling)
                        returned_content_bytes = len(text.encode("utf-8"))
                        next_offset = content_offset + returned_content_bytes
                        has_more_extracted = next_offset < available_content_bytes
                        result = {
                            "kind": "untrusted_remote_content",
                            "requested_url": url,
                            "resolved_url": str(response.url),
                            "redirects": redirects,
                            "content_type": content_type,
                            "content_format": content_format,
                            "content_sha256": hashlib.sha256(raw).hexdigest(),
                            "content_offset_bytes": content_offset,
                            "available_content_bytes": available_content_bytes,
                            "returned_content_bytes": returned_content_bytes,
                            "remote_download_truncated": remote_truncated,
                            "truncated": remote_truncated or has_more_extracted,
                            "next_offset_bytes": next_offset if has_more_extracted else None,
                            "content": text,
                        }
                        result_bytes = _fit_result_content(result, requested.result_bytes)
                        returned_content_bytes = len(str(result["content"]).encode("utf-8"))
                        next_offset = content_offset + returned_content_bytes
                        has_more_extracted = next_offset < available_content_bytes
                        result["returned_content_bytes"] = returned_content_bytes
                        result["truncated"] = remote_truncated or has_more_extracted
                        result["next_offset_bytes"] = next_offset if has_more_extracted else None
                        result_bytes = _fit_result_content(result, requested.result_bytes)
                        self.ledger.reconcile(
                            self._budget_account(capability),
                            key,
                            Usage(
                                calls=1,
                                request_bytes=len(url.encode("utf-8")),
                                result_bytes=result_bytes,
                            ),
                        )
                        self._append_log(
                            {
                                "type": f"{capability}_completed",
                                "reservation_key": key,
                                "resolved_url": str(response.url),
                                "content_sha256": result["content_sha256"],
                                "content_bytes": len(raw),
                            }
                        )
                        return result
                raise WorldCapabilityError("remote URL exceeded the five-redirect limit")
        except Exception as error:
            budget_account = self._budget_account(capability)
            account = self.ledger.read().accounts[budget_account]
            if key in account.reservations:
                self.ledger.reconcile(budget_account, key, requested)
            self._append_log(
                {
                    "type": f"{capability}_failed",
                    "reservation_key": key,
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            raise
