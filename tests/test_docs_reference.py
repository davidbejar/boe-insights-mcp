"""Tests of ``docs_build.reference``, the generator of the reference pages of the docs site.

The pure rendering and checking tests use small synthetic components, so they say exactly what
each rule is. The integration tests use the real server (built without network, with an
in-memory saved-search store) and the real Spanish summaries file, so a new tool, resource or
prompt without a summary, or a changed description, fails the suite and not only the docs build.
"""

from __future__ import annotations

import dataclasses
import re
import socket
import tomllib
from pathlib import Path
from typing import Any

import pytest
from docs_build import __main__ as cli
from docs_build import reference
from docs_build.reference import Component, Parameter, ReferenceBuildError, Summary

REPO = Path(__file__).resolve().parent.parent
SUMMARIES = REPO / "docs_build" / "summaries.es.toml"


def tool(
    name: str = "demo_tool", description: str = "Does the demo thing.", *parameters: Parameter
) -> Component:
    """A synthetic tool."""
    return Component("tools", name, description, parameters)


def param(name: str = "law_id", description: str = "The law.", **kwargs: Any) -> Parameter:
    """A synthetic parameter; a required string unless told otherwise."""
    values: dict[str, Any] = {"type": "string", "required": True, "default": None}
    values.update(kwargs)
    return Parameter(name, description=description, **values)


def summaries_for(*components: Component, text: str = "Resumen.") -> dict[str, Summary]:
    """A matching, fresh Spanish summary for each component."""
    return {c.key: Summary(text, reference.source_hash(c)) for c in components}


# --- the source hash ---------------------------------------------------------------------------


def test_the_hash_is_twelve_hex_characters_and_stable() -> None:
    first = reference.source_hash(tool())
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert first == reference.source_hash(tool())


def test_the_hash_changes_with_the_description() -> None:
    assert reference.source_hash(tool()) != reference.source_hash(tool(description="Other."))


def test_the_hash_changes_with_a_parameter_description() -> None:
    before = tool("t", "D.", param(description="One."))
    after = tool("t", "D.", param(description="Two."))
    assert reference.source_hash(before) != reference.source_hash(after)


def test_the_hash_ignores_rewrapping_and_what_is_not_prose() -> None:
    base = tool("t", "A long\ndescription here.", param(type="string"))
    rewrapped = tool("t", "A long   description\nhere.", param(type="integer", default="3"))
    assert reference.source_hash(base) == reference.source_hash(rewrapped)


def test_the_hash_does_not_depend_on_the_order_of_parameters() -> None:
    a, b = param("a", "A."), param("b", "B.")
    assert reference.source_hash(tool("t", "D.", a, b)) == reference.source_hash(
        tool("t", "D.", b, a)
    )


# --- the two build checks ----------------------------------------------------------------------


def test_a_complete_and_fresh_set_has_no_problems() -> None:
    components = [
        tool("a"),
        Component("prompts", "p", "A prompt."),
        Component("resources", "r", "R."),
    ]
    assert reference.check_summaries(components, summaries_for(*components)) == []


def test_a_missing_summary_names_the_entry_and_what_to_do() -> None:
    kept, missing = tool("kept"), tool("missing_one")
    problems = reference.check_summaries([kept, missing], summaries_for(kept))
    assert len(problems) == 1
    assert "tools.missing_one" in problems[0]
    assert "no Spanish summary" in problems[0]
    assert "summaries.es.toml" in problems[0]


def test_an_orphan_summary_names_the_entry_and_what_to_do() -> None:
    kept, gone = tool("kept"), tool("gone")
    problems = reference.check_summaries([kept], summaries_for(kept, gone))
    assert len(problems) == 1
    assert "tools.gone" in problems[0]
    assert "no longer" in problems[0]
    assert "delete" in problems[0]


def test_a_stale_summary_names_the_entry_and_the_command_to_refresh() -> None:
    original = tool("changing", "Old words.")
    summaries = summaries_for(original)
    changed = dataclasses.replace(original, description="New words.")
    problems = reference.check_summaries([changed], summaries)
    assert len(problems) == 1
    assert "tools.changing" in problems[0]
    assert "changed" in problems[0]
    assert "--refresh tools.changing" in problems[0]


def test_a_changed_parameter_description_makes_a_tool_stale() -> None:
    original = tool("t", "D.", param(description="Old."))
    summaries = summaries_for(original)
    changed = tool("t", "D.", param(description="New."))
    assert len(reference.check_summaries([changed], summaries)) == 1


def test_every_problem_is_reported_not_only_the_first() -> None:
    a, b, c = tool("a"), tool("b"), tool("c")
    summaries = summaries_for(a, tool("orphan"))
    summaries["tools.a"] = Summary("x", "000000000000")
    problems = reference.check_summaries([a, b, c], summaries)
    assert len(problems) == 4  # a is stale, b and c are missing, orphan is orphaned


def test_problems_come_in_a_stable_order() -> None:
    components = [tool("z"), tool("a")]
    assert reference.check_summaries(components, {}) == reference.check_summaries(
        components[::-1], {}
    )


def test_rendering_spanish_refuses_when_a_check_fails() -> None:
    with pytest.raises(ReferenceBuildError) as raised:
        reference.render_pages("es", components=[tool("a")], summaries={})
    assert len(raised.value.problems) == 1
    assert "tools.a" in str(raised.value)


def test_rendering_english_needs_no_summaries() -> None:
    pages = reference.render_pages("en", components=[tool("a")], summaries={})
    assert "`a`" in pages["tools"]


# --- the summaries file and refreshing one hash -------------------------------------------------

TOML = """\
# Hand-written; do not reorder.
[tools.one]
summary = "Uno."
source_hash = "aaaaaaaaaaaa"

[tools.two]
summary = "Dos, con \\"comillas\\"."
source_hash = "bbbbbbbbbbbb"  # keep this comment

[prompts.three]
summary = "Tres."
source_hash = "cccccccccccc"
"""


def test_load_summaries_reads_each_table_under_its_key(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    path.write_text(TOML, encoding="utf-8")
    loaded = reference.load_summaries(path)
    assert loaded["tools.one"] == Summary("Uno.", "aaaaaaaaaaaa")
    assert loaded["prompts.three"].text == "Tres."
    assert set(loaded) == {"tools.one", "tools.two", "prompts.three"}


@pytest.mark.parametrize(
    "body",
    [
        "[tools.a]\nsource_hash = 'x'\n",
        "[tools.a]\nsummary = 'x'\n",
        "[tools.a]\nsummary = ''\nsource_hash = 'x'\n",
        "[tool.a]\nsummary = 'x'\nsource_hash = 'y'\n",
        "this is = = not toml",
    ],
)
def test_a_malformed_summaries_file_is_a_build_error(tmp_path: Path, body: str) -> None:
    path = tmp_path / "s.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ReferenceBuildError):
        reference.load_summaries(path)


def test_refreshing_one_entry_changes_only_its_hash_line(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    path.write_text(TOML, encoding="utf-8")
    components = [tool("one"), tool("two", "Changed."), Component("prompts", "three", "P.")]
    new = reference.refresh_summary_hash("tools.two", components, path)
    assert new == reference.source_hash(components[1])
    expected = TOML.replace('"bbbbbbbbbbbb"', f'"{new}"')
    assert path.read_text(encoding="utf-8") == expected
    assert tomllib.loads(expected)["tools"]["two"]["source_hash"] == new


def test_refreshing_the_last_table_of_the_file_works(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    path.write_text(TOML, encoding="utf-8")
    components = [Component("prompts", "three", "P.")]
    new = reference.refresh_summary_hash("prompts.three", components, path)
    text = path.read_text(encoding="utf-8")
    assert f'source_hash = "{new}"' in text
    assert 'source_hash = "aaaaaaaaaaaa"' in text


def test_refreshing_an_unknown_entry_is_an_error_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    path.write_text(TOML, encoding="utf-8")
    with pytest.raises(ReferenceBuildError, match=r"tools\.nope"):
        reference.refresh_summary_hash("tools.nope", [tool("one")], path)
    with pytest.raises(ReferenceBuildError, match=r"tools\.one"):
        reference.refresh_summary_hash("tools.one", [], path)  # not on the server
    assert path.read_text(encoding="utf-8") == TOML


# --- escaping ----------------------------------------------------------------------------------

NASTY = (
    "Pipe a|b, <script>alert(1)</script>, `code`, *star*, _under_, [link](x) & more.\nSecond line."
)


def cells(row: str) -> list[str]:
    """Split a table row on its unescaped pipes."""
    return re.split(r"(?<!\\)\|", row.strip())[1:-1]


def test_escape_markdown_neutralises_significant_characters() -> None:
    out = reference.escape_markdown(NASTY)
    assert "<" not in out
    assert ">" not in out
    assert "&lt;script&gt;" in out
    assert "a\\|b" in out
    assert "\\`code\\`" in out
    assert "\\*star\\*" in out
    assert "\\_under\\_" in out
    assert "\\[link\\]" in out
    assert "&amp; more" in out


def test_escape_markdown_protects_a_line_that_would_become_a_list_or_heading() -> None:
    assert reference.escape_markdown("- not a list").startswith("\\-")
    assert reference.escape_markdown("# not a heading").startswith("\\#")
    assert reference.escape_markdown("1. not a list").startswith("1\\.")


def test_a_table_cell_stays_on_one_row_with_the_right_number_of_cells() -> None:
    page = reference.render_pages(
        "en", components=[tool("t", "D.", param("p|q", NASTY, type="string"))], summaries={}
    )["tools"]
    rows = [line for line in page.splitlines() if line.startswith("| ") and "alert" in line]
    assert len(rows) == 1
    assert len(cells(rows[0])) == 4
    assert "<script>" not in page


def test_a_description_with_angle_brackets_is_escaped_in_the_body() -> None:
    page = reference.render_pages(
        "en", components=[tool("t", "Use <b>this</b> | now.\n\nSecond paragraph.")], summaries={}
    )["tools"]
    assert "Use &lt;b&gt;this&lt;/b&gt; \\| now." in page
    assert "\nSecond paragraph.\n" in page


def test_the_description_keeps_its_words_when_reflowed() -> None:
    page = reference.render_pages(
        "en", components=[tool("t", "Line one\nline two\n\nPara two.")], summaries={}
    )["tools"]
    assert "Line one line two\n\nPara two." in page


# --- both languages' labels, grouping, structure ------------------------------------------------


def sample() -> list[Component]:
    """Two ordinary tools, the five saved-search tools, two resources and a prompt."""
    saved = [tool(n, f"{n} text.") for n in sorted(reference.SAVED_SEARCH_TOOLS)]
    return [
        tool("search_legislation", "Find laws.", param("title", "Words.", required=False)),
        tool("get_daily_summary", "A day."),
        *saved,
        Component("resources", "saved_searches", "Your searches.", uri="boe://saved-searches"),
        Component("resources", "law", "A law.", uri="boe://law/{law_id}", is_template=True),
        Component(
            "prompts",
            "explain_law",
            "Explain a law.",
            (param("law", "The law."), param("days", "Days.", type="integer", required=False)),
        ),
    ]


def test_english_labels() -> None:
    pages = reference.render_pages("en", components=sample(), summaries={})
    assert pages["tools"].startswith("# Tools\n")
    assert pages["resources"].startswith("# Resources\n")
    assert pages["prompts"].startswith("# Prompts\n")
    assert "| Parameter | Type | Required | Description |" in pages["tools"]
    assert "| Argument | Type | Required | Description |" in pages["prompts"]
    assert "boe://law/{law_id}" in pages["resources"]
    assert "## Resource templates" in pages["resources"]
    assert "## Static resources" in pages["resources"]


def test_spanish_labels_and_the_summary_next_to_each_entry() -> None:
    components = sample()
    summaries = summaries_for(*components, text="Resumen en español.")
    pages = reference.render_pages("es", components=components, summaries=summaries)
    assert pages["tools"].startswith("# Herramientas\n")
    assert pages["resources"].startswith("# Recursos\n")
    assert pages["prompts"].startswith("# Prompts\n")
    assert "| Parámetro | Tipo | Obligatorio | Descripción |" in pages["tools"]
    assert "## Plantillas de recursos" in pages["resources"]
    assert pages["tools"].count("Resumen en español.") == 7
    assert "Find laws." in pages["tools"]  # the English description stays
    assert "`title`" in pages["tools"]  # so does the parameter name


def test_the_spanish_summary_is_escaped_too() -> None:
    c = tool("a")
    pages = reference.render_pages(
        "es", components=[c], summaries={c.key: Summary("Usa <x> | y", reference.source_hash(c))}
    )
    assert "Usa &lt;x&gt; \\| y" in pages["tools"]


def test_required_and_default_are_shown() -> None:
    c = tool(
        "t",
        "D.",
        param("a", "A."),
        param("b", "B.", type="integer", required=False, default="20"),
        param("c", "C.", required=False),
    )
    en = reference.render_pages("en", components=[c], summaries={})["tools"]
    rows = {
        cells(line)[0].strip(): cells(line) for line in en.splitlines() if line.startswith("| `")
    }
    assert rows["`a`"][2].strip() == "yes"
    assert rows["`b`"][2].strip() == "no, default `20`"
    assert rows["`c`"][2].strip() == "no"
    es = reference.render_pages("es", components=[c], summaries=summaries_for(c))["tools"]
    assert "| `a` | `string` | sí |" in es
    assert "no, por defecto `20`" in es


def section(page: str, heading: str) -> str:
    """The text of the ``## heading`` section of a page, up to the next ``## `` heading."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", page, re.S | re.M)
    assert match, f"no section {heading!r}"
    return match.group(1)


def test_the_saved_search_tools_are_apart_and_marked_local_or_signed_in() -> None:
    page = reference.render_pages("en", components=sample(), summaries={})["tools"]
    saved = section(page, "Saved searches")
    outside = page.replace(saved, "")
    for name in reference.SAVED_SEARCH_TOOLS:
        assert f"### `{name}`" in saved
        assert f"### `{name}`" not in outside
    assert "only" in saved
    assert "locally" in saved
    assert "signed in" in saved
    assert "### `search_legislation`" in section(page, "Finding laws")
    assert "### `get_daily_summary`" in section(page, "The daily gazette")
    es = reference.render_pages("es", components=sample(), summaries=summaries_for(*sample()))
    assert "### `save_search`" in section(es["tools"], "Búsquedas guardadas")
    assert "en local" in section(es["tools"], "Búsquedas guardadas")


def test_a_tool_without_a_group_still_appears_under_other_tools() -> None:
    page = reference.render_pages("en", components=[tool("brand_new")], summaries={})["tools"]
    assert "### `brand_new`" in section(page, "Other tools")


def test_a_tool_with_no_parameters_says_so() -> None:
    page = reference.render_pages("en", components=[tool()], summaries={})["tools"]
    assert "No parameters." in page


def test_pages_are_deterministic() -> None:
    components = sample()
    first = reference.render_pages("en", components=components, summaries={})
    assert first == reference.render_pages("en", components=list(components), summaries={})
    assert all(text.endswith("\n") and not text.endswith("\n\n") for text in first.values())


def test_page_file_names_follow_the_suffix_convention() -> None:
    assert reference.page_filename("tools", "en") == "tools.md"
    assert reference.page_filename("tools", "es") == "tools.es.md"
    assert reference.page_filename("prompts", "es") == "prompts.es.md"


# --- the real server ---------------------------------------------------------------------------


@pytest.fixture
def real_components(server_components: list[Component]) -> list[Component]:
    """The components of the full server (shared for the whole run)."""
    return server_components


@pytest.fixture
def shared_collection(monkeypatch: pytest.MonkeyPatch, server_components: list[Component]) -> None:
    """Make ``reference.collect_components`` return the shared listing instead of a new one."""
    monkeypatch.setattr(reference, "collect_components", lambda: server_components)


def test_the_full_server_has_23_tools_5_resources_and_8_prompts(
    real_components: list[Component],
) -> None:
    kinds = [c.kind for c in real_components]
    assert (kinds.count("tools"), kinds.count("resources"), kinds.count("prompts")) == (23, 5, 8)
    assert len(real_components) == 36
    assert len({c.key for c in real_components}) == 36


def test_the_resources_split_into_one_static_and_four_templates(
    real_components: list[Component],
) -> None:
    resources = [c for c in real_components if c.kind == "resources"]
    assert [c.name for c in resources if not c.is_template] == ["saved_searches"]
    assert len([c for c in resources if c.is_template]) == 4
    assert all(c.uri and c.uri.startswith("boe://") for c in resources)


def test_the_saved_search_group_is_exactly_the_five_saved_search_tools(
    real_components: list[Component],
) -> None:
    names = {c.name for c in real_components if c.kind == "tools"}
    assert names >= reference.SAVED_SEARCH_TOOLS
    assert len(reference.SAVED_SEARCH_TOOLS) == 5
    page = reference.render_pages("en", components=real_components, summaries={})["tools"]
    saved = section(page, "Saved searches")
    assert set(re.findall(r"^### `(\w+)`", saved, re.M)) == set(reference.SAVED_SEARCH_TOOLS)
    assert "Other tools" not in page  # every real tool has a group


def test_every_real_component_has_a_fresh_spanish_summary(
    real_components: list[Component],
) -> None:
    assert reference.check_summaries(real_components, reference.load_summaries()) == []


def test_the_real_pages_cover_all_36_entries_in_both_languages(
    real_components: list[Component],
) -> None:
    for language in ("en", "es"):
        pages = reference.render_pages(language, components=real_components)
        assert set(pages) == {"tools", "resources", "prompts"}
        for kind in ("tools", "resources", "prompts"):
            headings = re.findall(r"^### `([^`]+)`", pages[kind], re.M)
            expected = [c.name for c in real_components if c.kind == kind]
            assert sorted(headings) == sorted(expected)


def test_the_real_english_page_has_the_description_and_the_parameters(
    real_components: list[Component],
) -> None:
    page = reference.render_pages("en", components=real_components)["tools"]
    assert "Find laws by title, text, rank, department, scope or publication date." in page
    assert "| `rank` |" in page
    assert "`ley_organica`" in page  # an enum is listed
    assert "| `topics` | `array of object` |" in page  # a $ref resolves to a readable type


def test_the_real_prompt_arguments_do_not_show_the_schema_boilerplate(
    real_components: list[Component],
) -> None:
    page = reference.render_pages("en", components=real_components)["prompts"]
    assert "Provide a value matching" not in page
    assert "| `days` | `integer` | no |" in page


def test_the_real_output_is_byte_identical_between_two_builds() -> None:
    first = reference.render_pages("es")
    second = reference.render_pages("es", components=reference.collect_components())
    assert first == second


# --- the command line --------------------------------------------------------------------------


def test_the_cli_writes_the_six_files_and_nothing_else(
    tmp_path: Path, real_components: list[Component]
) -> None:
    out = tmp_path / "out"
    assert cli.main(["--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "prompts.es.md",
        "prompts.md",
        "resources.es.md",
        "resources.md",
        "tools.es.md",
        "tools.md",
    ]
    assert (out / "tools.md").read_text(encoding="utf-8") == reference.render_pages(
        "en", components=real_components
    )["tools"]
    assert (out / "tools.es.md").read_text(encoding="utf-8") == reference.render_pages(
        "es", components=real_components
    )["tools"]


def test_the_cli_makes_no_network_call_and_writes_only_to_the_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    before = SUMMARIES.read_bytes()
    assert cli.main(["--out", str(tmp_path / "out")]) == 0
    assert list(work.iterdir()) == []  # nothing in the working directory
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "work"]
    assert SUMMARIES.read_bytes() == before


@pytest.mark.usefixtures("shared_collection")
def test_the_cli_fails_with_one_line_per_problem_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tomllib.loads(SUMMARIES.read_text(encoding="utf-8"))
    lines: list[str] = []
    for kind, entries in real.items():
        for name, entry in entries.items():
            if (kind, name) == ("tools", "find_codes"):
                continue  # one summary missing
            stale = (kind, name) == ("tools", "read_article")  # one summary stale
            lines += [
                f"[{kind}.{name}]",
                f'summary = "{entry["summary"]}"',
                f'source_hash = "{"000000000000" if stale else entry["source_hash"]}"',
            ]
    lines += ["[prompts.ghost]", 'summary = "x"', 'source_hash = "y"']  # one orphan
    path = tmp_path / "s.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["--out", str(out), "--summaries", str(path)]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 3
    assert any("tools.find_codes" in line for line in err)
    assert any("tools.read_article" in line for line in err)
    assert any("prompts.ghost" in line for line in err)
    assert not out.exists()


def test_the_cli_prints_the_hash_of_one_entry(
    capsys: pytest.CaptureFixture[str], real_components: list[Component]
) -> None:
    assert cli.main(["--hash", "tools.find_codes"]) == 0
    expected = reference.source_hash(next(c for c in real_components if c.name == "find_codes"))
    assert capsys.readouterr().out.strip() == expected
    assert cli.main(["--hash", "tools.nope"]) == 1


@pytest.mark.usefixtures("shared_collection")
def test_the_cli_refreshes_one_hash_in_a_copy_of_the_file(tmp_path: Path) -> None:
    source = SUMMARIES.read_text(encoding="utf-8")
    entry = tomllib.loads(source)["tools"]["find_codes"]
    path = tmp_path / "s.toml"
    path.write_text(source.replace(entry["source_hash"], "000000000000", 1), encoding="utf-8")
    assert cli.main(["--out", str(tmp_path / "o"), "--summaries", str(path)]) == 1
    assert cli.main(["--refresh", "tools.find_codes", "--summaries", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == source
    assert cli.main(["--out", str(tmp_path / "o"), "--summaries", str(path)]) == 0
