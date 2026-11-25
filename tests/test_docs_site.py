"""Tests of the docs-site wiring: the build hook that fills the reference pages, and the build.

No network: the generator reads only the server's listings. The full-build test runs
``mkdocs build --strict`` into a temporary directory, so nothing tracked or in ``site/`` changes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("material", reason="the docs dependency group is not installed")
from docs_build import reference
from mkdocs.exceptions import Abort

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def shared_collection(monkeypatch: pytest.MonkeyPatch, server_components: list) -> None:
    """Make the hook reuse the run's one listing of the server instead of collecting its own."""
    monkeypatch.setattr(reference, "collect_components", lambda: server_components)


def load_hook() -> ModuleType:
    """Import ``hooks/reference_pages.py`` by path, as MkDocs does, as a fresh module."""
    spec = importlib.util.spec_from_file_location(
        "reference_pages_hook", REPO / "hooks/reference_pages.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_readme_links_hook() -> ModuleType:
    """Import ``hooks/readme_links.py`` by path, as MkDocs does, as a fresh module."""
    spec = importlib.util.spec_from_file_location(
        "readme_links_hook", REPO / "hooks/readme_links.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def page_at(src_uri: str) -> object:
    """Return a stand-in for a MkDocs page whose source file is ``src_uri``."""
    return type("Page", (), {"file": type("File", (), {"src_uri": src_uri})()})()


_LOGO = (
    '<source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">'
    '<img src="docs/assets/logo.svg" alt="logo" width="420">'
)


@pytest.mark.parametrize(
    ("src_uri", "prefix"), [("index.md", "assets/"), ("index.es.md", "../assets/")]
)
def test_the_readme_hook_points_the_logo_at_the_published_assets(src_uri: str, prefix: str) -> None:
    hook = load_readme_links_hook()
    out = hook.on_page_markdown(_LOGO, page_at(src_uri), None, None)
    assert f'srcset="{prefix}logo-dark.svg"' in out
    assert f'src="{prefix}logo.svg"' in out
    assert "docs/assets" not in out


def test_the_readme_hook_leaves_other_pages_and_other_text_alone() -> None:
    hook = load_readme_links_hook()
    assert hook.on_page_markdown(_LOGO, page_at("operating.md"), None, None) == _LOGO
    text = 'See `src="docs/x"` and [notes](docs/api-notes.md#a) and [x](other.md).'
    out = hook.on_page_markdown(text, page_at("index.md"), None, None)
    assert out == 'See `src="docs/x"` and [notes](api-notes.md#a) and [x](other.md).'


@pytest.mark.usefixtures("shared_collection")
def test_the_hook_generates_the_six_pages_once() -> None:
    hook = load_hook()
    first = hook._pages()
    assert set(first) == {
        f"reference/{name}{suffix}.md"
        for name in ("tools", "resources", "prompts")
        for suffix in ("", ".es")
    }
    assert "search_legislation" in first["reference/tools.md"]
    assert "search_legislation" in first["reference/tools.es.md"]
    assert "Busca normas de la legislación consolidada" in first["reference/tools.es.md"]
    assert "!!! note" in first["reference/tools.md"]
    assert hook._pages() is first  # cached for the rest of the build
    hook.on_pre_build(None)
    assert hook._pages() is not first


def test_the_hook_leaves_other_pages_alone() -> None:
    hook = load_hook()
    page = type("Page", (), {"file": type("File", (), {"src_uri": "index.md"})()})()
    assert hook.on_page_markdown("# Home", page, None, None) == "# Home"


@pytest.mark.usefixtures("shared_collection")
def test_a_stale_or_missing_summary_aborts_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = load_hook()
    real = reference.load_summaries()
    stale = dict(real)
    stale["tools.find_codes"] = dataclasses.replace(
        stale["tools.find_codes"], source_hash="000000000000"
    )
    del stale["tools.verify_citation"]
    monkeypatch.setattr(reference, "load_summaries", lambda *a, **k: stale)
    with pytest.raises(Abort) as raised:
        hook._generate()
    lines = str(raised.value).splitlines()
    assert any(line.startswith("tools.find_codes:") and "changed" in line for line in lines)
    assert any(
        line.startswith("tools.verify_citation:") and "no Spanish summary" in line for line in lines
    )


def test_the_strict_build_publishes_the_reference_pages(tmp_path: Path) -> None:
    out = tmp_path / "site"
    env = {**os.environ, "NO_MKDOCS_2_WARNING": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(out)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    english = (out / "reference/tools/index.html").read_text(encoding="utf-8")
    spanish = (out / "es/reference/tools/index.html").read_text(encoding="utf-8")
    assert "search_legislation" in english
    assert "search_legislation" in spanish
    assert "Busca normas de la legislación consolidada" in spanish
    assert not list(out.rglob("summaries*.toml"))
    # The logo, the favicon and the stylesheet resolve from the English and the Spanish pages.
    for page in ("index.html", "es/index.html", "es/run/uvx/index.html"):
        html = (out / page).read_text(encoding="utf-8")
        for name in ("logo-icon.svg", "favicon.svg", "extra.css"):
            ref = re.search(rf'(?:src|href)="([^"]*assets/{re.escape(name)})"', html)
            assert ref, (page, name)
            assert ((out / page).parent / ref.group(1)).resolve().is_file(), (page, name)
    for page in ("index.html", "es/index.html"):
        html = (out / page).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|srcset)="([^"]*assets/logo(?:-dark)?\.svg)"', html):
            assert ((out / page).parent / ref).resolve().is_file(), (page, ref)
