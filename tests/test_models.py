"""Contract tests for ``boe_insights_mcp.models``, grounded in the recorded API fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from boe_insights_mcp.models import (
    CodedValue,
    LawAnalysis,
    LawMetadata,
    LawReference,
    LawSummary,
    as_list,
)

Loader = Callable[[str], Any]


@pytest.fixture
def hit() -> dict[str, Any]:
    """A minimal valid search hit (only the two required fields)."""
    return {"identificador": "BOE-A-2000-1", "titulo": "Ley de prueba."}


# --- as_list ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_as_list_turns_absent_and_empty_into_empty_list(value: object) -> None:
    assert as_list(value) == []


def test_as_list_wraps_a_single_object() -> None:
    obj = {"codigo": "1", "texto": "x"}
    assert as_list(obj) == [obj]


def test_as_list_returns_a_list_unchanged() -> None:
    items = [{"a": 1}, {"a": 2}]
    assert as_list(items) == items


def test_as_list_keeps_an_empty_list_empty() -> None:
    assert as_list([]) == []


# --- CodedValue ------------------------------------------------------------------------------


def test_coded_value_reads_spanish_field_names() -> None:
    value = CodedValue.model_validate({"codigo": "1300", "texto": "Ley"})
    assert (value.code, value.text) == ("1300", "Ley")


def test_coded_value_accepts_english_field_names() -> None:
    assert CodedValue(code="1300", text="Ley") == CodedValue.model_validate(
        {"codigo": "1300", "texto": "Ley"}
    )


def test_coded_value_strips_surrounding_whitespace_from_text() -> None:
    value = CodedValue.model_validate({"codigo": "217", "texto": "SE DEROGA tácitamente "})
    assert value.text == "SE DEROGA tácitamente"


def test_coded_value_is_frozen() -> None:
    value = CodedValue(code="1", text="x")
    with pytest.raises(ValidationError):
        value.code = "2"  # type: ignore[misc]


# --- LawSummary from search fixtures -----------------------------------------------------------


def test_summary_parses_every_hit_of_a_search_page(load_json: Loader) -> None:
    hits = load_json("search_page_limit10.json")["data"]
    summaries = [LawSummary.model_validate(h) for h in hits]
    assert len(summaries) == 10
    assert summaries[0].identifier == "BOE-A-2022-10675"


def test_summary_reads_all_fields_of_a_real_hit(load_json: Loader) -> None:
    summary = LawSummary.model_validate(load_json("search_page_limit10.json")["data"][0])
    assert summary.title.startswith("Real Decreto 502/2022, de 27 de junio")
    assert summary.rank == CodedValue(code="1340", text="Real Decreto")
    assert summary.department == CodedValue(
        code="3120", text="Ministerio de Agricultura, Pesca y Alimentación"
    )
    assert summary.scope == CodedValue(code="1", text="Estatal")
    assert summary.official_number == "502/2022"
    assert summary.enacted_on == date(2022, 6, 27)
    assert summary.published_on == date(2022, 6, 28)
    assert summary.effective_from == date(2022, 6, 29)
    assert summary.validity_exhausted is False
    assert summary.consolidation_state == CodedValue(code="3", text="Finalizado")
    assert summary.gazette == "Boletín Oficial del Estado"
    assert summary.gazette_number == "154"
    assert summary.eli_url == "https://www.boe.es/eli/es/rd/2022/06/27/502"
    assert summary.html_url == "https://www.boe.es/buscar/act.php?id=BOE-A-2022-10675"


def test_summary_parses_updated_at_as_utc_datetime(load_json: Loader) -> None:
    summary = LawSummary.model_validate(load_json("search_page_limit10.json")["data"][0])
    assert summary.updated_at == datetime(2026, 11, 20, 13, 8, 24, tzinfo=UTC)
    assert summary.updated_at is not None
    assert summary.updated_at.utcoffset() is not None


def test_summary_keeps_alphanumeric_official_numbers(load_json: Loader) -> None:
    second = LawSummary.model_validate(load_json("search_page_limit10.json")["data"][1])
    assert second.official_number == "APA/315/2020"


def test_summary_marks_exhausted_validity_from_flag(load_json: Loader) -> None:
    hits = load_json("search_titulo_phrase.json")["data"]
    by_id = {h["identificador"]: LawSummary.model_validate(h) for h in hits}
    assert by_id["BOE-A-2017-5859"].validity_exhausted is True
    assert by_id["BOE-A-2015-10566"].validity_exhausted is False


def test_summary_of_a_hit_without_eli_url_has_none(load_json: Loader) -> None:
    hits = load_json("search_numero_oficial_ambiguous.json")["data"]
    summaries = [LawSummary.model_validate(h) for h in hits]
    assert len(summaries) == 13
    assert sum(1 for s in summaries if s.eli_url is None) == 1


def test_summary_of_the_single_hit_fixture(load_json: Loader) -> None:
    (raw,) = load_json("search_numero_oficial_rango_single.json")["data"]
    summary = LawSummary.model_validate(raw)
    assert summary.identifier == "BOE-A-2018-16673"
    assert summary.rank == CodedValue(code="1290", text="Ley Orgánica")
    assert summary.official_number == "3/2018"


def test_summary_ignores_the_metadata_only_fields(load_json: Loader) -> None:
    raw = load_json("metadatos_in_force_ley40_2015.json")["data"][0]
    summary = LawSummary.model_validate(raw)
    assert summary.identifier == "BOE-A-2015-10566"
    assert not hasattr(summary, "repealed")


def test_summary_ignores_unknown_fields(hit: dict[str, Any]) -> None:
    summary = LawSummary.model_validate({**hit, "campo_nuevo": {"x": 1}})
    assert summary.identifier == "BOE-A-2000-1"


def test_summary_needs_only_identifier_and_title(hit: dict[str, Any]) -> None:
    summary = LawSummary.model_validate(hit)
    assert summary.rank is None
    assert summary.department is None
    assert summary.scope is None
    assert summary.official_number is None
    assert summary.enacted_on is None
    assert summary.published_on is None
    assert summary.effective_from is None
    assert summary.updated_at is None
    assert summary.consolidation_state is None
    assert summary.gazette is None
    assert summary.gazette_number is None
    assert summary.eli_url is None
    assert summary.html_url is None


@pytest.mark.parametrize("missing", ["identificador", "titulo"])
def test_summary_requires_identifier_and_title(hit: dict[str, Any], missing: str) -> None:
    del hit[missing]
    with pytest.raises(ValidationError):
        LawSummary.model_validate(hit)


def test_summary_missing_validity_flag_is_false(hit: dict[str, Any]) -> None:
    assert LawSummary.model_validate(hit).validity_exhausted is False


@pytest.mark.parametrize(("flag", "expected"), [("S", True), ("N", False)])
def test_summary_flag_letters_become_bool(hit: dict[str, Any], flag: str, expected: bool) -> None:
    summary = LawSummary.model_validate({**hit, "vigencia_agotada": flag})
    assert summary.validity_exhausted is expected


@pytest.mark.parametrize(
    ("field", "attribute"),
    [
        ("fecha_disposicion", "enacted_on"),
        ("fecha_publicacion", "published_on"),
        ("fecha_vigencia", "effective_from"),
    ],
)
def test_summary_empty_date_string_becomes_none(
    hit: dict[str, Any], field: str, attribute: str
) -> None:
    summary = LawSummary.model_validate({**hit, field: ""})
    assert getattr(summary, attribute) is None


def test_summary_empty_official_number_becomes_none(hit: dict[str, Any]) -> None:
    assert LawSummary.model_validate({**hit, "numero_oficial": ""}).official_number is None


def test_summary_future_effective_date_is_kept(hit: dict[str, Any]) -> None:
    summary = LawSummary.model_validate({**hit, "fecha_vigencia": "20270701"})
    assert summary.effective_from == date(2027, 7, 1)


def test_summary_can_be_built_with_english_names() -> None:
    summary = LawSummary(
        identifier="BOE-A-2000-1",
        title="Ley de prueba.",
        rank=CodedValue(code="1300", text="Ley"),
        published_on=date(2000, 1, 2),
    )
    assert summary.rank is not None
    assert summary.rank.code == "1300"
    assert summary.published_on == date(2000, 1, 2)


def test_summary_dump_uses_english_names_and_round_trips(load_json: Loader) -> None:
    summary = LawSummary.model_validate(load_json("search_page_limit10.json")["data"][0])
    dumped = summary.model_dump()
    assert dumped["identifier"] == "BOE-A-2022-10675"
    assert "identificador" not in dumped
    assert dumped["rank"] == {"code": "1340", "text": "Real Decreto"}
    assert LawSummary.model_validate(dumped) == summary


def test_summary_is_frozen(hit: dict[str, Any]) -> None:
    summary = LawSummary.model_validate(hit)
    with pytest.raises(ValidationError):
        summary.title = "otro"  # type: ignore[misc]


# --- LawMetadata -------------------------------------------------------------------------------


def test_metadata_for_law_in_force(load_json: Loader) -> None:
    meta = LawMetadata.model_validate(load_json("metadatos_in_force_ley40_2015.json")["data"][0])
    assert meta.identifier == "BOE-A-2015-10566"
    assert meta.title.startswith("Ley 40/2015, de 1 de octubre")
    assert meta.rank == CodedValue(code="1300", text="Ley")
    assert meta.department == CodedValue(code="7723", text="Jefatura del Estado")
    assert meta.official_number == "40/2015"
    assert meta.enacted_on == date(2015, 10, 1)
    assert meta.published_on == date(2015, 10, 2)
    assert meta.effective_from == date(2016, 10, 2)
    assert meta.updated_at == datetime(2026, 11, 20, 8, 50, 0, tzinfo=UTC)
    assert meta.eli_url == "https://www.boe.es/eli/es/l/2015/10/01/40"
    assert meta.repealed is False
    assert meta.repealed_on is None
    assert meta.annulled is False
    assert meta.validity_exhausted is False


def test_metadata_for_repealed_law_carries_repeal_date(load_json: Loader) -> None:
    meta = LawMetadata.model_validate(load_json("metadatos_repealed_ley30_1992.json")["data"][0])
    assert meta.identifier == "BOE-A-1992-26318"
    assert meta.repealed is True
    assert meta.repealed_on == date(2021, 4, 2)
    assert meta.validity_exhausted is True
    assert meta.effective_from == date(1993, 2, 27)
    assert meta.annulled is False


def test_metadata_of_partially_superseded_law_is_not_repealed(load_json: Loader) -> None:
    raw = load_json("metadatos_superseded_partial_lopd1999.json")["data"][0]
    meta = LawMetadata.model_validate(raw)
    assert meta.identifier == "BOE-A-1999-23750"
    assert meta.rank == CodedValue(code="1290", text="Ley Orgánica")
    assert meta.repealed is False
    assert meta.repealed_on is None


def test_metadata_is_a_law_summary(load_json: Loader) -> None:
    meta = LawMetadata.model_validate(load_json("metadatos_in_force_ley40_2015.json")["data"][0])
    assert isinstance(meta, LawSummary)


def test_metadata_missing_status_flags_default_to_false(hit: dict[str, Any]) -> None:
    meta = LawMetadata.model_validate(hit)
    assert (meta.repealed, meta.annulled, meta.repealed_on) == (False, False, None)


def test_metadata_annulled_flag_is_read(hit: dict[str, Any]) -> None:
    assert LawMetadata.model_validate({**hit, "estatus_anulacion": "S"}).annulled is True


def test_metadata_dump_uses_english_names(load_json: Loader) -> None:
    meta = LawMetadata.model_validate(load_json("metadatos_repealed_ley30_1992.json")["data"][0])
    dumped = meta.model_dump()
    assert dumped["repealed"] is True
    assert dumped["repealed_on"] == date(2021, 4, 2)
    assert "estatus_derogacion" not in dumped


# --- LawAnalysis -------------------------------------------------------------------------------


def _reference(law_id: str = "BOE-A-2000-1", code: str = "210") -> dict[str, Any]:
    return {"id_norma": law_id, "relacion": {"codigo": code, "texto": "DEROGA"}, "texto": "Art. 1"}


def test_analysis_of_ley_40_counts_references(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    assert len(analysis.earlier) == 19
    assert len(analysis.later) == 24


def test_analysis_earlier_reference_has_relation_and_text(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    first = analysis.earlier[0]
    assert isinstance(first, LawReference)
    assert first.law_id == "BOE-A-2014-9467"
    assert first.relation == CodedValue(code="210", text="DEROGA")
    assert first.text.startswith("Arts. 12 a 15 y disposición adicional 6 de la Ley 15/2014")


def test_analysis_later_references_use_passive_relations(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    assert analysis.later[0].law_id == "BOE-A-2020-4859"
    assert analysis.later[0].relation == CodedValue(code="210", text="SE DEROGA")
    assert analysis.later[1].relation == CodedValue(code="270", text="SE MODIFICA")


def test_analysis_subjects_from_list_of_one(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    assert analysis.subjects == [CodedValue(code="6936", text="Transportes marítimos")]


def test_analysis_notes_from_a_single_note_object(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    assert len(analysis.notes) == 1
    assert analysis.notes[0].startswith("Entrada en vigor:")


def test_analysis_subjects_from_single_object_instead_of_list(load_json: Loader) -> None:
    raw = load_json("analisis_constitucion_materias_dict.json")["data"][0]
    analysis = LawAnalysis.model_validate(raw)
    assert analysis.subjects == [CodedValue(code="1616", text="Constitución Española")]


def test_analysis_without_notes_has_empty_notes(load_json: Loader) -> None:
    raw = load_json("analisis_constitucion_materias_dict.json")["data"][0]
    assert LawAnalysis.model_validate(raw).notes == []


def test_analysis_of_the_constitution_reads_its_references(load_json: Loader) -> None:
    raw = load_json("analisis_constitucion_materias_dict.json")["data"][0]
    analysis = LawAnalysis.model_validate(raw)
    assert len(analysis.earlier) == 10
    assert len(analysis.later) == 7
    assert analysis.earlier[0].law_id == "BOE-A-1977-165"
    assert analysis.earlier[0].text == "Ley 1/1977, de 4 de enero"


def test_analysis_notes_wrapped_in_list_of_note_lists() -> None:
    raw = {"notas": [{"nota": ["Primera.", "Segunda."]}]}
    assert LawAnalysis.model_validate(raw).notes == ["Primera.", "Segunda."]


def test_analysis_missing_everything_is_all_empty() -> None:
    analysis = LawAnalysis.model_validate({})
    assert (analysis.subjects, analysis.notes, analysis.earlier, analysis.later) == ([], [], [], [])


@pytest.mark.parametrize("empty", [None, ""])
def test_analysis_empty_references_become_empty_lists(empty: object) -> None:
    raw = {"referencias": {"anteriores": empty, "posteriores": empty}}
    analysis = LawAnalysis.model_validate(raw)
    assert analysis.earlier == []
    assert analysis.later == []


def test_analysis_references_object_without_posteriores() -> None:
    raw = {"referencias": {"anteriores": [{"anterior": [_reference()]}]}}
    analysis = LawAnalysis.model_validate(raw)
    assert len(analysis.earlier) == 1
    assert analysis.later == []


def test_analysis_single_reference_object_instead_of_list() -> None:
    raw = {"referencias": {"posteriores": [{"posterior": _reference("BOE-A-2001-2", "270")}]}}
    analysis = LawAnalysis.model_validate(raw)
    assert [r.law_id for r in analysis.later] == ["BOE-A-2001-2"]
    assert analysis.later[0].relation.code == "270"


def test_analysis_unwrapped_references_group_object() -> None:
    raw = {"referencias": {"anteriores": {"anterior": [_reference(), _reference("BOE-A-2000-2")]}}}
    assert len(LawAnalysis.model_validate(raw).earlier) == 2


def test_analysis_ignores_unknown_fields() -> None:
    assert LawAnalysis.model_validate({"desconocido": 1}).subjects == []


def test_analysis_dump_uses_english_names(load_json: Loader) -> None:
    analysis = LawAnalysis.model_validate(load_json("analisis_ley40_2015.json")["data"][0])
    dumped = analysis.model_dump()
    assert set(dumped) >= {"subjects", "notes", "earlier", "later"}
    assert dumped["earlier"][0]["law_id"] == "BOE-A-2014-9467"
    assert dumped["earlier"][0]["relation"] == {"code": "210", "text": "DEROGA"}


def test_analysis_is_frozen() -> None:
    analysis = LawAnalysis.model_validate({})
    with pytest.raises(ValidationError):
        analysis.notes = ["x"]  # type: ignore[misc]


def test_reference_is_built_from_spanish_names() -> None:
    ref = LawReference.model_validate(_reference("BOE-A-1999-5", "230"))
    assert (ref.law_id, ref.relation.code, ref.text) == ("BOE-A-1999-5", "230", "Art. 1")
