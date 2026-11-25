"""Generator of the reference pages of the docs site, from the server itself.

Responsibility: list every tool, resource and prompt of the fully featured server through
FastMCP's own listing methods, and turn the list into six Markdown pages (tools, resources and
prompts, in English and in Spanish). The English pages are generated entirely from the server.
The Spanish pages have the same structure, with Spanish headings and labels and, next to each
entry, a hand-written summary read from ``docs_build/summaries.es.toml``; names, URIs,
parameter names and the English descriptions stay as they are, because they are what the model
sees.

Two checks make the build fail with a :class:`ReferenceBuildError` (see
:func:`check_summaries`): an entry without a Spanish summary (or a summary without an entry), and
a summary written against English text that has since changed. The second works with a short
hash of the English source stored beside each summary (:func:`source_hash`); only a human who
has re-read the Spanish refreshes it, for one named entry (:func:`refresh_summary_hash`).

Why this lives in a top-level ``docs_build`` package and not under ``src``: it is build tooling
for the docs site, not part of the server. The wheel packages only ``src/boe_insights_mcp``, so
nothing here is installed with, or becomes public surface of, the server. Nothing in this module
touches the network or the user's data directories: the server is built with an in-memory
store and only its listings are read; its lifespan (which would create the client) never runs.

``docs_build.__main__`` is the command line; a MkDocs build hook calls :func:`render_pages`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from boe_insights_mcp.searchstore import SavedSearchStore
from boe_insights_mcp.server import create_server

__all__ = [
    "DEFAULT_SUMMARIES",
    "PAGES",
    "SAVED_SEARCH_TOOLS",
    "Component",
    "Parameter",
    "ReferenceBuildError",
    "Summary",
    "check_summaries",
    "collect_components",
    "collect_components_async",
    "escape_markdown",
    "load_summaries",
    "page_filename",
    "refresh_summary_hash",
    "render_pages",
    "source_hash",
    "write_pages",
]

Kind = Literal["tools", "resources", "prompts"]
Language = Literal["en", "es"]

PAGES: tuple[Kind, ...] = ("tools", "resources", "prompts")
"""The page names, which are also the kinds of component and the tables of the summaries file."""

DEFAULT_SUMMARIES = Path(__file__).resolve().parent / "summaries.es.toml"
"""Where the hand-written Spanish summaries live."""

_SUMMARIES_NAME = "docs_build/summaries.es.toml"
_SINGULAR: dict[str, str] = {"tools": "tool", "resources": "resource", "prompts": "prompt"}

# Tool groups in page order, each with its tools in reading order. A tool not named here still
# appears, under "other", so a new tool is never silently left out of the page.
_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "finding",
        (
            "search_legislation",
            "resolve_reference",
            "find_codes",
            "get_law_overview",
            "get_law_structure",
        ),
    ),
    (
        "articles",
        (
            "read_article",
            "compare_article_versions",
            "get_article_history",
            "get_amendment_history",
            "explore_amendment_chain",
        ),
    ),
    (
        "trends",
        (
            "legislation_over_time",
            "top_departments",
            "compare_topics",
            "explain_spike",
            "most_amended_laws",
        ),
    ),
    ("gazette", ("get_daily_summary", "scan_gazette")),
    ("citations", ("verify_citation",)),
    (
        "saved",
        (
            "save_search",
            "list_saved_searches",
            "check_saved_searches",
            "edit_saved_search",
            "delete_saved_search",
        ),
    ),
)

SAVED_SEARCH_TOOLS: frozenset[str] = frozenset(dict(_TOOL_GROUPS)["saved"])
"""The tools that read or write a person's saved searches; they get their own section."""

_LABELS: dict[Language, dict[str, str]] = {
    "en": {
        "tools": "Tools",
        "resources": "Resources",
        "prompts": "Prompts",
        "intro_tools": (
            "This page is generated from the server's own tool definitions. The server offers "
            "{count} tools."
        ),
        "intro_resources": (
            "This page is generated from the server's own resource definitions. The server "
            "offers {count} resources, which in applications that support them a person can "
            "attach to a conversation."
        ),
        "intro_prompts": (
            "This page is generated from the server's own prompt definitions. The server offers "
            "{count} prompts, which are ready-made requests a person picks from a menu; each "
            "exists in English and in Spanish."
        ),
        "group_finding": "Finding laws",
        "group_articles": "Articles and amendments",
        "group_trends": "Trends and statistics",
        "group_gazette": "The daily gazette",
        "group_citations": "Citations",
        "group_saved": "Saved searches",
        "group_other": "Other tools",
        "note_saved": (
            "Available only when the server runs locally or when you are signed in. Saved "
            "searches belong to one person; nothing is sent automatically."
        ),
        "static_resources": "Static resources",
        "resource_templates": "Resource templates",
        "uri": "URI",
        "uri_template": "URI template",
        "mime": "MIME type",
        "parameters": "Parameters",
        "arguments": "Arguments",
        "parameter": "Parameter",
        "argument": "Argument",
        "type": "Type",
        "required": "Required",
        "description": "Description",
        "yes": "yes",
        "no": "no",
        "default": "default",
        "one_of": "one of",
        "no_parameters": "No parameters.",
        "no_arguments": "No arguments.",
        "model_description": "Description as the model receives it",
        "array_of": "array of",
    },
    "es": {
        "tools": "Herramientas",
        "resources": "Recursos",
        "prompts": "Prompts",
        "intro_tools": (
            "Esta página se genera a partir de las definiciones de herramientas del propio "
            "servidor, que ofrece {count} herramientas. Los nombres, los parámetros y las "
            "descripciones son los que recibe el modelo y se muestran tal cual; junto a cada "
            "herramienta hay un resumen en español."
        ),
        "intro_resources": (
            "Esta página se genera a partir de las definiciones de recursos del propio "
            "servidor, que ofrece {count} recursos. En las aplicaciones que los admiten, una "
            "persona puede adjuntarlos a una conversación. Los nombres, las URI y las "
            "descripciones son los que recibe el modelo y se muestran tal cual; junto a cada "
            "recurso hay un resumen en español."
        ),
        "intro_prompts": (
            "Esta página se genera a partir de las definiciones de prompts del propio servidor, "
            "que ofrece {count} prompts: peticiones ya preparadas que una persona elige en un "
            "menú; cada una existe en inglés y en español. Los nombres, los argumentos y las "
            "descripciones son los que recibe el modelo y se muestran tal cual; junto a cada "
            "prompt hay un resumen en español."
        ),
        "group_finding": "Búsqueda de normas",
        "group_articles": "Artículos y modificaciones",
        "group_trends": "Tendencias y estadísticas",
        "group_gazette": "El diario oficial día a día",
        "group_citations": "Citas",
        "group_saved": "Búsquedas guardadas",
        "group_other": "Otras herramientas",
        "note_saved": (
            "Disponibles solo cuando el servidor se ejecuta en local o con la sesión iniciada. "
            "Las búsquedas guardadas pertenecen a una persona; no se envía nada de forma "
            "automática."
        ),
        "static_resources": "Recursos estáticos",
        "resource_templates": "Plantillas de recursos",
        "uri": "URI",
        "uri_template": "Plantilla de URI",
        "mime": "Tipo MIME",
        "parameters": "Parámetros",
        "arguments": "Argumentos",
        "parameter": "Parámetro",
        "argument": "Argumento",
        "type": "Tipo",
        "required": "Obligatorio",
        "description": "Descripción",
        "yes": "sí",
        "no": "no",
        "default": "por defecto",
        "one_of": "uno de",
        "no_parameters": "Sin parámetros.",
        "no_arguments": "Sin argumentos.",
        "model_description": "Descripción tal como la recibe el modelo",
        "array_of": "array de",
    },
}


class ReferenceBuildError(Exception):
    """The reference pages cannot be built; ``problems`` has one line per problem."""

    def __init__(self, problems: Sequence[str]) -> None:
        """Keep the problems and join them, one per line, as the message."""
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Parameter:
    """One parameter of a tool or argument of a prompt, as the model is told about it."""

    name: str
    type: str
    required: bool
    default: str | None
    description: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Component:
    """One tool, resource or prompt of the server."""

    kind: Kind
    name: str
    description: str
    parameters: tuple[Parameter, ...] = ()
    uri: str | None = None
    mime_type: str | None = None
    is_template: bool = False

    @property
    def key(self) -> str:
        """The entry's key in the summaries file and in messages, e.g. ``tools.find_codes``."""
        return f"{self.kind}.{self.name}"


@dataclass(frozen=True)
class Summary:
    """A hand-written Spanish summary and the hash of the English text it was written against."""

    text: str
    source_hash: str


# --- reading the server ------------------------------------------------------------------------

# FastMCP appends this to the description of every prompt argument. It repeats the argument's own
# description and schema inside the text, so for the docs it is taken apart: the text goes in the
# description column and the schema's type in the type column.
_PROMPT_SCHEMA_NOTE = re.compile(
    r"\n\nProvide a value matching the following JSON schema: (?P<schema>\{.*\})\. "
    r"Encode non-string values as JSON\.\Z",
    re.S,
)


def _resolve(schema: Mapping[str, Any], defs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow a local ``$ref`` once; any other schema is returned as it is."""
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(target, Mapping):
            return target
    return schema


def _type_of(schema: Mapping[str, Any], defs: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return a short readable type and the allowed values of a JSON schema (``null`` is dropped).

    ``null`` is dropped because whether a value may be left out is the "required" column's job.
    """
    schema = _resolve(schema, defs)
    options = schema.get("anyOf")
    if isinstance(options, list):
        found = [
            _type_of(option, defs)
            for option in options
            if isinstance(option, Mapping) and option.get("type") != "null"
        ]
        if not found:
            return "any", ()
        label = " or ".join(dict.fromkeys(label for label, _ in found))
        return label, tuple(value for _, values in found for value in values)
    choices = tuple(str(value) for value in schema.get("enum", ()))
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items")
        inner = _type_of(items, defs)[0] if isinstance(items, Mapping) else "any"
        return f"array of {inner}", ()
    if isinstance(kind, str):
        return (f"{kind} ({schema['format']})" if "format" in schema else kind), choices
    return "any", choices


def _parameters_from_schema(schema: Mapping[str, Any]) -> tuple[Parameter, ...]:
    """The parameters of a tool's input schema, in the order the schema lists them."""
    defs = schema.get("$defs", {})
    required = set(schema.get("required", ()))
    found: list[Parameter] = []
    for name, prop in schema.get("properties", {}).items():
        label, choices = _type_of(prop, defs)
        default = prop.get("default")
        found.append(
            Parameter(
                name=name,
                type=label,
                required=name in required,
                default=None if default is None else json.dumps(default, ensure_ascii=False),
                description=str(prop.get("description", "")),
                choices=choices,
            )
        )
    return tuple(found)


def _prompt_parameters(arguments: Sequence[Any]) -> tuple[Parameter, ...]:
    """The arguments of a prompt, with FastMCP's appended schema note taken apart."""
    found: list[Parameter] = []
    for argument in arguments:
        description = argument.description or ""
        label, choices = "string", ()
        note = _PROMPT_SCHEMA_NOTE.search(description)
        if note is not None:
            description = description[: note.start()]
            with contextlib.suppress(json.JSONDecodeError):
                label, choices = _type_of(json.loads(note.group("schema")), {})
        found.append(
            Parameter(
                name=argument.name,
                type=label,
                required=bool(argument.required),
                default=None,
                description=description,
                choices=choices,
            )
        )
    return tuple(found)


async def collect_components_async() -> list[Component]:
    """List every tool, resource and prompt of the fully featured server, in registration order.

    The server gets an in-memory saved-search store, so the saved-search tools are present and
    nothing touches the disk, and only its listing methods are called: its lifespan, which would
    create the BOE client, never runs, so there is no network access.
    """
    # ``settings=None`` is the stdio (local) mode, the one that exposes all 36 entries.
    server = create_server(store=SavedSearchStore.in_directory(None))
    components: list[Component] = [
        Component(
            "tools", tool.name, tool.description or "", _parameters_from_schema(tool.parameters)
        )
        for tool in await server.list_tools()
    ]
    components += [
        Component(
            "resources",
            resource.name,
            resource.description or "",
            uri=str(resource.uri),
            mime_type=resource.mime_type,
        )
        for resource in await server.list_resources()
    ]
    components += [
        Component(
            "resources",
            template.name,
            template.description or "",
            uri=template.uri_template,
            mime_type=template.mime_type,
            is_template=True,
        )
        for template in await server.list_resource_templates()
    ]
    components += [
        Component(
            "prompts",
            prompt.name,
            prompt.description or "",
            _prompt_parameters(prompt.arguments or []),
        )
        for prompt in await server.list_prompts()
    ]
    return components


def collect_components() -> list[Component]:
    """Synchronous :func:`collect_components_async`, usable from a build hook.

    Must not be called from inside a running event loop (``asyncio.run`` raises there); the
    command line and the MkDocs hook, its only callers, run synchronously.
    """
    return asyncio.run(collect_components_async())


# --- the hash and the two checks ---------------------------------------------------------------


def _words(text: str) -> str:
    """Collapse all whitespace, so re-wrapping a description never changes a hash."""
    return " ".join(text.split())


def source_hash(component: Component) -> str:
    """Return the 12-hex-character hash of the English text a Spanish summary is written against.

    It covers the description and, for tools and prompts, the parameter names and descriptions
    (sorted by name, whitespace collapsed). Types and defaults are not prose a summary
    paraphrases, so changing them does not make a summary stale.
    """
    payload = {
        "description": _words(component.description),
        "parameters": sorted([p.name, _words(p.description)] for p in component.parameters),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def check_summaries(components: Sequence[Component], summaries: Mapping[str, Summary]) -> list[str]:
    """Return one line per problem between the server and the summaries; empty when all is well.

    The problems, in the order of the entry keys: an entry without a summary, a summary without
    an entry, and a summary whose stored hash is not the hash of the entry's current English text.
    """
    current = {component.key: component for component in components}
    problems: list[str] = []
    for key in sorted(current.keys() | summaries.keys()):
        component, summary = current.get(key), summaries.get(key)
        if summary is None:
            problems.append(
                f"{key}: no Spanish summary. Add a [{key}] table to {_SUMMARIES_NAME} with "
                f"summary and source_hash (`python -m docs_build --hash {key}` prints the hash)."
            )
        elif component is None:
            problems.append(
                f"{key}: the server no longer has this {_SINGULAR[key.split('.', 1)[0]]}; "
                f"delete the [{key}] table from {_SUMMARIES_NAME}."
            )
        elif summary.source_hash != source_hash(component):
            problems.append(
                f"{key}: the English text changed after the Spanish summary was written "
                f"(summary written against {summary.source_hash}, now {source_hash(component)}). "
                f"Re-read the summary in {_SUMMARIES_NAME}, rewrite it if needed, then run "
                f"`python -m docs_build --refresh {key}`."
            )
    return problems


def load_summaries(path: Path = DEFAULT_SUMMARIES) -> dict[str, Summary]:
    """Read the summaries file into ``{"tools.name": Summary}``.

    Raises ``ReferenceBuildError`` for a file that is missing, is not TOML, or has a table that
    is not ``[tools.x]``, ``[resources.x]`` or ``[prompts.x]`` with a non-empty ``summary`` and
    ``source_hash``.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReferenceBuildError(
            [f"{path}: cannot read the Spanish summaries: {error}"]
        ) from error
    problems: list[str] = []
    found: dict[str, Summary] = {}
    for kind, entries in data.items():
        if kind not in PAGES or not isinstance(entries, dict):
            problems.append(
                f"{path}: unexpected [{kind}]; the tables are [tools.NAME], [resources.NAME] "
                "and [prompts.NAME]."
            )
            continue
        for name, entry in entries.items():
            text = entry.get("summary") if isinstance(entry, dict) else None
            digest = entry.get("source_hash") if isinstance(entry, dict) else None
            if not (isinstance(text, str) and text.strip() and isinstance(digest, str) and digest):
                problems.append(
                    f"{kind}.{name}: the table needs a non-empty summary and a source_hash "
                    f"(`python -m docs_build --hash {kind}.{name}` prints the hash)."
                )
                continue
            found[f"{kind}.{name}"] = Summary(text.strip(), digest)
    if problems:
        raise ReferenceBuildError(problems)
    return found


def refresh_summary_hash(
    key: str, components: Sequence[Component], path: Path = DEFAULT_SUMMARIES
) -> str:
    """Store the current hash of ONE entry in the summaries file and return it.

    Call it only after a human has re-read the Spanish summary against the changed English. The
    file is edited in place, on that one ``source_hash`` line, so the hand-written text, comments
    and order of everything else stay byte for byte as they were.

    Raises ``ReferenceBuildError`` when the server has no such entry or the file has no table
    for it (the file is then not written).
    """
    component = next((c for c in components if c.key == key), None)
    if component is None:
        raise ReferenceBuildError([f"{key}: the server has no such entry; check the name."])
    text = path.read_bytes().decode("utf-8")
    header = re.search(rf"^\[{re.escape(key)}\][ \t]*(?:#.*)?$", text, re.M)
    if header is None:
        raise ReferenceBuildError(
            [f"{key}: no [{key}] table in {path}; write its summary first, with any source_hash."]
        )
    following = re.search(r"^\[", text[header.end() :], re.M)
    end = header.end() + following.start() if following else len(text)
    line = re.search(
        r"""^(source_hash[ \t]*=[ \t]*)(["'])[^"'\n]*\2""", text[header.end() : end], re.M
    )
    if line is None:
        raise ReferenceBuildError([f"{key}: the [{key}] table in {path} has no source_hash line."])
    digest = source_hash(component)
    start = header.end() + line.start()
    stop = header.end() + line.end()
    updated = text[:start] + f'{line.group(1)}"{digest}"' + text[stop:]
    path.write_bytes(updated.encode("utf-8"))
    return digest


# --- Markdown ----------------------------------------------------------------------------------

_INLINE_SPECIAL = re.compile(r"[\\`*_\[\]|]")


def escape_markdown(text: str) -> str:
    """Make ``text`` render as itself in Markdown, including inside a table cell.

    Paragraphs (separated by blank lines) are kept and the line breaks inside each are turned
    into spaces, which Markdown renders the same. Characters with a meaning in Markdown or
    HTML are escaped: ``< > &`` as entities (so a description can never inject markup), and
    backslash, backtick, ``*``, ``_``, brackets and pipes with a backslash. A paragraph that
    would start a list or a heading gets its first character escaped.
    """
    paragraphs = [_words(part) for part in re.split(r"\n[ \t]*\n", text.strip())]
    return "\n\n".join(_escape_paragraph(p) for p in paragraphs if p)


def _escape_paragraph(paragraph: str) -> str:
    """Escape one paragraph that is already a single line."""
    escaped = html.escape(paragraph, quote=False)
    escaped = _INLINE_SPECIAL.sub(lambda match: "\\" + match.group(0), escaped)
    if escaped[:1] in "-+#":
        return "\\" + escaped
    return re.sub(r"^(\d+)([.)])", r"\1\\\2", escaped)


def _code(text: str) -> str:
    """Wrap ``text`` in a code span that is safe inside a table cell."""
    text = text.replace("|", "\\|")
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def page_filename(page: str, language: Language) -> str:
    """The file name of a page: ``tools.md`` in English, ``tools.es.md`` in Spanish."""
    return f"{page}.md" if language == "en" else f"{page}.{language}.md"


def _quote(text: str) -> str:
    """Turn escaped paragraphs into a block quote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _table(component: Component, labels: Mapping[str, str]) -> list[str]:
    """The parameter (or argument) table of a tool or prompt, or the line saying there is none."""
    is_prompt = component.kind == "prompts"
    if not component.parameters:
        return [labels["no_arguments" if is_prompt else "no_parameters"]]
    head = labels["argument" if is_prompt else "parameter"]
    rows = [
        f"| {head} | {labels['type']} | {labels['required']} | {labels['description']} |",
        "| --- | --- | --- | --- |",
    ]
    for p in component.parameters:
        needed = labels["yes"] if p.required else labels["no"]
        if not p.required and p.default is not None:
            needed = f"{needed}, {labels['default']} {_code(p.default)}"
        kind = _code(p.type)
        if p.choices:
            kind += f"<br>{labels['one_of']}: " + ", ".join(_code(c) for c in p.choices)
        rows.append(f"| {_code(p.name)} | {kind} | {needed} | {escape_markdown(p.description)} |")
    return rows


def _entry(component: Component, labels: Mapping[str, str], summary: Summary | None) -> list[str]:
    """The Markdown blocks of one entry: heading, summary (Spanish only), text and details."""
    blocks = [f"### {_code(component.name)}"]
    description = escape_markdown(component.description)
    if summary is None:
        blocks.append(description)
    else:
        blocks.append(escape_markdown(summary.text))
        blocks.append(f"**{labels['model_description']}:**")
        blocks.append(_quote(description))
    if component.kind == "resources":
        uri_label = labels["uri_template" if component.is_template else "uri"]
        blocks.append(f"**{uri_label}:** {_code(component.uri or '')}")
        if component.mime_type:
            blocks.append(f"**{labels['mime']}:** {_code(component.mime_type)}")
    else:
        blocks.append(f"**{labels['arguments' if component.kind == 'prompts' else 'parameters']}**")
        blocks.append("\n".join(_table(component, labels)))
    return blocks


def _sections(
    kind: Kind, components: Sequence[Component], labels: Mapping[str, str]
) -> list[tuple[str, str | None, list[Component]]]:
    """The ``##`` sections of a page as ``(title, note, entries)``; none for prompts."""
    if kind == "tools":
        by_name = {c.name: c for c in components}
        placed: set[str] = set()
        sections: list[tuple[str, str | None, list[Component]]] = []
        for group, names in _TOOL_GROUPS:
            entries = [by_name[n] for n in names if n in by_name]
            placed.update(c.name for c in entries)
            if entries:
                note = labels["note_saved"] if group == "saved" else None
                sections.append((labels[f"group_{group}"], note, entries))
        other = sorted((c for c in components if c.name not in placed), key=lambda c: c.name)
        if other:
            sections.append((labels["group_other"], None, other))
        return sections
    if kind == "resources":
        static = [c for c in components if not c.is_template]
        templates = [c for c in components if c.is_template]
        return [
            (labels[title], None, entries)
            for title, entries in (("static_resources", static), ("resource_templates", templates))
            if entries
        ]
    return [("", None, list(components))]


def _render_page(
    kind: Kind,
    components: Sequence[Component],
    labels: Mapping[str, str],
    summaries: Mapping[str, Summary] | None,
) -> str:
    """Render one page; ``summaries`` is given for Spanish and ``None`` for English."""
    blocks = [f"# {labels[kind]}", labels[f"intro_{kind}"].format(count=len(components))]
    for title, note, entries in _sections(kind, components, labels):
        if title:
            blocks.append(f"## {title}")
        if note:
            # An admonition (the site enables the extension) sets the availability caveat apart
            # from the entries; on a plain Markdown renderer it still reads as an indented note.
            blocks.append(f'!!! note ""\n    {note}')
        for component in entries:
            summary = summaries[component.key] if summaries is not None else None
            blocks.extend(_entry(component, labels, summary))
    return "\n\n".join(blocks) + "\n"


def render_pages(
    language: Language,
    *,
    components: Sequence[Component] | None = None,
    summaries: Mapping[str, Summary] | None = None,
) -> dict[str, str]:
    """Render the three reference pages of one language as ``{"tools": markdown, ...}``.

    ``components`` default to the live server and ``summaries`` to the summaries file. The same
    input always gives byte-identical output. The Spanish pages run :func:`check_summaries` first
    and raise :class:`ReferenceBuildError` when it finds problems; the English pages need no
    summaries.
    """
    if language not in _LABELS:
        raise ValueError(f"unknown language {language!r}; use 'en' or 'es'")
    found = list(components) if components is not None else collect_components()
    labels = _LABELS[language]
    used: Mapping[str, Summary] | None = None
    if language == "es":
        used = summaries if summaries is not None else load_summaries()
        problems = check_summaries(found, used)
        if problems:
            raise ReferenceBuildError(problems)
    return {
        kind: _render_page(kind, [c for c in found if c.kind == kind], labels, used)
        for kind in PAGES
    }


def write_pages(
    out_dir: Path,
    *,
    components: Sequence[Component] | None = None,
    summaries: Mapping[str, Summary] | None = None,
) -> list[Path]:
    """Write the six pages into ``out_dir`` (created if needed) and return their paths.

    Both languages are rendered, and so checked, before anything is written, so a failing build
    leaves the directory untouched. Nothing is written outside ``out_dir``.
    """
    found = list(components) if components is not None else collect_components()
    languages: tuple[Language, ...] = ("en", "es")
    rendered: dict[Language, dict[str, str]] = {
        language: render_pages(language, components=found, summaries=summaries)
        for language in languages
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for language, pages in rendered.items():
        for page, text in pages.items():
            target = out_dir / page_filename(page, language)
            target.write_text(text, encoding="utf-8", newline="\n")
            written.append(target)
    return sorted(written)
