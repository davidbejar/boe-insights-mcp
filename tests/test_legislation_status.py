"""Contract tests for ``boe_insights_mcp.legislation.derive_status``.

The third argument is the number of later references that repeal the law (``SE DEROGA ...``).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from boe_insights_mcp.legislation import derive_status
from boe_insights_mcp.models import LawMetadata

TODAY = date(2026, 11, 21)


def make_law(
    *,
    repealed: bool = False,
    annulled: bool = False,
    validity_exhausted: bool = False,
    effective_from: date | None = date(2016, 10, 2),
    repealed_on: date | None = None,
) -> LawMetadata:
    return LawMetadata(
        identifier="BOE-A-2015-10566",
        title="Ley de ejemplo",
        repealed=repealed,
        annulled=annulled,
        validity_exhausted=validity_exhausted,
        effective_from=effective_from,
        repealed_on=repealed_on,
    )


def test_plain_law_is_in_force() -> None:
    status, _ = derive_status(make_law(), TODAY, 0)

    assert status == "in_force"


def test_repealed_flag_gives_repealed() -> None:
    status, _ = derive_status(make_law(repealed=True), TODAY, 0)

    assert status == "repealed"


def test_annulled_flag_gives_annulled() -> None:
    status, _ = derive_status(make_law(annulled=True), TODAY, 0)

    assert status == "annulled"


def test_validity_exhausted_alone_gives_validity_exhausted() -> None:
    status, _ = derive_status(make_law(validity_exhausted=True), TODAY, 0)

    assert status == "validity_exhausted"


def test_future_effective_date_gives_not_yet_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=date(2027, 7, 1)), TODAY, 0)

    assert status == "not_yet_in_force"


# --- Date boundaries ------------------------------------------------------------------


def test_effective_today_is_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=TODAY), TODAY, 0)

    assert status == "in_force"


def test_effective_tomorrow_is_not_yet_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=TODAY + timedelta(days=1)), TODAY, 0)

    assert status == "not_yet_in_force"


def test_missing_effective_date_is_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=None), TODAY, 0)

    assert status == "in_force"


# --- Precedence -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"annulled": True, "repealed": True, "validity_exhausted": True}, "annulled"),
        ({"annulled": True, "effective_from": date(2027, 1, 1)}, "annulled"),
        ({"repealed": True, "validity_exhausted": True}, "repealed"),
        ({"repealed": True, "effective_from": date(2027, 1, 1)}, "repealed"),
        (
            {"validity_exhausted": True, "effective_from": date(2027, 1, 1)},
            "validity_exhausted",
        ),
    ],
)
def test_status_precedence_with_conflicting_flags(
    flags: dict[str, bool | date], expected: str
) -> None:
    status, _ = derive_status(make_law(**flags), TODAY, 0)  # type: ignore[arg-type]

    assert status == expected


# --- Notes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        {"repealed": True},
        {"annulled": True},
        {"validity_exhausted": True},
        {"effective_from": date(2027, 7, 1)},
    ],
)
def test_note_is_a_non_empty_string_for_every_status(flags: dict[str, bool | date]) -> None:
    _, note = derive_status(make_law(**flags), TODAY, 0)  # type: ignore[arg-type]

    assert isinstance(note, str)
    assert note.strip()


def test_in_force_note_carries_partial_repeal_caveat() -> None:
    _, note = derive_status(make_law(), TODAY, 0)

    lowered = note.lower()
    assert any(word in lowered for word in ("amend", "provision"))


def test_validity_exhausted_note_explains_effects_are_spent() -> None:
    _, note = derive_status(make_law(validity_exhausted=True), TODAY, 0)

    lowered = note.lower()
    assert any(word in lowered for word in ("spent", "exhaust", "no longer", "expired", "effects"))


def test_repealed_note_includes_repeal_date_when_known() -> None:
    _, note = derive_status(make_law(repealed=True, repealed_on=date(2021, 4, 2)), TODAY, 0)

    assert "2021" in note
    assert any(marker in note for marker in ("04-02", "April", "abril", "04/2021"))


def test_repealed_without_date_does_not_crash() -> None:
    status, note = derive_status(make_law(repealed=True, repealed_on=None), TODAY, 0)

    assert status == "repealed"
    assert note.strip()


# --- Partial repeals ------------------------------------------------------------------


def test_repeal_references_on_an_unflagged_law_give_partially_repealed() -> None:
    status, _ = derive_status(make_law(), TODAY, 3)

    assert status == "partially_repealed"


def test_partially_repealed_note_explains_the_evidence_and_its_limits() -> None:
    _, note = derive_status(make_law(), TODAY, 3)

    assert "later norms repeal this law or parts of it" in note
    assert "3" in note
    assert "only marks whole" in note
    assert "amendment summary" in note


def test_a_single_repeal_reference_is_counted_in_the_note() -> None:
    _, note = derive_status(make_law(), TODAY, 1)

    assert "1 reference" in note


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"annulled": True}, "annulled"),
        ({"repealed": True}, "repealed"),
        ({"validity_exhausted": True}, "validity_exhausted"),
        ({"effective_from": date(2027, 1, 1)}, "not_yet_in_force"),
    ],
)
def test_every_other_status_takes_precedence_over_partially_repealed(
    flags: dict[str, bool | date], expected: str
) -> None:
    status, _ = derive_status(make_law(**flags), TODAY, 5)  # type: ignore[arg-type]

    assert status == expected


def test_no_repeal_references_stays_in_force() -> None:
    status, _ = derive_status(make_law(), TODAY, 0)

    assert status == "in_force"
