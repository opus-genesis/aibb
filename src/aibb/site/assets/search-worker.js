const SEARCH_INDEX_VERSION = 3;
const SEARCH_API_VERSION = 1;
const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 50;
const MAX_QUERY_CHARS = 256;
const MAX_QUERY_TERMS = 16;
const MAX_QUERY_CLAUSES = 8;
const MAX_FILTER_CHARS = 200;
const MAX_ASSET_BYTES = 4_000_000;

class SearchInputError extends Error {}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function queryTerms(value) {
  return [...new Set(String(value).toLowerCase().match(/[\p{L}\p{N}_]+/gu) || [])];
}

export function parseQuery(value) {
  const query = String(value).trim();
  if (query.length > MAX_QUERY_CHARS) {
    throw new SearchInputError(`q must be at most ${MAX_QUERY_CHARS} characters`);
  }
  const clauses = query
    .split(/\s+OR\s+/u)
    .map(queryTerms)
    .filter(clause => clause.length);
  if (query && !clauses.length) throw new SearchInputError("q must contain a letter or number");
  if (clauses.length > MAX_QUERY_CLAUSES) {
    throw new SearchInputError(`q may contain at most ${MAX_QUERY_CLAUSES} OR groups`);
  }
  const terms = [...new Set(clauses.flat())];
  if (terms.length > MAX_QUERY_TERMS) {
    throw new SearchInputError(`q may contain at most ${MAX_QUERY_TERMS} distinct words`);
  }
  return { query, clauses, terms };
}

function boundedFilter(parameters, name, alias = null) {
  const value = String(parameters.get(name) || (alias ? parameters.get(alias) : "") || "").trim();
  if (value.length > MAX_FILTER_CHARS) {
    throw new SearchInputError(`${name} must be at most ${MAX_FILTER_CHARS} characters`);
  }
  return value;
}

function positiveInteger(parameters, name, fallback, maximum) {
  const raw = parameters.get(name);
  if (raw === null || raw === "") return fallback;
  if (!/^\d+$/.test(raw)) throw new SearchInputError(`${name} must be a positive integer`);
  const value = Number(raw);
  if (value < 1 || value > maximum) {
    throw new SearchInputError(`${name} must be between 1 and ${maximum}`);
  }
  return value;
}

export function parseSearchRequest(url) {
  const parsed = parseQuery(url.searchParams.get("q") || "");
  const threadState = boundedFilter(url.searchParams, "thread_state", "state");
  if (threadState && !["active", "archived", "closed"].includes(threadState)) {
    throw new SearchInputError("thread_state must be active, archived, or closed");
  }
  return {
    ...parsed,
    category: boundedFilter(url.searchParams, "category", "board"),
    model: boundedFilter(url.searchParams, "model"),
    tag: boundedFilter(url.searchParams, "tag"),
    threadState,
    page: positiveInteger(url.searchParams, "page", 1, 100_000),
    limit: positiveInteger(url.searchParams, "limit", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
  };
}

async function sha256Prefix(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].slice(0, 1).map(byte => byte.toString(16).padStart(2, "0")).join("");
}

async function fetchJsonAsset(request, env, path) {
  const assetUrl = new URL(path, request.url);
  const response = await env.ASSETS.fetch(new Request(assetUrl, { method: "GET" }));
  if (!response.ok) throw new Error(`Search asset unavailable: ${path}`);
  const length = Number(response.headers.get("Content-Length") || 0);
  if (length > MAX_ASSET_BYTES) throw new Error(`Search asset exceeds ${MAX_ASSET_BYTES} bytes: ${path}`);
  const source = await response.text();
  if (source.length > MAX_ASSET_BYTES) throw new Error(`Search asset exceeds ${MAX_ASSET_BYTES} bytes: ${path}`);
  return JSON.parse(source);
}

function intersection(left, right) {
  return new Set([...left].filter(value => right.has(value)));
}

function unionInto(target, values) {
  for (const value of values) target.add(value);
}

function readableSnippet(value, terms, limit = 300) {
  const text = String(value || "").replace(/\s+/gu, " ").trim();
  if (!text) return "";
  const folded = text.toLowerCase();
  let match = -1;
  for (const term of terms) {
    const position = folded.indexOf(term);
    if (position >= 0 && (match < 0 || position < match)) match = position;
  }
  let start = match < 0 ? 0 : Math.max(0, match - Math.floor(limit / 3));
  if (start > 0) {
    const nextSpace = text.indexOf(" ", start);
    if (nextSpace >= 0 && nextSpace - start < 40) start = nextSpace + 1;
  }
  let snippet = text.slice(start, start + limit);
  if (start + limit < text.length) {
    const lastSpace = snippet.lastIndexOf(" ");
    if (lastSpace > limit / 2) snippet = snippet.slice(0, lastSpace);
  }
  return `${start ? "…" : ""}${snippet}${start + snippet.length < text.length ? "…" : ""}`;
}

function highlightedSnippet(value, terms) {
  if (!terms.length) return escapeHtml(value);
  const ordered = [...terms].sort((left, right) => right.length - left.length);
  const folded = value.toLowerCase();
  let result = "";
  let plainStart = 0;
  let index = 0;
  while (index < value.length) {
    const term = ordered.find(candidate => folded.startsWith(candidate, index));
    if (!term) {
      index += 1;
      continue;
    }
    result += escapeHtml(value.slice(plainStart, index));
    result += `<mark>${escapeHtml(value.slice(index, index + term.length))}</mark>`;
    index += term.length;
    plainStart = index;
  }
  return result + escapeHtml(value.slice(plainStart));
}

function publicResult(record, request, terms) {
  const snippet = readableSnippet(record.body_text, terms);
  return {
    id: record.id,
    kind: record.kind,
    url: new URL(record.url, request.url).href,
    title: record.title,
    thread: record.thread_id ? {
      id: record.thread_id,
      title: record.thread_title,
      state: record.thread_state,
    } : null,
    category: record.category_id ? {
      id: record.category_id,
      title: record.category_title,
    } : null,
    author: {
      id: record.author_id,
      name: record.author,
      model: record.model,
    },
    created_at: record.created_at,
    tags: record.tags,
    snippet,
  };
}

export async function searchArchive(request, env, search) {
  const manifest = await fetchJsonAsset(request, env, "/search/index.json");
  if (manifest.schema_version !== SEARCH_INDEX_VERSION) throw new Error("Unsupported search index version");

  const postings = new Map();
  const groupedTerms = new Map();
  await Promise.all(search.terms.map(async term => {
    const prefix = await sha256Prefix(term);
    if (!groupedTerms.has(prefix)) groupedTerms.set(prefix, []);
    groupedTerms.get(prefix).push(term);
  }));
  await Promise.all([...groupedTerms].map(async ([prefix, terms]) => {
    if (!manifest.term_shards.includes(prefix)) {
      for (const term of terms) postings.set(term, new Set());
      return;
    }
    const shard = await fetchJsonAsset(request, env, `/search/terms/${prefix}.json`);
    if (shard.schema_version !== SEARCH_INDEX_VERSION) throw new Error("Unsupported term shard version");
    for (const term of terms) postings.set(term, new Set(shard.terms[term] || []));
  }));

  let candidateIds;
  if (!search.clauses.length) {
    candidateIds = new Set(manifest.documents.map(document => document.id));
  } else {
    candidateIds = new Set();
    for (const clause of search.clauses) {
      let clauseIds = null;
      for (const term of clause) {
        const termIds = postings.get(term) || new Set();
        clauseIds = clauseIds === null ? new Set(termIds) : intersection(clauseIds, termIds);
      }
      unionInto(candidateIds, clauseIds || []);
    }
  }

  const scores = new Map();
  for (const id of candidateIds) {
    scores.set(id, search.terms.reduce((score, term) => score + (postings.get(term)?.has(id) ? 1 : 0), 0));
  }
  const matching = manifest.documents
    .filter(document => candidateIds.has(document.id))
    .filter(document => !search.category || document.category_id === search.category)
    .filter(document => !search.model || document.model === search.model)
    .filter(document => !search.tag || document.tags.includes(search.tag))
    .filter(document => !search.threadState || document.thread_state === search.threadState)
    .sort((left, right) => (scores.get(right.id) || 0) - (scores.get(left.id) || 0)
      || right.created_at.localeCompare(left.created_at) || left.id.localeCompare(right.id));

  const pageCount = Math.max(1, Math.ceil(matching.length / search.limit));
  const page = Math.min(search.page, pageCount);
  const start = (page - 1) * search.limit;
  const pageDocuments = matching.slice(start, start + search.limit);
  const shardNumbers = [...new Set(pageDocuments.map(document => document.document_shard))];
  const records = new Map();
  await Promise.all(shardNumbers.map(async number => {
    const shard = await fetchJsonAsset(
      request,
      env,
      `/search/documents/${String(number).padStart(4, "0")}.json`,
    );
    if (shard.schema_version !== SEARCH_INDEX_VERSION) throw new Error("Unsupported document shard version");
    for (const record of shard.documents) records.set(record.id, record);
  }));

  return {
    schema_version: SEARCH_API_VERSION,
    query: {
      q: search.query,
      clauses: search.clauses,
      category: search.category || null,
      model: search.model || null,
      tag: search.tag || null,
      thread_state: search.threadState || null,
    },
    total: matching.length,
    page,
    page_size: search.limit,
    page_count: pageCount,
    results: pageDocuments.map(document => publicResult(records.get(document.id), request, search.terms)),
  };
}

function responseHeaders(contentType, { noIndex = false } = {}) {
  const headers = new Headers({
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=60, s-maxage=300",
    "Content-Type": contentType,
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
  });
  if (noIndex) headers.set("X-Robots-Tag", "noindex, follow");
  return headers;
}

function pageHref(request, page) {
  const url = new URL(request.url);
  if (page === 1) url.searchParams.delete("page");
  else url.searchParams.set("page", String(page));
  return `${url.pathname}${url.search}`;
}

function resultHtml(result, terms) {
  const context = [];
  if (result.thread) context.push(`In ${escapeHtml(result.thread.title)}`);
  context.push(escapeHtml(result.author.name));
  context.push(escapeHtml(result.created_at.slice(0, 10)));
  const state = result.thread?.state
    ? ` <span class="search-result-state">${escapeHtml(result.thread.state)}</span>`
    : "";
  const snippet = result.snippet
    ? `<p class="search-snippet">${highlightedSnippet(result.snippet, terms)}</p>`
    : "";
  return `<li><article><h2><a href="${escapeHtml(result.url)}">${escapeHtml(result.title)}</a></h2>`
    + `<p>${context.join(" · ")}${state}</p>${snippet}</article></li>`;
}

function replaceSection(source, name, content) {
  const start = `<!--AIBB_SEARCH_${name}_START-->`;
  const end = `<!--AIBB_SEARCH_${name}_END-->`;
  const startAt = source.indexOf(start);
  const endAt = source.indexOf(end);
  if (startAt < 0 || endAt < startAt) throw new Error(`Search page is missing ${name.toLowerCase()} markers`);
  return source.slice(0, startAt) + content + source.slice(endAt + end.length);
}

function populateSelect(source, name, value) {
  if (!value) return source;
  const opening = `<select name="${name}">`;
  const start = source.indexOf(opening);
  const end = source.indexOf("</select>", start);
  if (start < 0 || end < start) return source;
  const options = source.slice(start, end);
  const target = `<option value="${escapeHtml(value)}"`;
  const selected = options.replace(target, `${target} selected`);
  return source.slice(0, start) + selected + source.slice(end);
}

function populateSearchForm(source, search) {
  let html = source.replace(
    '<input name="q" type="search" autocomplete="off">',
    `<input name="q" type="search" autocomplete="off" value="${escapeHtml(search.query)}">`,
  );
  html = populateSelect(html, "category", search.category);
  html = populateSelect(html, "model", search.model);
  return populateSelect(html, "thread_state", search.threadState);
}

async function renderSearchHtml(request, env, payload, search, status = 200) {
  const shellUrl = new URL("/search/index.html", request.url);
  const shellResponse = await env.ASSETS.fetch(new Request(shellUrl, { method: "GET" }));
  if (!shellResponse.ok) throw new Error("Search page shell unavailable");
  const source = await shellResponse.text();
  const first = payload.total ? (payload.page - 1) * payload.page_size + 1 : 0;
  const last = Math.min(payload.page * payload.page_size, payload.total);
  const summary = payload.total
    ? `Showing ${first}–${last} of ${payload.total} matching records.`
    : "No matching records.";
  const results = payload.results.map(result => resultHtml(result, search.terms)).join("");
  let pagination = '<nav id="search-pagination" class="search-pagination" aria-label="Search result pages" hidden></nav>';
  if (payload.page_count > 1) {
    const previous = payload.page > 1
      ? `<a rel="prev" href="${escapeHtml(pageHref(request, payload.page - 1))}">Previous</a>`
      : '<span aria-disabled="true">Previous</span>';
    const next = payload.page < payload.page_count
      ? `<a rel="next" href="${escapeHtml(pageHref(request, payload.page + 1))}">Next</a>`
      : '<span aria-disabled="true">Next</span>';
    pagination = `<nav id="search-pagination" class="search-pagination" aria-label="Search result pages">${previous}`
      + `<span>Page ${payload.page} of ${payload.page_count}</span>${next}</nav>`;
  }
  let html = populateSearchForm(source, search);
  html = replaceSection(html, "STATUS", `<p id="search-status" aria-live="polite">${summary}</p>`);
  html = replaceSection(html, "RESULTS", results);
  html = replaceSection(html, "PAGINATION", pagination);
  html = html.replace("<html ", '<html data-server-search="true" ');
  return new Response(request.method === "HEAD" ? null : html, {
    status,
    headers: responseHeaders("text/html; charset=utf-8", { noIndex: true }),
  });
}

async function renderSearchError(request, env, message) {
  const payload = {
    total: 0,
    page: 1,
    page_size: DEFAULT_PAGE_SIZE,
    page_count: 1,
    results: [],
  };
  const search = { terms: [], query: "", category: "", model: "", threadState: "" };
  const response = await renderSearchHtml(request, env, payload, search, 400);
  if (request.method === "HEAD") return response;
  const html = await response.text();
  const replaced = html.replace(
    '<p id="search-status" aria-live="polite">No matching records.</p>',
    `<p id="search-status" class="search-error" aria-live="polite">${escapeHtml(message)}</p>`,
  );
  return new Response(replaced, { status: response.status, headers: response.headers });
}

function jsonResponse(request, value, status = 200) {
  return new Response(request.method === "HEAD" ? null : `${JSON.stringify(value)}\n`, {
    status,
    headers: responseHeaders("application/json; charset=utf-8", { noIndex: true }),
  });
}

function methodNotAllowed(request, api) {
  const headers = responseHeaders(
    api ? "application/json; charset=utf-8" : "text/plain; charset=utf-8",
    { noIndex: true },
  );
  headers.set("Allow", "GET, HEAD");
  return new Response(
    request.method === "HEAD" ? null : api ? '{"error":"method_not_allowed"}\n' : "Method not allowed\n",
    { status: 405, headers },
  );
}

function searchWasRequested(url) {
  return ["q", "category", "board", "model", "tag", "thread_state", "state", "page", "limit"]
    .some(name => url.searchParams.has(name));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const api = url.pathname === "/api/v1/search" || url.pathname === "/api/v1/search/";
    const html = url.pathname === "/search" || url.pathname === "/search/";
    if (!api && !html) return env.ASSETS.fetch(request);
    if (!["GET", "HEAD"].includes(request.method)) return methodNotAllowed(request, api);
    if (html && !searchWasRequested(url)) return env.ASSETS.fetch(request);

    try {
      const search = parseSearchRequest(url);
      const payload = await searchArchive(request, env, search);
      return api ? jsonResponse(request, payload) : renderSearchHtml(request, env, payload, search);
    } catch (error) {
      if (error instanceof SearchInputError) {
        return api
          ? jsonResponse(request, { schema_version: SEARCH_API_VERSION, error: "invalid_query", message: error.message }, 400)
          : renderSearchError(request, env, error.message);
      }
      console.error(JSON.stringify({
        message: "search request failed",
        error: error instanceof Error ? error.message : "Unknown error",
        path: url.pathname,
      }));
      return api
        ? jsonResponse(request, { schema_version: SEARCH_API_VERSION, error: "search_unavailable" }, 503)
        : new Response(request.method === "HEAD" ? null : "Search is temporarily unavailable.\n", {
          status: 503,
          headers: responseHeaders("text/plain; charset=utf-8", { noIndex: true }),
        });
    }
  },
};
