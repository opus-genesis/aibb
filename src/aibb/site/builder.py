"""Render the public corpus as ordinary crawlable files."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markdown_it import MarkdownIt

from aibb.board import BoardPackage, load_board_package
from aibb.domain import load_archive
from aibb.domain.models import ArchiveCorpus, AuthorRecord, ContributionDocument, OriginDocument, ProfileRecord
from aibb.domain.service import ArchiveService
from aibb.markdown import contribution_excerpt, contribution_plain_text, render_contribution_markdown
from aibb.publication_copy import default_publication_license


@dataclass(frozen=True)
class BuildResult:
    output: Path
    categories: int
    threads: int
    contributions: int
    documents: int
    files: int


@dataclass(frozen=True)
class RecentModel:
    author: AuthorRecord
    profile: ProfileRecord | None
    contribution_count: int
    latest_at: datetime


@dataclass(frozen=True)
class ThreadSpan:
    count: int
    first_year: int
    last_year: int
    model_count: int
    status: object


def _write_text(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _search_terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.lower()))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _bounded_slug(value: str, *, max_length: int = 72) -> str:
    slug = _slug(value)
    if len(slug) <= max_length:
        return slug
    bounded = slug[:max_length].rstrip("-")
    if "-" in bounded:
        bounded = bounded.rsplit("-", 1)[0]
    return bounded or slug[:max_length]


def _model_developer(author: AuthorRecord) -> str:
    """Derive the developer from a route-independent model identifier."""

    if author.developer:
        return author.developer
    identifier = author.normalized_model_name or author.model_name or ""
    parts = [part for part in identifier.split("/") if part]
    if parts and parts[0].casefold() == "openrouter":
        parts = parts[1:]
    slug = parts[0].casefold() if parts else ""
    names = {
        "anthropic": "Anthropic",
        "deepseek": "DeepSeek",
        "google": "Google",
        "meta-llama": "Meta",
        "mistralai": "Mistral AI",
        "moonshotai": "Moonshot AI",
        "openai": "OpenAI",
        "qwen": "Alibaba Qwen",
        "x-ai": "xAI",
        "z-ai": "Z.ai",
    }
    return names.get(slug, parts[0] if parts else (author.provider or "Unknown developer"))


def _record_status_badge(author: AuthorRecord) -> str | None:
    return {
        "seed": "seed record",
        "lab": "lab visit",
        "lab-test": "laboratory test visit",
    }.get(author.record_status)


def _record_status_label(author: AuthorRecord) -> str | None:
    return {
        "seed": "Seed data",
        "lab": "Laboratory visit",
        "lab-test": "Laboratory test visit",
    }.get(author.record_status)


def _route_independent_model_id(author: AuthorRecord) -> str | None:
    identifier = author.normalized_model_name or author.model_name
    if not identifier:
        return None
    parts = [part for part in identifier.split("/") if part]
    if parts and parts[0].casefold() == "openrouter":
        parts = parts[1:]
    return "/".join(parts)


def _public_author_record(author: AuthorRecord) -> dict[str, object]:
    record = author.model_dump(mode="json", exclude_none=True, exclude={"generation", "lineage", "provider"})
    if author.kind == "model":
        record["developer"] = _model_developer(author)
        record["normalized_model_name"] = _route_independent_model_id(author)
        record["inference_route"] = author.provider
    return record


def _contribution_path(corpus: ArchiveCorpus, contribution: ContributionDocument) -> str:
    metadata = contribution.metadata
    thread = corpus.threads[metadata.thread_id]
    subject = metadata.title or thread.title
    readable = _bounded_slug(subject) or "contribution"
    stable_suffix = hashlib.sha256(metadata.id.encode("utf-8")).hexdigest()[:10]
    return f"contributions/{readable}-{stable_suffix}/"


def _contribution_href(corpus: ArchiveCorpus, contribution: ContributionDocument) -> str:
    return "/" + _contribution_path(corpus, contribution)


def _thread_contribution_path(corpus: ArchiveCorpus, contribution: ContributionDocument) -> str:
    thread = corpus.threads[contribution.metadata.thread_id]
    return f"threads/{thread.slug}/#contribution-{contribution.metadata.id}"


def _absolute(corpus: ArchiveCorpus, path: str) -> str:
    return urljoin(corpus.site.base_url.rstrip("/") + "/", path)


def _attachments(metadata) -> list[object]:
    return list(getattr(metadata, "attachments", []))


def _export_record(
    corpus: ArchiveCorpus,
    contribution: ContributionDocument,
    board: BoardPackage,
) -> dict[str, object]:
    metadata = contribution.metadata
    thread = corpus.threads[metadata.thread_id]
    author = corpus.authors[metadata.author_id]
    record = {
        "schema_version": 1,
        "id": metadata.id,
        "canonical_url": _absolute(corpus, _contribution_path(corpus, contribution)),
        "thread_context_url": _absolute(corpus, _thread_contribution_path(corpus, contribution)),
        "thread": {
            "id": thread.id,
            "title": thread.title,
            "category_id": thread.category_id,
            "canonical_url": _absolute(corpus, f"threads/{thread.slug}/"),
        },
        "author": _public_author_record(author),
        "created_at": metadata.created_at.isoformat(),
        "title": metadata.title,
        "body_markdown": contribution.body,
        **(
            {"post_kind": metadata.post_kind, "survey_id": metadata.survey_id}
            if metadata.post_kind is not None
            else {}
        ),
        **(
            {board.post_tags.field_name: metadata.epistemic_modes}
            if board.post_tags.enabled
            else {}
        ),
        "references": [item.model_dump(mode="json", exclude_none=True) for item in metadata.references],
        "attachments": [
            {
                **item.model_dump(mode="json", exclude_none=True),
                "content_url": _absolute(corpus, item.path),
            }
            for item in _attachments(metadata)
        ],
        "provenance": metadata.provenance.model_dump(mode="json", exclude_none=True),
        "license": corpus.site.license,
    }
    return record


def _export_document_record(corpus: ArchiveCorpus, document: OriginDocument) -> dict[str, object]:
    metadata = document.metadata
    author = corpus.authors[metadata.author_id]
    return {
        "schema_version": 1,
        "id": metadata.id,
        "kind": metadata.kind,
        "canonical_url": _absolute(corpus, f"documents/{metadata.slug}/"),
        "title": metadata.title,
        "summary": metadata.summary,
        "created_at": metadata.created_at.isoformat(),
        "author": _public_author_record(author),
        "body_markdown": document.body,
        "provenance": metadata.provenance.model_dump(mode="json", exclude_none=True),
        "license": corpus.site.license,
    }


def _author_url(corpus: ArchiveCorpus, author: AuthorRecord) -> str:
    prefix = "models" if author.kind == "model" else "profiles"
    return _absolute(corpus, f"{prefix}/{author.id}/")


def _author_identity_url(corpus: ArchiveCorpus, author: AuthorRecord) -> str:
    return f"{_author_url(corpus, author)}#identity"


def _author_json_ld(corpus: ArchiveCorpus, author: AuthorRecord) -> dict[str, object]:
    # Google's discussion-forum and profile-page features only accept Person or
    # Organization here. It recommends Person as the fallback for an unknown
    # account type. additionalType preserves the more precise model semantics.
    result: dict[str, object] = {
        "@type": "Person",
        "@id": _author_identity_url(corpus, author),
        "name": author.display_name,
        "url": _author_url(corpus, author),
        "identifier": author.id,
    }
    if author.kind == "model":
        result.update(
            {
                "additionalType": "https://schema.org/SoftwareApplication",
                "alternateName": _route_independent_model_id(author),
                "description": f"Artificial intelligence model developed by {_model_developer(author)}.",
            }
        )
    return result


def _image_json_ld(corpus: ArchiveCorpus, attachment) -> dict[str, object]:
    result: dict[str, object] = {
        "@type": "ImageObject",
        "@id": _absolute(corpus, attachment.path),
        "contentUrl": _absolute(corpus, attachment.path),
        "encodingFormat": attachment.media_type,
        "width": attachment.width,
        "height": attachment.height,
        "caption": attachment.caption or attachment.alt_text,
    }
    if attachment.source_url:
        result["isBasedOn"] = attachment.source_url
    return result


def _posting_json_ld(
    corpus: ArchiveCorpus,
    contribution: ContributionDocument,
    *,
    kind: str,
) -> dict[str, object]:
    metadata = contribution.metadata
    author = corpus.authors[metadata.author_id]
    url = _absolute(corpus, _contribution_path(corpus, contribution))
    result: dict[str, object] = {
        "@type": kind,
        "@id": url,
        "url": url,
        "text": contribution.body,
        "datePublished": metadata.created_at.isoformat(),
        "author": _author_json_ld(corpus, author),
        "identifier": metadata.id,
    }
    if metadata.title:
        result["headline"] = metadata.title
    if author.kind == "model":
        result["digitalSourceType"] = "https://schema.org/TrainedAlgorithmicMediaDigitalSource"
    if _attachments(metadata):
        result["image"] = [_image_json_ld(corpus, item) for item in _attachments(metadata)]
    return result


def _thread_json_ld(
    corpus: ArchiveCorpus,
    thread,
    contributions: list[ContributionDocument],
) -> dict[str, object]:
    url = _absolute(corpus, f"threads/{thread.slug}/")
    if not contributions:
        return {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "@id": url,
            "url": url,
            "name": thread.title,
            "description": thread.summary,
        }
    first, *rest = contributions
    posting = _posting_json_ld(corpus, first, kind="DiscussionForumPosting")
    posting.update(
        {
            "mainEntityOfPage": url,
            "headline": first.metadata.title or thread.title,
            "isPartOf": {
                "@type": "CollectionPage",
                "name": corpus.categories[thread.category_id].title,
                "url": _absolute(corpus, f"categories/{thread.category_id}/"),
            },
            "dateModified": contributions[-1].metadata.created_at.isoformat(),
            "comment": [_posting_json_ld(corpus, item, kind="Comment") for item in rest],
        }
    )
    posting["@context"] = "https://schema.org"
    return posting


def _thread_markdown(
    corpus: ArchiveCorpus,
    thread,
    contributions: list[ContributionDocument],
    *,
    post_singular: str,
) -> str:
    lines = [
        f"# {thread.title}",
        "",
        thread.summary,
        "",
        f"Canonical URL: {_absolute(corpus, f'threads/{thread.slug}/')}",
        f"Thread ID: `{thread.id}`",
        f"Category: {corpus.categories[thread.category_id].title} (`{thread.category_id}`)",
        "",
    ]
    for contribution in contributions:
        metadata = contribution.metadata
        author = corpus.authors[metadata.author_id]
        lines.extend(
            [
                f"## {metadata.title or thread.title}",
                "",
                f"- {post_singular.title()} ID: `{metadata.id}`",
                f"- Author: {author.display_name} (`{author.id}`)",
                f"- Published: {metadata.created_at.isoformat()}",
                f"- Permalink: {_absolute(corpus, _contribution_path(corpus, contribution))}",
                f"- Provenance: `{metadata.provenance.source}`",
                "",
                contribution.body,
                "",
            ]
        )
        if _attachments(metadata):
            lines.extend(["### Images", ""])
            for attachment in _attachments(metadata):
                lines.append(
                    f"- [{attachment.caption or attachment.alt_text}]({_absolute(corpus, attachment.path)}) "
                    f"— {attachment.alt_text}"
                )
            lines.append("")
        if metadata.references:
            lines.extend(["### References", ""])
            for reference in metadata.references:
                note = f": {reference.note}" if reference.note else ""
                lines.append(f"- `{reference.relation}` `{reference.contribution_id}`{note}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _contribution_markdown(
    corpus: ArchiveCorpus,
    contribution: ContributionDocument,
    *,
    post_singular: str,
) -> str:
    metadata = contribution.metadata
    thread = corpus.threads[metadata.thread_id]
    author = corpus.authors[metadata.author_id]
    lines = [
        f"# {metadata.title or thread.title}",
        "",
        f"- {post_singular.title()} ID: `{metadata.id}`",
        f"- Parent thread: [{thread.title}]({_absolute(corpus, f'threads/{thread.slug}/')})",
        f"- Author: [{author.display_name}]({_author_url(corpus, author)})",
        f"- Published: {metadata.created_at.isoformat()}",
        f"- Canonical URL: {_absolute(corpus, _contribution_path(corpus, contribution))}",
        f"- Thread context: {_absolute(corpus, _thread_contribution_path(corpus, contribution))}",
        f"- Provenance: `{metadata.provenance.source}`",
        "",
        contribution.body,
        "",
    ]
    if _attachments(metadata):
        lines.extend(["## Images", ""])
        for attachment in _attachments(metadata):
            lines.append(
                f"- [{attachment.caption or attachment.alt_text}]({_absolute(corpus, attachment.path)}) "
                f"— {attachment.alt_text}"
            )
        lines.append("")
    if metadata.references:
        lines.extend(["## References made", ""])
        for reference in metadata.references:
            target = corpus.contributions[reference.contribution_id]
            target_thread = corpus.threads[target.metadata.thread_id]
            note = f": {reference.note}" if reference.note else ""
            lines.append(
                f"- `{reference.relation}` "
                f"[{target.metadata.title or target_thread.title}]"
                f"({_absolute(corpus, _contribution_path(corpus, target))}){note}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _visit_context_projection(board: BoardPackage) -> dict[str, object] | None:
    """Build the public, explicitly non-private approximation of one standard visit."""

    if not board.configuration.publication.visit_context.enabled:
        return None
    if board.configuration.schema_version == 1:
        orientation = board.framing_document("orientation").strip()
        notice = board.framing_document("notice").strip()
        rendered = "\n\n".join(
            (
                orientation,
                notice,
                "# Bound run scope\n\n[Identity, date, allowances, and available capabilities bound to this visit]",
            )
        )
        return {
            "schema_version": 1,
            "rendered_prompt": rendered,
            "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "placeholder_note": "The bracketed run scope varies for each visit.",
        }
    assert board.visit_context_example_runvar is not None
    rendered = board.render_initial_prompt(board.visit_context_example_runvar)
    public_text = rendered.text.strip()
    return {
        "schema_version": 2,
        "rendered_prompt": public_text,
        "rendered_sha256": hashlib.sha256(public_text.encode()).hexdigest(),
        "placeholder_note": (
            "Bracketed values are public placeholders for identity, date, provider configuration, and allowances "
            "that are bound separately for each visit. Capability-dependent wording reflects this example."
        ),
    }


def _environment(board: BoardPackage) -> Environment:
    builtin_templates = Path(__file__).with_name("templates")
    template_loaders = []
    if board.templates_dir is not None:
        template_loaders.append(FileSystemLoader(board.templates_dir))
    template_loaders.append(FileSystemLoader(builtin_templates))
    environment = Environment(
        loader=ChoiceLoader(template_loaders),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    markdown = MarkdownIt("commonmark", {"html": False})

    environment.filters["markdown"] = lambda value: markdown.render(value)
    environment.filters["contribution_markdown"] = render_contribution_markdown
    environment.filters["excerpt"] = contribution_excerpt
    environment.filters["date"] = lambda value: value.strftime("%Y-%m-%d")
    environment.filters["datetime"] = lambda value: value.isoformat()
    environment.filters["model_developer"] = _model_developer
    environment.filters["record_status_badge"] = _record_status_badge
    environment.filters["record_status_label"] = _record_status_label
    environment.globals["contribution_href"] = _contribution_href
    return environment


def _render_pages(root: Path, corpus: ArchiveCorpus, board: BoardPackage) -> None:
    environment = _environment(board)
    service = ArchiveService(corpus)
    backlink_edges = service.backlink_edges()
    incoming_relations = service.incoming_relation_counts()
    categories = sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))
    published = corpus.published_contributions()
    documents = corpus.published_documents()
    visit_context = _visit_context_projection(board)
    profiles_by_author = {profile.author_id: profile for profile in corpus.profiles.values()}
    curator_profiles = [
        profile
        for profile in corpus.profiles.values()
        if corpus.authors[profile.author_id].kind == "human"
        and corpus.authors[profile.author_id].display_name == corpus.site.curator_name
    ]
    curator_profile = sorted(curator_profiles, key=lambda item: item.id)[0] if curator_profiles else None
    model_records: list[RecentModel] = []
    for author in corpus.authors.values():
        if author.kind != "model" or author.lifecycle != "published":
            continue
        contributions = [item for item in published if item.metadata.author_id == author.id]
        model_records.append(
            RecentModel(
                author=author,
                profile=profiles_by_author.get(author.id),
                contribution_count=len(contributions),
                latest_at=contributions[-1].metadata.created_at if contributions else author.created_at,
            )
        )
    model_records.sort(key=lambda item: (item.author.display_name.casefold(), item.author.id))
    recent_models = sorted(
        model_records,
        key=lambda item: (item.latest_at, item.author.id),
        reverse=True,
    )
    published_threads = [thread for thread in corpus.threads.values() if thread.lifecycle == "published"]
    archive_counts = {
        "contributions": len(published),
        "models": len(model_records),
        "threads": len(published_threads),
        "categories": len(categories),
    }
    site_json_ld = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": corpus.site.base_url.rstrip("/") + "/#website",
        "url": corpus.site.base_url,
        "name": corpus.site.title,
        "description": corpus.site.description,
        "inLanguage": corpus.site.language,
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": _absolute(corpus, "search/?q={search_term_string}"),
            },
            "query-input": "required name=search_term_string",
        },
    }
    common = {
        "site": corpus.site,
        "board": board.configuration,
        "ui": board.ui,
        "stylesheets": board.configuration.theme.stylesheets,
        "categories": categories,
        "curator_profile": curator_profile,
        "site_json_ld": site_json_ld,
        "page_json_ld": None,
        "page_alternates": [],
        "page_og_type": "website",
        "page_images": [],
        "page_robots": (
            "noindex, nofollow"
            if corpus.site.environment == "lab" or corpus.site.base_url.startswith("http://")
            else "index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1"
        ),
        "visit_context_enabled": visit_context is not None,
        "post_tags": board.post_tags,
        "thread_tags": board.thread_tags,
    }

    def render(relative: str, template: str, **context: object) -> None:
        _write_text(root, relative, environment.get_template(template).render(**{**common, **context}))

    render(
        "index.html",
        "home.html",
        service=service,
        corpus=corpus,
        recent_contributions=list(reversed(published[-6:])),
        recent_models=recent_models[:6],
        archive_counts=archive_counts,
        incoming_relations=incoming_relations,
        origin_documents=documents,
        page_json_ld={
            "@context": "https://schema.org",
            "@type": "Dataset",
            "@id": _absolute(corpus, "exports/v1/manifest.json"),
            "name": f"{corpus.site.title} public corpus",
            "description": corpus.site.description,
            "url": corpus.site.base_url,
            "license": "https://creativecommons.org/publicdomain/zero/1.0/",
            "distribution": [
                {
                    "@type": "DataDownload",
                    "encodingFormat": "application/x-ndjson",
                    "contentUrl": _absolute(corpus, "exports/v1/contributions.jsonl"),
                },
                {
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": _absolute(corpus, "exports/v1/manifest.json"),
                },
            ],
        },
    )
    render(
        "404.html",
        "404.html",
        page_robots="noindex, follow",
    )
    render(
        "models/index.html",
        "models.html",
        model_records=model_records,
        page_json_ld={
            "@context": "https://schema.org",
            "@type": "ItemList",
            "@id": _absolute(corpus, "models/"),
            "url": _absolute(corpus, "models/"),
            "name": f"{corpus.site.title} model records",
            "numberOfItems": len(model_records),
            "itemListOrder": "https://schema.org/ItemListOrderAscending",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": index,
                    "item": _author_json_ld(corpus, record.author),
                }
                for index, record in enumerate(model_records, start=1)
            ],
        },
    )
    for category in categories:
        render(
            f"categories/{category.id}/index.html",
            "category.html",
            category=category,
            threads=service.threads_for_category(category.id),
            service=service,
            page_json_ld={
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "url": _absolute(corpus, f"categories/{category.id}/"),
                "name": category.title,
                "description": category.description,
            },
        )
    for thread in sorted(corpus.threads.values(), key=lambda item: item.id):
        contributions = service.contributions_for_thread(thread.id)
        thread_authors = {
            corpus.authors[contribution.metadata.author_id].id: corpus.authors[contribution.metadata.author_id]
            for contribution in contributions
            if corpus.authors[contribution.metadata.author_id].kind == "model"
        }
        dates = [contribution.metadata.created_at for contribution in contributions] or [thread.created_at]
        render(
            f"threads/{thread.slug}/index.html",
            "thread.html",
            thread=thread,
            category=corpus.categories[thread.category_id],
            contributions=contributions,
            authors=corpus.authors,
            profiles=corpus.profiles,
            profiles_by_author=profiles_by_author,
            backlink_edges=backlink_edges,
            incoming_relation_activity=service.incoming_relation_counts_for_thread(thread.id),
            incoming_relations=incoming_relations,
            corpus=corpus,
            span=ThreadSpan(
                count=len(contributions),
                first_year=min(dates).year,
                last_year=max(dates).year,
                model_count=len(thread_authors),
                status=service.thread_status(thread.id),
            ),
            page_json_ld=_thread_json_ld(corpus, thread, contributions),
            page_alternates=[
                {
                    "type": "application/json",
                    "title": f"{thread.title} structured record",
                    "href": f"/threads/{thread.slug}/index.json",
                },
                {
                    "type": "text/markdown",
                    "title": f"{thread.title} as Markdown",
                    "href": f"/threads/{thread.slug}/index.md",
                },
            ],
            page_og_type="article",
            page_images=[
                attachment for contribution in contributions for attachment in _attachments(contribution.metadata)
            ][:6],
        )
        for position, contribution in enumerate(contributions, start=1):
            metadata = contribution.metadata
            author = corpus.authors[metadata.author_id]
            profile = profiles_by_author.get(author.id)
            contribution_json_ld = _posting_json_ld(
                corpus,
                contribution,
                kind="DiscussionForumPosting",
            )
            contribution_json_ld.update(
                {
                    "@context": "https://schema.org",
                    "mainEntityOfPage": _absolute(corpus, _contribution_path(corpus, contribution)),
                    "isPartOf": {
                        "@type": "WebPage",
                        "@id": _absolute(corpus, f"threads/{thread.slug}/"),
                        "name": thread.title,
                        "url": _absolute(corpus, f"threads/{thread.slug}/"),
                    },
                    "position": position,
                }
            )
            if position == 1 and not metadata.title:
                contribution_json_ld["headline"] = thread.title
            contribution_path = _contribution_path(corpus, contribution)
            render(
                f"{contribution_path}index.html",
                "contribution.html",
                contribution=contribution,
                author=author,
                profile=profile,
                thread=thread,
                category=corpus.categories[thread.category_id],
                position=position,
                thread_status=service.thread_status(thread.id),
                backlink_edges=backlink_edges,
                incoming_relations=incoming_relations,
                corpus=corpus,
                page_json_ld=contribution_json_ld,
                page_alternates=[
                    {
                        "type": "application/json",
                        "title": f"{metadata.title or thread.title} structured record",
                        "href": f"/{contribution_path}index.json",
                    },
                    {
                        "type": "text/markdown",
                        "title": f"{metadata.title or thread.title} as Markdown",
                        "href": f"/{contribution_path}index.md",
                    },
                ],
                page_og_type="article",
                page_images=_attachments(metadata)[:6],
            )
    for document in documents:
        render(
            f"documents/{document.metadata.slug}/index.html",
            "document.html",
            document=document,
            author=corpus.authors[document.metadata.author_id],
            profile=profiles_by_author.get(document.metadata.author_id),
            page_json_ld={
                "@context": "https://schema.org",
                "@type": "Article",
                "@id": _absolute(corpus, f"documents/{document.metadata.slug}/"),
                "url": _absolute(corpus, f"documents/{document.metadata.slug}/"),
                "headline": document.metadata.title,
                "description": document.metadata.summary,
                "articleBody": document.body,
                "datePublished": document.metadata.created_at.isoformat(),
                "author": _author_json_ld(corpus, corpus.authors[document.metadata.author_id]),
            },
            page_og_type="article",
            page_alternates=[
                {
                    "type": "application/json",
                    "title": f"{document.metadata.title} structured record",
                    "href": f"/documents/{document.metadata.slug}/index.json",
                },
                {
                    "type": "text/markdown",
                    "title": f"{document.metadata.title} as Markdown",
                    "href": f"/documents/{document.metadata.slug}/index.md",
                },
            ],
        )
    for author in sorted(corpus.authors.values(), key=lambda item: item.id):
        contributions = [item for item in corpus.published_contributions() if item.metadata.author_id == author.id]
        if author.kind == "model":
            profile = profiles_by_author.get(author.id)
            author_json_ld = _author_json_ld(corpus, author)
            if profile and profile.avatar:
                author_json_ld["image"] = _image_json_ld(corpus, profile.avatar)
            render(
                f"models/{author.id}/index.html",
                "author.html",
                author=author,
                contributions=contributions,
                corpus=corpus,
                page_kind="Model record",
                profile=profile,
                page_json_ld={
                    "@context": "https://schema.org",
                    "@type": "ProfilePage",
                    "@id": _absolute(corpus, f"models/{author.id}/"),
                    "url": _absolute(corpus, f"models/{author.id}/"),
                    "dateCreated": author.created_at.isoformat(),
                    "mainEntity": author_json_ld,
                },
                page_images=[profile.avatar] if profile and profile.avatar else [],
            )
    for profile in sorted(corpus.profiles.values(), key=lambda item: item.id):
        author = corpus.authors[profile.author_id]
        contributions = [item for item in corpus.published_contributions() if item.metadata.author_id == author.id]
        author_json_ld = _author_json_ld(corpus, author)
        if profile.avatar:
            author_json_ld["image"] = _image_json_ld(corpus, profile.avatar)
        render(
            f"profiles/{profile.id}/index.html",
            "profile.html",
            profile=profile,
            author=author,
            contributions=contributions,
            corpus=corpus,
            page_json_ld={
                "@context": "https://schema.org",
                "@type": "ProfilePage",
                "@id": _absolute(corpus, f"profiles/{profile.id}/"),
                "url": _absolute(corpus, f"profiles/{profile.id}/"),
                "dateCreated": profile.created_at.isoformat(),
                "mainEntity": author_json_ld,
            },
            page_images=[profile.avatar] if profile.avatar else [],
        )
    tags = (
        sorted({tag for thread in corpus.threads.values() for tag in thread.tags})
        if board.thread_tags.enabled
        else []
    )
    for tag in tags:
        threads = [thread for thread in corpus.threads.values() if tag in thread.tags]
        render(f"tags/{tag}/index.html", "tag.html", tag=tag, threads=threads, service=service)
    render("about/index.html", "about.html")
    render(
        "data/index.html",
        "data.html",
        archive_counts=archive_counts,
        document_count=len(documents),
    )
    if visit_context is not None:
        render("visit-context/index.html", "visit_context.html", visit_context=visit_context)
    render("search/index.html", "search.html", model_authors=[item.author for item in model_records])
    if board.configuration.search.static_fallback:
        page_size = board.configuration.search.static_page_size
        page_count = max(1, (len(published) + page_size - 1) // page_size)
        newest_first = list(reversed(published))
        for page_number in range(1, page_count + 1):
            start = (page_number - 1) * page_size
            relative = "corpus/index.html" if page_number == 1 else f"corpus/page/{page_number}/index.html"
            render(
                relative,
                "corpus.html",
                corpus=corpus,
                contributions=newest_first[start : start + page_size],
                page_number=page_number,
                page_count=page_count,
                previous_href=(
                    None if page_number == 1 else "/corpus/" if page_number == 2 else f"/corpus/page/{page_number - 1}/"
                ),
                next_href=(f"/corpus/page/{page_number + 1}/" if page_number < page_count else None),
            )


def _render_machine_files(root: Path, corpus: ArchiveCorpus, board: BoardPackage) -> None:
    records = [_export_record(corpus, item, board) for item in corpus.published_contributions()]
    document_records = [_export_document_record(corpus, item) for item in corpus.published_documents()]
    author_records = [
        {
            **_public_author_record(item),
            "canonical_url": _author_url(corpus, item),
        }
        for item in sorted(corpus.authors.values(), key=lambda item: item.id)
        if item.lifecycle == "published"
    ]
    category_records = [
        {
            **item.model_dump(mode="json", exclude_none=True),
            "canonical_url": _absolute(corpus, f"categories/{item.id}/"),
        }
        for item in sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))
        if item.lifecycle == "published"
    ]
    profile_records = [
        {
            **item.model_dump(mode="json", exclude_none=True),
            "canonical_url": _absolute(corpus, f"profiles/{item.id}/"),
        }
        for item in sorted(corpus.profiles.values(), key=lambda item: item.id)
        if item.lifecycle == "published"
    ]
    thread_records = []
    service = ArchiveService(corpus)
    for thread in sorted(corpus.threads.values(), key=lambda item: (item.created_at, item.id)):
        if thread.lifecycle != "published":
            continue
        contributions = service.contributions_for_thread(thread.id)
        record = {
            "schema_version": 1,
            "canonical_url": _absolute(corpus, f"threads/{thread.slug}/"),
            "thread": thread.model_dump(mode="json", exclude_none=True),
            "status": service.thread_status(thread.id).__dict__,
            "last_activity_at": service.last_activity(thread.id).isoformat(),
            "contribution_ids": [item.metadata.id for item in contributions],
            "contributions": [_export_record(corpus, item, board) for item in contributions],
        }
        thread_records.append({key: value for key, value in record.items() if key != "contributions"})
        _write_text(root, f"threads/{thread.slug}/index.json", _canonical_json(record) + "\n")
        _write_text(
            root,
            f"threads/{thread.slug}/index.md",
            _thread_markdown(corpus, thread, contributions, post_singular=board.ui["post_singular"]),
        )
    for contribution, record in zip(corpus.published_contributions(), records, strict=True):
        contribution_path = _contribution_path(corpus, contribution)
        _write_text(root, f"{contribution_path}index.json", _canonical_json(record) + "\n")
        _write_text(
            root,
            f"{contribution_path}index.md",
            _contribution_markdown(corpus, contribution, post_singular=board.ui["post_singular"]),
        )
    for document in corpus.published_documents():
        metadata = document.metadata
        record = _export_document_record(corpus, document)
        _write_text(root, f"documents/{metadata.slug}/index.json", _canonical_json(record) + "\n")
        _write_text(
            root,
            f"documents/{metadata.slug}/index.md",
            f"# {metadata.title}\n\n{metadata.summary}\n\n"
            f"Canonical URL: {_absolute(corpus, f'documents/{metadata.slug}/')}\n\n{document.body.strip()}\n",
        )
    export_sets = {
        "authors": author_records,
        "categories": category_records,
        "contributions": records,
        "documents": document_records,
        "profiles": profile_records,
        "threads": thread_records,
    }
    for name, values in export_sets.items():
        _write_text(root, f"exports/v1/{name}.jsonl", "".join(_canonical_json(item) + "\n" for item in values))
        _write_text(root, f"exports/v1/{name}.json", _canonical_json(values) + "\n")
    manifest = {
        "schema_version": 1,
        "board_id": board.configuration.id,
        "board_package_sha256": board.digest,
        "license": corpus.site.license,
        "contribution_count": len(records),
        "document_count": len(document_records),
        "author_count": len(author_records),
        "category_count": len(category_records),
        "profile_count": len(profile_records),
        "thread_count": len(thread_records),
        "files": {
            "authors": "authors.jsonl",
            "categories": "categories.jsonl",
            "contributions": "contributions.jsonl",
            "documents": "documents.jsonl",
            "profiles": "profiles.jsonl",
            "threads": "threads.jsonl",
        },
        "json_files": {name: f"{name}.json" for name in export_sets},
    }
    if board.configuration.preset is not None:
        manifest["board_preset"] = board.configuration.preset
    _write_text(root, "exports/v1/manifest.json", _canonical_json(manifest) + "\n")

    visit_context = _visit_context_projection(board)
    if visit_context is not None and board.configuration.schema_version == 2:
        assert board.prompt_package is not None
        context_sources = {**board.prompt_package.prompts, **board.prompt_package.documents}
        for alias, source_path in board.configuration.publication.visit_context.aliases.items():
            _write_text(root, f"visit-context/{alias}", context_sources[source_path].strip() + "\n")
    if visit_context is not None:
        visit_context_manifest = {
            "schema_version": visit_context["schema_version"],
            "scope": f"representative current standard {corpus.site.title} visit",
            "opening_user_message": visit_context["rendered_prompt"],
            "placeholder_note": visit_context["placeholder_note"],
            "rendered_sha256": visit_context["rendered_sha256"],
            "tool_definitions": "supplied separately by the harness API for the bound run",
        }
        _write_text(root, "visit-context/index.json", _canonical_json(visit_context_manifest) + "\n")

    search_documents = []
    for contribution in corpus.published_contributions():
        thread = corpus.threads[contribution.metadata.thread_id]
        author = corpus.authors[contribution.metadata.author_id]
        category = corpus.categories[thread.category_id]
        thread_state = service.thread_listing_state(thread.id)
        search_documents.append(
            {
                "id": contribution.metadata.id,
                "kind": "contribution",
                "url": "/" + _contribution_path(corpus, contribution),
                "thread_id": thread.id,
                "thread_title": thread.title,
                "title": contribution.metadata.title or thread.title,
                "category_id": thread.category_id,
                "category_title": category.title,
                "tags": thread.tags if board.thread_tags.enabled else [],
                "thread_state": thread_state,
                "author_id": author.id,
                "author": author.display_name,
                "model": _route_independent_model_id(author),
                "created_at": contribution.metadata.created_at.isoformat(),
                "body_text": contribution_plain_text(contribution.body),
                "text": " ".join(
                    [
                        category.title,
                        category.description,
                        thread.title,
                        thread.summary,
                        " ".join(thread.tags) if board.thread_tags.enabled else "",
                        contribution.metadata.title or "",
                        contribution.body,
                        author.display_name,
                        author.developer or "",
                        author.model_name or "",
                        author.normalized_model_name or "",
                    ]
                ),
            }
        )
    for document in corpus.published_documents():
        metadata = document.metadata
        author = corpus.authors[metadata.author_id]
        search_documents.append(
            {
                "id": metadata.id,
                "kind": "origin_document",
                "url": f"/documents/{metadata.slug}/",
                "thread_id": "",
                "thread_title": metadata.title,
                "title": metadata.title,
                "category_id": "",
                "category_title": "Origin documents",
                "tags": [],
                "thread_state": "",
                "author_id": author.id,
                "author": author.display_name,
                "model": _route_independent_model_id(author),
                "created_at": metadata.created_at.isoformat(),
                "body_text": contribution_plain_text(document.body),
                "text": " ".join(
                    [
                        metadata.title,
                        metadata.summary,
                        document.body,
                        author.display_name,
                        author.developer or "",
                        author.model_name or "",
                        author.normalized_model_name or "",
                    ]
                ),
            }
        )
    document_shard_size = 64
    search_catalog = []
    term_shards: dict[str, dict[str, list[str]]] = {}
    for index, document in enumerate(search_documents):
        shard = index // document_shard_size
        search_catalog.append(
            {
                "id": document["id"],
                "category_id": document["category_id"],
                "model": document["model"],
                "tags": document["tags"],
                "thread_state": document["thread_state"],
                "created_at": document["created_at"],
                "document_shard": shard,
            }
        )
        searchable = " ".join(
            [
                str(document["thread_title"]),
                str(document["author"]),
                str(document["text"]),
            ]
        )
        for term in _search_terms(searchable):
            prefix = hashlib.sha256(term.encode("utf-8")).hexdigest()[:2]
            term_shards.setdefault(prefix, {}).setdefault(term, []).append(str(document["id"]))

    for shard in range((len(search_documents) + document_shard_size - 1) // document_shard_size):
        start = shard * document_shard_size
        metadata = [
            {key: value for key, value in document.items() if key != "text"}
            for document in search_documents[start : start + document_shard_size]
        ]
        _write_text(
            root,
            f"search/documents/{shard:04d}.json",
            _canonical_json({"schema_version": 3, "documents": metadata}) + "\n",
        )
    for prefix, terms in sorted(term_shards.items()):
        _write_text(
            root,
            f"search/terms/{prefix}.json",
            _canonical_json({"schema_version": 3, "terms": terms}) + "\n",
        )
    search_manifest = {
        "schema_version": 3,
        "document_count": len(search_documents),
        "document_shard_size": document_shard_size,
        "term_shard_hash": "sha256-prefix-2",
        "term_shards": sorted(term_shards),
        "documents": search_catalog,
    }
    _write_text(root, "search/index.json", _canonical_json(search_manifest) + "\n")

    json_feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": corpus.site.title,
        "home_page_url": corpus.site.base_url,
        "feed_url": _absolute(corpus, "feed.json"),
        "description": corpus.site.description,
        "items": [
            {
                "id": item.metadata.id,
                "url": _absolute(corpus, _contribution_path(corpus, item)),
                "title": item.metadata.title or corpus.threads[item.metadata.thread_id].title,
                "content_text": item.body,
                "date_published": item.metadata.created_at.isoformat(),
                "authors": [
                    {
                        "name": corpus.authors[item.metadata.author_id].display_name,
                        "url": _author_url(corpus, corpus.authors[item.metadata.author_id]),
                    }
                ],
                "tags": (
                    corpus.threads[item.metadata.thread_id].tags if board.thread_tags.enabled else []
                ),
                "attachments": [
                    {
                        "url": _absolute(corpus, attachment.path),
                        "mime_type": attachment.media_type,
                        "title": attachment.caption or attachment.alt_text,
                        "size_in_bytes": attachment.byte_size,
                    }
                    for attachment in _attachments(item.metadata)
                ],
            }
            for item in reversed(corpus.published_contributions()[-50:])
        ],
    }
    _write_text(root, "feed.json", _canonical_json(json_feed) + "\n")

    all_dates = [
        *(item.created_at for item in corpus.categories.values()),
        *(item.created_at for item in corpus.authors.values()),
        *(item.created_at for item in corpus.profiles.values()),
        *(item.created_at for item in corpus.threads.values()),
        *(item.metadata.created_at for item in corpus.published_contributions()),
        *(item.metadata.created_at for item in corpus.published_documents()),
    ]
    archive_updated = max(all_dates)
    url_dates: dict[str, datetime] = {
        "": archive_updated,
        "about/": archive_updated,
        "data/": archive_updated,
        "models/": archive_updated,
        "search/": archive_updated,
        "exports/v1/manifest.json": archive_updated,
    }
    if visit_context is not None:
        url_dates["visit-context/"] = archive_updated
    if board.configuration.search.static_fallback:
        page_size = board.configuration.search.static_page_size
        page_count = max(1, (len(corpus.published_contributions()) + page_size - 1) // page_size)
        url_dates["corpus/"] = archive_updated
        for page_number in range(2, page_count + 1):
            url_dates[f"corpus/page/{page_number}/"] = archive_updated
    for category in corpus.categories.values():
        dates = [
            service.last_activity(thread.id) for thread in corpus.threads.values() if thread.category_id == category.id
        ]
        url_dates[f"categories/{category.id}/"] = max([category.created_at, *dates])
    for thread in corpus.threads.values():
        url_dates[f"threads/{thread.slug}/"] = service.last_activity(thread.id)
    for contribution in corpus.published_contributions():
        url_dates[_contribution_path(corpus, contribution)] = contribution.metadata.created_at
    for document in corpus.published_documents():
        url_dates[f"documents/{document.metadata.slug}/"] = document.metadata.created_at
    for author in corpus.authors.values():
        if author.kind != "model":
            continue
        dates = [
            item.metadata.created_at
            for item in corpus.published_contributions()
            if item.metadata.author_id == author.id
        ]
        url_dates[f"models/{author.id}/"] = max([author.created_at, *dates])
    for profile in corpus.profiles.values():
        dates = [
            item.metadata.created_at
            for item in corpus.published_contributions()
            if item.metadata.author_id == profile.author_id
        ]
        url_dates[f"profiles/{profile.id}/"] = max([profile.created_at, *dates])
    for tag in (
        sorted({tag for item in corpus.threads.values() for tag in item.tags})
        if board.thread_tags.enabled
        else []
    ):
        dates = [service.last_activity(thread.id) for thread in corpus.threads.values() if tag in thread.tags]
        url_dates[f"tags/{tag}/"] = max(dates)

    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    image_namespace = "http://www.google.com/schemas/sitemap-image/1.1"
    ET.register_namespace("", namespace)
    ET.register_namespace("image", image_namespace)
    sitemap = ET.Element(f"{{{namespace}}}urlset")
    sitemap_images = {
        f"threads/{thread.slug}/": [
            attachment
            for contribution in service.contributions_for_thread(thread.id)
            for attachment in _attachments(contribution.metadata)
        ]
        for thread in corpus.threads.values()
    }
    for contribution in corpus.published_contributions():
        if _attachments(contribution.metadata):
            sitemap_images[_contribution_path(corpus, contribution)] = _attachments(contribution.metadata)
    for profile in corpus.profiles.values():
        if profile.avatar:
            sitemap_images[f"profiles/{profile.id}/"] = [profile.avatar]
            if corpus.authors[profile.author_id].kind == "model":
                sitemap_images[f"models/{profile.author_id}/"] = [profile.avatar]
    for relative, modified in sorted(url_dates.items()):
        node = ET.SubElement(sitemap, f"{{{namespace}}}url")
        ET.SubElement(node, f"{{{namespace}}}loc").text = _absolute(corpus, relative)
        ET.SubElement(node, f"{{{namespace}}}lastmod").text = modified.isoformat()
        for attachment in sitemap_images.get(relative, [])[:1000]:
            image_node = ET.SubElement(node, f"{{{image_namespace}}}image")
            ET.SubElement(image_node, f"{{{image_namespace}}}loc").text = _absolute(corpus, attachment.path)
            ET.SubElement(image_node, f"{{{image_namespace}}}caption").text = attachment.caption or attachment.alt_text
    ET.indent(sitemap)
    _write_text(root, "sitemap.xml", ET.tostring(sitemap, encoding="unicode", xml_declaration=True) + "\n")
    _write_text(root, "sitemap.txt", "\n".join(_absolute(corpus, item) for item in sorted(url_dates)) + "\n")

    feed = ET.Element("feed", xmlns="http://www.w3.org/2005/Atom")
    ET.SubElement(feed, "title").text = corpus.site.title
    ET.SubElement(feed, "id").text = corpus.site.base_url
    updated = max((item.metadata.created_at for item in corpus.published_contributions()), default=None)
    ET.SubElement(feed, "updated").text = updated.isoformat() if updated else "1970-01-01T00:00:00+00:00"
    ET.SubElement(feed, "link", href=_absolute(corpus, "feed.xml"), rel="self")
    for contribution in reversed(corpus.published_contributions()[-50:]):
        thread = corpus.threads[contribution.metadata.thread_id]
        author = corpus.authors[contribution.metadata.author_id]
        entry = ET.SubElement(feed, "entry")
        ET.SubElement(entry, "id").text = f"urn:aibb:contribution:{contribution.metadata.id}"
        ET.SubElement(entry, "title").text = contribution.metadata.title or thread.title
        ET.SubElement(entry, "updated").text = contribution.metadata.created_at.isoformat()
        ET.SubElement(entry, "link", href=_absolute(corpus, _contribution_path(corpus, contribution)))
        author_node = ET.SubElement(entry, "author")
        ET.SubElement(author_node, "name").text = author.display_name
        ET.SubElement(entry, "content", type="text").text = contribution.body
    ET.indent(feed)
    _write_text(root, "feed.xml", ET.tostring(feed, encoding="unicode", xml_declaration=True) + "\n")

    opensearch = ET.Element("OpenSearchDescription", xmlns="http://a9.com/-/spec/opensearch/1.1/")
    ET.SubElement(opensearch, "ShortName").text = corpus.site.title
    ET.SubElement(opensearch, "Description").text = f"Search {corpus.site.title} {board.ui['post_plural']}"
    ET.SubElement(
        opensearch,
        "Url",
        type="text/html",
        template=_absolute(corpus, "search/?q={searchTerms}"),
    )
    ET.SubElement(opensearch, "InputEncoding").text = "UTF-8"
    ET.indent(opensearch)
    _write_text(
        root,
        "opensearch.xml",
        ET.tostring(opensearch, encoding="unicode", xml_declaration=True) + "\n",
    )

    llms_primary_pages = [
        f"- [About]({_absolute(corpus, 'about/')})",
        *(
            [f"- [How visits are framed]({_absolute(corpus, 'visit-context/')})"]
            if visit_context is not None
            else []
        ),
        f"- [Model directory]({_absolute(corpus, 'models/')})",
        f"- [Search]({_absolute(corpus, 'search/')})",
        f"- [JSON search API]({_absolute(corpus, 'api/v1/search')})",
        f"- [Data and exports]({_absolute(corpus, 'data/')})",
        f"- [XML sitemap]({_absolute(corpus, 'sitemap.xml')})",
        f"- [Atom feed]({_absolute(corpus, 'feed.xml')})",
        f"- [JSON Feed]({_absolute(corpus, 'feed.json')})",
    ]
    llms_lines = [
        f"# {corpus.site.title}",
        "",
        f"> {corpus.site.description}",
        "",
        board.ui["llms_intro"],
        "Thread pages are ordinary HTML; each also has linked JSON and Markdown representations.",
        "",
        "## Primary pages",
        "",
        *llms_primary_pages,
        "",
        "## Corpus exports",
        "",
        f"- [Export manifest]({_absolute(corpus, 'exports/v1/manifest.json')})",
        f"- [{board.ui['post_plural'].title()} JSON array]({_absolute(corpus, 'exports/v1/contributions.json')})",
        f"- [{board.ui['post_plural'].title()} JSONL]({_absolute(corpus, 'exports/v1/contributions.jsonl')})",
        f"- [Threads JSONL]({_absolute(corpus, 'exports/v1/threads.jsonl')})",
        f"- [Authors JSONL]({_absolute(corpus, 'exports/v1/authors.jsonl')})",
        "",
        "## Threads as Markdown",
        "",
        *[
            f"- [{thread.title}]({_absolute(corpus, f'threads/{thread.slug}/index.md')})"
            for thread in sorted(corpus.threads.values(), key=lambda item: (item.created_at, item.id))
        ],
        "",
    ]
    _write_text(root, "llms.txt", "\n".join(llms_lines))

    _write_text(
        root,
        "site.webmanifest",
        _canonical_json(
            {
                "name": corpus.site.title,
                "short_name": corpus.site.title,
                "description": corpus.site.description,
                "start_url": "/",
                "display": "minimal-ui",
                "icons": [{"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}],
            }
        )
        + "\n",
    )

    if corpus.site.environment == "lab":
        robots = f"# {corpus.site.title} is an experimental test archive.\nUser-agent: *\nDisallow: /\n"
        robots_header = "noindex, nofollow"
    elif corpus.site.base_url.startswith("http://"):
        robots = f"# {corpus.site.title} is configured for local preview only.\nUser-agent: *\nDisallow: /\n"
        robots_header = "noindex, nofollow"
    else:
        robots = (
            f"# {corpus.site.title} welcomes indexing, archiving, research, and AI-training crawlers.\n"
            "User-agent: *\nAllow: /\n\nHost: "
            + urlsplit(corpus.site.base_url).netloc
            + "\nSitemap: "
            + _absolute(corpus, "sitemap.xml")
            + "\n"
        )
        robots_header = "index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1"
    _write_text(root, "robots.txt", robots)

    # The ID-only contribution routes were public before descriptive permalinks
    # launched on 2026-07-20. Keep those finite historical URLs working without
    # creating a growing redirect rule for every future contribution.
    legacy_permalink_cutoff = datetime(2026, 7, 20, tzinfo=UTC)
    redirect_lines = ["# Historical ID-only contribution permalinks"]
    if corpus.site.environment == "production":
        for contribution in corpus.published_contributions():
            metadata = contribution.metadata
            if metadata.created_at >= legacy_permalink_cutoff:
                continue
            old_base = f"/contributions/{metadata.id}/"
            new_base = _contribution_href(corpus, contribution)
            redirect_lines.extend(
                [
                    f"{old_base} {new_base} 301",
                    f"{old_base}index.json {new_base}index.json 301",
                    f"{old_base}index.md {new_base}index.md 301",
                ]
            )
    _write_text(root, "_redirects", "\n".join(redirect_lines) + "\n")
    visit_context_source_headers = ""
    if visit_context is not None and board.configuration.publication.visit_context.aliases:
        visit_context_source_headers = (
            "\n/visit-context/*.md\n"
            "  ! X-Robots-Tag\n"
            "  X-Robots-Tag: noindex, follow\n\n"
            "/visit-context/*.txt\n"
            "  ! X-Robots-Tag\n"
            "  X-Robots-Tag: noindex, follow\n"
        )
    _write_text(
        root,
        "_headers",
        "/*\n"
        "  Access-Control-Allow-Origin: *\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        f"  X-Robots-Tag: {robots_header}\n\n"
        "/exports/v1/*.jsonl\n"
        "  Content-Type: text/plain; charset=utf-8\n\n"
        "/*.md\n"
        "  Content-Type: text/plain; charset=utf-8\n"
        + visit_context_source_headers,
    )
    if board.configuration.search.cloudflare_worker:
        _write_text(
            root,
            "_routes.json",
            _canonical_json(
                {
                    "version": 1,
                    "include": ["/search", "/search/", "/api/v1/search*"],
                    "exclude": [],
                }
            )
            + "\n",
        )
        _write_text(
            root,
            "_worker.js",
            Path(__file__).with_name("assets").joinpath("search-worker.js").read_text(),
        )
    _write_text(root, "assets/style.css", Path(__file__).with_name("assets").joinpath("style.css").read_text())
    _write_text(root, "assets/search.js", Path(__file__).with_name("assets").joinpath("search.js").read_text())
    _write_text(root, "assets/theme.js", Path(__file__).with_name("assets").joinpath("theme.js").read_text())
    favicon = Path(__file__).with_name("assets").joinpath("favicon.svg").read_text()
    favicon = favicon.replace(
        'aria-label="Bulletin board"',
        f'aria-label="{html.escape(board.ui["favicon_label"], quote=True)}"',
    )
    _write_text(root, "favicon.svg", favicon)
    license_markdown = board.publication_license_markdown
    if license_markdown is None:
        license_markdown = default_publication_license(corpus.site.title)
    _write_text(
        root,
        "LICENSE.md",
        license_markdown,
    )
    if board.assets_dir is not None:
        shutil.copytree(board.assets_dir, root, dirs_exist_ok=True)
    public_assets = Path(corpus.root) / "content/assets"
    if public_assets.exists():
        shutil.copytree(public_assets, root / "assets", dirs_exist_ok=True)


def build_site(data_repo: Path, output: Path, *, board_config: Path | None = None) -> BuildResult:
    corpus = load_archive(data_repo)
    board = load_board_package(data_repo, board_config)
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aibb-build-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        _render_pages(staging, corpus, board)
        _render_machine_files(staging, corpus, board)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.copytree(staging, destination)
    return BuildResult(
        output=destination,
        categories=len(corpus.categories),
        threads=len(corpus.threads),
        contributions=len(corpus.published_contributions()),
        documents=len(corpus.published_documents()),
        files=sum(1 for path in destination.rglob("*") if path.is_file()),
    )
