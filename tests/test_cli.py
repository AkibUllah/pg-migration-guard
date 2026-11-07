import json
import subprocess

import pytest

from migration_guard.cli import main
from migration_guard.config import ConfigError, load_config
from migration_guard.files import SelectionError, changed_since
from migration_guard.model import RULES, Severity


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_exit_one_on_error(workdir, capsys):
    file = write(workdir / "1.sql", "CREATE INDEX i ON t (a);")
    assert main(["check", file]) == 1
    assert "PMG001" in capsys.readouterr().out


def test_exit_zero_when_clean(workdir, capsys):
    file = write(workdir / "1.sql", "CREATE INDEX CONCURRENTLY i ON t (a);")
    assert main(["check", file]) == 0
    assert "no findings" in capsys.readouterr().out


def test_warning_passes_by_default_and_fails_on_request(workdir):
    file = write(workdir / "1.sql", "ALTER TABLE t ADD COLUMN a int;")
    assert main(["check", file]) == 0
    assert main(["check", "--fail-on", "warning", file]) == 1


def test_fail_on_never_reports_and_passes(workdir, capsys):
    file = write(workdir / "1.sql", "TRUNCATE t;")
    assert main(["check", "--fail-on", "never", file]) == 0
    assert "PMG014" in capsys.readouterr().out


def test_directory_is_checked_in_name_order(workdir, capsys):
    write(workdir / "m" / "002.sql", "CREATE INDEX i ON n (id);")
    write(workdir / "m" / "001.sql", "CREATE TABLE n (id bigint PRIMARY KEY);")
    assert main(["check", str(workdir / "m")]) == 0


def test_stdin(workdir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("TRUNCATE t;"))
    assert main(["check", "-"]) == 1
    assert "<stdin>:1" in capsys.readouterr().out


def test_missing_file_is_a_usage_error(workdir, capsys):
    assert main(["check", "nope.sql"]) == 2
    assert "no such file" in capsys.readouterr().err


def test_no_paths_is_a_usage_error(workdir, capsys):
    assert main(["check"]) == 2


def test_non_utf8_file_is_a_usage_error(workdir):
    (workdir / "1.sql").write_bytes(b"SELECT '\xff';")
    assert main(["check", "1.sql"]) == 2


def test_syntax_error_fails_the_run(workdir, capsys):
    file = write(workdir / "1.sql", "ALTER TABEL t;")
    assert main(["check", file]) == 1
    assert "PMG999" in capsys.readouterr().out


def test_json_format(workdir, capsys):
    file = write(workdir / "1.sql", "TRUNCATE t;")
    main(["check", "--format", "json", file])
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "PMG014"


def test_github_format_prints_nothing_when_clean(workdir, capsys):
    file = write(workdir / "1.sql", "SELECT 1;")
    assert main(["check", "--format", "github", file]) == 0
    assert capsys.readouterr().out == ""


def test_disable_flag(workdir):
    file = write(workdir / "1.sql", "TRUNCATE t;")
    assert main(["check", "--disable", "PMG014", file]) == 0


def test_disable_unknown_rule_is_a_usage_error(workdir):
    file = write(workdir / "1.sql", "SELECT 1;")
    assert main(["check", "--disable", "PMG777", file]) == 2


def test_assume_in_transaction_flag(workdir):
    file = write(workdir / "1.sql", "CREATE INDEX CONCURRENTLY i ON t (a);")
    assert main(["check", file]) == 0
    assert main(["check", "--assume-in-transaction", file]) == 1


def test_target_version_out_of_range(workdir):
    file = write(workdir / "1.sql", "SELECT 1;")
    assert main(["check", "--target-version", "9", file]) == 2


def test_config_file_is_read_and_flags_win(workdir):
    file = write(workdir / "1.sql", "ALTER TABLE t ADD COLUMN a int;")
    write(workdir / ".pgmg.toml", 'fail_on = "warning"\n')
    assert main(["check", file]) == 1
    assert main(["check", "--fail-on", "error", file]) == 0


def test_config_values(workdir):
    write(
        workdir / ".pgmg.toml",
        'target_version = 15\nassume_in_transaction = true\ndisable = ["PMG051"]\n'
        'fail_on = "info"\n[severity]\nPMG019 = "error"\n',
    )
    options, fail_on = load_config()
    assert options.target_version == 15
    assert options.assume_in_transaction
    assert options.disabled == {"PMG051"}
    assert options.severity == {"PMG019": Severity.ERROR}
    assert fail_on == Severity.INFO


@pytest.mark.parametrize(
    "text",
    [
        "target_verison = 16",
        'target_version = "16"',
        "target_version = 9",
        "target_version = true",
        'assume_in_transaction = "yes"',
        'disable = "PMG001"',
        'disable = ["PMG777"]',
        'severity = "error"',
        '[severity]\nPMG777 = "error"',
        '[severity]\nPMG001 = "fatal"',
        'fail_on = "maybe"',
        "not toml at all",
    ],
)
def test_bad_config_is_rejected(workdir, text):
    write(workdir / ".pgmg.toml", text)
    with pytest.raises(ConfigError):
        load_config()


def test_named_config_must_exist(workdir):
    with pytest.raises(ConfigError):
        load_config("missing.toml")


def test_rules_lists_every_rule(capsys):
    assert main(["rules"]) == 0
    out = capsys.readouterr().out
    assert all(code in out for code in RULES)


def test_rules_json(capsys):
    main(["rules", "--format", "json"])
    rules = json.loads(capsys.readouterr().out)
    pmg005 = next(r for r in rules if r["code"] == "PMG005")
    assert pmg005["claim"]["lock"] == "AccessExclusiveLock"
    assert pmg005["claim"]["blocks_reads"] is True


def test_explain(capsys):
    assert main(["explain", "pmg001"]) == 0
    assert "CONCURRENTLY" in capsys.readouterr().out


def test_explain_unknown(capsys):
    assert main(["explain", "PMG777"]) == 2


class TestChangedSince:
    @pytest.fixture
    def repo(self, workdir):
        def git(*args):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                check=True,
                capture_output=True,
            )

        git("init", "-q", "-b", "main")
        write(workdir / "m" / "001.sql", "CREATE INDEX old ON t (a);")
        git("add", ".")
        git("commit", "-qm", "base")
        git("checkout", "-qb", "feature")
        write(workdir / "m" / "002.sql", "TRUNCATE t;")
        write(workdir / "notes.txt", "x")
        git("add", ".")
        git("commit", "-qm", "change")
        return workdir

    def test_only_changed_sql_files(self, repo):
        assert changed_since("main", []) == ["m/002.sql"]

    def test_limited_to_a_directory(self, repo):
        assert changed_since("main", ["m"]) == ["m/002.sql"]
        write(repo / "other" / "x.sql", "SELECT 1;")
        assert changed_since("main", ["other"]) == []

    def test_cli_checks_only_those_files(self, repo, capsys):
        assert main(["check", "--changed-since", "main"]) == 1
        out = capsys.readouterr().out
        assert "PMG014" in out
        assert "PMG001" not in out

    def test_unknown_ref(self, repo):
        with pytest.raises(SelectionError):
            changed_since("no-such-ref", [])

    def test_ref_cannot_be_an_option(self, repo):
        with pytest.raises(SelectionError):
            changed_since("--output=/tmp/x", [])
