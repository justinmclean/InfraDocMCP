"""FastMCP tool definitions for the Infra Docs MCP server.

Tools to list, get, search and refresh the corpus, plus code examples,
related docs, a documentation map and link checking, over a chunked BM25
search of the Pelican markdown pages behind infra.apache.org.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from math import log
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from infra_doc_mcp import corpus, cwiki

mcp = FastMCP("infra-docs")

# Terms that appear on nearly every page discriminate nothing, so they are
# dropped before scoring. That leaves the plainest questions ("what is Infra")
# with no terms at all, which _overview_key handles.
STOP_WORDS = {
    "a", "an", "and", "apache", "are", "as", "asf", "can", "do", "does", "for",
    "how", "i", "in", "infra", "infrastructure", "into", "is", "it", "my", "of",
    "on", "or", "teh", "the", "to", "use", "using", "we", "what", "when",
    "where", "which", "who", "why", "with",
}

# Pages that answer "what is this", in preference order, for a query that
# consists only of stop words.
OVERVIEW_KEYS = ("services", "index", "doc", "contact")

# Pages listed per topic in the short form of explain_doc_map.
MAP_SAMPLE_SIZE = 4

AUDIENCE_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    "new-committer": (
        "new-committers-guide",
        "account-mgmt",
        "committer-email",
        "git-primer",
        "mfa",
        "contact",
    ),
    "release-manager": (
        "release-publishing",
        "release-signing",
        "release-distribution",
        "openpgp",
        "release-download-pages",
        "publishing-maven-artifacts",
        "atr",
    ),
    "pmc": (
        "managing-committers",
        "asf-yaml",
        "asfyaml/readme",
        "project-repo-policy",
        "project-site-policy",
        "github-actions-policy",
        "request-bug-tracker",
        "mailing-list-moderation",
    ),
    "website-maintainer": (
        "project-site",
        "asfyaml/readme",
        "website-guidelines",
        "asf-pelican",
        "asf-pelican-gettingstarted",
        "csp",
        "matomo",
    ),
    "ci-maintainer": (
        "github-actions-policy",
        "github-actions-secrets",
        "asfyaml/readme",
        "self-hosted-runners",
        "build-supported-services",
        "docker-hub-policy",
    ),
}

# Most pages sit flat in content/pages, so the directory says nothing about
# what a page covers. Topics group them by key prefix instead. Order matters:
# the first match wins, so a narrow prefix (github-pages, committer-email)
# must come before a broad one that would also catch it (github-, committer).
TOPICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("websites", "Project websites", (
        "asf-pelican", "pelican-buildbot", "project-site", "website-", "csp", "tools/csp",
        "matomo", "gfm", "project-blogs", "localization", "github-pages",
        "release-download-pages", "reference/committer/website",
    )),
    ("mail", "Mail and communication", (
        "mail", "email_lists", "committer-email", "contrib-email", "infra-mail", "spam",
        "slack", "qbot", "asfbot", "pypubsub", "content-moderation",
        "reference/committer/email",
    )),
    ("issues", "Issue tracking and wiki", (
        "jira", "bug-writing", "request-bug-tracker", "cwiki",
    )),
    ("builds", "Builds and CI", (
        "build-supported", "docker-hub", "gradle", "hosting-external-agent",
        "github-actions", "self-hosted-runners", "dependabot", "nightlies",
        "reference/committer/jenkins-docker",
    )),
    ("hosting", "VMs and hosting", (
        "vm-", "os-upgrade", "app-upgrade", "backup-policy", "third-party-policy",
    )),
    ("vcs", "Git, GitHub and Subversion", (
        "github-", "git", "apache-github", "asf-yaml", "asfyaml", "version-control", "svn",
        "open-access-svn", "repository-faq", "project-repo-policy", "patch", "how-to-mirror",
        "reference/committer/git", "reference/committer/github",
        "reference/committer/subversion", "reference/committer/svngit2jira",
    )),
    ("releases", "Releases and signing", (
        "release-", "openpgp", "key-transition", "code-signing", "digicert", "nexus101",
        "publishing-maven", "mirrors", "crypto", "licensing-howto", "atr",
        "reference/committer/release",
    )),
    ("accounts", "Accounts and security", (
        "account-mgmt", "mfa", "password-policy", "phishing", "user-ssh", "sensitive_info",
        "infra-ban", "apple-dev-program", "managing-committers", "new-committers-guide",
        "committer", "contributors", "reference/committer/id",
        "reference/committer/newaccount", "reference/committer/opie",
    )),
    ("about", "About Infra", (
        "infra-", "contact", "team", "responsetime", "services", "policies", "stats",
        "roundtable", "present", "doc", "glossary", "index", "surveys/", "tools/",
        "reference/", "retired", "understanding-opensource", "user-doc", "project-names",
        "asfquart",
    )),
)

_TOPIC_LABELS = {slug: label for slug, label, _ in TOPICS}
_TOPIC_LABELS["wiki"] = "Infra wiki (cwiki, login only)"

SOURCE_CHOICES = ("all", "website", "asfyaml", "cwiki")
SOURCE_LABELS = {"website": "infra.apache.org", "asfyaml": ".asf.yaml reference", "cwiki": "Infra wiki"}


def _source_of(doc: dict[str, Any]) -> str:
    return str(doc.get("source") or "website")


def _source_label(doc: dict[str, Any]) -> str:
    """Label a result with its source; wiki pages carry their last edit date,
    because the space holds pages untouched for a decade."""
    label = SOURCE_LABELS.get(_source_of(doc), "infra.apache.org")
    if doc.get("updated"):
        label += f", updated {doc['updated']}"
    stale = _stale_label(doc)
    if stale:
        label += f", {stale}"
    return label


def _filter_by_source(
    docs: dict[str, dict[str, Any]], source: str
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return ``(filtered_docs, error)``. An unknown source is an error, not a silent all."""
    if source in ("", "all"):
        return docs, None
    if source not in SOURCE_CHOICES:
        listed = ", ".join(f"'{choice}'" for choice in SOURCE_CHOICES)
        return docs, f"Unknown source {source!r}; choose one of {listed}."
    return {k: d for k, d in docs.items() if _source_of(d) == source}, None

_RAW_MD_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_RAW_HTML_LINK = re.compile(r"<a\b[^>]*\bhref=\"([^\"]*)\"[^>]*>(.*?)</a>", re.I)
_EXPLICIT_ANCHOR = re.compile(r"\b(?:id|name)=\"([^\"]+)\"")
_HEADING_ATTR_ID = re.compile(r"\{#([^}\s]+)\}")
_ANCHOR_TEXT = re.compile(r"[^a-z0-9 _-]")
_FENCE_START = re.compile(r"^(?:```|~~~)\s*([A-Za-z0-9_+#.-]*)\s*$")
_FENCE = re.compile(r"^(?:```|~~~)")

# Vocabulary bridges between how people ask and how the pages are written.
# Grown from real questions; keep additions honest and small.
TOKEN_VARIANTS: dict[str, set[str]] = {
    "2fa": {"mfa"},
    "actions": {"github"},
    "ci": {"build", "actions", "jenkins"},
    "dist": {"distribution", "svn"},
    "download": {"distribution", "mirrors"},
    "gpg": {"openpgp", "signing", "keys"},
    "jenkins": {"build", "ci"},
    "key": {"openpgp", "signing"},
    "ldap": {"account"},
    "list": {"mailing"},
    "lists": {"mailing"},
    "maven": {"nexus", "repository"},
    "moderate": {"moderation", "moderator"},
    "password": {"account", "reset"},
    "pgp": {"openpgp", "signing"},
    "pelican": {"website"},
    "repo": {"repository"},
    "runner": {"actions", "github"},
    "secret": {"secrets"},
    "sha512": {"checksum"},
    "sign": {"signing", "signature"},
    "site": {"website"},
    "ssh": {"key"},
    "subversion": {"svn"},
    "ticket": {"jira"},
    "totp": {"mfa"},
    "two": {"mfa"},
    "vote": {"release"},
    "webpage": {"website"},
    "wiki": {"cwiki", "confluence"},
    "yaml": {"asf"},
}

# Doc-key hints: a query token that names a page's subject boosts that page.
# Keep each set small and distinctive. A token that would fit three pages
# belongs in none of them; let BM25 decide those.
SOURCE_HINTS: dict[str, set[str]] = {
    "account-mgmt": {"account", "ldap", "username"},
    "asf-yaml": {"asf", "yaml", "asfyaml", "notifications", "staging"},
    "asf-pelican": {"pelican"},
    "atr": {"atr", "trusted"},
    "committer-email": {"forwarding", "alias"},
    "cwiki": {"cwiki", "confluence", "wiki"},
    "docker-hub-policy": {"docker", "dockerhub"},
    "github-actions-policy": {"actions", "workflow", "allowlist"},
    "github-actions-secrets": {"secrets", "secret"},
    "jira-guidelines": {"jira"},
    "mailing-list-moderation": {"moderation", "moderator", "moderate"},
    "matomo": {"matomo", "analytics"},
    "mfa": {"mfa", "2fa", "totp"},
    "openpgp": {"openpgp", "pgp", "gpg"},
    "publishing-maven-artifacts": {"maven", "nexus", "staging"},
    "release-distribution": {"distribution", "dist", "mirrors", "archive"},
    "release-publishing": {"release", "vote"},
    "release-signing": {"signing", "signature", "checksum", "sha512"},
    "self-hosted-runners": {"runner", "runners"},
    "slack": {"slack"},
    "vm-for-project": {"vm", "virtual"},
    "contact": {"contact", "help", "support"},
}


# Notices a page carries in its opening when it should no longer be relied
# on. Neither source has a structured marker: the wiki has no pages in
# Confluence's archived status and no archive subtree, so the page text is the
# only signal. The patterns are deliberately narrow, because most pages that
# mention "archive" are about mailing-list archives, not about being one.
STALE_NOTICES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("archived", re.compile(r"\b(?:this|the) (?:page|document) (?:is|has been) archived\b", re.I)),
    ("superseded", re.compile(r"\b(?:has been|is) superseded by\b", re.I)),
    ("outdated", re.compile(
        r"\bcontains outdated information\b"
        r"|\b(?:this|the) (?:page|document) is (?:outdated|out of date|obsolete|deprecated)\b",
        re.I,
    )),
)
# How far into a page a notice may sit and still describe the whole page.
STALE_NOTICE_WINDOW = 600
# A stale page still answers questions nothing else covers, so it is demoted
# rather than dropped: current pages win ties, and it is still found alone.
# Archived and superseded pages have a replacement, so they drop further than
# an outdated page, which is often still the only material on its subject.
STALE_WEIGHTS = {"archived": 0.4, "superseded": 0.4, "outdated": 0.75}
_URL = re.compile(r"https?://[^\s)\]>,]+")


def _stale_notice(doc: dict[str, Any]) -> dict[str, str] | None:
    """Return ``{"kind", "replacement"}`` when a page says it is stale, else None.

    replacement is the first URL on the notice line, which is where pages
    name the page that replaced them; it is empty when the notice names none.
    """
    opening = str(doc.get("text", ""))[:STALE_NOTICE_WINDOW]
    for kind, pattern in STALE_NOTICES:
        match = pattern.search(opening)
        if match:
            line_start = opening.rfind("\n", 0, match.start()) + 1
            line_end = opening.find("\n", match.end())
            line = opening[line_start:line_end if line_end != -1 else None]
            url = _URL.search(line)
            return {"kind": kind, "replacement": url.group(0).rstrip(".") if url else ""}
    return None


def _stale_label(doc: dict[str, Any]) -> str:
    notice = _stale_notice(doc)
    if notice is None:
        return ""
    label = notice["kind"].upper()
    if notice["replacement"]:
        label += f", see {notice['replacement']}"
    return label


def _term_forms(term: str) -> set[str]:
    """Return simple singular/plural forms for a normalized English term."""
    forms = {term}
    if len(term) <= 2 or not term.isalpha():
        return forms
    if term.endswith("ies") and len(term) > 4:
        forms.add(f"{term[:-3]}y")
    elif term.endswith(("ches", "shes", "sses", "xes", "zes")) and len(term) > 4:
        forms.add(term[:-2])
    elif term.endswith("s") and not term.endswith("ss"):
        forms.add(term[:-1])
    else:
        if term.endswith("y") and len(term) > 3 and term[-2] not in "aeiou":
            forms.add(f"{term[:-1]}ies")
        elif term.endswith(("ch", "sh", "ss", "x", "z")):
            forms.add(f"{term}es")
        else:
            forms.add(f"{term}s")
    return forms


def _tokenize_terms(text: str) -> list[str]:
    """Return normalized search terms, preserving duplicates for BM25 scoring."""
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        if token in STOP_WORDS:
            continue
        tokens.extend(sorted(_term_forms(token)))
        variants: set[str] = set(TOKEN_VARIANTS.get(token, set()))
        for form in _term_forms(token):
            variants.update(TOKEN_VARIANTS.get(form, set()))
        for variant in sorted(variants):
            tokens.extend(sorted(_term_forms(variant)))
    return tokens


def _nearest_heading(lines: list[str], line_num: int) -> str | None:
    """Return the last heading at or before *line_num*, ignoring code fences.

    Fence-aware because a ``#`` comment in a YAML or shell example is not a
    heading: without this, the .asf.yaml branch protection example was
    labelled "strict means ..." after a comment inside it.
    """
    heading: str | None = None
    in_fence = False
    for line in lines[:line_num + 1]:
        stripped = line.strip()
        if _FENCE.match(stripped):
            in_fence = not in_fence
        elif not in_fence and stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
    return heading


def _doc_chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one doc into heading-aware chunks that never break a code fence."""
    lines = str(doc["text"]).split("\n")
    chunks: list[dict[str, Any]] = []
    max_lines = 18
    overlap = 3
    start = 0
    in_fence = False
    notice = _stale_notice(doc)
    weight = STALE_WEIGHTS[notice["kind"]] if notice else 1.0

    def add_chunk(chunk_start: int, chunk_end: int) -> None:
        excerpt = "\n".join(lines[chunk_start:chunk_end]).strip()
        if not excerpt:
            return
        body = [
            line for line in excerpt.split("\n")
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not body:
            return
        heading = _nearest_heading(lines, chunk_start)
        source_text = f"{doc['key']} {doc['title']} {heading or ''}"
        chunks.append({
            "key": doc["key"],
            "title": doc["title"],
            "url": doc["url"],
            "line": chunk_start,
            "heading": heading,
            "excerpt": excerpt,
            "terms": _tokenize_terms(f"{source_text} {source_text} {excerpt}"),
            "source_terms": set(_tokenize_terms(source_text)),
            "weight": weight,
        })

    for i, line in enumerate(lines):
        stripped = line.strip()
        if _FENCE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        is_heading = stripped.startswith("#")
        if i > start and (is_heading or i - start >= max_lines):
            add_chunk(start, i)
            start = i if is_heading else max(i - overlap, 0)
    add_chunk(start, len(lines))
    return chunks


def _code_snippets(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract fenced code blocks from one converted markdown document."""
    lines = str(doc["text"]).split("\n")
    snippets: list[dict[str, Any]] = []
    in_fence = False
    language = ""
    start_line = 0
    body: list[str] = []
    heading: str | None = None
    for i, line in enumerate(lines):
        match = _FENCE_START.match(line.strip())
        if match and not in_fence:
            in_fence = True
            language = match.group(1).strip().lower()
            start_line = i + 1
            body = []
            heading = _nearest_heading(lines, i)
            continue
        if _FENCE.match(line.strip()) and in_fence:
            code = "\n".join(body).strip("\n")
            if code:
                snippets.append(
                    {
                        "key": doc["key"],
                        "title": doc["title"],
                        "url": doc["url"],
                        "heading": heading,
                        "language": language or "text",
                        "line": start_line,
                        "code": code,
                    }
                )
            in_fence = False
            continue
        if in_fence:
            body.append(line)
    return snippets


def _all_code_snippets(docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    for doc in docs.values():
        snippets.extend(_code_snippets(doc))
    return snippets


def _bm25_scores(chunks: list[dict[str, Any]], query_terms: list[str]) -> list[dict[str, Any]]:
    """Score chunks with a compact BM25 implementation plus doc-key hints."""
    if not chunks:
        return []
    doc_freq: Counter[str] = Counter()
    term_counts: list[Counter[str]] = []
    doc_lengths: list[int] = []
    for chunk in chunks:
        counts = Counter(chunk["terms"])
        term_counts.append(counts)
        doc_lengths.append(sum(counts.values()))
        doc_freq.update(counts.keys())

    total = len(chunks)
    avg_len = sum(doc_lengths) / total if total else 0.0
    query_counts = Counter(query_terms)
    query_words = set(query_terms)
    k1, b = 1.5, 0.75

    scored: list[dict[str, Any]] = []
    for chunk, counts, doc_len in zip(chunks, term_counts, doc_lengths):
        score = 0.0
        for term, query_count in query_counts.items():
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = log(1 + (total - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            denom = freq + k1 * (1 - b + b * (doc_len / avg_len if avg_len else 0.0))
            score += query_count * idf * (freq * (k1 + 1) / denom)
        score += len(query_words & SOURCE_HINTS.get(str(chunk["key"]), set())) * 6.0
        score += len(query_words & chunk["source_terms"]) * 0.4
        score *= chunk.get("weight", 1.0)
        if score:
            result = dict(chunk)
            result["score"] = score
            scored.append(result)
    return scored


def _overview_key(docs: dict[str, dict[str, Any]]) -> str:
    """Return the best page to answer a question with no distinguishing terms."""
    for key in OVERVIEW_KEYS:
        if key in docs:
            return key
    return sorted(docs)[0]


def _section_for(key: str) -> str:
    """Return the topic slug for a doc key, by the first matching prefix."""
    if key.startswith("wiki/"):
        return "wiki"
    for slug, _, prefixes in TOPICS:
        if key.startswith(prefixes):
            return slug
    return "other"


def _section_label(section: str) -> str:
    return _TOPIC_LABELS.get(section, section.replace("_", " ").title())


def _slug(text: str) -> str:
    """Return the heading slug Pelican's toc plugin generates for a heading."""
    slug = _ANCHOR_TEXT.sub("", text.strip().lower())
    return re.sub(r"\s+", "-", slug).strip("-")


def _anchors(raw: str) -> set[str]:
    """Return every anchor a page defines: heading slugs plus explicit ids.

    The pages mix markdown headings, ``{#id}`` heading attributes and inline
    HTML with ``id=`` or ``name=``, and links target all three.
    """
    anchors: set[str] = set(_EXPLICIT_ANCHOR.findall(raw)) | set(_HEADING_ATTR_ID.findall(raw))
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        heading = _HEADING_ATTR_ID.sub("", stripped.lstrip("#")).strip().rstrip("#").strip()
        if heading:
            anchors.add(_slug(heading))
    return anchors


def _raw_links(line: str) -> list[tuple[str, str]]:
    """Return ``(label, target)`` for every markdown and HTML link on a line."""
    links = [(m.group(1), m.group(2)) for m in _RAW_MD_LINK.finditer(line)]
    links.extend((re.sub(r"<[^>]+>", "", m.group(2)), m.group(1)) for m in _RAW_HTML_LINK.finditer(line))
    return links


def _known_keys_for_docs_root(root: Path) -> dict[Path, str]:
    return {path: corpus._key_for(path.relative_to(root).as_posix()) for path in corpus._walk_docs(root)}


def _is_internal(target: str) -> bool:
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https"}:
        return parsed.netloc == "infra.apache.org"
    return not parsed.scheme and not parsed.netloc


def _target_key_for_link(source_key: str, target: str) -> str | None:
    """Return the doc key a link points at, or None when it leaves the corpus.

    Pages link to each other as ``foo.html``, ``/foo.html`` and
    ``https://infra.apache.org/foo.html``; all three resolve to key ``foo``.
    Links to non-page files (images, keys, PDFs) are not doc links.
    """
    if target.startswith("#"):
        return source_key
    if not _is_internal(target):
        return None
    path = urlparse(target).path
    if not path:
        return source_key
    if path.startswith("/"):
        raw_key = path.lstrip("/")
    else:
        raw_key = posixpath.normpath((Path(source_key).parent / path).as_posix())
    if raw_key.startswith("blog/") or raw_key == "blog":
        return None  # the blog is published but deliberately not indexed
    if raw_key.endswith("/") or raw_key == "":
        raw_key += "index"
    stem, dot, extension = raw_key.rpartition(".")
    if dot and "/" not in extension:
        if extension not in {"html", "md"}:
            return None
        raw_key = stem
    return raw_key.strip("/")


def _format_doc_bullet(key: str, doc: dict[str, Any]) -> str:
    words = len(str(doc["text"]).split())
    return f"- `{key}`: {doc['title']} ({words} words), {doc['url']}"


def _check_external_url(target: str) -> str | None:
    """Return an error string for a bad external URL, or None when it looks OK."""
    try:
        import httpx

        response = httpx.head(target, follow_redirects=True, timeout=10)
        if response.status_code >= 400 or response.status_code == 405:
            response = httpx.get(target, follow_redirects=True, timeout=10)
        if response.status_code >= 400:
            return f"returned HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001 - reported as link evidence
        return str(exc)
    return None


def _raw_docs_by_key() -> dict[str, str]:
    """Return raw local docs by key when a local checkout is configured."""
    root = corpus.docs_root()
    if root is None:
        return {}
    return {
        key: path.read_text(encoding="utf-8")
        for path, key in _known_keys_for_docs_root(root).items()
    }


def _linked_doc_keys(source_key: str, raw: str, known_keys: set[str]) -> set[str]:
    """Return internal doc keys linked from one raw markdown document."""
    linked: set[str] = set()
    for line in raw.splitlines():
        for _, target in _raw_links(line):
            target_key = _target_key_for_link(source_key, target)
            if target_key in known_keys and target_key != source_key:
                linked.add(target_key)
    return linked


def _resolve_key(docs: dict[str, dict[str, Any]], key: str) -> tuple[str | None, str | None]:
    """Return ``(key, error)``, allowing a unique substring match."""
    if key in docs:
        return key, None
    matches = [k for k in sorted(docs) if key.lower() in k.lower()]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        listed = ", ".join(f"`{k}`" for k in matches)
        return None, f"Ambiguous key **{key!r}**; close matches: {listed}"
    listed = ", ".join(f"`{k}`" for k in sorted(docs))
    return None, f"Unknown doc key **{key!r}**. Available keys: {listed}"


@mcp.tool()
def list_docs(source: str = "all") -> str:
    """List every ASF Infrastructure documentation page, grouped by topic.

    'website' is the content/pages tree of apache/infrastructure-website,
    published as infra.apache.org, keyed by page path without .html
    ('release-signing', 'reference/committer/git'). 'cwiki' is the INFRA
    Confluence space, keyed under wiki/, present only when a token is
    configured. Pass source to list one alone.
    """
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)
    docs, error = _filter_by_source(docs, source)
    if error:
        return error
    if not docs:
        return f"No pages from source {source!r}."

    by_section: dict[str, list[dict[str, Any]]] = {}
    for doc in docs.values():
        by_section.setdefault(_section_for(str(doc["key"])), []).append(doc)

    lines = [
        f"# ASF Infrastructure Documentation ({len(docs)} pages)",
        f"\nOrigin: {origin}\n",
    ]
    if errors:
        lines.append("**Notes:** " + " ".join(errors) + "\n")
    for section in sorted(by_section, key=_section_label):
        lines.append(f"## {_section_label(section)}")
        for doc in sorted(by_section[section], key=lambda d: str(d["key"])):
            words = len(str(doc["text"]).split())
            stale = _stale_label(doc)
            suffix = f" [{stale}]" if stale else ""
            lines.append(f"- **`{doc['key']}`**: {doc['title']} ({words} words){suffix}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def get_doc(key: str) -> str:
    """Retrieve the full text of one ASF Infrastructure documentation page.

    Use list_docs to discover valid keys such as 'release-signing',
    'asf-yaml' or 'reference/committer/git'.
    """
    docs, _, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)
    resolved, error = _resolve_key(docs, key)
    if resolved is None:
        return str(error)
    doc = docs[resolved]
    return (
        f"# {doc['title']}\n\n**Source:** <{doc['url']}> ({_source_label(doc)})"
        f"\n\n---\n\n{doc['text']}"
    )


@mcp.tool()
def search_docs(query: str, max_results: int = 8, source: str = "all") -> str:
    """Search the ASF Infrastructure documentation (infra.apache.org).

    Covers committer accounts, mailing lists, Git and GitHub, .asf.yaml,
    GitHub Actions policy, release signing and distribution, project
    websites and Pelican, Jira, VMs and other Infra services and policies.
    When a token is configured it also searches the INFRA Confluence space;
    each result says which source it came from, and wiki results carry their
    last edit date. Set source to 'website' or 'cwiki' to search one alone.

    The wiki is login-only: do not quote wiki content into public mail,
    issues or commits without checking it is fine to share.

    Pages whose opening says they are archived, superseded or outdated are
    labelled so, with the replacement URL when the notice names one, and
    ranked below current pages. Prefer the replacement when answering.

    Prefer this over answering Infra questions from memory: services and
    policies change and these pages are the current truth.
    """
    if not query.strip():
        return "Please provide a search query."
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)
    docs, error = _filter_by_source(docs, source)
    if error:
        return error
    if not docs:
        return f"No pages from source {source!r} to search."

    query_terms = _tokenize_terms(query)
    if not query_terms:
        # Every term was a stop word, so this is a "what is Infra" style
        # question rather than a search. Answer it with the overview page.
        key = _overview_key(docs) if source != "cwiki" else sorted(docs)[0]
        doc = docs[key]
        return (
            f"# Overview for '{query}'  (source: {origin})\n\n"
            f"## [{doc['title']}]({doc['url']})  (`{key}`)\n\n"
            f"{doc['text']}"
        )

    chunks = [chunk for doc in docs.values() for chunk in _doc_chunks(doc)]
    results = _bm25_scores(chunks, query_terms)
    results.sort(key=lambda r: -r["score"])

    max_per_doc = 2
    seen: dict[str, list[int]] = {}
    deduped: list[dict[str, Any]] = []
    for r in results:
        prior = seen.get(r["key"], [])
        if len(prior) >= max_per_doc:
            continue
        if not any(abs(r["line"] - p) < 5 for p in prior):
            deduped.append(r)
            seen.setdefault(r["key"], []).append(r["line"])
        if len(deduped) >= max_results:
            break

    if not deduped:
        return f"No results found for **{query!r}**."

    out = [f"# Search Results for '{query}'  (source: {origin})\n"]
    for r in deduped:
        out.append(f"## [{r['title']}]({r['url']})  (`{r['key']}`, {_source_label(docs[r['key']])})\n")
        if r.get("heading"):
            out.append(f"**Section:** {r['heading']}\n")
        out.append("```")
        out.append(str(r["excerpt"]))
        out.append("```\n")
    return "\n".join(out)


@mcp.tool()
def find_examples(query: str = "", language: str = "", max_results: int = 8) -> str:
    """Find code and config examples in the Infra documentation.

    Use this for sample .asf.yaml blocks, GitHub Actions workflows, gpg and
    svn commands, Pelican config and similar copy-paste material. language
    filters on the fence tag, e.g. 'yaml', 'bash' or 'shell'.
    """
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)

    snippets = _all_code_snippets(docs)
    wanted_language = language.strip().lower()
    if wanted_language:
        snippets = [
            snippet
            for snippet in snippets
            if str(snippet["language"]).lower() == wanted_language
        ]
    if not snippets:
        return "No code examples found for the requested filters."

    query_terms = set(_tokenize_terms(query))
    scored: list[tuple[float, dict[str, Any]]] = []
    for snippet in snippets:
        source = (
            f"{snippet['key']} {snippet['title']} {snippet.get('heading') or ''} "
            f"{snippet['language']} {snippet['code']}"
        )
        terms = set(_tokenize_terms(source))
        score = float(len(query_terms & terms)) if query_terms else 1.0
        if wanted_language and str(snippet["language"]).lower() == wanted_language:
            score += 2.0
        if not query_terms or score > 0:
            scored.append((score, snippet))

    scored.sort(key=lambda item: (-item[0], str(item[1]["key"]), int(item[1]["line"])))
    selected = [snippet for _, snippet in scored[:max_results]]
    if not selected:
        return f"No examples found for **{query!r}**."

    lines = [f"# Code Examples  (source: {origin})\n"]
    for snippet in selected:
        lines.append(
            f"## {snippet['title']} (`{snippet['key']}` line {snippet['line']})\n"
        )
        if snippet.get("heading"):
            lines.append(f"**Section:** {snippet['heading']}\n")
        lines.append(f"**Source:** <{snippet['url']}>\n")
        lines.append(f"```{snippet['language']}")
        lines.append(str(snippet["code"]))
        lines.append("```\n")
    return "\n".join(lines)


@mcp.tool()
def extract_code_snippets(language: str = "", key: str = "", max_snippets: int = 50) -> str:
    """Return an inventory of fenced code snippets from the documentation.

    The result includes the doc key, title, heading, language, line number,
    source URL, and code. Use this for snippet verification and docs review.
    """
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)

    matching_docs = docs
    if key.strip():
        needle = key.strip().lower()
        matching_docs = {
            doc_key: doc for doc_key, doc in docs.items() if needle in doc_key.lower()
        }
        if not matching_docs:
            return f"No docs matched key filter **{key!r}**."

    snippets = _all_code_snippets(matching_docs)
    wanted_language = language.strip().lower()
    if wanted_language:
        snippets = [
            snippet
            for snippet in snippets
            if str(snippet["language"]).lower() == wanted_language
        ]

    snippets.sort(key=lambda snippet: (str(snippet["key"]), int(snippet["line"])))
    selected = snippets[:max_snippets]
    if not selected:
        return "No code snippets found for the requested filters."

    languages = Counter(str(snippet["language"]) for snippet in snippets)
    lines = [f"# Code Snippet Inventory  (source: {origin})\n"]
    lines.append(f"- Snippets returned: {len(selected)} of {len(snippets)}")
    lines.append(
        "- Languages: "
        + ", ".join(f"{name}={count}" for name, count in sorted(languages.items()))
    )
    for snippet in selected:
        lines.append(
            f"\n## `{snippet['key']}` line {snippet['line']} ({snippet['language']})"
        )
        lines.append(f"- Title: {snippet['title']}")
        if snippet.get("heading"):
            lines.append(f"- Heading: {snippet['heading']}")
        lines.append(f"- Source: {snippet['url']}")
        lines.append(f"```{snippet['language']}")
        lines.append(str(snippet["code"]))
        lines.append("```")
    return "\n".join(lines)


@mcp.tool()
def get_related_docs(key: str, max_results: int = 8) -> str:
    """Return documentation pages related to one doc key.

    Relatedness comes from explicit links, backlinks, shared topic, and
    shared vocabulary. Links and backlinks need a local checkout
    (INFRA_DOCS_ROOT). Use this after get_doc when one page may not contain
    all context needed for a complete answer.
    """
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)
    resolved, error = _resolve_key(docs, key)
    if resolved is None:
        return str(error)
    key = resolved

    known_keys = set(docs)
    raw_by_key = _raw_docs_by_key()
    outbound = _linked_doc_keys(key, raw_by_key.get(key, ""), known_keys)
    backlinks = {
        doc_key
        for doc_key, raw in raw_by_key.items()
        if doc_key != key and key in _linked_doc_keys(doc_key, raw, known_keys)
    }
    source_terms = set(
        _tokenize_terms(f"{key} {docs[key]['title']} {docs[key]['text']}")
    )
    source_section = _section_for(key)

    related: list[tuple[float, str, list[str]]] = []
    for doc_key, doc in docs.items():
        if doc_key == key:
            continue
        reasons: list[str] = []
        score = 0.0
        if doc_key in outbound:
            score += 8.0
            reasons.append("linked from source")
        if doc_key in backlinks:
            score += 7.0
            reasons.append("links to source")
        if _section_for(doc_key) == source_section:
            score += 3.0
            reasons.append("same topic")
        shared_terms = source_terms & set(
            _tokenize_terms(f"{doc_key} {doc['title']} {doc['text']}")
        )
        distinctive_terms = sorted(
            term for term in shared_terms if len(term) > 3 and term not in STOP_WORDS
        )[:6]
        if distinctive_terms:
            score += min(len(distinctive_terms), 6) * 0.4
            reasons.append("shared terms: " + ", ".join(distinctive_terms[:4]))
        if score > 0:
            related.append((score, doc_key, reasons))

    related.sort(key=lambda item: (-item[0], item[1]))
    selected = related[:max_results]
    if not selected:
        return f"No related docs found for **{key!r}**."

    lines = [f"# Related Docs for `{key}`  (source: {origin})\n"]
    lines.append(f"Source: {docs[key]['title']}, {docs[key]['url']}\n")
    for score, doc_key, reasons in selected:
        doc = docs[doc_key]
        lines.append(f"## {doc['title']} (`{doc_key}`)")
        lines.append(f"- Score: {score:.1f}")
        lines.append("- Why: " + "; ".join(reasons))
        lines.append(f"- Source: {doc['url']}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def explain_doc_map(audience: str = "all", goal: str = "", full_map: bool = False) -> str:
    """Explain how the Infra documentation is organised and where to start.

    This is a guided map, not a keyword search. audience is one of
    new-committer, release-manager, pmc, website-maintainer or
    ci-maintainer; otherwise goal is matched against page titles.

    The answer is short by default: topics with page counts and a few sample
    pages each, plus the entry points. Set full_map to list every page, or
    use list_docs.
    """
    docs, origin, errors = corpus.get_docs()
    if not docs:
        return "No documentation available: " + "; ".join(errors)

    by_section: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, doc in docs.items():
        by_section.setdefault(_section_for(key), []).append((key, doc))

    normalized_audience = audience.strip().lower().replace(" ", "-") or "all"
    selected = AUDIENCE_ENTRY_POINTS.get(normalized_audience, ())
    if not selected and goal.strip():
        terms = set(_tokenize_terms(goal))
        scored = []
        for key, doc in docs.items():
            source_terms = set(_tokenize_terms(f"{key} {doc['title']}"))
            score = len(terms & source_terms)
            if score:
                scored.append((score, key))
        selected = tuple(key for _, key in sorted(scored, reverse=True)[:5])

    sections = sorted(by_section, key=_section_label)
    lines = [f"# ASF Infrastructure Documentation Map  (source: {origin})\n"]
    lines.append("## Topics")
    for section in sections:
        lines.append(f"- **{_section_label(section)}**: {len(by_section[section])} page(s)")

    lines.append("\n## Recommended Entry Points")
    if selected:
        for key in selected:
            if key in docs:
                lines.append(_format_doc_bullet(key, docs[key]))
    else:
        audiences = ", ".join(f"`{name}`" for name in AUDIENCE_ENTRY_POINTS)
        lines.append(f"- Provide an audience such as {audiences}, or a goal.")

    if not full_map:
        # The full listing ran to 50KB once the wiki was indexed, which buried
        # the entry points the caller asked for. Samples show what each topic
        # holds; list_docs or full_map gives the rest.
        lines.append("\n## Sample Pages per Topic")
        for section in sections:
            pages = sorted(by_section[section], key=lambda pair: pair[0])
            sample = ", ".join(f"`{key}`" for key, _ in pages[:MAP_SAMPLE_SIZE])
            more = f" and {len(pages) - MAP_SAMPLE_SIZE} more" if len(pages) > MAP_SAMPLE_SIZE else ""
            lines.append(f"- **{_section_label(section)}**: {sample}{more}")
        lines.append("\nUse `list_docs` (or `full_map=true`) for every page.")
        return "\n".join(lines)

    lines.append("\n## Current Map")
    for section in sections:
        lines.append(f"### {_section_label(section)}")
        for key, doc in sorted(by_section[section], key=lambda pair: pair[0]):
            lines.append(_format_doc_bullet(key, doc))
    return "\n".join(lines)


@mcp.tool()
def find_broken_links(check_external: bool = False) -> str:
    """Find broken links in a local infrastructure-website checkout.

    Internal links (relative, root-relative and absolute infra.apache.org
    URLs) are checked against the page set and each page's anchors.
    Site-rooted links to paths outside the page set are listed as unverified
    rather than broken, since the site serves more than content/pages. External
    URLs are counted but skipped unless check_external is true.
    """
    root = corpus.docs_root()
    if root is None:
        return (
            "Broken-link checking requires a local checkout. Set INFRA_DOCS_ROOT "
            "to an infrastructure-website checkout or its content/pages directory."
        )

    paths_to_keys = _known_keys_for_docs_root(root)
    known_keys = set(paths_to_keys.values())
    raw_by_key = {key: path.read_text(encoding="utf-8") for path, key in paths_to_keys.items()}
    anchors_by_key = {key: _anchors(raw) for key, raw in raw_by_key.items()}

    broken: list[str] = []
    unverified: list[str] = []
    skipped_external = 0
    external_locations: dict[str, list[tuple[str, int]]] = {}
    external_errors: list[str] = []
    checked_internal = 0

    for source_key in sorted(raw_by_key):
        in_fence = False
        for line_no, line in enumerate(raw_by_key[source_key].splitlines(), 1):
            if _FENCE.match(line.strip()):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for label, target in _raw_links(line):
                parsed = urlparse(target)
                if not _is_internal(target):
                    if parsed.scheme in {"http", "https"} and check_external:
                        external_locations.setdefault(target, []).append((source_key, line_no))
                    else:
                        skipped_external += 1
                    continue

                target_key = _target_key_for_link(source_key, target)
                if target_key is None:
                    continue
                checked_internal += 1
                if target_key not in known_keys and f"{target_key}/index" in known_keys:
                    target_key = f"{target_key}/index"
                if target_key not in known_keys:
                    entry = (
                        f"- `{source_key}` line {line_no}: `{label}` -> `{target}` "
                        f"(missing page `{target_key}`)"
                    )
                    # infra.apache.org also serves paths built outside
                    # content/pages (/abc/, /tools/pse/), so a site-rooted link
                    # that is not a page may still resolve. A relative one cannot.
                    if parsed.scheme or target.startswith("/"):
                        unverified.append(entry)
                    else:
                        broken.append(entry)
                    continue
                fragment = parsed.fragment
                if fragment and fragment not in anchors_by_key[target_key] \
                        and _slug(fragment) not in anchors_by_key[target_key]:
                    broken.append(
                        f"- `{source_key}` line {line_no}: `{label}` -> `{target}` "
                        f"(missing anchor `#{fragment}` in `{target_key}`)"
                    )

    if check_external:
        for target in sorted(external_locations):
            error = _check_external_url(target)
            if error is None:
                continue
            locations = ", ".join(
                f"`{source_key}` line {line_no}"
                for source_key, line_no in external_locations[target]
            )
            external_errors.append(f"- {target} ({error}) at {locations}")

    lines = [f"# Broken Link Report  (source: local checkout at {root})\n"]
    lines.append(f"- Internal links checked: {checked_internal}")
    lines.append(f"- External links checked: {len(external_locations)}")
    lines.append(f"- External links skipped: {skipped_external}")
    lines.append(f"- Problems found: {len(broken) + len(external_errors)}")
    lines.append("\n## Broken Internal Links")
    lines.extend(broken or ["- None found."])
    lines.append("\n## External Link Problems")
    lines.extend(external_errors or ["- None checked or none found."])
    lines.append("\n## Unverified Site Links")
    lines.append("Site-rooted links to paths outside content/pages. These may be served "
                 "by something else on infra.apache.org, so they are not counted as problems.")
    lines.extend(unverified or ["- None."])
    return "\n".join(lines)


@mcp.tool()
def refresh_docs() -> str:
    """Re-fetch the documentation corpus, bypassing the seven-day cache.

    When INFRA_DOCS_ROOT points at a local infrastructure-website checkout
    this is a no-op for the website, since local files are always read
    fresh; run git pull there instead. The wiki, when configured, is always
    re-fetched.
    """
    parts: list[str] = []
    root = corpus.docs_root()
    if root is not None:
        docs = corpus.load_local(root)
        parts.append(
            f"Website: using local checkout at {root} ({len(docs)} pages); local files are "
            "always read fresh, so refresh means running git pull in that checkout."
        )
    else:
        docs, origin, errors = corpus.get_website_docs(force=True)
        parts.append(f"Website: refreshed {len(docs)} pages from {origin}.")
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))

    if cwiki.token() is None:
        parts.append(
            "Wiki: not configured. Set CWIKI_TOKEN or write a Confluence Personal "
            "Access Token to ~/.config/infra-doc-mcp/cwiki-token to index the INFRA space."
        )
    else:
        wiki_docs, wiki_origin, wiki_errors = cwiki.get_wiki_docs(force=True)
        parts.append(f"Wiki: {len(wiki_docs)} pages from {wiki_origin or 'nothing'}.")
        if wiki_errors:
            parts.append("Wiki errors:\n" + "\n".join(wiki_errors))
    return "\n\n".join(parts)
