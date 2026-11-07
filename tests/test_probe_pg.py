"""Real server tests. Set PGMG_TEST_DSN to a throwaway database to run them."""

import json
import os

import pytest

from migration_guard.cli import main
from migration_guard.evidence import FIXTURE, collect, mismatches
from migration_guard.probe import ProbeError, connect, ensure_throwaway, probe_sql

DSN = os.environ.get("PGMG_TEST_DSN")
pytestmark = [pytest.mark.pg, pytest.mark.skipif(not DSN, reason="PGMG_TEST_DSN is not set")]


def one(sql, setup=FIXTURE):
    return probe_sql(DSN, sql, setup=setup)[0]


def test_every_claim_holds_on_this_server():
    report = collect(DSN)
    assert mismatches(report) == []
    assert len(report["results"]) >= 30


def test_nothing_survives_the_rollback():
    conn = connect(DSN, autocommit=True)
    try:
        conn.execute("DROP TABLE IF EXISTS pgmg_keep")
        conn.execute("CREATE TABLE pgmg_keep (id bigint)")
        conn.execute("INSERT INTO pgmg_keep VALUES (1)")
        probe_sql(DSN, "ALTER TABLE pgmg_keep ADD COLUMN x int; DELETE FROM pgmg_keep;")
        columns = conn.execute("SELECT * FROM pgmg_keep").fetchall()
        assert columns == [(1,)]
    finally:
        conn.execute("DROP TABLE IF EXISTS pgmg_keep")
        conn.close()


def test_scratch_schema_is_removed():
    probe_sql(DSN, "SELECT 1", setup=FIXTURE)
    conn = connect(DSN, autocommit=True)
    try:
        left = conn.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'pgmg_probe_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert left == 0


def test_observer_sees_a_read_get_through_a_share_lock():
    result = one("CREATE INDEX demo_note_idx ON demo (note)")
    assert result.locks["demo"] == "ShareLock"
    assert result.blocked_writes == ["demo"]
    assert result.blocked_reads == []


def test_new_table_in_the_transaction_blocks_nobody():
    result = one("CREATE TABLE brand_new (id bigint PRIMARY KEY)")
    assert result.status == "ok"
    assert result.blocked_reads == []
    assert result.blocked_writes == []


def test_dropped_table_keeps_its_name_in_the_report():
    result = one("DROP TABLE parent_nokey")
    assert result.locks["parent_nokey"] == "AccessExclusiveLock"
    assert result.blocked_reads == ["parent_nokey"]


def test_renamed_table_is_tested_under_the_name_others_see():
    result = one("ALTER TABLE parent_nokey RENAME TO renamed")
    assert result.blocked_reads == ["renamed"]


def test_rewrite_is_visible_only_for_the_table_rewritten():
    result = one("ALTER TABLE demo ALTER COLUMN score TYPE bigint")
    assert result.rewrites == ["demo"]


def test_later_statements_are_not_run_after_a_failure():
    results = probe_sql(DSN, "SELECT nope; SELECT 1;", setup=FIXTURE)
    assert [r.status for r in results] == ["failed", "not_run"]
    assert results[0].sqlstate == "42703"


def test_concurrent_statement_is_skipped_by_default():
    assert one("CREATE INDEX CONCURRENTLY i ON demo (note)").status == "skipped"


def test_begin_and_commit_in_the_file_do_not_end_the_probe_transaction():
    text = "BEGIN;\nALTER TABLE demo ADD COLUMN x int;\nCOMMIT;\nSELECT x FROM demo;"
    results = probe_sql(DSN, text, setup=FIXTURE)
    assert [r.status for r in results] == ["skipped", "ok", "skipped", "ok"]


def test_refuses_a_database_with_a_real_looking_name():
    class FakeConn:
        def execute(self, query):
            class Row:
                def fetchone(self):
                    return ("orders", False)

            return Row()

    with pytest.raises(ProbeError, match="refusing"):
        ensure_throwaway(FakeConn(), False)
    ensure_throwaway(FakeConn(), True)


def test_cli_probe_with_setup(tmp_path, capsys):
    setup = tmp_path / "schema.sql"
    setup.write_text(FIXTURE)
    file = tmp_path / "1.sql"
    file.write_text("ALTER TABLE demo ADD COLUMN x uuid DEFAULT gen_random_uuid();")
    assert main(["probe", "--dsn", DSN, "--setup", str(setup), str(file)]) == 0
    out = capsys.readouterr().out
    assert "rewrote demo" in out
    assert "reads blocked on demo" in out


def test_cli_probe_json_exit_code_on_failure(tmp_path, capsys):
    file = tmp_path / "1.sql"
    file.write_text("SELECT nope;")
    assert main(["probe", "--dsn", DSN, "--format", "json", str(file)]) == 1
    assert json.loads(capsys.readouterr().out)[0]["sqlstate"] == "42703"


def test_cli_evidence_writes_a_report(tmp_path, capsys):
    target = tmp_path / "pg.json"
    assert main(["evidence", "--dsn", DSN, "-o", str(target)]) == 0
    assert json.loads(target.read_text())["major"] >= 14
    assert "claims hold" in capsys.readouterr().out
