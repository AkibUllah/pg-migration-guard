import json

from migration_guard.checker import check_text
from migration_guard.model import Finding, Severity
from migration_guard.report import to_github, to_json, to_markdown, to_sarif, to_text, totals

SQL = "CREATE INDEX i ON t (a);\nALTER TABLE t ADD COLUMN b int;\n"


def findings():
    return check_text(SQL, path="db/001.sql")


def test_text_shows_location_rule_and_fix():
    out = to_text(findings())
    assert "db/001.sql:1: error PMG001 add-index-not-concurrent" in out
    assert "fix: CREATE INDEX CONCURRENTLY i ON t (a)" in out
    assert out.splitlines()[-1] == "1 error, 1 warning"


def test_text_can_leave_fixes_out():
    assert "fix:" not in to_text(findings(), fixes=False)


def test_multi_line_fix_is_aligned():
    out = to_text(check_text("ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)"))
    assert "         -- next migration:" in out


def test_totals_with_no_findings():
    assert totals([]) == "no findings"
    assert to_text([]) == "no findings"


def test_json_is_ordered_by_line():
    data = json.loads(to_json(findings()))
    assert [f["line"] for f in data["findings"]] == [1, 1]
    assert data["findings"][0]["severity"] == "error"


def test_github_annotation_shape():
    first = to_github(findings()).splitlines()[0]
    assert first.startswith(
        "::error file=db/001.sql,line=1,title=PMG001 add-index-not-concurrent::"
    )


def test_github_escapes_newlines_and_properties():
    finding = Finding("PMG001", "n", Severity.ERROR, "a%b\nc", "a,b:c.sql", 3)
    out = to_github([finding])
    assert "file=a%2Cb%3Ac.sql" in out
    assert out.endswith("::a%25b%0Ac")
    assert "\n" not in out


def test_sarif_is_valid_shape():
    sarif = json.loads(to_sarif(findings()))
    run = sarif["runs"][0]
    assert sarif["version"] == "2.1.0"
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} == {"PMG001", "PMG019"}
    result = run["results"][0]
    assert run["tool"]["driver"]["rules"][result["ruleIndex"]]["id"] == result["ruleId"]
    location = result["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "db/001.sql"
    assert location["region"]["startLine"] == 1


def test_sarif_covers_findings_with_no_registered_rule():
    finding = Finding("PMG999", "syntax-error", Severity.ERROR, "bad", "a.sql", 1)
    run = json.loads(to_sarif([finding]))["runs"][0]
    assert run["tool"]["driver"]["rules"][0]["name"] == "syntax-error"


def test_markdown_table_escapes_pipes():
    finding = Finding("PMG001", "n", Severity.ERROR, "a|b", "a.sql", 1)
    assert "a\\|b" in to_markdown([finding])


def test_markdown_with_no_findings():
    assert "no findings" in to_markdown([])
