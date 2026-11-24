"""MkDocs build hook: fill the six reference pages from the server, in memory, at build time.

The files ``docs/reference/{tools,resources,prompts}[.es].md`` are placeholders. This hook
replaces their Markdown with what ``docs_build.reference`` generates from the server itself, so
the generated pages are never committed and the build never dirties tracked files.

Why ``on_page_markdown`` keyed on the source path and not ``on_files``: the i18n plugin
(suffix layout) rewrites the file list in its own ``on_files`` and the Spanish files come back
as new objects, so replacing files there depends on plugin ordering. ``on_page_markdown`` runs
after the pages exist and ``page.file.src_uri`` still carries the suffix
(``reference/tools.es.md``),
the same key ``readme_links.py`` relies on.

The generation runs once per build (once per ``mkdocs serve`` rebuild): ``on_pre_build`` clears
the cache and the first page that needs it fills it. A ``ReferenceBuildError`` is turned into a
MkDocs ``Abort`` with one line per problem, which fails the build with a non-zero exit.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from mkdocs.exceptions import Abort

# docs_build is build tooling at the repository root, outside the installed package, so it is
# importable only from the root; MkDocs may run from another directory (and loads hooks by path).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from docs_build import reference  # noqa: E402

_cache: dict[str, str] | None = None


def _generate() -> dict[str, str]:
    """Render all six pages as ``{"reference/tools.es.md": markdown, ...}``; raise ``Abort``."""
    try:
        found = reference.collect_components()
        pages: dict[str, str] = {}
        for language in ("en", "es"):
            rendered = reference.render_pages(language, components=found)
            for page, text in rendered.items():
                name = reference.page_filename(page, language)
                pages[str(PurePosixPath("reference") / name)] = text
    except reference.ReferenceBuildError as error:
        raise Abort("Reference pages cannot be built:\n" + "\n".join(error.problems)) from error
    return pages


def _pages() -> Mapping[str, str]:
    """The generated pages of this build, generated on first use."""
    global _cache
    if _cache is None:
        _cache = _generate()
    return _cache


def on_pre_build(config) -> None:
    """Forget the previous build's pages so ``mkdocs serve`` regenerates them on each rebuild."""
    global _cache
    _cache = None


def on_page_markdown(markdown: str, page, config, files) -> str:
    """Swap the placeholder's text for the generated page; leave every other page alone."""
    generated = _pages().get(page.file.src_uri)
    return markdown if generated is None else generated
