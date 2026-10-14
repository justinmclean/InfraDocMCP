# Infra Docs MCP

MCP server for answering questions from the Apache Software Foundation Infrastructure documentation at [infra.apache.org](https://infra.apache.org).

The corpus is the `content/pages` tree of [apache/infrastructure-website](https://github.com/apache/infrastructure-website), around 140 pages covering committer accounts and MFA, mailing lists, Git, GitHub and `.asf.yaml`, GitHub Actions policy, release signing and distribution, project websites and ASF Pelican, Jira, VMs and other Infra services and policies. Pages are keyed by site path without `.html` (`release-signing`, `reference/committer/git`) and linked to infra.apache.org.

It also indexes the `.asf.yaml` reference, the README of [apache/infrastructure-asfyaml](https://github.com/apache/infrastructure-asfyaml), keyed `asfyaml/readme`. That is where the full documentation for branch protection, notifications, staging, publishing and other GitHub settings lives; infra.apache.org and the wiki only point to it. The parser's developer docs under `docs/` are not indexed.

The blog (`content/blog`) is not indexed. It is dated news, and old posts would otherwise answer questions about current services.

Optionally, the Infra Confluence space (cwiki.apache.org, space `INFRA`) is indexed too. It requires an ASF login, so it is only read when a Confluence Personal Access Token is configured; see [Infra wiki](#infra-wiki). Results say which source they came from, and wiki results carry their last edit date.

## Tools

| Tool | What it does |
| --- | --- |
| `list_docs` | Lists every page by topic |
| `get_doc` | Returns one page in full |
| `search_docs` | Searches all sources |
| `find_examples` | Finds code and config examples |
| `extract_code_snippets` | Lists every code block |
| `get_related_docs` | Finds pages related to one page |
| `explain_doc_map` | Suggests where to start, by role |
| `find_broken_links` | Checks a local checkout for broken links |
| `refresh_docs` | Re-fetches everything, ignoring caches |

Most pages sit flat in `content/pages`, so topics come from key prefixes (`release-*`, `github-actions*`, `asf-pelican*`) rather than directories. A question made only of stop words ("what is Infra") returns the services overview.

## Stale pages

A page whose opening says it is archived, superseded or outdated is labelled so in `search_docs`, `get_doc` and `list_docs`, with the replacement URL when the notice names one (`ARCHIVED, see https://github.com/apache/infrastructure-asfyaml/...`). It is ranked lower rather than dropped, so it is still found when nothing else covers the subject. Archived and superseded pages have a replacement and drop further (score × 0.4) than outdated ones (× 0.75), which are often still the only material on their subject.

Wiki pages are also ranked lower by age, since many have not been edited in years. A page edited in the last three years keeps its full score; after that the score slides linearly to × 0.6 at seven years and stays there. Age and notice weights multiply. Website pages carry no edit date and are not aged.

Neither source has a structured marker for this, so detection reads the page's first 600 characters, using narrow patterns. Pages that merely mention mailing-list archives or a deprecated tool are not flagged.

## Corpus sources

Checked in this order:

1. **Local checkout**, when `INFRA_DOCS_ROOT` points at an infrastructure-website checkout (the repo root or `content/pages` directly). Files are read fresh on every call, so `git pull` is the refresh. Link and backlink tools need this mode.
2. **GitHub**, otherwise: the pages tree is discovered via the GitHub API and fetched from raw files, cached in `~/.cache/infra-doc-mcp/` for seven days.

The `.asf.yaml` README works the same way: `INFRA_ASFYAML_ROOT` pointing at an infrastructure-asfyaml checkout is read fresh, and otherwise the file is fetched from GitHub and cached for seven days.

Pages are Pelican markdown: a `Title:` metadata block, then markdown mixed with inline HTML. HTML headings become markdown headings so search stays section-aware, tags are stripped outside code fences, and placeholders such as `<alice>` are kept.

## Infra wiki

The `INFRA` space is login-only. To index it, create a Personal Access Token in Confluence (avatar, Settings, Personal Access Tokens) and give it to the server in one of two ways:

- `CWIKI_TOKEN` in the server's environment, or
- a file at `~/.config/infra-doc-mcp/cwiki-token` (override with `CWIKI_TOKEN_FILE`), readable only by you: `chmod 600`.

Pages are fetched through the REST API, keyed `wiki/<Title-With-Dashes>`, and cached in `~/.cache/infra-doc-mcp/cwiki_cache.json` for seven days. The cache is written mode 600 because it holds private content. `refresh_docs` always re-fetches the wiki. `list_docs` and `search_docs` take `source` (`all`, `website`, `cwiki`).

The token can read whatever your account can read. Run this source locally only: never deploy a server with a wiki token to a public endpoint, and check before quoting wiki content anywhere public.

`CWIKI_BASE_URL` and `CWIKI_SPACE_KEY` override the Confluence instance and space.

## Link checking

`find_broken_links` resolves relative, root-relative and absolute `infra.apache.org` links, in both markdown and `<a href>` form, and checks anchors against heading slugs, `id=`/`name=` attributes and `{#id}` heading attributes.

Root-relative and absolute links to paths outside `content/pages` (such as `/abc/` or `/tools/pse/`) are listed as unverified rather than broken, because the site serves more than the page tree.

## Install and run

```sh
make install
make check
```

Claude Desktop or Claude Code configuration:

```json
"infra-docs": {
  "command": "/path/to/InfraDocMCP/.venv/bin/python",
  "args": ["-m", "infra_doc_mcp.server"],
  "env": {
    "INFRA_DOCS_ROOT": "/path/to/infrastructure-website",
    "INFRA_ASFYAML_ROOT": "/path/to/infrastructure-asfyaml"
  }
}
```

Set `INFRA_DOC_MCP_TRANSPORT=streamable-http` (and `PORT`) to serve over HTTP instead of stdio.

## Licence

Apache License 2.0.
