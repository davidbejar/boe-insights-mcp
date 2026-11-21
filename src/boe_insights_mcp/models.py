"""Typed, immutable models of the BOE API data, with English names.

The API uses Spanish field names and a few serialisation quirks (``yyyymmdd`` dates, ``S``/``N``
flags, single-element lists collapsed into objects, empty lists as ``""``). The models absorb
all of that: they validate from the raw API objects and dump with English names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import (
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

__all__ = [
    "CodedValue",
    "LawAnalysis",
    "LawMetadata",
    "LawReference",
    "LawSummary",
    "StrippedText",
    "as_list",
]


def as_list(value: Any) -> list[Any]:
    """Normalise the API's collapsed collections: ``None``, ``""``, one object or a list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _blank_to_none(value: Any) -> Any:
    """Turn the API's empty elements (``""``) into ``None``."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_date(value: Any) -> Any:
    """Parse a ``yyyymmdd`` string; blank means no date. Other values are left to pydantic."""
    value = _blank_to_none(value)
    if isinstance(value, str):
        return datetime.strptime(value, "%Y%m%d").date()
    return value


def _parse_timestamp(value: Any) -> Any:
    """Parse a ``yyyymmddTHHMMSSZ`` UTC stamp into an aware datetime."""
    value = _blank_to_none(value)
    if isinstance(value, str):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return value


def _parse_flag(value: Any) -> Any:
    """Parse the API's ``"S"``/``"N"`` (yes/no) flags."""
    if isinstance(value, str):
        if value not in ("S", "N"):
            raise ValueError(f"expected 'S' or 'N', got {value!r}")
        return value == "S"
    return value


def _unwrapper(key: str) -> Callable[[Any], list[Any]]:
    """Build a validator flattening ``[{key: item-or-items}, ...]`` wrappers into a list.

    Items that are not wrapped (no ``key``) are kept as they are.
    """

    def unwrap(value: Any) -> list[Any]:
        items: list[Any] = []
        for entry in as_list(value):
            if isinstance(entry, Mapping) and key in entry:
                items.extend(as_list(entry[key]))
            else:
                items.append(entry)
        return items

    return unwrap


StrippedText = Annotated[str, StringConstraints(strip_whitespace=True)]
"""A string without surrounding whitespace."""

_OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
_OptionalDate = Annotated[date | None, BeforeValidator(_parse_date)]
_Flag = Annotated[bool, BeforeValidator(_parse_flag)]


class _ApiModel(BaseModel):
    """Frozen base: reads Spanish API names or English names, ignores unknown fields."""

    model_config = ConfigDict(frozen=True, validate_by_name=True, extra="ignore")


class CodedValue(_ApiModel):
    """A ``{"codigo": ..., "texto": ...}`` pair, such as a rank or a department."""

    code: str = Field(validation_alias="codigo")
    text: StrippedText = Field(validation_alias="texto")


_OptionalCoded = Annotated[CodedValue | None, BeforeValidator(_blank_to_none)]


class LawSummary(_ApiModel):
    """One search hit of the consolidated legislation."""

    identifier: str = Field(validation_alias="identificador")
    title: str = Field(validation_alias="titulo")
    rank: _OptionalCoded = Field(default=None, validation_alias="rango")
    department: _OptionalCoded = Field(default=None, validation_alias="departamento")
    scope: _OptionalCoded = Field(default=None, validation_alias="ambito")
    official_number: _OptionalText = Field(default=None, validation_alias="numero_oficial")
    enacted_on: _OptionalDate = Field(default=None, validation_alias="fecha_disposicion")
    published_on: _OptionalDate = Field(default=None, validation_alias="fecha_publicacion")
    effective_from: _OptionalDate = Field(default=None, validation_alias="fecha_vigencia")
    updated_at: Annotated[datetime | None, BeforeValidator(_parse_timestamp)] = Field(
        default=None, validation_alias="fecha_actualizacion"
    )
    validity_exhausted: _Flag = Field(default=False, validation_alias="vigencia_agotada")
    consolidation_state: _OptionalCoded = Field(
        default=None, validation_alias="estado_consolidacion"
    )
    gazette: _OptionalText = Field(default=None, validation_alias="diario")
    gazette_number: _OptionalText = Field(default=None, validation_alias="diario_numero")
    eli_url: _OptionalText = Field(default=None, validation_alias="url_eli")
    html_url: _OptionalText = Field(default=None, validation_alias="url_html_consolidada")


class LawMetadata(LawSummary):
    """The metadata of one law, which adds the repeal and annulment status to a summary."""

    repealed: _Flag = Field(default=False, validation_alias="estatus_derogacion")
    repealed_on: _OptionalDate = Field(default=None, validation_alias="fecha_derogacion")
    annulled: _Flag = Field(default=False, validation_alias="estatus_anulacion")


class LawReference(_ApiModel):
    """One amendment cross-reference between two norms."""

    law_id: str = Field(validation_alias="id_norma")
    relation: CodedValue = Field(validation_alias="relacion")
    text: str = Field(validation_alias="texto")


class LawAnalysis(_ApiModel):
    """The subjects, notes and amendment references of one law."""

    subjects: Annotated[list[CodedValue], BeforeValidator(_unwrapper("materia"))] = Field(
        default_factory=list, validation_alias="materias"
    )
    notes: Annotated[list[str], BeforeValidator(_unwrapper("nota"))] = Field(
        default_factory=list, validation_alias="notas"
    )
    earlier: Annotated[list[LawReference], BeforeValidator(_unwrapper("anterior"))] = Field(
        default_factory=list, validation_alias=AliasPath("referencias", "anteriores")
    )
    later: Annotated[list[LawReference], BeforeValidator(_unwrapper("posterior"))] = Field(
        default_factory=list, validation_alias=AliasPath("referencias", "posteriores")
    )
