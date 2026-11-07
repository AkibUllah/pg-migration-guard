"""Turn migration text into parsed statements with line numbers and ignore directives.

The real PostgreSQL grammar (libpg_query, through pglast) does the parsing, so dollar quoting,
nested comments and new syntax are not this project's problem. pglast.split gives character
offsets, which stay correct in files with Bangla or other non-ASCII comments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pglast import ast, parse_sql, split
from pglast.parser import ParseError

DIRECTIVE = re.compile(
    r"--\s*pgmg:ignore\b[ \t]*(?P<codes>(?:PMG\d{3}(?:[ \t]*,[ \t]*)?)*)[ \t]*(?P<reason>[^\n]*)"
)


@dataclass(frozen=True)
class Directive:
    """One `-- pgmg:ignore PMG001 why` comment."""

    codes: tuple[str, ...]
    reason: str
    line: int


@dataclass(frozen=True)
class Statement:
    sql: str
    line: int
    node: ast.Node
    directives: tuple[Directive, ...] = ()

    @property
    def kind(self) -> str:
        return type(self.node).__name__


@dataclass(frozen=True)
class SourceError(Exception):
    """The file is not valid PostgreSQL. Nothing in it can be judged."""

    message: str
    line: int


@dataclass
class Migration:
    path: str
    text: str
    statements: list[Statement] = field(default_factory=list)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _directives(text: str, start: int, end: int) -> list[Directive]:
    found = []
    for match in DIRECTIVE.finditer(text, start, end):
        codes = tuple(c.strip() for c in match["codes"].split(",") if c.strip())
        reason = match["reason"].strip().removeprefix("reason:").strip().lstrip("-: ").strip()
        found.append(Directive(codes, reason, line_of(text, match.start())))
    return found


def parse_migration(text: str, path: str = "<stdin>") -> Migration:
    try:
        slices = list(split(text, with_parser=True, only_slices=True))
    except ParseError as exc:
        location = exc.args[1] if len(exc.args) > 1 else 0
        raise SourceError(str(exc.args[0]), line_of(text, location)) from exc

    migration = Migration(path, text)
    previous_end = 0
    pending: list[list[Directive]] = []
    for index, piece in enumerate(slices):
        gap_end = piece.start
        # A comment on the same line as the previous statement belongs to that statement.
        newline = text.find("\n", previous_end, gap_end)
        same_line_end = newline if newline != -1 else gap_end
        if index:
            pending[-1].extend(_directives(text, previous_end, same_line_end))
        own = _directives(text, same_line_end if index else 0, gap_end)
        pending.append(own)
        previous_end = piece.stop
    if slices:
        pending[-1].extend(_directives(text, previous_end, len(text)))

    for piece, directives in zip(slices, pending, strict=True):
        sql = text[piece]
        try:
            node = parse_sql(sql)[0].stmt
        except ParseError as exc:  # pragma: no cover - split already parsed this text
            raise SourceError(str(exc.args[0]), line_of(text, piece.start)) from exc
        migration.statements.append(
            Statement(sql, line_of(text, piece.start), node, tuple(directives))
        )
    return migration


def walk(node: object):
    """Yield every AST node under `node`, itself included."""
    if isinstance(node, ast.Node):
        yield node
        for name in node.__slots__:
            yield from walk(getattr(node, name, None))
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from walk(item)
