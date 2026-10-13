"""Tests for the Infra Docs MCP tools, run against a small fixture corpus."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from infra_doc_mcp import asfyaml, corpus, cwiki, server, tools

FIXTURES = {
    "release-signing.md": """\
        Title: Release Signing
        license: https://www.apache.org/licenses/LICENSE-2.0

        All releases must be signed with an OpenPGP key.

        <h2 id="basics">Basics<a class="headerlink" href="#basics" title="Permanent link">&para;</a></h2>

        Generate a checksum and a detached signature:

        ```bash
        gpg --armor --detach-sign apache-foo-1.0.tar.gz
        sha512sum apache-foo-1.0.tar.gz > apache-foo-1.0.tar.gz.sha512
        ```

        Replace <alice> with your Apache id.
        """,
    "asf-yaml.md": """\
        Title: Working with .asf.yaml

        ## Website staging {#staging}

        ```yaml
        staging:
          profile: ~
          whoami: asf-staging
        ```

        See [signing](release-signing.html#basics) for release keys.
        """,
    "services.md": """\
        Title: Infrastructure services

        ## Services

        Infra runs mail, Jira, Confluence and build services for every project.
        """,
    "reference/committer/git.md": """\
        Title: Git for committers
        slug: reference/committer/git

        ## Cloning

        Clone from GitBox. Read <a href="https://infra.apache.org/asf-yaml.html">.asf.yaml</a>
        and the [services list](/services.html).
        """,
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the real token, the real caches and the network.

    A developer with a wiki token configured would otherwise have the real
    INFRA space merged into every fixture, and a stray refresh would overwrite
    their cache.
    """
    scratch = tmp_path_factory.mktemp("isolated")
    monkeypatch.delenv("CWIKI_TOKEN", raising=False)
    monkeypatch.setenv("CWIKI_TOKEN_FILE", str(scratch / "no-token"))
    monkeypatch.setattr(cwiki, "CACHE_FILE", scratch / "cwiki_cache.json")
    monkeypatch.setattr(corpus, "CACHE_FILE", scratch / "docs_cache.json")
    monkeypatch.delenv("INFRA_ASFYAML_ROOT", raising=False)
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: (None, []))
    monkeypatch.setattr(asfyaml, "CACHE_FILE", scratch / "asfyaml_cache.json")


@pytest.fixture()
def docs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for relative, body in FIXTURES.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setenv("INFRA_DOCS_ROOT", str(tmp_path))
    return tmp_path


def test_md_to_text_reads_pelican_metadata() -> None:
    text, title = corpus.md_to_text("Title: Release Signing\nlicense: x\n\nBody here.")
    assert title == "Release Signing"
    assert "license:" not in text
    assert text == "Body here."


def test_md_to_text_without_metadata_keeps_body() -> None:
    text, title = corpus.md_to_text("Note: this is prose\nthat continues.")
    assert title is None
    assert "Note: this is prose" in text


def test_html_heading_becomes_markdown_heading(docs_root: Path) -> None:
    text, _ = corpus.md_to_text((docs_root / "release-signing.md").read_text(encoding="utf-8"))
    assert "## Basics" in text
    assert "&para;" not in text and "¶" not in text
    assert "headerlink" not in text


def test_placeholders_and_fences_survive(docs_root: Path) -> None:
    text, _ = corpus.md_to_text((docs_root / "release-signing.md").read_text(encoding="utf-8"))
    assert "<alice>" in text
    assert "gpg --armor --detach-sign" in text


def test_heading_attribute_is_dropped(docs_root: Path) -> None:
    text, _ = corpus.md_to_text((docs_root / "asf-yaml.md").read_text(encoding="utf-8"))
    assert "## Website staging" in text
    assert "{#staging}" not in text


def test_links_become_plain_text(docs_root: Path) -> None:
    text, _ = corpus.md_to_text((docs_root / "reference/committer/git.md").read_text(encoding="utf-8"))
    assert "Read .asf.yaml" in text
    assert "services list" in text
    assert "](" not in text and "<a " not in text


def test_load_local_keys_and_urls(docs_root: Path) -> None:
    docs = corpus.load_local(docs_root)
    assert set(docs) == {"release-signing", "asf-yaml", "services", "reference/committer/git"}
    assert docs["reference/committer/git"]["url"] == "https://infra.apache.org/reference/committer/git.html"
    assert docs["asf-yaml"]["title"] == "Working with .asf.yaml"


def test_list_docs_groups_by_topic(docs_root: Path) -> None:
    out = tools.list_docs()
    assert "## Releases and signing" in out
    assert "## Git, GitHub and Subversion" in out
    assert "`release-signing`" in out
    assert "local checkout" in out


def test_section_prefix_order() -> None:
    assert tools._section_for("github-pages") == "websites"
    assert tools._section_for("github-actions-policy") == "builds"
    assert tools._section_for("github-roles") == "vcs"
    assert tools._section_for("committer-email") == "mail"
    assert tools._section_for("release-download-pages") == "websites"
    assert tools._section_for("release-signing") == "releases"


def test_get_doc_exact_and_fuzzy(docs_root: Path) -> None:
    assert "detach-sign" in tools.get_doc("release-signing")
    assert "GitBox" in tools.get_doc("committer/git")
    assert "Unknown doc key" in tools.get_doc("nonexistent")


def test_search_finds_signing(docs_root: Path) -> None:
    out = tools.search_docs("how do I sign a release with gpg")
    assert "release-signing" in out.split("##")[1]


def test_search_variant_maps_site_to_website(docs_root: Path) -> None:
    out = tools.search_docs("site staging")
    assert "asf-yaml" in out.split("##")[1]


def test_search_empty_query(docs_root: Path) -> None:
    assert "provide a search query" in tools.search_docs("   ").lower()


def test_stop_word_only_query_returns_overview(docs_root: Path) -> None:
    for query in ("what is Infra", "what is ASF infrastructure", "apache"):
        out = tools.search_docs(query)
        assert out.startswith("# Overview"), query
        assert "`services`" in out, query


def test_refresh_reports_local_mode(docs_root: Path) -> None:
    out = tools.refresh_docs()
    assert "local checkout" in out
    assert "4 pages" in out


def test_find_examples_filters_by_language(docs_root: Path) -> None:
    out = tools.find_examples(language="yaml")
    assert "whoami: asf-staging" in out
    assert "gpg" not in out


def test_find_examples_uses_query_terms(docs_root: Path) -> None:
    out = tools.find_examples("checksum signature")
    assert "release-signing" in out.split("##")[1]


def test_find_examples_reports_empty_filter(docs_root: Path) -> None:
    assert "No code examples found" in tools.find_examples(language="ruby")


def test_extract_code_snippets_inventory_and_filters(docs_root: Path) -> None:
    out = tools.extract_code_snippets()
    assert "Languages: bash=1, yaml=1" in out
    filtered = tools.extract_code_snippets(language="bash", key="release")
    assert "sha512sum" in filtered and "whoami" not in filtered
    assert "No docs matched" in tools.extract_code_snippets(key="missing")


def test_get_related_docs_uses_links(docs_root: Path) -> None:
    out = tools.get_related_docs("reference/committer/git")
    assert "asf-yaml" in out and "services" in out
    assert "linked from source" in out
    backlinked = tools.get_related_docs("release-signing")
    assert "links to source" in backlinked


def test_get_related_docs_reports_unknown_key(docs_root: Path) -> None:
    assert "Unknown doc key" in tools.get_related_docs("missing/page")


def test_explain_doc_map_audience_and_goal(docs_root: Path) -> None:
    out = tools.explain_doc_map("release-manager")
    assert "ASF Infrastructure Documentation Map" in out
    assert "`release-signing`" in out.split("## Current Map")[0]
    goal = tools.explain_doc_map("unknown", "git cloning")
    assert "`reference/committer/git`" in goal.split("## Current Map")[0]


def test_find_broken_links_passes_clean_fixture(docs_root: Path) -> None:
    out = tools.find_broken_links()
    assert "Internal links checked: 4" in out
    assert "Problems found: 0" in out


def test_find_broken_links_reports_missing_page_and_anchor(docs_root: Path) -> None:
    target = docs_root / "services.md"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\n[Missing](missing-page.html)\n<a href="asf-yaml.html#nope">bad</a>\n[ok](asf-yaml.html#staging)\n',
        encoding="utf-8",
    )
    out = tools.find_broken_links()
    assert "Problems found: 2" in out
    assert "missing page `missing-page`" in out
    assert "missing anchor `#nope`" in out


def test_find_broken_links_ignores_links_in_fences_and_blog(docs_root: Path) -> None:
    target = docs_root / "services.md"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n```\n[x](nowhere.html)\n```\n[blog](https://infra.apache.org/blog/)\n",
        encoding="utf-8",
    )
    assert "Problems found: 0" in tools.find_broken_links()


def test_find_broken_links_checks_each_external_url_once(
    docs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.invalid/shared"
    for name in ("services.md", "asf-yaml.md"):
        target = docs_root / name
        target.write_text(target.read_text(encoding="utf-8") + f"\n[Shared]({url})\n", encoding="utf-8")
    calls: list[str] = []

    def fake_check(target: str) -> str:
        calls.append(target)
        return "failed for test"

    monkeypatch.setattr(tools, "_check_external_url", fake_check)
    out = tools.find_broken_links(check_external=True)
    assert calls == [url]
    assert "Problems found: 1" in out
    assert "`services` line" in out and "`asf-yaml` line" in out


def test_find_broken_links_needs_local_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    assert "requires a local checkout" in tools.find_broken_links()


def test_docs_root_prefers_content_pages_over_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full checkout must index content/pages, not the blog or README."""
    pages = tmp_path / "content" / "pages"
    pages.mkdir(parents=True)
    (pages / "mfa.md").write_text("Title: MFA\n\nBody.\n", encoding="utf-8")
    blog = tmp_path / "content" / "blog"
    blog.mkdir(parents=True)
    (blog / "post.md").write_text("Title: Post\n\nBlog.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Readme.\n", encoding="utf-8")

    monkeypatch.setenv("INFRA_DOCS_ROOT", str(tmp_path))
    root = corpus.docs_root()
    assert root == pages
    assert set(corpus.load_local(root)) == {"mfa"}


def test_docs_root_returns_none_for_unusable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INFRA_DOCS_ROOT", str(tmp_path / "missing"))
    assert corpus.docs_root() is None


def test_load_cache_recovers_from_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.json"
    cache.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(corpus, "CACHE_FILE", cache)
    assert corpus.load_cache() == {}


def test_save_cache_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "nested" / "cache.json"
    monkeypatch.setattr(corpus, "CACHE_FILE", cache)
    corpus.save_cache({"docs": {"index": {"title": "Home"}}})
    assert json.loads(cache.read_text(encoding="utf-8"))["docs"]["index"]["title"] == "Home"


def _write_cache(cache: Path, fetched_at: float) -> None:
    cache.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "docs": {"index": {"key": "index", "title": "Home", "url": "u", "text": "Body"}},
            }
        ),
        encoding="utf-8",
    )


def test_get_docs_uses_fresh_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache.json"
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    monkeypatch.setattr(corpus, "CACHE_FILE", cache)
    _write_cache(cache, time.time())
    docs, origin, errors = corpus.get_docs()
    assert set(docs) == {"index"}
    assert "cached" in origin
    assert errors == []


def test_get_docs_returns_stale_cache_when_refresh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.json"
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    monkeypatch.setattr(corpus, "CACHE_FILE", cache)
    _write_cache(cache, 0)
    monkeypatch.setattr(corpus, "fetch_remote", lambda: ({}, ["network down"]))
    docs, origin, errors = corpus.get_docs()
    assert set(docs) == {"index"}
    assert "stale cache" in origin
    assert errors == ["network down"]


def test_get_docs_fetches_remote_and_saves_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.json"
    fetched = {"mfa": {"key": "mfa", "title": "MFA", "url": "https://infra.apache.org/mfa.html", "text": "Body"}}
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    monkeypatch.setattr(corpus, "CACHE_FILE", cache)
    monkeypatch.setattr(corpus, "fetch_remote", lambda: (fetched, ["one warning"]))
    docs, origin, errors = corpus.get_docs(force=True)
    assert docs == fetched
    assert origin == "GitHub (fetched now)"
    assert errors == ["one warning"]
    assert json.loads(cache.read_text(encoding="utf-8"))["docs"] == fetched


def test_get_docs_reports_unavailable_without_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    monkeypatch.setattr(corpus, "CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(corpus, "fetch_remote", lambda: ({}, ["network down"]))
    docs, origin, errors = corpus.get_docs(force=True)
    assert docs == {}
    assert origin == "unavailable"
    assert errors == ["network down"]


def test_fetch_remote_discovers_pages_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None = None, text: str = "") -> None:
            self._payload = payload or {}
            self.text = text

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, url: str, **_: object) -> FakeResponse:
            if url == corpus.GITHUB_TREE_URL:
                return FakeResponse(
                    {
                        "tree": [
                            {"type": "blob", "path": "content/pages/reference/committer/git.md"},
                            {"type": "blob", "path": "content/blog/post.md"},
                            {"type": "blob", "path": "content/pages/images/logo.png"},
                        ]
                    }
                )
            return FakeResponse(text="Title: Git\n\nBody.")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=FakeClient))
    docs, errors = corpus.fetch_remote()
    assert errors == []
    assert set(docs) == {"reference/committer/git"}
    assert docs["reference/committer/git"]["title"] == "Git"


def test_fetch_remote_reports_tree_discovery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=FailingClient))
    docs, errors = corpus.fetch_remote()
    assert docs == {}
    assert errors == ["tree discovery failed: boom"]


def test_refresh_docs_reports_remote_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFRA_DOCS_ROOT", raising=False)
    monkeypatch.setattr(
        corpus,
        "get_website_docs",
        lambda force=False: (
            {"index": {"key": "index", "title": "Home", "url": "u", "text": "Body"}},
            "GitHub (fetched now)",
            ["one warning"],
        ),
    )
    out = tools.refresh_docs()
    assert "Website: refreshed 1 pages" in out
    assert "one warning" in out
    assert "Wiki: not configured" in out


def test_server_main_runs_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.delenv("INFRA_DOC_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: called.append(kwargs))
    server.main()
    assert called == [{"transport": "stdio"}]


def test_server_main_prints_and_reraises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_: object) -> None:
        raise RuntimeError("cannot start")

    monkeypatch.setattr(server.mcp, "run", fail)
    with pytest.raises(RuntimeError, match="cannot start"):
        server.main()
    assert "cannot start" in capsys.readouterr().err


def test_find_broken_links_lists_site_rooted_misses_as_unverified(docs_root: Path) -> None:
    target = docs_root / "services.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n[ABC](https://infra.apache.org/abc/)\n[Hosts](/machines.html)\n",
        encoding="utf-8",
    )
    out = tools.find_broken_links()
    assert "Problems found: 0" in out
    unverified = out.split("## Unverified Site Links")[1]
    assert "missing page `abc/index`" in unverified
    assert "missing page `machines`" in unverified


@pytest.mark.parametrize(
    ("opening", "kind", "replacement"),
    [
        ("### The authoritative version is now at https://github.com/apache/x/README.md, "
         "and this page is ARCHIVED.\nBody.", "archived", "https://github.com/apache/x/README.md"),
        ("This page has been superseded by: https://infra.apache.org/abc/, redirecting...",
         "superseded", "https://infra.apache.org/abc/"),
        ("Note: this document contains outdated information and will have a major update.",
         "outdated", ""),
    ],
)
def test_stale_notice_detected(opening: str, kind: str, replacement: str) -> None:
    assert tools._stale_notice({"text": opening}) == {"kind": kind, "replacement": replacement}


@pytest.mark.parametrize(
    "text",
    [
        "All dev@ lists are publicly archived, and subject to the archive policy.",
        "The Apache CMS is deprecated as of 2021.",
        "Intro.\n" + "filler " * 120 + "\nThis page is archived.",
    ],
)
def test_stale_notice_ignores_mentions_and_late_notices(text: str) -> None:
    assert tools._stale_notice({"text": text}) is None


def test_archived_page_is_labelled_and_ranked_below_current(docs_root: Path) -> None:
    (docs_root / "old-staging.md").write_text(
        "Title: Old staging notes\n\n"
        "Moved to https://infra.apache.org/asf-yaml.html and this page is archived.\n\n"
        "## Website staging\n\nWebsite staging staging profile whoami asf-staging.\n",
        encoding="utf-8",
    )
    out = tools.search_docs("website staging profile")
    headings = [line for line in out.splitlines() if line.startswith("## [")]
    assert "`asf-yaml`" in headings[0]
    stale = next(line for line in headings if "`old-staging`" in line)
    assert "ARCHIVED, see https://infra.apache.org/asf-yaml.html" in stale
    assert "[ARCHIVED" in tools.list_docs()
    assert "ARCHIVED" in tools.get_doc("old-staging").split("---")[0]
