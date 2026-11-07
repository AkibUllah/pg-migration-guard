import pytest

from migration_guard.checker import Options, check_text
from migration_guard.cli import main
from migration_guard.evidence import FIXTURE, PATTERNS, compare, mismatches
from migration_guard.model import RULES, Claim
from migration_guard.probe import (
    LOCK_ORDER,
    StatementResult,
    cannot_run_in_transaction,
    is_throwaway,
    strongest,
    to_text,
)
from migration_guard.source import parse_migration

CLAIMED = [rule for rule in RULES.values() if rule.claim]


def statement(sql):
    return parse_migration(sql).statements[0]


def measured(**overrides):
    base = {
        "lock": "ShareLock",
        "rewrites": False,
        "scans": True,
        "blocks_reads": False,
        "blocks_writes": True,
        "fails_with": None,
    }
    return base | overrides


@pytest.mark.parametrize("rule", CLAIMED, ids=lambda r: r.code)
def test_every_demo_triggers_its_own_rule(rule):
    found = check_text(rule.demo.sql, Options(assume_in_transaction=True))
    assert rule.code in [f.code for f in found]


@pytest.mark.parametrize("rule", CLAIMED, ids=lambda r: r.code)
def test_every_claim_has_a_demo(rule):
    assert rule.demo is not None


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.name)
def test_recommended_patterns_raise_no_errors(pattern):
    last_line = pattern.setup.count("\n") + 2
    found = check_text(f"{pattern.setup}\n{pattern.sql}")
    assert [f.code for f in found if f.line == last_line and f.severity.label == "error"] == []


def test_fixture_parses():
    assert len(parse_migration(FIXTURE).statements) > 8


def test_every_rule_explains_itself():
    for rule in RULES.values():
        assert rule.title and rule.why and rule.fix


def test_rule_codes_are_well_formed():
    assert all(len(code) == 6 and code.startswith("PMG") for code in RULES)


def test_strongest_lock():
    assert strongest(["RowShareLock", "ShareLock", "AccessShareLock"]) == "ShareLock"
    assert LOCK_ORDER[-1] == "AccessExclusiveLock"


@pytest.mark.parametrize("name", ["app_shadow", "scratch", "pgmg_test", "ci-db", "orders_dev"])
def test_throwaway_names(name):
    assert is_throwaway(name)


@pytest.mark.parametrize("name", ["orders", "production", "pricing", "latest", "contest"])
def test_real_looking_names(name):
    assert not is_throwaway(name)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX CONCURRENTLY i ON t (a)",
        "DROP INDEX CONCURRENTLY i",
        "REINDEX INDEX CONCURRENTLY i",
        "VACUUM t",
        "ALTER TABLE t DETACH PARTITION p CONCURRENTLY",
    ],
)
def test_statements_the_probe_must_skip(sql):
    assert cannot_run_in_transaction(statement(sql))


@pytest.mark.parametrize("sql", ["CREATE INDEX i ON t (a)", "ANALYZE t", "SELECT 1"])
def test_statements_the_probe_can_run(sql):
    assert not cannot_run_in_transaction(statement(sql))


def test_compare_accepts_a_match():
    assert compare(Claim(lock="ShareLock", scans=True), measured()) == []


def test_compare_names_the_difference():
    problems = compare(Claim(lock="ShareLock", scans=False), measured())
    assert problems == ["scans: claimed False, measured True"]


def test_compare_ignores_what_a_rule_does_not_claim():
    assert compare(Claim(lock="ShareLock", scans=None), measured()) == []


def test_compare_catches_a_blocking_difference():
    problems = compare(Claim(lock="ShareLock", scans=True), measured(blocks_writes=False))
    assert problems == ["blocks_writes: claimed True, measured False"]


def test_compare_on_failing_demo_checks_only_the_sqlstate():
    assert compare(Claim(fails_with="25001"), measured(lock=None, fails_with="25001")) == []


def test_compare_when_a_failing_demo_succeeds():
    assert compare(Claim(fails_with="25001"), measured()) != []


def test_compare_when_a_working_demo_fails():
    problems = compare(Claim(lock="ShareLock"), measured(fails_with="42P01"))
    assert problems == ["fails_with: claimed None, measured '42P01'"]


def test_override_of_read_blocking():
    claim = Claim(lock="ShareLock", reads_blocked=True)
    assert claim.blocks_reads and claim.blocks_writes


def test_override_of_write_blocking():
    claim = Claim(lock="AccessExclusiveLock", writes_blocked=False)
    assert claim.blocks_reads and not claim.blocks_writes


def test_mismatches_are_labelled():
    report = {
        "results": [
            {"code": "PMG001", "name": "a", "mismatches": ["lock: x"]},
            {"code": "", "name": "widen-varchar", "mismatches": ["scans: y"]},
        ]
    }
    assert mismatches(report) == ["PMG001: lock: x", "widen-varchar: scans: y"]


def test_probe_text_output():
    results = [
        StatementResult(
            2,
            "ALTER TABLE t\n  ADD COLUMN a int",
            "ok",
            ms=1.5,
            locks={"t": "AccessExclusiveLock"},
            rewrites=["t"],
            scans=["t"],
            blocked_reads=["t"],
            blocked_writes=["t"],
        ),
        StatementResult(3, "CREATE INDEX CONCURRENTLY i ON t (a)", "skipped", reason="no tx"),
        StatementResult(4, "SELECT x", "failed", reason="boom", sqlstate="42703"),
    ]
    out = to_text(results)
    assert "line 2: ALTER TABLE t ADD COLUMN a int" in out
    assert "holds AccessExclusiveLock on t" in out
    assert "rewrote t" in out
    assert "reads blocked on t" in out
    assert "skipped: no tx" in out
    assert "failed [42703]: boom" in out
    assert out.endswith("rolled back: nothing was changed")


def test_probe_needs_a_dsn(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PGMG_DSN", raising=False)
    file = tmp_path / "1.sql"
    file.write_text("SELECT 1;")
    assert main(["probe", str(file)]) == 2
    assert "PGMG_DSN" in capsys.readouterr().err


def test_probe_reports_a_bad_connection(tmp_path, capsys):
    file = tmp_path / "1.sql"
    file.write_text("SELECT 1;")
    code = main(["probe", "--dsn", "postgresql://nobody@127.0.0.1:1/x", str(file)])
    assert code == 2
    assert "could not connect" in capsys.readouterr().err
