"""Run a migration on a real server, roll it back, and report what actually happened.

After every statement the probe reads pg_locks for its own backend, compares each relation's
filenode to detect a rewrite, reads the transaction's sequential scan counters, and uses a second
connection with a short lock_timeout to test whether a read and a write would get through.

The transaction always rolls back, but the locks are real while it runs. So the probe refuses
any database that does not look like a throwaway one, unless told otherwise.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from pglast import ast
from pglast.enums import AlterTableType

from migration_guard.rules.locking import concurrently
from migration_guard.source import Statement, parse_migration
from migration_guard.state import commands

LOCK_ORDER = [
    "AccessShareLock",
    "RowShareLock",
    "RowExclusiveLock",
    "ShareUpdateExclusiveLock",
    "ShareLock",
    "ShareRowExclusiveLock",
    "ExclusiveLock",
    "AccessExclusiveLock",
]
THROWAWAY_WORDS = {"shadow", "scratch", "test", "probe", "ci", "dev", "tmp"}
RELATIONS_SQL = """
SELECT c.oid::bigint, c.oid::regclass::text, c.relkind::text, pg_relation_filenode(c.oid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm', 'i')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
"""
LOCKS_SQL = """
SELECT l.relation::bigint, l.mode
FROM pg_locks l
WHERE l.pid = pg_backend_pid() AND l.granted AND l.locktype = 'relation'
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""
SCANS_SQL = "SELECT relid::bigint, seq_scan FROM pg_stat_xact_user_tables"
STORAGE_KINDS = {"r", "m"}
TABLE_KINDS = {"r", "p", "m"}


class ProbeError(RuntimeError):
    pass


@dataclass
class StatementResult:
    line: int
    sql: str
    status: str  # ok, failed, skipped, not_run
    reason: str = ""
    sqlstate: str | None = None
    ms: float = 0.0
    locks: dict[str, str] = field(default_factory=dict)
    rewrites: list[str] = field(default_factory=list)
    scans: list[str] = field(default_factory=list)
    blocked_reads: list[str] = field(default_factory=list)
    blocked_writes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def strongest(modes: list[str]) -> str:
    return max(modes, key=LOCK_ORDER.index)


def is_throwaway(database: str) -> bool:
    return bool(set(re.split(r"[^a-z0-9]+", database.lower())) & THROWAWAY_WORDS)


def cannot_run_in_transaction(stmt: Statement) -> bool:
    node = stmt.node
    if isinstance(node, (ast.IndexStmt, ast.DropStmt)) and node.concurrent:
        return True
    if isinstance(node, ast.ReindexStmt) and concurrently(node.params):
        return True
    if isinstance(node, ast.VacuumStmt) and node.is_vacuumcmd:
        return True
    return any(
        c.subtype == AlterTableType.AT_DetachPartition and c.def_.concurrent for c in commands(stmt)
    )


def _driver():
    try:
        import psycopg
    except ImportError:
        raise ProbeError(
            "the probe needs psycopg: pip install 'pg-migration-guard[probe]'"
        ) from None
    return psycopg


def connect(dsn: str, *, autocommit: bool, search_path: str | None = None):
    psycopg = _driver()
    try:
        conn = psycopg.connect(dsn, autocommit=autocommit, application_name="pgmg-probe")
    except psycopg.OperationalError as exc:
        raise ProbeError(f"could not connect: {exc}") from None
    if search_path:
        conn.execute(
            psycopg.sql.SQL("SET search_path = {}").format(psycopg.sql.Identifier(search_path))
        )
        if not autocommit:
            conn.commit()
    return conn


def ensure_throwaway(conn, allow_any: bool) -> None:
    database, standby = conn.execute("SELECT current_database(), pg_is_in_recovery()").fetchone()
    if standby:
        raise ProbeError("the server is a standby, and DDL cannot run there")
    if not allow_any and not is_throwaway(database):
        raise ProbeError(
            f"refusing to probe database {database!r}: the locks are real while it runs. Use a "
            "shadow copy (a name containing shadow, scratch, test, probe, ci, dev or tmp), or "
            "pass --allow-any-database."
        )


def _rows(conn, query: str) -> list[tuple]:
    return conn.execute(query).fetchall()


def _relations(conn) -> dict[int, tuple[str, str, int | None]]:
    return {row[0]: tuple(row[1:]) for row in _rows(conn, RELATIONS_SQL)}


def _held_locks(conn, names: dict[int, str]) -> dict[int, str]:
    """Strongest lock per relation. `names` keeps relations this transaction already dropped."""
    modes: dict[int, list[str]] = {}
    for oid, mode in _rows(conn, LOCKS_SQL):
        if oid in names:
            modes.setdefault(oid, []).append(mode)
    return {oid: strongest(found) for oid, found in modes.items()}


def _blocked(observer, oids: list[int]) -> tuple[list[int], list[int]]:
    """Try a read and a write from the second connection. Each gives up after lock_timeout.

    The observer resolves each oid to the name it can see, which is the old name after a rename
    the open transaction has not committed. A table created by that transaction has no name yet.
    """
    psycopg = _driver()
    reads, writes = [], []
    for oid in oids:
        visible = observer.execute("SELECT %s::oid::regclass::text", (oid,)).fetchone()[0]
        if visible.isdigit():
            continue
        try:
            observer.execute(f"SELECT 1 FROM {visible} LIMIT 0")
        except psycopg.errors.LockNotAvailable:
            reads.append(oid)
        try:
            with observer.transaction():
                observer.execute(f"LOCK TABLE {visible} IN ROW EXCLUSIVE MODE")
        except psycopg.errors.LockNotAvailable:
            writes.append(oid)
        except psycopg.errors.WrongObjectType:
            continue  # materialized views take no writes
    return reads, writes


def run_statements(
    conn, observer, statements: list[Statement], *, skip_non_transactional: bool = True
) -> list[StatementResult]:
    """Run inside the open transaction on `conn`. The caller rolls back."""
    psycopg = _driver()
    results: list[StatementResult] = []
    known: dict[int, tuple[str, str]] = {}
    failed = False
    for stmt in statements:
        result = StatementResult(stmt.line, stmt.sql, "ok")
        results.append(result)
        if failed:
            result.status, result.reason = "not_run", "an earlier statement failed"
            continue
        if isinstance(stmt.node, ast.TransactionStmt):
            result.status, result.reason = "skipped", "the probe holds its own transaction"
            continue
        if skip_non_transactional and cannot_run_in_transaction(stmt):
            result.status, result.reason = "skipped", "cannot run inside a transaction"
            continue
        before = _relations(conn)
        scans_before = dict(_rows(conn, SCANS_SQL))
        known.update({oid: (name, kind) for oid, (name, kind, _) in before.items()})
        started = time.perf_counter()
        try:
            conn.execute(stmt.sql)
        except psycopg.Error as exc:
            result.ms = (time.perf_counter() - started) * 1000
            result.status = "failed"
            result.sqlstate = exc.sqlstate
            result.reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            failed = True
            continue
        result.ms = (time.perf_counter() - started) * 1000
        after = _relations(conn)
        known.update({oid: (name, kind) for oid, (name, kind, _) in after.items()})
        names = {oid: name for oid, (name, _) in known.items()}
        result.rewrites = sorted(
            names[oid]
            for oid, (_, kind, filenode) in after.items()
            if kind in STORAGE_KINDS and oid in before and before[oid][2] != filenode
        )
        result.scans = sorted(
            names[oid]
            for oid, count in _rows(conn, SCANS_SQL)
            if oid in names and count > scans_before.get(oid, 0)
        )
        held = _held_locks(conn, names)
        result.locks = {names[oid]: held[oid] for oid in sorted(held, key=names.get)}
        tables = [oid for oid in held if known[oid][1] in TABLE_KINDS]
        reads, writes = _blocked(observer, tables)
        result.blocked_reads = sorted(names[oid] for oid in reads)
        result.blocked_writes = sorted(names[oid] for oid in writes)
    return results


def scratch_schema(admin, setup: str | None) -> str:
    psycopg = _driver()
    name = f"pgmg_probe_{time.monotonic_ns():x}"
    admin.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(name)))
    admin.execute(psycopg.sql.SQL("SET search_path = {}").format(psycopg.sql.Identifier(name)))
    if setup:
        admin.execute(setup)
    return name


def drop_schema(admin, name: str) -> None:
    psycopg = _driver()
    admin.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(name)))


def probe_sql(
    dsn: str,
    text: str,
    *,
    setup: str | None = None,
    allow_any_database: bool = False,
    skip_non_transactional: bool = True,
    observer_timeout_ms: int = 100,
) -> list[StatementResult]:
    """Probe a migration. With `setup`, everything happens in a schema that is dropped after."""
    statements = parse_migration(text).statements
    admin = connect(dsn, autocommit=True)
    schema = None
    try:
        ensure_throwaway(admin, allow_any_database)
        if setup is not None:
            schema = scratch_schema(admin, setup)
        conn = connect(dsn, autocommit=False, search_path=schema)
        observer = connect(dsn, autocommit=True, search_path=schema)
        try:
            observer.execute(f"SET lock_timeout = {int(observer_timeout_ms)}")
            conn.execute("SET LOCAL statement_timeout = '60s'")
            conn.execute("SET LOCAL lock_timeout = '5s'")
            return run_statements(
                conn, observer, statements, skip_non_transactional=skip_non_transactional
            )
        finally:
            conn.rollback()
            conn.close()
            observer.close()
    finally:
        if schema:
            drop_schema(admin, schema)
        admin.close()


def to_text(results: list[StatementResult]) -> str:
    lines = []
    for r in results:
        first_line = " ".join(r.sql.split())
        lines.append(f"line {r.line}: {first_line[:88]}")
        if r.status != "ok":
            detail = f" [{r.sqlstate}]" if r.sqlstate else ""
            lines.append(f"  {r.status}{detail}: {r.reason}")
            continue
        lines.append(f"  {r.ms:.1f} ms")
        lines.extend(f"  holds {mode} on {relation}" for relation, mode in r.locks.items())
        if r.rewrites:
            lines.append(f"  rewrote {', '.join(r.rewrites)}")
        if r.scans:
            lines.append(f"  scanned {', '.join(r.scans)}")
        if r.blocked_reads:
            lines.append(f"  reads blocked on {', '.join(r.blocked_reads)}")
        if r.blocked_writes:
            lines.append(f"  writes blocked on {', '.join(r.blocked_writes)}")
    lines.append("rolled back: nothing was changed")
    return "\n".join(lines)
