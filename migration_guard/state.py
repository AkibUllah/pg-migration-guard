"""What the migration has done so far, and helpers for reading DDL out of the AST.

Rules need history: an index on a table created three lines up is harmless, and SET NOT NULL
after a validated CHECK does not scan. State is updated after the rules see each statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pglast import ast
from pglast.enums import (
    AlterTableType,
    ConstrType,
    NullTestType,
    ObjectType,
    TransactionStmtKind,
    VariableSetKind,
)

from migration_guard.source import Statement, walk

NOT_BLOCKING = {
    AlterTableType.AT_ValidateConstraint,
    AlterTableType.AT_SetStatistics,
    AlterTableType.AT_AttachPartition,
}
ZERO_TIMEOUTS = {"0", "0ms", "0s", "0min"}


def name_of(relation: ast.RangeVar) -> str:
    if relation.schemaname and relation.schemaname != "public":
        return f"{relation.schemaname}.{relation.relname}"
    return relation.relname


def dotted(names: tuple) -> str:
    parts = [n.sval for n in names]
    if len(parts) == 2 and parts[0] == "public":
        parts = parts[1:]
    return ".".join(parts)


def commands(stmt: Statement) -> list[ast.AlterTableCmd]:
    """The commands of an ALTER TABLE, or nothing for any other statement."""
    node = stmt.node
    if isinstance(node, ast.AlterTableStmt) and node.objtype == ObjectType.OBJECT_TABLE:
        return list(node.cmds)
    return []


def alter_table(stmt: Statement) -> str | None:
    return name_of(stmt.node.relation) if commands(stmt) else None


def constraint_columns(constraint: ast.Constraint) -> set[str]:
    """Columns a CHECK proves NOT NULL: every `col IS NOT NULL` joined by AND at the top."""
    return _not_null_columns(constraint.raw_expr)


def _not_null_columns(expr: object) -> set[str]:
    if isinstance(expr, ast.NullTest) and expr.nulltesttype == NullTestType.IS_NOT_NULL:
        if isinstance(expr.arg, ast.ColumnRef):
            return {expr.arg.fields[-1].sval}
        return set()
    if isinstance(expr, ast.BoolExpr) and expr.boolop.name == "AND_EXPR":
        found: set[str] = set()
        for arg in expr.args:
            found |= _not_null_columns(arg)
        return found
    return set()


def blocking_tables(stmt: Statement) -> set[str]:
    """Tables this statement locks strongly enough to stop writers, and queue everyone behind."""
    node = stmt.node
    cmds = commands(stmt)
    if cmds:
        if all(c.subtype in NOT_BLOCKING for c in cmds):
            return set()
        return {name_of(node.relation)}
    if isinstance(node, ast.DropStmt) and not node.concurrent:
        if node.removeType in (ObjectType.OBJECT_TABLE, ObjectType.OBJECT_INDEX):
            return {dotted(names) for names in node.objects}
    if isinstance(node, ast.TruncateStmt):
        return {name_of(r) for r in node.relations}
    if isinstance(node, ast.RenameStmt) and node.relation is not None:
        return {name_of(node.relation)}
    if isinstance(node, ast.IndexStmt) and not node.concurrent:
        return {name_of(node.relation)}
    return set()


def setting_value(node: ast.VariableSetStmt) -> str | None:
    if node.kind != VariableSetKind.VAR_SET_VALUE or not node.args:
        return None
    value = node.args[0].val
    return str(getattr(value, "sval", None) or getattr(value, "ival", None) or 0)


@dataclass
class State:
    target_version: int = 16
    assume_in_transaction: bool = False
    new_tables: set[str] = field(default_factory=set)
    in_transaction: bool = False
    lock_timeout: bool = False
    lock_timeout_local: bool = False
    warned_lock_timeout: bool = False
    tx_blocking: set[str] = field(default_factory=set)
    tx_multi_reported: bool = False
    tx_not_valid: set[tuple[str, str]] = field(default_factory=set)
    not_null_checks: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    proven_not_null: set[tuple[str, str]] = field(default_factory=set)

    def start_file(self) -> None:
        """Session and transaction state end with the file. Facts about the schema carry over."""
        self.in_transaction = self.assume_in_transaction
        self.lock_timeout = self.lock_timeout_local = self.warned_lock_timeout = False
        self._end_transaction()

    def is_new(self, table: str) -> bool:
        return table in self.new_tables

    def existing(self, tables: set[str]) -> set[str]:
        return {t for t in tables if t not in self.new_tables}

    def _end_transaction(self) -> None:
        self.tx_blocking.clear()
        self.tx_not_valid.clear()
        self.tx_multi_reported = False
        if self.lock_timeout_local:
            self.lock_timeout = self.lock_timeout_local = False

    def apply(self, stmt: Statement) -> None:
        node = stmt.node
        if isinstance(node, ast.TransactionStmt):
            if node.kind in (
                TransactionStmtKind.TRANS_STMT_BEGIN,
                TransactionStmtKind.TRANS_STMT_START,
            ):
                self.in_transaction = True
            elif node.kind in (
                TransactionStmtKind.TRANS_STMT_COMMIT,
                TransactionStmtKind.TRANS_STMT_ROLLBACK,
            ):
                self.in_transaction = False
                self._end_transaction()
            return
        if isinstance(node, ast.VariableSetStmt) and node.name == "lock_timeout":
            value = setting_value(node)
            self.lock_timeout = value is not None and value not in ZERO_TIMEOUTS
            self.lock_timeout_local = self.lock_timeout and bool(node.is_local)
            return
        if isinstance(node, ast.CreateStmt) and not node.if_not_exists:
            self.new_tables.add(name_of(node.relation))
        if isinstance(node, ast.CreateTableAsStmt) and not node.if_not_exists:
            self.new_tables.add(name_of(node.into.rel))
        if self.in_transaction:
            self.tx_blocking |= self.existing(blocking_tables(stmt))
        table = alter_table(stmt)
        if table is None:
            return
        for cmd in commands(stmt):
            if cmd.subtype == AlterTableType.AT_AddConstraint:
                self._add_constraint(table, cmd.def_)
            elif cmd.subtype == AlterTableType.AT_ValidateConstraint:
                for column in self.not_null_checks.get((table, cmd.name), ()):
                    self.proven_not_null.add((table, column))

    def _add_constraint(self, table: str, constraint: ast.Constraint) -> None:
        if constraint.skip_validation:
            self.tx_not_valid.add((table, constraint.conname or ""))
        if constraint.contype == ConstrType.CONSTR_CHECK:
            columns = constraint_columns(constraint)
            if constraint.skip_validation:
                self.not_null_checks[(table, constraint.conname or "")] = columns
            else:
                self.proven_not_null |= {(table, column) for column in columns}


def function_calls(expr: object) -> set[str]:
    return {n.funcname[-1].sval.lower() for n in walk(expr) if isinstance(n, ast.FuncCall)}
