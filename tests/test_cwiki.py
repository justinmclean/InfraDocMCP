"""Tests for the INFRA Confluence source. No network: the fetch is faked."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infra_doc_mcp import asfyaml, corpus, cwiki, tools

STORAGE = (
    "<h2>Restarting the service</h2><p>Log in to the host and run:</p>"
    '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>'
    "<ac:plain-text-body><![CDATA[sudo systemctl restart <svc> && echo ok]]></ac:plain-text-body>"
    "</ac:structured-macro><ul><li>first</li><li>second &amp; last</li></ul>"
    "<table><tr><th>Host</th><th>Role</th></tr><tr><td>gitbox</td><td>git</td></tr></table>"
)

PAGES = [
    {
        "id": "101",
        "title": "Gitbox Runbook",
        "_links": {"webui": "/display/INFRA/Gitbox+Runbook"},
        "version": {"when": "2019-03-04T10:00:00.000Z"},
        "body": {"storage": {"value": STORAGE}},
    },
    {
        "id": "102",
        "title": "Mail Relay Notes",
        "_links": {},
        "version": {"when": "2024-01-02T00:00:00.000Z"},
        "body": {"storage": {"value": "<p>The relay forwards committer mail.</p>"}},
    },
]


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self.is_redirect = 300 <= status < 400
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_httpx(responses: list[FakeResponse], seen: list[dict[str, Any]]) -> SimpleNamespace:
    class Client:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, url: str, params: Any = None, headers: Any = None) -> FakeResponse:
            seen.append({"url": url, "params": params, "headers": headers})
            return responses.pop(0)

    return SimpleNamespace(Client=Client)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CWIKI_TOKEN", raising=False)
    monkeypatch.setenv("CWIKI_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setattr(cwiki, "CACHE_FILE", tmp_path / "cwiki_cache.json")
    monkeypatch.setattr(corpus, "CACHE_FILE", tmp_path / "docs_cache.json")
    monkeypatch.delenv("INFRA_ASFYAML_ROOT", raising=False)
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: (None, []))
    monkeypatch.setattr(asfyaml, "CACHE_FILE", tmp_path / "asfyaml_cache.json")
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "services.md").write_text("Title: Services\n\nInfra runs mail.\n", encoding="utf-8")
    monkeypatch.setenv("INFRA_DOCS_ROOT", str(pages))


def test_storage_to_text_keeps_structure_and_code() -> None:
    text = cwiki.storage_to_text(STORAGE)
    assert "## Restarting the service" in text
    assert "```bash\nsudo systemctl restart <svc> && echo ok\n```" in text
    assert "- first" in text and "- second & last" in text
    assert "gitbox" in text and "|" in text


def test_token_from_env_then_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cwiki.token() is None
    (tmp_path / "token").write_text("from-file\n", encoding="utf-8")
    assert cwiki.token() == "from-file"
    monkeypatch.setenv("CWIKI_TOKEN", "from-env")
    assert cwiki.token() == "from-env"


def test_no_token_means_website_only() -> None:
    docs, origin, errors = corpus.get_docs()
    assert set(docs) == {"services"}
    assert "cwiki" not in origin
    assert errors == []


def test_fetch_follows_pagination_with_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    responses = [
        FakeResponse(200, {"results": PAGES[:1], "_links": {"next": "/rest/api/content?start=1"}}),
        FakeResponse(200, {"results": PAGES[1:], "_links": {}}),
    ]
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx(responses, seen))
    docs, errors = cwiki.fetch_remote("secret")
    assert errors == []
    assert set(docs) == {"wiki/Gitbox-Runbook", "wiki/Mail-Relay-Notes"}
    assert seen[0]["headers"]["Authorization"] == "Bearer secret"
    assert seen[0]["params"]["spaceKey"] == "INFRA"
    assert seen[1]["url"].endswith("/rest/api/content?start=1") and seen[1]["params"] is None
    runbook = docs["wiki/Gitbox-Runbook"]
    assert runbook["url"] == "https://cwiki.apache.org/confluence/display/INFRA/Gitbox+Runbook"
    assert runbook["updated"] == "2019-03-04"
    assert docs["wiki/Mail-Relay-Notes"]["url"].endswith("pageId=102")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_token_is_reported_without_leaking_it(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx([FakeResponse(status, {})], []))
    docs, errors = cwiki.fetch_remote("secret")
    assert docs == {}
    assert "token not accepted" in errors[0]
    assert "secret" not in errors[0]


def _with_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CWIKI_TOKEN", "secret")
    monkeypatch.setattr(
        cwiki, "fetch_remote", lambda _token: ({d["key"]: d for d in map(cwiki._doc_entry, PAGES)}, [])
    )


def test_cache_is_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wiki(monkeypatch)
    docs, origin, _ = cwiki.get_wiki_docs()
    assert len(docs) == 2 and "fetched now" in origin
    assert stat.S_IMODE(os.stat(cwiki.CACHE_FILE).st_mode) == 0o600
    monkeypatch.setattr(cwiki, "fetch_remote", lambda _token: pytest.fail("cache not used"))
    assert "cached" in cwiki.get_wiki_docs()[1]


def test_search_merges_and_labels_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wiki(monkeypatch)
    out = tools.search_docs("restart gitbox runbook")
    first = out.split("\n## ")[1]
    assert "wiki/Gitbox-Runbook" in first
    assert "Infra wiki, updated 2019-03-04" in first
    website_only = tools.search_docs("mail", source="website")
    assert "wiki/" not in website_only
    assert "Unknown source" in tools.search_docs("mail", source="jira")


def test_list_and_get_include_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wiki(monkeypatch)
    listing = tools.list_docs(source="cwiki")
    assert "## Infra wiki (cwiki, login only)" in listing
    assert "`services`" not in listing
    page = tools.get_doc("Gitbox-Runbook")
    assert "Infra wiki, updated 2019-03-04" in page
    assert "systemctl restart" in tools.find_examples(language="bash")


def test_refresh_reports_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wiki(monkeypatch)
    out = tools.refresh_docs()
    assert "Wiki: 2 pages from cwiki INFRA (fetched now)" in out
