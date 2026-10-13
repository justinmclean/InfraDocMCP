"""Tests for the .asf.yaml reference README source, and the short doc map."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from infra_doc_mcp import asfyaml, corpus, cwiki, tools

README = textwrap.dedent("""\
    > [!Note]
    > This is a work in progress.

    # Working with .asf.yaml

    <h2 id="github">GitHub settings</h2>

    <h3 id="branchpro">Branch protection</h3>

    Projects can enable branch protection in their repos. Here are some examples:

    ~~~yaml
    github:
      protected_branches:
        main:
          required_status_checks:
            # strict means "Require branches to be up to date before merging".
            strict: true
          required_pull_request_reviews:
            required_approving_review_count: 3
    ~~~

    <h3 id="pages">GitHub Pages</h3>

    Pages can be enabled per branch.
    """)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CWIKI_TOKEN", raising=False)
    monkeypatch.setenv("CWIKI_TOKEN_FILE", str(tmp_path / "no-token"))
    monkeypatch.setattr(cwiki, "CACHE_FILE", tmp_path / "cwiki_cache.json")
    monkeypatch.setattr(corpus, "CACHE_FILE", tmp_path / "docs_cache.json")
    monkeypatch.setattr(asfyaml, "CACHE_FILE", tmp_path / "asfyaml_cache.json")
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: (None, ["asfyaml: offline"]))
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "asf-yaml.md").write_text(
        "Title: Working with .asf.yaml\n\nThe full reference is in the infrastructure-asfyaml README.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INFRA_DOCS_ROOT", str(pages))
    checkout = tmp_path / "asfyaml"
    checkout.mkdir()
    (checkout / "README.md").write_text(README, encoding="utf-8")
    monkeypatch.setenv("INFRA_ASFYAML_ROOT", str(checkout))


def test_local_readme_is_indexed_with_headings() -> None:
    docs, origin, errors = corpus.get_docs()
    assert set(docs) == {"asf-yaml", "asfyaml/readme"}
    assert "asfyaml checkout" in origin and errors == []
    doc = docs["asfyaml/readme"]
    assert doc["url"] == asfyaml.SITE_URL
    assert "### Branch protection" in doc["text"]
    assert "~~~yaml" in doc["text"]


def test_branch_protection_example_is_found_under_its_heading() -> None:
    out = tools.find_examples("asf.yaml branch protection")
    first = out.split("\n## ")[1]
    assert "asfyaml/readme" in first
    assert "**Section:** Branch protection" in out
    assert "protected_branches" in out


def test_search_labels_the_reference() -> None:
    out = tools.search_docs("required approving reviews branch protection")
    first = out.split("\n## ")[1]
    assert "`asfyaml/readme`, .asf.yaml reference" in first
    only = tools.search_docs("branch protection", source="asfyaml")
    assert "`asf-yaml`" not in only


def test_comment_in_fence_is_not_a_heading() -> None:
    text = corpus.md_to_text(README)[0].split("\n")
    line = next(i for i, row in enumerate(text) if "strict: true" in row)
    assert tools._nearest_heading(text, line) == "Branch protection"


def test_github_fallback_uses_cache_then_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFRA_ASFYAML_ROOT")
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: (README, []))
    docs, origin, _ = asfyaml.get_asfyaml_docs()
    assert set(docs) == {"asfyaml/readme"} and "fetched now" in origin
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: pytest.fail("cache not used"))
    assert "cached" in asfyaml.get_asfyaml_docs()[1]
    monkeypatch.setattr(asfyaml, "fetch_remote", lambda: (None, ["asfyaml: offline"]))
    docs, origin, errors = asfyaml.get_asfyaml_docs(force=True)
    assert "stale cache" in origin and errors == ["asfyaml: offline"]


def test_unavailable_readme_leaves_website_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFRA_ASFYAML_ROOT")
    docs, origin, errors = corpus.get_docs()
    assert set(docs) == {"asf-yaml"}
    assert errors == ["asfyaml: offline"]


def test_doc_map_is_short_unless_full_map_requested() -> None:
    short = tools.explain_doc_map("pmc")
    assert "## Recommended Entry Points" in short
    assert "`asfyaml/readme`" in short.split("## Sample Pages")[0]
    assert "## Current Map" not in short
    assert "list_docs" in short
    assert "## Current Map" in tools.explain_doc_map("pmc", full_map=True)
