"""Tests for ``boe_insights_mcp.identifiers``: the law-id pattern and its normalisation."""

from __future__ import annotations

import subprocess
import sys

import pytest

from boe_insights_mcp.identifiers import LAW_ID, normalise_identifier


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BOE-A-2015-10566", "BOE-A-2015-10566"),
        ("boe-a-2015-10566", "BOE-A-2015-10566"),
        ("  BOE-A-2015-10566\n", "BOE-A-2015-10566"),
        ("Boe-a-1889-4763", "BOE-A-1889-4763"),
        ("DOUE-L-2016-80807", "DOUE-L-2016-80807"),
        ("doue-L-2016-80807", "DOUE-L-2016-80807"),
    ],
)
def test_valid_identifiers_are_canonicalised(text: str, expected: str) -> None:
    assert normalise_identifier(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "Ley 40/2015", "BOE-A-15-10566", "BOE-A-2015-", "BOE-A-2015-1234567", "X-A-2015-1"],
)
def test_other_text_is_not_an_identifier(text: str) -> None:
    assert normalise_identifier(text) is None


def test_the_client_pattern_is_case_sensitive_on_the_prefix() -> None:
    assert LAW_ID.fullmatch("BOE-A-2015-10566")
    assert not LAW_ID.fullmatch("boe-A-2015-10566")


def test_references_no_longer_depends_on_the_client_module() -> None:
    code = (
        "import sys, boe_insights_mcp.references; print('boe_insights_mcp.client' in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False"
