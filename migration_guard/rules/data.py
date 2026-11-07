"""Data changes inside schema migrations."""

from __future__ import annotations

from pglast import ast

from migration_guard.model import Claim, Demo, Hit, Severity, rule
from migration_guard.state import State, name_of


@rule(
    "PMG040",
    "unbounded-data-change",
    Severity.WARNING,
    "UPDATE or DELETE with no WHERE",
    "Touches every row in one transaction: row locks on the whole table, a burst of WAL for "
    "replicas to replay, and dead tuples for vacuum to clean up.",
    "Backfill in batches of a few thousand rows, each in its own transaction.",
    Claim(lock="RowExclusiveLock", scans=True),
    Demo("UPDATE demo SET score = 0"),
)
def unbounded(stmt, state: State):
    node = stmt.node
    if isinstance(node, (ast.UpdateStmt, ast.DeleteStmt)) and node.whereClause is None:
        table = name_of(node.relation)
        if not state.is_new(table):
            verb = "UPDATE" if isinstance(node, ast.UpdateStmt) else "DELETE"
            yield Hit(f"{verb} touches every row of {table} in one transaction")


@rule(
    "PMG041",
    "backfill-under-lock",
    Severity.ERROR,
    "Data change in a transaction that holds a strong lock",
    "Every lock taken by earlier DDL is held until commit, so the table stays blocked for as long "
    "as the backfill runs.",
    "Commit the DDL first and run the backfill separately.",
)
def backfill_under_lock(stmt, state: State):
    if not isinstance(stmt.node, (ast.UpdateStmt, ast.DeleteStmt, ast.InsertStmt)):
        return
    if state.in_transaction and state.tx_blocking:
        tables = ", ".join(sorted(state.tx_blocking))
        yield Hit(f"this data change runs while the transaction blocks {tables}")
