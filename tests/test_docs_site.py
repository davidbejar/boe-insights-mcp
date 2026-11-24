"""Tests of the docs-site wiring: the build hook that fills the reference pages, and the build.

No network: the generator reads only the server's listings. The full-build test runs
``mkdocs build --strict`` into a temporary directory, so nothing tracked or in ``site/`` changes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("material", reason="the docs dependency group is not installed")
from docs_build import reference
from mkdocs.exceptions import Abort

REPO = Path(__file__).resolve().parent.parent


def load_hook() -> ModuleType:
    """Import ``hooks/reference_pages.py`` by path, as MkDocs does, as a fresh module."""
    spec = importlib.util.spec_from_file_location(
        "reference_pages_hook", REPO / "hooks/reference_pages.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert "Busca leyes en la legislación consolidada" in first["reference/tools.es.md"]
    assert "!!! note" in first["reference/tools.md"]
    assert hook._pages() is first  # cached for the rest of the build
    hook.on_pre_build(None)
    assert hook._pages() is not first


def test_the_hook_leaves_other_pages_alone() -> None:
    hook = load_hook()
    page = type("Page", (), {"file": type("File", (), {"src_uri": "index.md"})()})()
    assert hook.on_page_markdown("# Home", page, None, None) == "# Home"


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
    assert "Busca leyes en la legislación consolidada" in spanish
    assert not list(out.rglob("summaries*.toml"))
