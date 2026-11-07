"""Check every rule's claim, and every recommended fix, on a real server."""

from __future__ import annotations

from dataclasses import dataclass

import migration_guard.rules  # noqa: F401 - registers the rules
from migration_guard import __version__
from migration_guard.model import RULES, Claim
from migration_guard.probe import StatementResult, connect, ensure_throwaway, probe_sql

FIXTURE = """
CREATE TABLE parent (id bigint PRIMARY KEY);
INSERT INTO parent SELECT generate_series(1, 100);
CREATE TABLE parent_nokey (id bigint NOT NULL);
INSERT INTO parent_nokey SELECT generate_series(1, 100);
CREATE TABLE demo (
    id bigint PRIMARY KEY, parent_id bigint, note text, score int, label varchar(10)
);
INSERT INTO demo SELECT g, g % 100 + 1, 'row ' || g, g, 'x' FROM generate_series(1, 2000) g;
CREATE INDEX demo_score_idx ON demo (score);
CREATE TYPE demo_status AS ENUM ('queued', 'sent');
CREATE MATERIALIZED VIEW demo_mv AS SELECT parent_id, count(*) AS n FROM demo GROUP BY parent_id;
CREATE TABLE events (id bigint, at date) PARTITION BY RANGE (at);
CREATE TABLE events_2025 PARTITION OF events FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
INSERT INTO events SELECT g, DATE '2025-01-01' + g % 300 FROM generate_series(1, 500) g;
"""


@dataclass(frozen=True)
class Pattern:
    """A safe statement this project recommends, with what it claims to cost."""

    name: str
    sql: str
    claim: Claim
    setup: str = ""
    target: str = "demo"


AE = "AccessExclusiveLock"
PATTERNS = [
    Pattern(
        "add-column-constant-default",
        "ALTER TABLE demo ADD COLUMN region text DEFAULT 'dhaka'",
        Claim(lock=AE),
    ),
    Pattern(
        "add-column-now-default",
        "ALTER TABLE demo ADD COLUMN seen_at timestamptz DEFAULT now()",
        Claim(lock=AE),
    ),
    Pattern(
        "add-foreign-key-not-valid",
        "ALTER TABLE demo ADD CONSTRAINT demo_parent_fk FOREIGN KEY (parent_id) "
        "REFERENCES parent (id) NOT VALID",
        Claim(lock="ShareRowExclusiveLock"),
    ),
    Pattern(
        "validate-foreign-key",
        "ALTER TABLE demo VALIDATE CONSTRAINT demo_parent_fk",
        Claim(lock="ShareUpdateExclusiveLock", scans=True),
        "ALTER TABLE demo ADD CONSTRAINT demo_parent_fk FOREIGN KEY (parent_id) "
        "REFERENCES parent (id) NOT VALID;",
    ),
    Pattern(
        "add-check-not-valid",
        "ALTER TABLE demo ADD CONSTRAINT demo_score_ck CHECK (score >= 0) NOT VALID",
        Claim(lock=AE),
    ),
    Pattern(
        "validate-check",
        "ALTER TABLE demo VALIDATE CONSTRAINT demo_score_ck",
        Claim(lock="ShareUpdateExclusiveLock", scans=True),
        "ALTER TABLE demo ADD CONSTRAINT demo_score_ck CHECK (score >= 0) NOT VALID;",
    ),
    Pattern(
        "set-not-null-after-validated-check",
        "ALTER TABLE demo ALTER COLUMN score SET NOT NULL",
        Claim(lock=AE),
        "ALTER TABLE demo ADD CONSTRAINT demo_score_nn CHECK (score IS NOT NULL);",
    ),
    Pattern(
        "unique-using-existing-index",
        "ALTER TABLE demo ADD CONSTRAINT demo_score_uq UNIQUE USING INDEX demo_score_uq_idx",
        Claim(lock=AE),
        "CREATE UNIQUE INDEX demo_score_uq_idx ON demo (score);",
    ),
    Pattern(
        "widen-varchar",
        "ALTER TABLE demo ALTER COLUMN label TYPE varchar(40)",
        Claim(lock=AE),
    ),
    Pattern(
        "varchar-to-text",
        "ALTER TABLE demo ALTER COLUMN label TYPE text",
        Claim(lock=AE),
    ),
]


def measure(results: list[StatementResult], target: str) -> dict:
    """Sum up a demo. Locks come from the last statement, since they are held until commit."""
    ran = [r for r in results if r.status == "ok"]
    last = results[-1]
    failed = next((r for r in results if r.status == "failed"), None)
    return {
        "lock": last.locks.get(target),
        "rewrites": any(target in r.rewrites for r in ran),
        "scans": any(target in r.scans for r in ran),
        "blocks_reads": target in last.blocked_reads,
        "blocks_writes": target in last.blocked_writes,
        "fails_with": failed.sqlstate if failed else None,
    }


def compare(claim: Claim, measured: dict) -> list[str]:
    """Differences between what a rule says and what the server did."""
    if claim.fails_with or measured["fails_with"]:
        return (
            []
            if claim.fails_with == measured["fails_with"]
            else [f"fails_with: claimed {claim.fails_with!r}, measured {measured['fails_with']!r}"]
        )
    fields = {
        "lock": claim.lock,
        "rewrites": claim.rewrites,
        "scans": claim.scans,
        "blocks_reads": claim.blocks_reads,
        "blocks_writes": claim.blocks_writes,
    }
    return [
        f"{key}: claimed {expected!r}, measured {measured[key]!r}"
        for key, expected in fields.items()
        if expected is not None and measured[key] != expected
    ]


def _entry(kind: str, code: str, name: str, sql: str, claim: Claim, measured: dict) -> dict:
    return {
        "kind": kind,
        "code": code,
        "name": name,
        "sql": sql,
        "measured": measured,
        "mismatches": compare(claim, measured),
    }


def collect(dsn: str, *, allow_any_database: bool = False) -> dict:
    admin = connect(dsn, autocommit=True)
    try:
        ensure_throwaway(admin, allow_any_database)
        number = int(admin.execute("SHOW server_version_num").fetchone()[0])
        version = admin.execute("SHOW server_version").fetchone()[0].split()[0]
    finally:
        admin.close()

    def run(sql: str, setup: str, target: str) -> dict:
        results = probe_sql(
            dsn,
            sql,
            setup=FIXTURE + setup,
            allow_any_database=allow_any_database,
            skip_non_transactional=False,
        )
        return measure(results, target)

    entries = []
    for code in sorted(RULES):
        rule = RULES[code]
        if rule.claim and rule.demo:
            measured = run(rule.demo.sql, rule.demo.setup, rule.demo.target)
            entries.append(_entry("rule", code, rule.name, rule.demo.sql, rule.claim, measured))
    for pattern in PATTERNS:
        measured = run(pattern.sql, pattern.setup, pattern.target)
        entries.append(_entry("safe", "", pattern.name, pattern.sql, pattern.claim, measured))
    return {
        "tool_version": __version__,
        "server_version": version,
        "major": number // 10000,
        "results": entries,
    }


def mismatches(report: dict) -> list[str]:
    return [
        f"{e['code'] or e['name']}: {problem}"
        for e in report["results"]
        for problem in e["mismatches"]
    ]
