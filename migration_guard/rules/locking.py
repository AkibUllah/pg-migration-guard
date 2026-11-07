"""Statements that hold a lock longer, or a stronger lock, than a live system can absorb."""

from __future__ import annotations

import re

from pglast import ast
from pglast.enums import AlterTableType, ConstrType, ObjectType

from migration_guard.model import Claim, Demo, Hit, Severity, rule
from migration_guard.state import (
    State,
    alter_table,
    blocking_tables,
    commands,
    dotted,
    function_calls,
    name_of,
)

ACCESS_EXCLUSIVE = "AccessExclusiveLock"
# Evaluated once for the whole ALTER, so PostgreSQL stores one value and does not rewrite.
STABLE_DEFAULTS = {
    "now",
    "current_timestamp",
    "current_date",
    "current_time",
    "localtimestamp",
    "localtime",
    "statement_timestamp",
    "transaction_timestamp",
}
REWRITE_TYPES = {"serial", "bigserial", "smallserial", "serial2", "serial4", "serial8"}
ROOMY_TYPES = {"text", "varchar"}


def constraints(column: ast.ColumnDef) -> tuple:
    return column.constraints or ()


def added_columns(stmt) -> list[ast.ColumnDef]:
    return [c.def_ for c in commands(stmt) if c.subtype == AlterTableType.AT_AddColumn]


def added_constraints(stmt) -> list[ast.Constraint]:
    return [c.def_ for c in commands(stmt) if c.subtype == AlterTableType.AT_AddConstraint]


def has(column: ast.ColumnDef, *kinds: ConstrType) -> bool:
    return any(c.contype in kinds for c in constraints(column))


def concurrently(params: tuple | None) -> bool:
    return any(p.defname == "concurrently" for p in params or ())


def existing_alter(stmt, state: State) -> str | None:
    table = alter_table(stmt)
    return None if table is None or state.is_new(table) else table


@rule(
    "PMG001",
    "add-index-not-concurrent",
    Severity.ERROR,
    "CREATE INDEX without CONCURRENTLY",
    "Takes SHARE on the table for the whole build. Reads go on; every INSERT, UPDATE and DELETE "
    "waits until the index is finished.",
    "Use CREATE INDEX CONCURRENTLY, outside a transaction.",
    Claim(lock="ShareLock", scans=True),
    Demo("CREATE INDEX demo_note_idx ON demo (note)"),
)
def add_index(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.IndexStmt) and not node.concurrent:
        if not state.is_new(name_of(node.relation)):
            fix = re.sub(r"\bINDEX\b", "INDEX CONCURRENTLY", stmt.sql, count=1, flags=re.I)
            yield Hit(f"index build on {name_of(node.relation)} blocks writes until done", fix)


@rule(
    "PMG002",
    "drop-index-not-concurrent",
    Severity.WARNING,
    "DROP INDEX without CONCURRENTLY",
    "Takes ACCESS EXCLUSIVE on the index's table. The drop is quick, but it has to wait for every "
    "open query on the table, and everything behind it in the lock queue waits with it.",
    "Use DROP INDEX CONCURRENTLY, outside a transaction.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("DROP INDEX demo_score_idx"),
)
def drop_index(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.DropStmt) and node.removeType == ObjectType.OBJECT_INDEX:
        if not node.concurrent:
            fix = re.sub(r"\bINDEX\b", "INDEX CONCURRENTLY", stmt.sql, count=1, flags=re.I)
            yield Hit("DROP INDEX queues behind every open query on the table", fix)


def _concurrent_in_tx(node: object) -> str | None:
    if isinstance(node, (ast.IndexStmt, ast.DropStmt)) and node.concurrent:
        return "CREATE INDEX" if isinstance(node, ast.IndexStmt) else "DROP"
    if isinstance(node, ast.ReindexStmt) and concurrently(node.params):
        return "REINDEX"
    if isinstance(node, ast.VacuumStmt) and node.is_vacuumcmd:
        return "VACUUM"
    return None


@rule(
    "PMG003",
    "concurrent-in-transaction",
    Severity.ERROR,
    "CONCURRENTLY inside a transaction",
    "CONCURRENTLY commits internally between its phases, so PostgreSQL refuses it inside a "
    "transaction block. The migration fails at deploy time.",
    "Run the statement in its own migration with the tool's transaction wrapper switched off.",
    Claim(fails_with="25001"),
    Demo("CREATE INDEX CONCURRENTLY demo_note_idx ON demo (note)"),
)
def concurrent_in_transaction(stmt, state: State):
    label = _concurrent_in_tx(stmt.node)
    detach = any(
        c.subtype == AlterTableType.AT_DetachPartition and c.def_.concurrent for c in commands(stmt)
    )
    if state.in_transaction and (label or detach):
        yield Hit(f"{label or 'DETACH PARTITION'} CONCURRENTLY cannot run inside a transaction")


@rule(
    "PMG004",
    "foreign-key-validates-now",
    Severity.ERROR,
    "Foreign key added without NOT VALID",
    "Checks every row while holding SHARE ROW EXCLUSIVE on both tables, so writes to the child and "
    "the parent stop for the length of the scan.",
    "Add the constraint NOT VALID, then VALIDATE CONSTRAINT in a separate transaction.",
    Claim(lock="ShareRowExclusiveLock", scans=True),
    Demo(
        "ALTER TABLE demo ADD CONSTRAINT demo_parent_fk FOREIGN KEY (parent_id) "
        "REFERENCES parent (id)"
    ),
)
def foreign_key(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for constraint in added_constraints(stmt):
        if constraint.contype == ConstrType.CONSTR_FOREIGN and not constraint.skip_validation:
            name = constraint.conname or "<constraint>"
            yield Hit(
                f"{name} checks every row of {table} while blocking writes",
                f"ALTER TABLE {table} ADD ... NOT VALID;\n"
                f"-- next migration:\nALTER TABLE {table} VALIDATE CONSTRAINT {name};",
            )
    for column in added_columns(stmt):
        if has(column, ConstrType.CONSTR_FOREIGN):
            yield Hit(
                f"REFERENCES on new column {column.colname} takes a lock on both tables",
                "Add the column first, then add the constraint NOT VALID and validate it.",
                Severity.WARNING,
            )


@rule(
    "PMG005",
    "check-validates-now",
    Severity.ERROR,
    "CHECK constraint added without NOT VALID",
    "Scans the whole table under ACCESS EXCLUSIVE, so reads and writes both stop until it ends.",
    "Add the constraint NOT VALID, then VALIDATE CONSTRAINT in a separate transaction.",
    Claim(lock=ACCESS_EXCLUSIVE, scans=True),
    Demo("ALTER TABLE demo ADD CONSTRAINT demo_score_ck CHECK (score >= 0)"),
)
def check_constraint(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for constraint in added_constraints(stmt):
        if constraint.contype == ConstrType.CONSTR_CHECK and not constraint.skip_validation:
            name = constraint.conname or "<constraint>"
            yield Hit(
                f"{name} scans {table} under ACCESS EXCLUSIVE",
                f"ALTER TABLE {table} ADD ... NOT VALID;\n"
                f"-- next migration:\nALTER TABLE {table} VALIDATE CONSTRAINT {name};",
            )


@rule(
    "PMG006",
    "set-not-null-scans",
    Severity.ERROR,
    "SET NOT NULL with no validated CHECK behind it",
    "Scans the table under ACCESS EXCLUSIVE to prove there are no NULLs. If a validated CHECK "
    "(col IS NOT NULL) already exists, PostgreSQL uses it and skips the scan.",
    "Add CHECK (col IS NOT NULL) NOT VALID, validate it in a separate transaction, then SET NOT "
    "NULL and drop the CHECK. On PostgreSQL 18, ADD CONSTRAINT ... NOT NULL col NOT VALID does the "
    "same directly.",
    Claim(lock=ACCESS_EXCLUSIVE, scans=True),
    Demo("ALTER TABLE demo ALTER COLUMN note SET NOT NULL"),
)
def set_not_null(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for cmd in commands(stmt):
        if cmd.subtype == AlterTableType.AT_SetNotNull:
            if (table, cmd.name) not in state.proven_not_null:
                yield Hit(
                    f"SET NOT NULL on {table}.{cmd.name} scans the table under ACCESS EXCLUSIVE",
                    f"ALTER TABLE {table} ADD CONSTRAINT {cmd.name}_not_null "
                    f"CHECK ({cmd.name} IS NOT NULL) NOT VALID;\n"
                    f"-- next migration:\nALTER TABLE {table} VALIDATE CONSTRAINT "
                    f"{cmd.name}_not_null;\n"
                    f"ALTER TABLE {table} ALTER COLUMN {cmd.name} SET NOT NULL;\n"
                    f"ALTER TABLE {table} DROP CONSTRAINT {cmd.name}_not_null;",
                )
        elif cmd.subtype == AlterTableType.AT_AddConstraint:
            constraint = cmd.def_
            if constraint.contype == ConstrType.CONSTR_NOTNULL and not constraint.skip_validation:
                yield Hit(f"NOT NULL constraint on {table} scans under ACCESS EXCLUSIVE")


def _index_constraint(stmt, state: State, kind: ConstrType, label: str):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for constraint in added_constraints(stmt):
        if constraint.contype == kind and not constraint.indexname:
            yield Hit(f"{label} builds its index on {table} under ACCESS EXCLUSIVE")
    for column in added_columns(stmt):
        if has(column, kind):
            yield Hit(f"{label} on new column {column.colname} builds an index in place")


@rule(
    "PMG007",
    "unique-builds-index",
    Severity.ERROR,
    "UNIQUE constraint without USING INDEX",
    "Builds the index inside ALTER TABLE, under ACCESS EXCLUSIVE, for as long as the build takes.",
    "CREATE UNIQUE INDEX CONCURRENTLY first, then ADD CONSTRAINT ... UNIQUE USING INDEX.",
    Claim(lock=ACCESS_EXCLUSIVE, scans=True),
    Demo("ALTER TABLE demo ADD CONSTRAINT demo_id_note_uq UNIQUE (id, note)"),
)
def unique_constraint(stmt, state: State):
    yield from _index_constraint(stmt, state, ConstrType.CONSTR_UNIQUE, "UNIQUE")


@rule(
    "PMG008",
    "primary-key-builds-index",
    Severity.ERROR,
    "PRIMARY KEY without USING INDEX",
    "Builds the index and proves NOT NULL inside ALTER TABLE, under ACCESS EXCLUSIVE.",
    "CREATE UNIQUE INDEX CONCURRENTLY first, then ADD CONSTRAINT ... PRIMARY KEY USING INDEX.",
    Claim(lock=ACCESS_EXCLUSIVE, scans=True),
    Demo("ALTER TABLE parent_nokey ADD PRIMARY KEY (id)", "parent_nokey"),
)
def primary_key(stmt, state: State):
    yield from _index_constraint(stmt, state, ConstrType.CONSTR_PRIMARY, "PRIMARY KEY")


@rule(
    "PMG009",
    "add-column-volatile-default",
    Severity.ERROR,
    "New column whose default is evaluated per row",
    "A constant default is stored once in the catalog. A volatile one, such as gen_random_uuid(), "
    "has to be written to every row, so the table is rewritten under ACCESS EXCLUSIVE.",
    "Add the column with no default, set the default in a second statement, backfill in batches.",
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, scans=True),
    Demo("ALTER TABLE demo ADD COLUMN token uuid DEFAULT gen_random_uuid()"),
)
def volatile_default(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for column in added_columns(stmt):
        for constraint in constraints(column):
            if constraint.contype != ConstrType.CONSTR_DEFAULT:
                continue
            calls = function_calls(constraint.raw_expr) - STABLE_DEFAULTS
            if calls:
                name = ", ".join(sorted(calls))
                yield Hit(
                    f"default {name}() on {table}.{column.colname} rewrites the table if it is "
                    "volatile (ignore this if the function is immutable or stable)",
                    f"ALTER TABLE {table} ADD COLUMN {column.colname} ...;  -- no default\n"
                    f"ALTER TABLE {table} ALTER COLUMN {column.colname} SET DEFAULT ...;\n"
                    "-- then backfill existing rows in batches, outside this transaction",
                )


@rule(
    "PMG010",
    "add-column-rewrites",
    Severity.ERROR,
    "New serial, identity or stored generated column",
    "Each of these needs a value in every existing row, so the table is rewritten under ACCESS "
    "EXCLUSIVE.",
    "Add a plain column, attach a sequence as the default, and backfill in batches.",
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, scans=True),
    Demo("ALTER TABLE demo ADD COLUMN seq bigserial"),
)
def rewriting_column(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for column in added_columns(stmt):
        kind = column.typeName.names[-1].sval
        if kind in REWRITE_TYPES:
            yield Hit(f"{kind} column {table}.{column.colname} rewrites the table")
        elif has(column, ConstrType.CONSTR_IDENTITY):
            yield Hit(f"identity column {table}.{column.colname} rewrites the table")
        elif has(column, ConstrType.CONSTR_GENERATED):
            stored = any(
                c.contype == ConstrType.CONSTR_GENERATED and c.generated_kind == "s"
                for c in constraints(column)
            )
            if stored:
                yield Hit(f"stored generated column {table}.{column.colname} rewrites the table")


@rule(
    "PMG011",
    "add-column-not-null-no-default",
    Severity.ERROR,
    "NOT NULL column with no default",
    "Existing rows have no value for the column, so the statement fails on any non-empty table.",
    "Add it nullable or with a constant default, backfill, then enforce NOT NULL.",
    Claim(fails_with="23502"),
    Demo("ALTER TABLE demo ADD COLUMN region text NOT NULL"),
)
def not_null_no_default(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for column in added_columns(stmt):
        kind = column.typeName.names[-1].sval
        if (
            has(column, ConstrType.CONSTR_NOTNULL)
            and not has(column, ConstrType.CONSTR_DEFAULT, ConstrType.CONSTR_IDENTITY)
            and not has(column, ConstrType.CONSTR_GENERATED)
            and kind not in REWRITE_TYPES
        ):
            yield Hit(f"{table}.{column.colname} is NOT NULL with no default: fails if rows exist")


@rule(
    "PMG012",
    "alter-type-rewrites",
    Severity.ERROR,
    "ALTER COLUMN TYPE",
    "Most type changes rewrite the table and rebuild its indexes under ACCESS EXCLUSIVE. The "
    "exceptions are binary-compatible ones, such as varchar to text or a longer varchar.",
    "Add a new column, write to both, backfill in batches, switch readers, drop the old one.",
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, scans=True),
    Demo("ALTER TABLE demo ALTER COLUMN score TYPE bigint"),
)
def alter_type(stmt, state: State):
    table = existing_alter(stmt, state)
    if table is None:
        return
    for cmd in commands(stmt):
        if cmd.subtype != AlterTableType.AT_AlterColumnType:
            continue
        target = cmd.def_.typeName
        kind = target.names[-1].sval
        if kind in ROOMY_TYPES and cmd.def_.raw_default is None:
            yield Hit(
                f"{table}.{cmd.name} to {kind}: no rewrite from varchar or text, full rewrite "
                "from anything else",
                severity=Severity.WARNING,
            )
        else:
            yield Hit(f"{table}.{cmd.name} to {kind} rewrites the table under ACCESS EXCLUSIVE")


@rule(
    "PMG013",
    "full-table-rebuild",
    Severity.ERROR,
    "VACUUM FULL or CLUSTER",
    "Both copy the whole table under ACCESS EXCLUSIVE. Neither belongs in a deploy.",
    "Schedule pg_repack or pg_squeeze, or a maintenance window, outside the migration.",
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, scans=True),
    Demo("CLUSTER demo USING demo_pkey"),
)
def full_rebuild(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.ClusterStmt):
        yield Hit("CLUSTER copies the table under ACCESS EXCLUSIVE")
    if isinstance(node, ast.VacuumStmt) and node.is_vacuumcmd:
        if any(o.defname == "full" for o in node.options or ()):
            yield Hit("VACUUM FULL copies the table under ACCESS EXCLUSIVE")


@rule(
    "PMG014",
    "truncate",
    Severity.ERROR,
    "TRUNCATE",
    "Removes every row and takes ACCESS EXCLUSIVE. Unlike DELETE it also fires no row triggers.",
    "If the data really is disposable, say why in a pgmg:ignore comment.",
    # Rebuilding the indexes reads the new, empty heap, which the scan counter also counts.
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, scans=None),
    Demo("TRUNCATE demo CASCADE"),
)
def truncate(stmt, state: State):
    if isinstance(stmt.node, ast.TruncateStmt):
        tables = state.existing({name_of(r) for r in stmt.node.relations})
        if tables:
            yield Hit(f"TRUNCATE removes every row of {', '.join(sorted(tables))}")


@rule(
    "PMG015",
    "reindex-not-concurrent",
    Severity.ERROR,
    "REINDEX without CONCURRENTLY",
    "Blocks writes to the table, and blocks reads that would have used the index, for the whole "
    "rebuild.",
    "Use REINDEX ... CONCURRENTLY, outside a transaction.",
    Claim(lock="ShareLock", scans=True, reads_blocked=True),
    Demo("REINDEX INDEX demo_score_idx"),
)
def reindex(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.ReindexStmt) and not concurrently(node.params):
        fix = re.sub(
            r"\b(INDEX|TABLE|SCHEMA|DATABASE|SYSTEM)\b",
            r"\1 CONCURRENTLY",
            stmt.sql,
            count=1,
            flags=re.I,
        )
        yield Hit("REINDEX blocks writes, and reads through the index, until it ends", fix)


@rule(
    "PMG016",
    "refresh-matview-not-concurrent",
    Severity.ERROR,
    "REFRESH MATERIALIZED VIEW without CONCURRENTLY",
    "Replaces the view's contents under ACCESS EXCLUSIVE, so nothing can read it during the "
    "refresh.",
    "Add a unique index on the view and use REFRESH MATERIALIZED VIEW CONCURRENTLY.",
    Claim(lock=ACCESS_EXCLUSIVE, rewrites=True, writes_blocked=False),
    Demo("REFRESH MATERIALIZED VIEW demo_mv", "demo_mv"),
)
def refresh_matview(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.RefreshMatViewStmt) and not node.concurrent:
        yield Hit(f"nothing can read {name_of(node.relation)} during the refresh")


@rule(
    "PMG017",
    "detach-partition-not-concurrent",
    Severity.WARNING,
    "DETACH PARTITION without CONCURRENTLY",
    "Takes ACCESS EXCLUSIVE on the partitioned table, which stops all traffic to every partition "
    "while it waits for and holds the lock.",
    "Use DETACH PARTITION ... CONCURRENTLY (PostgreSQL 14 and later), outside a transaction.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("ALTER TABLE events DETACH PARTITION events_2025", "events"),
)
def detach_partition(stmt, state: State):
    if state.target_version < 14:
        return
    for cmd in commands(stmt):
        if cmd.subtype == AlterTableType.AT_DetachPartition and not cmd.def_.concurrent:
            yield Hit(f"DETACH PARTITION locks all of {name_of(stmt.node.relation)}")


@rule(
    "PMG019",
    "missing-lock-timeout",
    Severity.WARNING,
    "Blocking DDL with no lock_timeout",
    "DDL that has to wait for a long query sits at the head of the lock queue, and every later "
    "query on the table waits behind it. A short lock_timeout turns an outage into a failed, "
    "retryable migration.",
    "Start the migration with SET lock_timeout = '2s'; and retry on failure.",
)
def missing_lock_timeout(stmt, state: State):
    if state.lock_timeout or state.warned_lock_timeout:
        return
    tables = state.existing(blocking_tables(stmt))
    if tables:
        state.warned_lock_timeout = True
        yield Hit(
            f"no lock_timeout set before locking {', '.join(sorted(tables))}",
            "SET lock_timeout = '2s';",
        )


@rule(
    "PMG020",
    "validate-in-same-transaction",
    Severity.ERROR,
    "VALIDATE in the same transaction as NOT VALID",
    "The strong lock from ADD CONSTRAINT is kept until commit, so the validation scan runs under "
    "it, and the two-step pattern gains nothing.",
    "Commit between the two statements, or put VALIDATE in the next migration.",
    Claim(lock=ACCESS_EXCLUSIVE, scans=True),
    Demo(
        "ALTER TABLE demo ADD CONSTRAINT demo_score_ck CHECK (score >= 0) NOT VALID;\n"
        "ALTER TABLE demo VALIDATE CONSTRAINT demo_score_ck"
    ),
)
def validate_same_tx(stmt, state: State):
    table = alter_table(stmt)
    if table is None or not state.in_transaction:
        return
    for cmd in commands(stmt):
        if cmd.subtype == AlterTableType.AT_ValidateConstraint:
            if (table, cmd.name) in state.tx_not_valid:
                yield Hit(f"{cmd.name} validates while the lock from ADD CONSTRAINT is still held")


@rule(
    "PMG021",
    "many-tables-locked-in-transaction",
    Severity.WARNING,
    "Strong locks on several tables in one transaction",
    "Locks are held until commit. The more tables a transaction holds, the wider the outage and "
    "the likelier a deadlock against application transactions that touch them in another order.",
    "One table per transaction. Split the migration.",
)
def many_tables(stmt, state: State):
    if not state.in_transaction or state.tx_multi_reported:
        return
    tables = state.tx_blocking | state.existing(blocking_tables(stmt))
    if len(tables) > 1:
        state.tx_multi_reported = True
        yield Hit(f"one transaction holds strong locks on {', '.join(sorted(tables))}")


@rule(
    "PMG022",
    "enum-value-in-transaction",
    Severity.WARNING,
    "ALTER TYPE ADD VALUE inside a transaction",
    "The new value cannot be used until the transaction commits. A later statement in the same "
    "migration that uses it fails.",
    "Add the value in its own migration, and use it in the next one.",
    Claim(fails_with="55P04"),
    Demo(
        "ALTER TYPE demo_status ADD VALUE 'held';\nSELECT 'held'::demo_status",
    ),
)
def enum_value(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.AlterEnumStmt) and state.in_transaction and not node.oldVal:
        yield Hit(f"{dotted(node.typeName)} value {node.newVal!r} is unusable until commit")
