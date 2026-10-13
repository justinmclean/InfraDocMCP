"""Loader for the .asf.yaml reference in apache/infrastructure-asfyaml.

The full .asf.yaml documentation (branch protection, notifications, staging,
publishing, GitHub settings) lives in that repository's README, not on
infra.apache.org; the website and the wiki only point to it. It is indexed as
one document, key ``asfyaml/readme``.

Two origins, matching the website:

- a local checkout, when ``INFRA_ASFYAML_ROOT`` points at an
  infrastructure-asfyaml checkout, read fresh on every call; or
- GitHub, fetched from the raw file and cached on disk for seven days.

The parser's developer docs under ``docs/`` are not indexed: they are for
people changing the parser, not for projects writing an .asf.yaml.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from infra_doc_mcp import corpus

KEY = "asfyaml/readme"
TITLE = ".asf.yaml reference"
RAW_URL = "https://raw.githubusercontent.com/apache/infrastructure-asfyaml/main/README.md"
SITE_URL = "https://github.com/apache/infrastructure-asfyaml/blob/main/README.md"
CACHE_TTL: int = 7 * 24 * 3600
CACHE_FILE: Path = Path.home() / ".cache" / "infra-doc-mcp" / "asfyaml_cache.json"


def readme_path() -> Path | None:
    """Return the local README when INFRA_ASFYAML_ROOT is usable."""
    configured = os.environ.get("INFRA_ASFYAML_ROOT")
    if not configured:
        return None
    path = Path(configured).expanduser() / "README.md"
    return path if path.is_file() else None


def _doc_entry(raw: str) -> dict[str, Any]:
    text, _ = corpus.md_to_text(raw)
    return {"key": KEY, "title": TITLE, "url": SITE_URL, "text": text, "source": "asfyaml"}


def fetch_remote() -> tuple[str | None, list[str]]:
    import httpx  # imported lazily, as in corpus.fetch_remote

    try:
        response = httpx.get(RAW_URL, follow_redirects=True, timeout=30,
                             headers={"User-Agent": corpus.USER_AGENT})
        response.raise_for_status()
        return response.text, []
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        return None, [f"asfyaml: fetch failed: {exc}"]


def load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")


def get_asfyaml_docs(force: bool = False) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
    """Return ``(docs, origin, errors)``; empty with origin "" when unavailable."""
    path = readme_path()
    if path is not None:
        return {KEY: _doc_entry(path.read_text(encoding="utf-8"))}, f"asfyaml checkout at {path.parent}", []

    cache = load_cache()
    if not force and cache.get("raw") and time.time() - float(cache.get("fetched_at", 0)) < CACHE_TTL:
        age_h = int((time.time() - float(cache["fetched_at"])) // 3600)
        return {KEY: _doc_entry(str(cache["raw"]))}, f"asfyaml README (cached {age_h}h ago)", []

    raw, errors = fetch_remote()
    if raw:
        save_cache({"raw": raw, "fetched_at": time.time()})
        return {KEY: _doc_entry(raw)}, "asfyaml README (fetched now)", errors
    if cache.get("raw"):
        return {KEY: _doc_entry(str(cache["raw"]))}, "asfyaml README (stale cache; refresh failed)", errors
    return {}, "", errors
