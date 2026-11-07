from pathlib import Path

from migration_guard.checker import Options, check_sources

EXAMPLES = Path(__file__).parent.parent / "examples" / "migrations"


def findings(folder):
    files = sorted((EXAMPLES / folder).glob("*.sql"))
    return check_sources([(f.name, f.read_text(encoding="utf-8")) for f in files], Options())


def test_the_risky_migration_trips_the_rules_the_readme_shows():
    assert {f.code for f in findings("risky")} >= {
        "PMG001",
        "PMG004",
        "PMG006",
        "PMG009",
        "PMG019",
        "PMG041",
    }


def test_the_safe_migrations_pass():
    assert [f for f in findings("safe") if f.severity.label == "error"] == []
