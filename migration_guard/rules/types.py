"""Column types that are cheap to pick well now and expensive to change later."""

from __future__ import annotations

from collections.abc import Iterator

from pglast import ast
from pglast.enums import AlterTableType, ConstrType

from migration_guard.model import Hit, Severity, rule
from migration_guard.state import State, commands

NARROW_INTEGERS = {"int2", "int4", "serial", "serial2", "serial4", "smallserial"}


def new_columns(stmt) -> Iterator[ast.ColumnDef]:
    node = stmt.node
    if isinstance(node, ast.CreateStmt):
        yield from (e for e in node.tableElts or () if isinstance(e, ast.ColumnDef))
    for cmd in commands(stmt):
        if cmd.subtype == AlterTableType.AT_AddColumn:
            yield cmd.def_


def type_of(column: ast.ColumnDef) -> str:
    return column.typeName.names[-1].sval


def primary_key_columns(node: ast.CreateStmt) -> set[str]:
    keys: set[str] = set()
    for element in node.tableElts or ():
        if isinstance(element, ast.ColumnDef):
            if any(c.contype == ConstrType.CONSTR_PRIMARY for c in element.constraints or ()):
                keys.add(element.colname)
        elif isinstance(element, ast.Constraint) and element.contype == ConstrType.CONSTR_PRIMARY:
            keys |= {k.sval for k in element.keys or ()}
    return keys


@rule(
    "PMG050",
    "prefer-bigint-primary-key",
    Severity.WARNING,
    "Primary key narrower than bigint",
    "An int key runs out at 2.1 billion, and widening it later is a table rewrite on what is by "
    "then the largest table in the system.",
    "Use bigint GENERATED ALWAYS AS IDENTITY, or uuid.",
)
def bigint_key(stmt, state: State):
    node = stmt.node
    if not isinstance(node, ast.CreateStmt):
        return
    keys = primary_key_columns(node)
    for column in new_columns(stmt):
        if column.colname in keys and type_of(column) in NARROW_INTEGERS:
            yield Hit(f"primary key {column.colname} is {type_of(column)}")


@rule(
    "PMG051",
    "prefer-timestamptz",
    Severity.WARNING,
    "timestamp without time zone",
    "Stores a wall-clock reading with no zone. It goes wrong the first time a server, a client "
    "or a report runs in a different zone.",
    "Use timestamptz, which stores an instant.",
)
def timestamptz(stmt, state: State):
    for column in new_columns(stmt):
        if type_of(column) == "timestamp":
            yield Hit(f"{column.colname} is timestamp without time zone", "timestamptz")


@rule(
    "PMG052",
    "prefer-jsonb",
    Severity.WARNING,
    "json column",
    "json keeps the raw text and re-parses it on every access. It has no equality operator and "
    "no GIN indexing, so GROUP BY, DISTINCT and containment queries on it fail or crawl.",
    "Use jsonb, unless key order and exact whitespace have to survive.",
)
def jsonb(stmt, state: State):
    for column in new_columns(stmt):
        if type_of(column) == "json":
            yield Hit(f"{column.colname} is json", "jsonb")


@rule(
    "PMG053",
    "prefer-text-over-char",
    Severity.INFO,
    "char(n) column",
    "char(n) pads with spaces, which changes comparisons and sizes, and is no faster than text.",
    "Use text with a CHECK on length if the length matters.",
)
def no_char(stmt, state: State):
    for column in new_columns(stmt):
        if type_of(column) == "bpchar":
            yield Hit(f"{column.colname} is char(n)", "text")
