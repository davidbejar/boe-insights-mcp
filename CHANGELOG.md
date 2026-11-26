# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [0.1.0] - 2026-11-26

First stable release. It is the same code as `0.1.0rc1`, apart from the documentation, which now
describes installing from PyPI and pulling the image from GHCR, and the CI and release workflows,
which are pinned to newer action versions.

As a final release it also adds the image tags `latest` and `0.1` (a pre-release only gets its exact
version tag) and publishes the documentation site, which a pre-release leaves as it is.

### Added

- 23 tools over Spain's BOE open data: law lookup, search, status overview and code lookup;
  amendment history and amendment chains; reading, comparing and tracing single articles;
  citation checks against the text; a law's table of contents; trend analytics over the
  consolidated collection; the BOE's daily gazette; and saved searches.
- Five resources and eight prompts (four, each in English and Spanish).
- stdio transport, and Streamable HTTP transport for remote use.
- Optional GitHub sign-in with an allow-list of users, and a call limit per caller.
- Container image (`ghcr.io/davidbejar/boe-insights-mcp`) for linux/amd64 and linux/arm64.
- Bilingual (English and Spanish) documentation site.
- Every result carries the BOE attribution, the informational-only notice for consolidated
  texts and the data's last-update date.

## [0.1.0rc1] - 2026-11-26

Pre-release that published the package name on PyPI and exercised the release pipeline. It has the
same content as `0.1.0`.

[0.1.0]: https://github.com/davidbejar/boe-insights-mcp/releases/tag/v0.1.0
[0.1.0rc1]: https://github.com/davidbejar/boe-insights-mcp/releases/tag/v0.1.0rc1
