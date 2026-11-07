"""Run the rules over migrations, in order, with state carried between them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import migration_guard.rules  # noqa: F401 - registers the rules
from migration_guard.model import RULES, Finding, Severity
from migration_guard.source import Migration, SourceError, Statement, parse_migration
from migration_guard.state import State

DIRECTIVE_RULE = ("PMG000", "bad-ignore-directive")
SYNTAX_RULE = ("PMG999", "syntax-error")


@dataclass
class Options:
    target_version: int = 16
    assume_in_transaction: bool = False
    disabled: set[str] = field(default_factory=set)
    severity: dict[str, Severity] = field(default_factory=dict)


def _directive_findings(stmt: Statement, path: str, used: set[str]) -> Iterable[Finding]:
    def finding(line: int, message: str) -> Finding:
        code, name = DIRECTIVE_RULE
        return Finding(code, name, Severity.WARNING, message, path, line, stmt.sql)

    for directive in stmt.directives:
        if not directive.codes:
            yield finding(directive.line, "pgmg:ignore names no rule, so it does nothing")
        if directive.codes and not directive.reason:
            yield finding(
                directive.line, "pgmg:ignore needs a reason, so it does nothing without one"
            )
        for code in directive.codes:
            if code not in RULES:
                yield finding(directive.line, f"pgmg:ignore names unknown rule {code}")
            elif directive.reason and code not in used:
                yield finding(directive.line, f"{code} did not fire here, so this ignore is stale")


def check_statement(stmt: Statement, state: State, path: str, options: Options) -> list[Finding]:
    ignored = {c for d in stmt.directives if d.reason for c in d.codes}
    findings: list[Finding] = []
    used: set[str] = set()
    for rule in RULES.values():
        if rule.code in options.disabled:
            continue
        for hit in rule.check(stmt, state):
            used.add(rule.code)
            if rule.code in ignored:
                continue
            severity = options.severity.get(rule.code) or hit.severity or rule.severity
            findings.append(
                Finding(
                    rule.code,
                    rule.name,
                    severity,
                    hit.message,
                    path,
                    stmt.line,
                    stmt.sql,
                    hit.fix or rule.fix,
                )
            )
    findings.extend(_directive_findings(stmt, path, used))
    state.apply(stmt)
    return findings


def check_migration(migration: Migration, state: State, options: Options) -> list[Finding]:
    state.start_file()
    findings: list[Finding] = []
    for stmt in migration.statements:
        findings.extend(check_statement(stmt, state, migration.path, options))
    return findings


def check_sources(sources: Iterable[tuple[str, str]], options: Options) -> list[Finding]:
    """Check (path, text) pairs in order. Tables created in one file are new in the next."""
    state = State(options.target_version, options.assume_in_transaction)
    findings: list[Finding] = []
    for path, text in sources:
        try:
            migration = parse_migration(text, path)
        except SourceError as exc:
            code, name = SYNTAX_RULE
            findings.append(Finding(code, name, Severity.ERROR, exc.message, path, exc.line))
            continue
        findings.extend(check_migration(migration, state, options))
    return findings


def check_text(text: str, options: Options | None = None, path: str = "<stdin>") -> list[Finding]:
    return check_sources([(path, text)], options or Options())
