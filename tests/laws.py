"""Factories for ``LawSummary`` records shared by the trend-analytics tests."""

from __future__ import annotations

from datetime import date, datetime

from boe_insights_mcp.models import CodedValue, LawSummary


def make_law(
    identifier: str,
    *,
    title: str = "Ley de prueba",
    published: date | None = None,
    enacted: date | None = None,
    rank: tuple[str, str] | None = ("1300", "Ley"),
    department: tuple[str, str] | None = ("7723", "Jefatura del Estado"),
    scope: tuple[str, str] | None = ("1", "Estatal"),
    updated_at: datetime | None = None,
    number: str | None = None,
) -> LawSummary:
    """Build a search hit; each coded field is a ``(code, text)`` pair or ``None``."""

    def coded(pair: tuple[str, str] | None) -> CodedValue | None:
        return CodedValue(code=pair[0], text=pair[1]) if pair else None

    return LawSummary(
        identifier=identifier,
        title=title,
        published_on=published,
        enacted_on=enacted,
        rank=coded(rank),
        department=coded(department),
        scope=coded(scope),
        updated_at=updated_at,
        official_number=number,
    )
