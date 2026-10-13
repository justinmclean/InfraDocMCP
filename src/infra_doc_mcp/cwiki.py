"""Loader for the Infra Confluence space on cwiki.apache.org.

The INFRA space is not public, so this source is only used when a Confluence
Personal Access Token is configured, either in ``CWIKI_TOKEN`` or in the file
named by ``CWIKI_TOKEN_FILE`` (default ``~/.config/infra-doc-mcp/cwiki-token``).
Without a token the wiki is skipped and the website answers alone.

Pages are fetched through the REST API in Confluence storage format, converted
to markdown-ish text, and cached on disk for seven days. The cache holds
private content, so it is written readable by the owner only.

Wiki pages are keyed ``wiki/<Title-With-Dashes>`` so they never collide with
website keys.
"""

from __future__ import annotations

import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("CWIKI_BASE_URL", "https://cwiki.apache.org/confluence").rstrip("/")
SPACE_KEY = os.environ.get("CWIKI_SPACE_KEY", "INFRA")
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "infra-doc-mcp" / "cwiki-token"
CACHE_TTL: int = 7 * 24 * 3600
CACHE_FILE: Path = Path.home() / ".cache" / "infra-doc-mcp" / "cwiki_cache.json"
PAGE_SIZE = 50
USER_AGENT = "infra-doc-mcp/0.1.0"

_BLOCK_TAGS = {"p", "div", "br", "tr", "table", "ul", "ol", "blockquote", "hr", "pre"}


def token() -> str | None:
    """Return the configured token, or None. Never logged or returned to a client."""
    value = os.environ.get("CWIKI_TOKEN", "").strip()
    if value:
        return value
    path = Path(os.environ.get("CWIKI_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


class _StorageToText(HTMLParser):
    """Convert Confluence storage XHTML to markdown-ish plain text.

    Headings become ``#`` headings so search stays section-aware, list items
    become ``-`` bullets, and code macros become fenced blocks with their
    language, so find_examples works on wiki pages too.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._macro: list[str] = []
        self._param: str | None = None
        self._language = ""
        self._in_code_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "ac:structured-macro":
            self._macro.append(str(attributes.get("ac:name") or ""))
            if self._macro[-1] in {"code", "noformat"}:
                self._language = ""
        elif tag == "ac:parameter":
            self._param = str(attributes.get("ac:name") or "")
        elif tag == "ac:plain-text-body" and self._macro and self._macro[-1] in {"code", "noformat"}:
            self._in_code_body = True
            self.out.append(f"\n```{self._language}\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in {"td", "th"}:
            self.out.append(" | ")
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "ac:structured-macro" and self._macro:
            self._macro.pop()
        elif tag == "ac:parameter":
            self._param = None
        elif tag == "ac:plain-text-body" and self._in_code_body:
            self._in_code_body = False
            self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag) or tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._param is not None:
            if self._param == "language" and self._macro and self._macro[-1] == "code":
                self._language = data.strip().lower()
            return
        self.out.append(data)

    # CDATA (the body of a code macro) arrives here rather than handle_data.
    def unknown_decl(self, data: str) -> None:
        if data.startswith("CDATA["):
            self.out.append(data[len("CDATA["):])


def storage_to_text(storage: str) -> str:
    parser = _StorageToText()
    parser.feed(storage)
    parser.close()
    text = "".join(parser.out)
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _key_for(title: str) -> str:
    return "wiki/" + re.sub(r"\s+", "-", title.strip())


def _doc_entry(page: dict[str, Any]) -> dict[str, Any]:
    title = str(page.get("title") or page.get("id"))
    webui = str(page.get("_links", {}).get("webui") or f"/pages/viewpage.action?pageId={page.get('id')}")
    when = str(page.get("version", {}).get("when") or "")[:10]
    return {
        "key": _key_for(title),
        "title": title,
        "url": BASE_URL + webui,
        "text": storage_to_text(str(page.get("body", {}).get("storage", {}).get("value") or "")),
        "source": "cwiki",
        "updated": when,
    }


def fetch_remote(api_token: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fetch every page in the space. Returns (docs, errors)."""
    import httpx  # imported lazily, as in corpus.fetch_remote

    docs: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    headers = {"Authorization": f"Bearer {api_token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
    url: str | None = f"{BASE_URL}/rest/api/content"
    params: dict[str, Any] | None = {
        "spaceKey": SPACE_KEY,
        "type": "page",
        "status": "current",
        "expand": "body.storage,version",
        "limit": PAGE_SIZE,
    }
    try:
        with httpx.Client(follow_redirects=False, timeout=60) as client:
            while url:
                response = client.get(url, params=params, headers=headers)
                # Confluence treats an unrecognised token as anonymous, and an
                # anonymous request for a private space gets 404, not 401.
                if response.status_code in {401, 403, 404} or response.is_redirect:
                    errors.append(
                        f"cwiki: token not accepted, or no access to space {SPACE_KEY} "
                        f"(HTTP {response.status_code}); check the token is current and complete"
                    )
                    return {}, errors
                response.raise_for_status()
                payload = response.json()
                for page in payload.get("results", []):
                    entry = _doc_entry(page)
                    docs[entry["key"]] = entry
                next_link = payload.get("_links", {}).get("next")
                url = f"{BASE_URL}{next_link}" if next_link else None
                params = None  # the next link carries its own query
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        # The exception text never includes request headers, so the token stays out of it.
        errors.append(f"cwiki: fetch failed: {exc}")
        return {}, errors
    if not docs:
        errors.append(f"cwiki: space {SPACE_KEY} returned no pages; the token may lack access")
    return docs, errors


def load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(cache, handle)
    os.chmod(CACHE_FILE, 0o600)


def get_wiki_docs(force: bool = False) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    """Return ``(docs, origin, errors)``; empty with origin "" when no token is set."""
    api_token = token()
    if api_token is None:
        return {}, "", []

    cache = load_cache()
    fresh = time.time() - float(cache.get("fetched_at", 0)) < CACHE_TTL
    if not force and cache.get("docs") and fresh and cache.get("space") == SPACE_KEY:
        age_h = int((time.time() - float(cache["fetched_at"])) // 3600)
        return dict(cache["docs"]), f"cwiki {SPACE_KEY} (cached {age_h}h ago)", []

    docs, errors = fetch_remote(api_token)
    if docs:
        save_cache({"docs": docs, "fetched_at": time.time(), "space": SPACE_KEY})
        return docs, f"cwiki {SPACE_KEY} (fetched now)", errors
    if cache.get("docs") and cache.get("space") == SPACE_KEY:
        return dict(cache["docs"]), f"cwiki {SPACE_KEY} (stale cache; refresh failed)", errors
    return {}, "", errors
