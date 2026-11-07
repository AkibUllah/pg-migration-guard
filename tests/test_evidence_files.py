"""The committed evidence has to keep up with the rules."""

import json
from pathlib import Path

import pytest

from migration_guard.evidence import PATTERNS
from migration_guard.model import RULES

FILES = sorted((Path(__file__).parent.parent / "evidence").glob("pg*.json"))


def test_the_supported_versions_have_evidence():
    assert [f.stem for f in FILES] == ["pg14", "pg16", "pg17", "pg18"]


@pytest.mark.parametrize("file", FILES, ids=lambda f: f.stem)
def test_no_claim_was_contradicted(file):
    report = json.loads(file.read_text())
    assert [e for e in report["results"] if e["mismatches"]] == []


@pytest.mark.parametrize("file", FILES, ids=lambda f: f.stem)
def test_every_claim_and_pattern_was_measured(file):
    entries = json.loads(file.read_text())["results"]
    assert {e["code"] for e in entries if e["kind"] == "rule"} == {
        code for code, rule in RULES.items() if rule.claim
    }
    assert {e["name"] for e in entries if e["kind"] == "safe"} == {p.name for p in PATTERNS}


@pytest.mark.parametrize("file", FILES, ids=lambda f: f.stem)
def test_measured_sql_is_the_current_demo(file):
    entries = json.loads(file.read_text())["results"]
    for entry in entries:
        if entry["kind"] == "rule":
            assert entry["sql"] == RULES[entry["code"]].demo.sql
