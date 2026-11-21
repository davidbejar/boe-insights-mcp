"""Pure builder for the ``query`` parameter of the BOE legislation search.

The API takes a JSON document wrapping a Lucene-style ``query_string`` expression. User words
are escaped so they can only ever be search terms, never query syntax.

The one public name is ``build_search_query``, which maps keyword arguments (title, text, rank,
department, scope, subject, dates, sort) to that JSON string, plus the ``SortField`` literal
used by ``legislation`` and ``server``. It does no I/O and is the only place that knows the
API's query grammar (``docs/api-notes.md`` section 2). ``client.search_laws`` sends the result
as the ``query`` URL parameter.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

__all__ = ["build_search_query"]

SortField = Literal["published_on", "updated_at", "enacted_on"]

_SORT_FIELDS: dict[str, str] = {
    "published_on": "fecha_publicacion",
    "updated_at": "fecha_actualizacion",
    "enacted_on": "fecha_disposicion",
}
_RANGE_FIELD = "fecha_publicacion"
# Filler bounds for a one-sided date range: the API ignores a range that has only ``gte`` or only
# ``lte`` (api-notes 2), so the missing side is replaced by a date outside any real publication.
_EARLIEST = "18000101"
_LATEST = "99991231"

_CODE = re.compile(r"[A-Za-z0-9_-]+")
# Every character with a meaning in query_string syntax (the operators && and || are covered by
# escaping their single characters). Escaping them with a backslash makes them literal; the API
# honours this (verified live, api-notes 2 "Escaping"). ``/`` is included because it matters in
# official numbers such as 40/2015.
_TERM_SPECIAL = re.compile(r'[+\-=&|<>!(){}\[\]^"~*?:\\/]')
# Inside double quotes only the quote and the backslash are special.
_PHRASE_SPECIAL = re.compile(r'["\\]')
# The words the code treats as boolean operators when they appear on their own in user text.
_OPERATORS = frozenset({"AND", "OR", "NOT"})
# A balanced quoted phrase, or any run of non-space characters. A lone unbalanced quote does
# not match the first alternative, so it ends up in a plain token and gets escaped.
_TOKEN = re.compile(r'"[^"]*"|\S+')


def _escape(text: str, special: re.Pattern[str]) -> str:
    """Prefix every character matched by ``special`` with a backslash."""
    return special.sub(r"\\\g<0>", text)


def _phrase(text: str) -> str:
    """Return ``text`` as a double-quoted phrase."""
    return f'"{_escape(text, _PHRASE_SPECIAL)}"'


def _term(token: str) -> str | None:
    """Turn one token of user text into a safe term, or ``None`` if it is an empty phrase."""
    # A bare AND/OR/NOT typed by the user would otherwise change the meaning of the query, so
    # it is quoted into a literal word. Escaping alone would not stop it, as it has no special
    # characters.
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        content = " ".join(token[1:-1].split())
        return _phrase(content) if content else None
    if token in _OPERATORS:
        return _phrase(token)
    return _escape(token, _TERM_SPECIAL)


def _field_clause(field: str, text: str | None) -> str | None:
    """Return ``field:terms`` requiring all words (or quoted phrases) of ``text``, if any."""
    # Words are joined with AND, so all of them must match.
    terms = [t for token in _TOKEN.findall(text or "") if (t := _term(token)) is not None]
    if not terms:
        return None
    if len(terms) == 1:
        return f"{field}:{terms[0]}"
    return f"{field}:({' AND '.join(terms)})"


def _number_clause(value: str | None) -> str | None:
    """Return ``numero_oficial:"..."`` for a non-blank official number."""
    number = (value or "").strip()
    return f"numero_oficial:{_phrase(number)}" if number else None


def _code_clause(field: str, code: str | None) -> str | None:
    """Return ``field:code``; codes may only contain letters, digits, ``-`` and ``_``."""
    # Codes are not escaped like text: they are validated against a whitelist instead, so a
    # value can never carry query syntax (a space, a parenthesis, ``OR``...).
    if code is None:
        return None
    if not _CODE.fullmatch(code):
        raise ValueError(f"invalid code for {field}: {code!r}")
    return f"{field}:{code}"


def _date_range(start: date | None, end: date | None) -> dict[str, dict[str, str]] | None:
    """Return the publication-date range; the API ignores a range with only one bound."""
    if start is None and end is None:
        return None
    if start is not None and end is not None and start > end:
        raise ValueError("published_from must not be after published_to")
    # Both bounds are always sent, using the filler dates for a missing side (see _EARLIEST).
    lower = start.isoformat().replace("-", "") if start else _EARLIEST
    upper = end.isoformat().replace("-", "") if end else _LATEST
    return {_RANGE_FIELD: {"gte": lower, "lte": upper}}


def build_search_query(
    *,
    title: str | None = None,
    text: str | None = None,
    official_number: str | None = None,
    rank_code: str | None = None,
    department_code: str | None = None,
    scope_code: str | None = None,
    subject_code: str | None = None,
    published_from: date | None = None,
    published_to: date | None = None,
    sort_by: SortField | None = "published_on",
    descending: bool = True,
) -> str | None:
    """Build the JSON ``query`` parameter for a legislation search, or ``None`` if empty.

    ``title`` and ``text`` match all their words (a double-quoted part stays a phrase) and are
    escaped so user input cannot inject query syntax. Codes must be letters, digits, ``-`` or
    ``_``. Raises ``ValueError`` for an invalid code or an inverted date range. Equal arguments
    always give an equal string.
    """
    # Clauses are joined with AND, so every filter narrows the result. They are built in a fixed
    # order, which together with ``json.dumps`` below gives equal arguments an equal string and
    # therefore the same cache key.
    clauses = [
        clause
        for clause in (
            _field_clause("titulo", title),
            _field_clause("texto", text),
            _number_clause(official_number),
            _code_clause("rango@codigo", rank_code),
            _code_clause("departamento@codigo", department_code),
            _code_clause("ambito@codigo", scope_code),
            _code_clause("materia@codigo", subject_code),
        )
        if clause is not None
    ]
    query: dict[str, Any] = {}
    if clauses:
        query["query_string"] = {"query": " AND ".join(clauses)}
    date_range = _date_range(published_from, published_to)
    if date_range is not None:
        query["range"] = date_range

    # ``sort`` can exist without ``query`` (list everything, newest first). No clauses, no range
    # and no sort at all means "no query parameter", and the API then uses its default order
    # (api-notes 1).
    document: dict[str, Any] = {}
    if query:
        document["query"] = query
    if sort_by is not None:
        document["sort"] = [{_SORT_FIELDS[sort_by]: "desc" if descending else "asc"}]
    if not document:
        return None
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
