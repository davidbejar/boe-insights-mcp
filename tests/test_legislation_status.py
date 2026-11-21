"""Contract tests for ``boe_insights_mcp.legislation.derive_status``.

The third argument is the number of later references that repeal the law (``SE DEROGA ...``);
the fourth is the version of the law's initial note that announces a repeal, if any.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from boe_insights_mcp.legislation import derive_status
from boe_insights_mcp.models import Block, BlockVersion, LawMetadata, Paragraph
from boe_insights_mcp.repeal import whole_law_repeal
from boe_insights_mcp.textparse import parse_block
from boe_insights_mcp.versions import repeal_date, repeal_note

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
    status, _ = derive_status(make_law(), TODAY, 0, None)

    assert status == "in_force"


def test_repealed_flag_gives_repealed() -> None:
    status, _ = derive_status(make_law(repealed=True), TODAY, 0, None)

    assert status == "repealed"


def test_annulled_flag_gives_annulled() -> None:
    status, _ = derive_status(make_law(annulled=True), TODAY, 0, None)

    assert status == "annulled"


def test_validity_exhausted_alone_gives_validity_exhausted() -> None:
    status, _ = derive_status(make_law(validity_exhausted=True), TODAY, 0, None)

    assert status == "validity_exhausted"


def test_future_effective_date_gives_not_yet_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=date(2027, 7, 1)), TODAY, 0, None)

    assert status == "not_yet_in_force"


# --- Date boundaries ------------------------------------------------------------------


def test_effective_today_is_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=TODAY), TODAY, 0, None)

    assert status == "in_force"


def test_effective_tomorrow_is_not_yet_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=TODAY + timedelta(days=1)), TODAY, 0, None)

    assert status == "not_yet_in_force"


def test_missing_effective_date_is_in_force() -> None:
    status, _ = derive_status(make_law(effective_from=None), TODAY, 0, None)

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
    status, _ = derive_status(make_law(**flags), TODAY, 0, None)  # type: ignore[arg-type]

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
    _, note = derive_status(make_law(**flags), TODAY, 0, None)  # type: ignore[arg-type]

    assert isinstance(note, str)
    assert note.strip()


def test_in_force_note_carries_partial_repeal_caveat() -> None:
    _, note = derive_status(make_law(), TODAY, 0, None)

    lowered = note.lower()
    assert any(word in lowered for word in ("amend", "provision"))


def test_validity_exhausted_note_explains_effects_are_spent() -> None:
    _, note = derive_status(make_law(validity_exhausted=True), TODAY, 0, None)

    lowered = note.lower()
    assert any(word in lowered for word in ("spent", "exhaust", "no longer", "expired", "effects"))


def test_repealed_note_includes_repeal_date_when_known() -> None:
    _, note = derive_status(make_law(repealed=True, repealed_on=date(2021, 4, 2)), TODAY, 0, None)

    assert "2021" in note
    assert any(marker in note for marker in ("04-02", "April", "abril", "04/2021"))


def test_repealed_without_date_does_not_crash() -> None:
    status, note = derive_status(make_law(repealed=True, repealed_on=None), TODAY, 0, None)

    assert status == "repealed"
    assert note.strip()


# --- Partial repeals ------------------------------------------------------------------


def test_repeal_references_on_an_unflagged_law_give_partially_repealed() -> None:
    status, _ = derive_status(make_law(), TODAY, 3, None)

    assert status == "partially_repealed"


def test_partially_repealed_note_explains_the_evidence_and_its_limits() -> None:
    _, note = derive_status(make_law(), TODAY, 3, None)

    assert "later norms repeal this law or parts of it" in note
    assert "3" in note
    assert "only marks whole" in note
    assert "amendment summary" in note


def test_a_single_repeal_reference_is_counted_in_the_note() -> None:
    _, note = derive_status(make_law(), TODAY, 1, None)

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
    status, _ = derive_status(make_law(**flags), TODAY, 5, None)  # type: ignore[arg-type]

    assert status == expected


def test_no_repeal_references_stays_in_force() -> None:
    status, _ = derive_status(make_law(), TODAY, 0, None)

    assert status == "in_force"


# --- Repeal announced by the initial note ---------------------------------------------


def note_version(load_fixture: Callable[[str], bytes], name: str) -> BlockVersion | None:
    """The repeal version of a recorded initial note (``None`` when it announces no repeal)."""
    note: Block = parse_block(load_fixture(name))
    return repeal_note(note)


def test_repeal_note_on_an_unflagged_law_gives_repealed_with_the_note_quoted(
    load_fixture: Callable[[str], bytes],
) -> None:
    # LO 15/1999: the metadata flag says N, but the consolidated text opens with a repeal note.
    version = note_version(load_fixture, "texto_bloque_no_lopd1999_repealed.xml")

    status, note = derive_status(make_law(), TODAY, 0, version)

    assert status == "repealed"
    assert "opens with a repeal note" in note
    assert "Norma derogada, con efectos de 7 de diciembre de 2018" in note
    assert "flag is not set" in note


def test_the_note_quote_is_the_first_sentence_only(
    load_fixture: Callable[[str], bytes],
) -> None:
    version = note_version(
        load_fixture, "texto_bloque_no_ley30_1992_two_versions_empty_vigencia.xml"
    )

    _, note = derive_status(make_law(), TODAY, 0, version)

    assert "Ley 39/2015, de 1 de octubre." in note
    assert "No obstante" not in note


def test_a_very_long_note_sentence_is_cut_at_300_characters() -> None:
    long_note = BlockVersion(
        source_law_id="BOE-A-2018-16673",
        published_on=None,
        effective_from=None,
        paragraphs=(Paragraph(kind="siempreSeVe", text="Norma derogada " + "x" * 500),),
    )

    _, note = derive_status(make_law(), TODAY, 0, long_note)

    quoted = note.split('"')[1]
    assert len(quoted) <= 300


def test_flag_and_note_together_keep_the_flag_wording(
    load_fixture: Callable[[str], bytes],
) -> None:
    version = note_version(load_fixture, "texto_bloque_no_lopd1999_repealed.xml")

    status, note = derive_status(make_law(repealed=True), TODAY, 0, version)

    assert status == "repealed"
    assert note.startswith("The BOE flags this law as repealed")


@pytest.mark.parametrize(
    "name",
    [
        "texto_bloque_no_rd424_2005_rename_not_repeal.xml",
        "texto_bloque_no_rd1624_1992_correction_not_repeal.xml",
    ],
)
def test_a_rename_or_errata_note_does_not_change_the_status(
    load_fixture: Callable[[str], bytes], name: str
) -> None:
    version = note_version(load_fixture, name)

    status, _ = derive_status(make_law(), TODAY, 0, version)

    assert version is None
    assert status == "in_force"


def test_flag_without_any_note_is_repealed() -> None:
    # Ley 30/2007: flagged S, and the BOE has no initial note at all (api-notes, "Initial note").
    status, note = derive_status(
        make_law(repealed=True, repealed_on=date(2011, 12, 16)), TODAY, 0, None
    )

    assert status == "repealed"
    assert "2011-12-16" in note


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"annulled": True}, "annulled"),
        ({"validity_exhausted": True}, "repealed"),
        ({"effective_from": date(2027, 1, 1)}, "repealed"),
    ],
)
def test_the_note_ranks_below_annulled_and_above_the_remaining_statuses(
    load_fixture: Callable[[str], bytes], flags: dict[str, bool | date], expected: str
) -> None:
    version = note_version(load_fixture, "texto_bloque_no_lopd1999_repealed.xml")

    status, _ = derive_status(make_law(**flags), TODAY, 2, version)  # type: ignore[arg-type]

    assert status == expected


# --- Repeal date and announced repeals --------------------------------------------------


def synthetic_note(first_paragraph: str, *, effective: date | None = None) -> Block:
    """An initial note whose only version starts with ``first_paragraph``."""
    version = BlockVersion(
        source_law_id="BOE-A-2026-1",
        published_on=date(2026, 1, 1),
        effective_from=effective,
        paragraphs=(Paragraph(kind="siempreSeVe", text=first_paragraph),),
    )
    return Block(block_id="no", kind="nota_inicial", title="", versions=(version,))


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("texto_bloque_no_ley11_2007_repealed_with_tengase.xml", date(2016, 10, 2)),
        ("texto_bloque_no_ley30_1992_two_versions_empty_vigencia.xml", date(2016, 10, 2)),
        ("texto_bloque_no_lopd1999_repealed.xml", date(2018, 12, 7)),
    ],
)
def test_overview_repeal_date_logic_reads_the_date_from_the_note_wording(
    load_fixture: Callable[[str], bytes], fixture: str, expected: date
) -> None:
    note = parse_block(load_fixture(fixture))

    verdict = whole_law_repeal(
        flagged=False,
        flag_on=None,
        note_version=repeal_note(note),
        note_on=repeal_date(note),
        today=TODAY,
    )

    assert (verdict.in_effect, verdict.on) == (True, expected)


def test_a_repeal_dated_in_the_future_is_announced_and_the_status_stays_as_derived() -> None:
    note = synthetic_note("Norma derogada, con efectos de 1 de enero de 2027, por la ley X.")

    status, text = derive_status(
        make_law(), TODAY, 0, repeal_note(note), repeal_on=repeal_date(note)
    )

    assert status == "in_force"
    assert "announced" in text
    assert "2027-01-01" in text
    assert "opens with a repeal note" not in text


def test_a_flagged_repeal_dated_in_the_future_is_announced_too() -> None:
    status, text = derive_status(
        make_law(repealed=True, repealed_on=TODAY + timedelta(days=30)), TODAY, 0, None
    )

    assert status == "in_force"
    assert (TODAY + timedelta(days=30)).isoformat() in text
    assert "announced" in text


def test_an_announced_repeal_does_not_hide_a_stronger_status() -> None:
    status, text = derive_status(
        make_law(annulled=True, repealed=True, repealed_on=TODAY + timedelta(days=30)),
        TODAY,
        0,
        None,
    )

    assert status == "annulled"
    assert "announced" not in text


def test_a_repeal_dated_today_is_in_effect() -> None:
    status, _ = derive_status(make_law(repealed=True, repealed_on=TODAY), TODAY, 0, None)

    assert status == "repealed"


def test_a_partially_repealed_note_is_not_a_whole_law_repeal() -> None:
    note = synthetic_note("Norma derogada parcialmente, con efectos de 1 de enero de 2020.")

    status, _ = derive_status(make_law(), TODAY, 0, repeal_note(note))

    assert repeal_note(note) is None
    assert status == "in_force"


def test_without_a_given_date_derive_status_reads_it_from_the_note_version() -> None:
    note = synthetic_note("Norma derogada, con efectos de 1 de enero de 2027.")

    status, text = derive_status(make_law(), TODAY, 0, repeal_note(note))

    assert status == "in_force"
    assert "2027-01-01" in text
