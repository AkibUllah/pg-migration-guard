"""Changes that are quick on the server and still break the application version in production.

During a rolling deploy the old code runs against the new schema. These rules hold whether the
table has ten rows or ten billion.
"""

from __future__ import annotations

from pglast import ast
from pglast.enums import AlterTableType, ObjectType

from migration_guard.model import Claim, Demo, Hit, Severity, rule
from migration_guard.state import State, alter_table, commands, dotted, name_of

ACCESS_EXCLUSIVE = "AccessExclusiveLock"


@rule(
    "PMG030",
    "drop-column",
    Severity.ERROR,
    "DROP COLUMN",
    "Old application code still selects and inserts the column until the deploy finishes. The "
    "catalog change itself is quick and does not rewrite the table.",
    "Remove every use from the code, deploy, then drop the column in a later migration.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("ALTER TABLE demo DROP COLUMN note"),
)
def drop_column(stmt, state: State):
    table = alter_table(stmt)
    if table is None or state.is_new(table):
        return
    for cmd in commands(stmt):
        if cmd.subtype == AlterTableType.AT_DropColumn:
            yield Hit(f"running code may still use {table}.{cmd.name}")


@rule(
    "PMG031",
    "rename-column",
    Severity.ERROR,
    "RENAME COLUMN",
    "The old name is gone the moment this commits, while the old code is still running.",
    "Add the new column, write to both, backfill, switch readers, drop the old one.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("ALTER TABLE demo RENAME COLUMN note TO remark"),
)
def rename_column(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.RenameStmt) and node.renameType == ObjectType.OBJECT_COLUMN:
        table = name_of(node.relation)
        if not state.is_new(table):
            yield Hit(f"running code still uses {table}.{node.subname}")


@rule(
    "PMG032",
    "rename-table",
    Severity.ERROR,
    "RENAME TABLE",
    "The old name is gone the moment this commits, while the old code is still running.",
    "Rename, and keep a view under the old name until the old code is gone.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("ALTER TABLE demo RENAME TO demo_archive"),
)
def rename_table(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.RenameStmt) and node.renameType == ObjectType.OBJECT_TABLE:
        table = name_of(node.relation)
        if not state.is_new(table):
            yield Hit(f"running code still uses {table}")


@rule(
    "PMG033",
    "drop-table",
    Severity.ERROR,
    "DROP TABLE",
    "Destroys the data and breaks anything still reading the table, and only a backup restore "
    "brings it back.",
    "Stop using the table, deploy, keep it for a while, then drop it with a recorded reason.",
    Claim(lock=ACCESS_EXCLUSIVE),
    Demo("DROP TABLE demo CASCADE"),
)
def drop_table(stmt, state: State):
    node = stmt.node
    if isinstance(node, ast.DropStmt) and node.removeType == ObjectType.OBJECT_TABLE:
        tables = state.existing({dotted(names) for names in node.objects})
        if tables:
            yield Hit(f"drops {', '.join(sorted(tables))}")
