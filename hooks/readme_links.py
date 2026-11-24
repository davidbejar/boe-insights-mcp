"""MkDocs build hook: make the READMEs' repository-relative links work on the site.

The home pages (``docs/index.md`` and ``docs/index.es.md``) include ``README.md`` and
``README.es.md`` verbatim, so the READMEs keep the links that work on the repository host. This
hook rewrites those links in the included text, only for the two home pages. It must run after
the include plugin has expanded them; hooks run after plugins for ``on_page_markdown``.
"""

from __future__ import annotations

import re

REPO_BLOB = "https://github.com/davidbejar/boe-insights-mcp/blob/main/"

# Link targets as written in the READMEs -> targets that resolve on the site. The language
# links are plain URLs, because the i18n plugin renames suffixed files, so a link to
# index.es.md does not resolve; the English home is the site root and the Spanish one is /es/
# (README.md is only linked from the Spanish page, README.es.md from the English one).
# LICENSE lives outside docs/, so it points at the repository; docs/api-notes.md is a docs
# page, so it becomes a docs-relative link.
_REWRITES = {
    "README.md": "../",
    "README.es.md": "es/",
    "LICENSE": REPO_BLOB + "LICENSE",
    "docs/api-notes.md": "api-notes.md",
}
_HOME_PAGES = {"index.md", "index.es.md"}
_LINK = re.compile(r"\]\(([^)\s#]+)(#[^)\s]*)?\)")


def on_page_markdown(markdown: str, page, config, files) -> str:
    """Rewrite the READMEs' relative link targets on the home pages; leave other pages alone."""
    if page.file.src_uri not in _HOME_PAGES:
        return markdown

    def replace(match: re.Match[str]) -> str:
        """Return the link with its target swapped when it is one of the known README paths."""
        target = _REWRITES.get(match.group(1))
        if target is None:
            return match.group(0)
        return f"]({target}{match.group(2) or ''})"

    return _LINK.sub(replace, markdown)
