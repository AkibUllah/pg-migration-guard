import pytest
from pglast import ast

from migration_guard.source import SourceError, parse_migration, walk


def test_statements_carry_their_own_line_number():
    migration = parse_migration("SELECT 1;\n\n\nSELECT 2;\n")
    assert [s.line for s in migration.statements] == [1, 4]


def test_dollar_quoted_bodies_are_one_statement():
    text = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN; SELECT 1; END $$ LANGUAGE sql;\nSELECT 2;"
    assert [s.kind for s in parse_migration(text).statements] == [
        "CreateFunctionStmt",
        "SelectStmt",
    ]


def test_non_ascii_comments_do_not_shift_offsets():
    text = "-- গ্রাহকের ঠিকানা কলাম\nALTER TABLE customer ADD COLUMN address text;\nSELECT 1;\n"
    migration = parse_migration(text)
    assert migration.statements[0].sql == "ALTER TABLE customer ADD COLUMN address text"
    assert migration.statements[0].line == 2
    assert migration.statements[1].line == 3


def test_syntax_error_reports_the_line():
    with pytest.raises(SourceError) as exc:
        parse_migration("SELECT 1;\nALTER TABEL x;\n")
    assert exc.value.line == 2
    assert "TABEL" in exc.value.message


def test_empty_file_has_no_statements():
    assert parse_migration("-- nothing yet\n").statements == []


def test_directive_above_applies_to_next_statement():
    text = "SELECT 1;\n-- pgmg:ignore PMG001 table has 40 rows\nSELECT 2;\n"
    first, second = parse_migration(text).statements
    assert first.directives == ()
    assert second.directives[0].codes == ("PMG001",)
    assert second.directives[0].reason == "table has 40 rows"
    assert second.directives[0].line == 2


def test_trailing_directive_applies_to_the_statement_on_that_line():
    text = "SELECT 1; -- pgmg:ignore PMG002 off-peak window\nSELECT 2;\n"
    first, second = parse_migration(text).statements
    assert first.directives[0].codes == ("PMG002",)
    assert second.directives == ()


def test_trailing_directive_on_last_statement():
    statement = parse_migration("SELECT 1; -- pgmg:ignore PMG002 done by hand").statements[0]
    assert statement.directives[0].reason == "done by hand"


def test_directive_lists_codes_and_strips_reason_prefix():
    text = "-- pgmg:ignore PMG001, PMG019 reason: reviewed with on-call\nSELECT 1;"
    directive = parse_migration(text).statements[0].directives[0]
    assert directive.codes == ("PMG001", "PMG019")
    assert directive.reason == "reviewed with on-call"


def test_directive_text_inside_a_string_is_not_a_directive_for_the_next_statement():
    text = "SELECT '-- pgmg:ignore PMG001 nope';\nSELECT 2;"
    assert all(not s.directives for s in parse_migration(text).statements)


def test_walk_reaches_nested_nodes():
    statement = parse_migration("ALTER TABLE t ADD COLUMN c int DEFAULT random();").statements[0]
    names = [n.funcname[-1].sval for n in walk(statement.node) if isinstance(n, ast.FuncCall)]
    assert names == ["random"]
