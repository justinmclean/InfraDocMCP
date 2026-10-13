"""Document loading, Pelican markdown extraction, and disk cache for the Infra docs corpus.

The corpus is the ``content/pages`` tree of the apache/infrastructure-website
repository, which ASF Pelican builds into infra.apache.org. Two origins are
supported:

- a local checkout, when ``INFRA_DOCS_ROOT`` points at ``content/pages`` (or a
  directory containing it), read fresh on every index build; or
- GitHub, discovered through the git trees API and fetched from raw files,
  cached on disk for seven days.

Docs are keyed by their path under ``content/pages`` without the extension,
e.g. ``release-signing`` or ``reference/committer/git``. The published page is
the key plus ``.html`` on infra.apache.org.

The blog (``content/blog``) is deliberately not indexed: it is dated news, and
a 2011 post about the CMS would otherwise answer questions about today's
Pelican builds.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from infra_doc_mcp import cwiki

# 7 days
CACHE_TTL: int = 7 * 24 * 3600
CACHE_FILE: Path = Path.home() / ".cache" / "infra-doc-mcp" / "docs_cache.json"

GITHUB_TREE_URL = "https://api.github.com/repos/apache/infrastructure-website/git/trees/master?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/apache/infrastructure-website/master/"
DOCS_PREFIX = "content/pages/"
# Never index dependency or build trees when a local root is broader than expected
SKIP_DIRS = {"node_modules", ".git", ".venv", "build", "dist", "output", "images"}
SITE_BASE = "https://infra.apache.org/"
USER_AGENT = "infra-doc-mcp/0.1.0"

# Pelican metadata is a block of "Key: value" lines at the top of the file,
# ended by the first blank line.
_METADATA_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ -]*):\s*(.*?)\s*$")
_FENCE = re.compile(r"^(```|~~~)")
_HTML_HEADING = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>\s*$", re.I)
_HEADERLINK = re.compile(r"<a\b[^>]*class=\"headerlink\"[^>]*>.*?</a>", re.I)
_HEADING_ATTR = re.compile(r"\s*\{#[^}]*\}\s*$")
_TRAILING_HASHES = re.compile(r"\s+#+\s*$")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\([^)]+\)")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
# Only real HTML tags are stripped. The pages also use angle brackets for
# placeholders such as <alice> or <pmc>, which are content, not markup.
_HTML_TAG = re.compile(
    r"</?(?:a|abbr|b|blockquote|br|caption|center|code|dd|div|dl|dt|em|font|h[1-6]|hr|i|img|"
    r"kbd|li|ol|p|pre|small|span|strong|sub|sup|table|tbody|td|tfoot|th|thead|tr|tt|u|ul)"
    r"\b[^>]*>",
    re.I,
)


def _clean_inline(line: str) -> str:
    line = _HEADERLINK.sub("", line)
    line = _MD_IMAGE.sub(r"\1", line)
    line = _MD_LINK.sub(r"\1", line)
    line = _HTML_TAG.sub("", line)
    return html.unescape(line)


def split_metadata(raw: str) -> tuple[dict[str, str], str]:
    """Return ``(metadata, body)`` for a Pelican markdown document."""
    metadata: dict[str, str] = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        match = _METADATA_LINE.match(lines[i])
        if not match:
            break
        metadata[match.group(1).strip().lower()] = match.group(2)
        i += 1
    if not metadata or (i < len(lines) and lines[i].strip()):
        # No block, or the "metadata" ran straight into prose: treat it all as body.
        return {}, raw
    return metadata, "\n".join(lines[i:])


def md_to_text(raw: str) -> tuple[str, str | None]:
    """Return ``(plain_text, title)`` for a Pelican markdown page.

    Fence-aware: HTML is converted only OUTSIDE code fences. Inside a fence
    every line is kept verbatim, because ``<Directory>`` in an httpd example is
    content, not markup. Inline HTML headings become markdown headings so the
    search chunker still sees the page structure.
    """
    metadata, body = split_metadata(raw)
    title = metadata.get("title") or None

    out: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if _FENCE.match(stripped):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if stripped == "[TOC]":
            continue
        heading = _HTML_HEADING.match(line)
        if heading:
            out.append("#" * int(heading.group(1)) + " " + _clean_inline(heading.group(2)).strip())
            continue
        if stripped.startswith("#"):
            line = _TRAILING_HASHES.sub("", _HEADING_ATTR.sub("", line))
        out.append(_clean_inline(line))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text, title


def _doc_entry(key: str, raw: str) -> dict[str, Any]:
    text, title = md_to_text(raw)
    return {
        "key": key,
        "title": title or key.rsplit("/", 1)[-1].replace("-", " ").title(),
        "url": SITE_BASE + key + ".html",
        "text": text,
        "source": "website",
    }


def _key_for(relative: str) -> str:
    return relative.rsplit(".", 1)[0]


def docs_root() -> Path | None:
    """Return the local pages directory when INFRA_DOCS_ROOT is usable.

    Candidates are tried most specific first, so pointing at a whole
    infrastructure-website checkout selects ``content/pages`` rather than the
    repository root, which would otherwise pull in the blog and README.
    """
    configured = os.environ.get("INFRA_DOCS_ROOT")
    if not configured:
        return None
    root = Path(configured).expanduser()
    for candidate in (root / "content" / "pages", root / "pages", root):
        if candidate.is_dir() and any(_walk_docs(candidate)):
            return candidate
    return None


def _walk_docs(root: Path) -> Iterator[Path]:
    """Yield markdown files under *root*, skipping build and dependency trees."""
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(root).parts[:-1]):
            continue
        yield path


def load_local(root: Path) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for path in _walk_docs(root):
        key = _key_for(path.relative_to(root).as_posix())
        docs[key] = _doc_entry(key, path.read_text(encoding="utf-8"))
    return docs


def fetch_remote() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Discover and fetch the pages tree from GitHub. Returns (docs, errors)."""
    import httpx  # imported lazily so local-checkout use needs no network stack

    errors: list[str] = []
    docs: dict[str, dict[str, Any]] = {}
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(GITHUB_TREE_URL, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            paths = [
                item["path"]
                for item in resp.json().get("tree", [])
                if item.get("type") == "blob"
                and item["path"].startswith(DOCS_PREFIX)
                and item["path"].endswith(".md")
            ]

            def fetch_one(repo_path: str) -> tuple[str, str]:
                r = client.get(RAW_BASE + repo_path, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                return repo_path, r.text

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(fetch_one, p): p for p in paths}
                for future in as_completed(futures):
                    try:
                        repo_path, raw = future.result()
                        key = _key_for(repo_path[len(DOCS_PREFIX):])
                        docs[key] = _doc_entry(key, raw)
                    except Exception as exc:  # noqa: BLE001 - reported, not fatal
                        errors.append(f"{futures[future]}: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tree discovery failed: {exc}")
    return docs, errors


def load_cache() -> dict[str, Any]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except Exception:
            pass
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_website_docs(force: bool = False) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    """Return ``(docs, origin, errors)`` for the website alone.

    origin is a human-readable description of where the corpus came from.
    """
    root = docs_root()
    if root is not None:
        return load_local(root), f"local checkout at {root}", []

    cache = load_cache()
    if not force and cache.get("docs") and time.time() - float(cache.get("fetched_at", 0)) < CACHE_TTL:
        age_h = int((time.time() - float(cache["fetched_at"])) // 3600)
        return dict(cache["docs"]), f"GitHub (cached {age_h}h ago)", []

    docs, errors = fetch_remote()
    if docs:
        save_cache({"docs": docs, "fetched_at": time.time()})
        return docs, "GitHub (fetched now)", errors
    if cache.get("docs"):
        return dict(cache["docs"]), "GitHub (stale cache; refresh failed)", errors
    return {}, "unavailable", errors


def get_docs(force: bool = False) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    """Return ``(docs, origin, errors)`` for the website, the .asf.yaml
    reference README and, when a token is configured, the INFRA Confluence space.

    The extra sources have their own key prefixes (``asfyaml/``, ``wiki/``), so
    keys never collide. A missing or failing extra source is not an error for
    the website: it answers alone.
    """
    # Imported here because asfyaml reuses this module's markdown converter.
    from infra_doc_mcp import asfyaml

    docs, origin, errors = get_website_docs(force=force)
    for extra_docs, extra_origin, extra_errors in (
        asfyaml.get_asfyaml_docs(force=force),
        cwiki.get_wiki_docs(force=force),
    ):
        if extra_docs:
            docs = {**docs, **extra_docs}
            origin = f"{origin} + {extra_origin}" if origin != "unavailable" else extra_origin
        errors = errors + extra_errors
    return docs, origin, errors
