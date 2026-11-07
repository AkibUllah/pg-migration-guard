import pytest

from migration_guard.checker import Options, check_text

TABLE = "CREATE TABLE fresh (id bigint PRIMARY KEY, note text, parent_id bigint);\n"

FIRES = [
    ("PMG001", "CREATE INDEX i ON t (a)"),
    ("PMG001", "CREATE UNIQUE INDEX i ON t (a)"),
    ("PMG002", "DROP INDEX i"),
    ("PMG004", "ALTER TABLE t ADD CONSTRAINT f FOREIGN KEY (a) REFERENCES p (id)"),
    ("PMG004", "ALTER TABLE t ADD COLUMN a bigint REFERENCES p (id)"),
    ("PMG005", "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)"),
    ("PMG006", "ALTER TABLE t ALTER COLUMN a SET NOT NULL"),
    ("PMG006", "ALTER TABLE t ADD CONSTRAINT n NOT NULL a"),
    ("PMG007", "ALTER TABLE t ADD CONSTRAINT u UNIQUE (a)"),
    ("PMG007", "ALTER TABLE t ADD COLUMN a int UNIQUE"),
    ("PMG008", "ALTER TABLE t ADD PRIMARY KEY (a)"),
    ("PMG009", "ALTER TABLE t ADD COLUMN a uuid DEFAULT gen_random_uuid()"),
    ("PMG009", "ALTER TABLE t ADD COLUMN a double precision DEFAULT random() * 2"),
    ("PMG010", "ALTER TABLE t ADD COLUMN a bigserial"),
    ("PMG010", "ALTER TABLE t ADD COLUMN a bigint GENERATED ALWAYS AS IDENTITY"),
    ("PMG010", "ALTER TABLE t ADD COLUMN a int GENERATED ALWAYS AS (b + 1) STORED"),
    ("PMG011", "ALTER TABLE t ADD COLUMN a int NOT NULL"),
    ("PMG012", "ALTER TABLE t ALTER COLUMN a TYPE bigint"),
    ("PMG013", "VACUUM FULL t"),
    ("PMG013", "VACUUM (FULL, ANALYZE) t"),
    ("PMG013", "CLUSTER t USING i"),
    ("PMG014", "TRUNCATE t"),
    ("PMG015", "REINDEX TABLE t"),
    ("PMG015", "REINDEX INDEX i"),
    ("PMG016", "REFRESH MATERIALIZED VIEW m"),
    ("PMG017", "ALTER TABLE t DETACH PARTITION p"),
    ("PMG030", "ALTER TABLE t DROP COLUMN a"),
    ("PMG031", "ALTER TABLE t RENAME COLUMN a TO b"),
    ("PMG032", "ALTER TABLE t RENAME TO u"),
    ("PMG033", "DROP TABLE t"),
    ("PMG033", "DROP TABLE IF EXISTS app.t"),
    ("PMG040", "UPDATE t SET a = 1"),
    ("PMG040", "DELETE FROM t"),
    ("PMG050", "CREATE TABLE n (id serial PRIMARY KEY)"),
    ("PMG050", "CREATE TABLE n (id int, PRIMARY KEY (id))"),
    ("PMG051", "ALTER TABLE t ADD COLUMN a timestamp"),
    ("PMG051", "CREATE TABLE n (a timestamp without time zone)"),
    ("PMG052", "CREATE TABLE n (a json)"),
    ("PMG053", "CREATE TABLE n (a char(3))"),
]

QUIET = [
    "CREATE INDEX CONCURRENTLY i ON t (a)",
    "DROP INDEX CONCURRENTLY i",
    "ALTER TABLE t ADD CONSTRAINT f FOREIGN KEY (a) REFERENCES p (id) NOT VALID",
    "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0) NOT VALID",
    "ALTER TABLE t VALIDATE CONSTRAINT c",
    "ALTER TABLE t ADD CONSTRAINT u UNIQUE USING INDEX i",
    "ALTER TABLE t ADD CONSTRAINT k PRIMARY KEY USING INDEX i",
    "ALTER TABLE t ADD COLUMN a int",
    "ALTER TABLE t ADD COLUMN a int DEFAULT 7",
    "ALTER TABLE t ADD COLUMN a int NOT NULL DEFAULT 7",
    "ALTER TABLE t ADD COLUMN a timestamptz DEFAULT now()",
    "ALTER TABLE t ADD COLUMN a timestamptz DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE t ADD COLUMN a text DEFAULT 'x'::text",
    "ALTER TABLE t ADD COLUMN a int GENERATED ALWAYS AS (b + 1) VIRTUAL",
    "VACUUM t",
    "ANALYZE t",
    "REINDEX INDEX CONCURRENTLY i",
    "REINDEX (CONCURRENTLY) TABLE t",
    "REFRESH MATERIALIZED VIEW CONCURRENTLY m",
    "ALTER TABLE t DETACH PARTITION p CONCURRENTLY",
    "UPDATE t SET a = 1 WHERE id BETWEEN 1 AND 1000",
    "DELETE FROM t WHERE id = 1",
    "SELECT 1",
    "CREATE TABLE n (id bigint PRIMARY KEY, a timestamptz, b jsonb, c text)",
    "ALTER TABLE t ALTER COLUMN a DROP NOT NULL",
    "ALTER TABLE t ALTER COLUMN a SET DEFAULT 1",
    "DROP VIEW v",
]


@pytest.mark.parametrize(("code", "sql"), FIRES)
def test_rule_fires(codes, code, sql):
    assert code in codes(sql)


@pytest.mark.parametrize("sql", QUIET)
def test_safe_statement_is_quiet(codes, sql):
    assert codes(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX i ON fresh (note)",
        "ALTER TABLE fresh ADD CONSTRAINT f FOREIGN KEY (parent_id) REFERENCES p (id)",
        "ALTER TABLE fresh ADD CONSTRAINT c CHECK (id > 0)",
        "ALTER TABLE fresh ALTER COLUMN note SET NOT NULL",
        "ALTER TABLE fresh ADD CONSTRAINT u UNIQUE (note)",
        "ALTER TABLE fresh ADD COLUMN a bigserial",
        "ALTER TABLE fresh ADD COLUMN a int NOT NULL",
        "ALTER TABLE fresh ALTER COLUMN parent_id TYPE int",
        "ALTER TABLE fresh DROP COLUMN note",
        "ALTER TABLE fresh RENAME COLUMN note TO remark",
        "ALTER TABLE fresh RENAME TO fresher",
        "DROP TABLE fresh",
        "TRUNCATE fresh",
        "UPDATE fresh SET note = 'x'",
    ],
)
def test_tables_created_in_this_migration_are_exempt(codes, sql):
    assert codes(TABLE + sql) == []


def test_create_table_as_is_also_new(codes):
    assert codes("CREATE TABLE c AS SELECT 1 AS a;\nCREATE INDEX i ON c (a);") == []


def test_if_not_exists_might_be_an_old_table(codes):
    sql = "CREATE TABLE IF NOT EXISTS n (id bigint);\nCREATE INDEX i ON n (id);"
    assert codes(sql) == ["PMG001"]


def test_schema_is_part_of_the_table_name(codes):
    sql = "CREATE TABLE app.t (id bigint);\nCREATE INDEX i ON other.t (id);"
    assert codes(sql) == ["PMG001"]


def test_public_schema_matches_unqualified_name(codes):
    assert codes("CREATE TABLE public.t (id bigint);\nCREATE INDEX i ON t (id);") == []


def test_varchar_widening_is_downgraded_to_a_warning():
    found = [f for f in check_text("ALTER TABLE t ALTER COLUMN a TYPE text") if f.code == "PMG012"]
    assert found[0].severity.label == "warning"


def test_type_change_with_using_keeps_error_severity():
    sql = "ALTER TABLE t ALTER COLUMN a TYPE varchar USING a::varchar"
    found = [f for f in check_text(sql) if f.code == "PMG012"]
    assert found[0].severity.label == "error"


def test_fix_keeps_the_author_spelling_of_the_index():
    finding = check_text("create unique index i on t (a)")[0]
    assert finding.fix == "create unique INDEX CONCURRENTLY i on t (a)"


def test_several_problems_in_one_alter_are_all_reported(codes):
    sql = "ALTER TABLE t ADD COLUMN a bigserial, DROP COLUMN b, ALTER COLUMN c SET NOT NULL"
    assert codes(sql) == ["PMG006", "PMG010", "PMG030"]


def test_set_not_null_after_validated_check_does_not_scan(codes):
    sql = """
    ALTER TABLE t ADD CONSTRAINT a_nn CHECK (a IS NOT NULL) NOT VALID;
    ALTER TABLE t VALIDATE CONSTRAINT a_nn;
    ALTER TABLE t ALTER COLUMN a SET NOT NULL;
    """
    assert codes(sql) == []


def test_set_not_null_on_another_column_still_scans(codes):
    sql = """
    ALTER TABLE t ADD CONSTRAINT a_nn CHECK (a IS NOT NULL) NOT VALID;
    ALTER TABLE t VALIDATE CONSTRAINT a_nn;
    ALTER TABLE t ALTER COLUMN b SET NOT NULL;
    """
    assert codes(sql) == ["PMG006"]


def test_unvalidated_check_does_not_help(codes):
    sql = """
    ALTER TABLE t ADD CONSTRAINT a_nn CHECK (a IS NOT NULL) NOT VALID;
    ALTER TABLE t ALTER COLUMN a SET NOT NULL;
    """
    assert codes(sql) == ["PMG006"]


def test_check_with_and_proves_each_column(codes):
    sql = """
    ALTER TABLE t ADD CONSTRAINT nn CHECK (a IS NOT NULL AND b IS NOT NULL) NOT VALID;
    ALTER TABLE t VALIDATE CONSTRAINT nn;
    ALTER TABLE t ALTER COLUMN b SET NOT NULL;
    """
    assert codes(sql) == []


def test_or_in_a_check_proves_nothing(codes):
    sql = """
    ALTER TABLE t ADD CONSTRAINT nn CHECK (a IS NOT NULL OR b IS NOT NULL) NOT VALID;
    ALTER TABLE t VALIDATE CONSTRAINT nn;
    ALTER TABLE t ALTER COLUMN b SET NOT NULL;
    """
    assert codes(sql) == ["PMG006"]


def test_not_null_not_valid_is_the_postgres_18_way(codes):
    assert codes("ALTER TABLE t ADD CONSTRAINT n NOT NULL a NOT VALID") == []


def test_detach_rule_is_silent_before_concurrent_detach_existed(codes):
    assert codes("ALTER TABLE t DETACH PARTITION p", target_version=13) == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX CONCURRENTLY i ON t (a)",
        "DROP INDEX CONCURRENTLY i",
        "REINDEX INDEX CONCURRENTLY i",
        "VACUUM t",
        "ALTER TABLE t DETACH PARTITION p CONCURRENTLY",
    ],
)
def test_concurrent_statements_fail_in_a_transaction(codes, sql):
    assert "PMG003" in codes(f"BEGIN;\n{sql};\nCOMMIT;")
    assert "PMG003" not in codes(sql)


def test_assumed_transaction_counts(codes):
    sql = "CREATE INDEX CONCURRENTLY i ON t (a)"
    assert "PMG003" in codes(sql, assume_in_transaction=True)


def test_commit_ends_the_transaction(codes):
    assert codes("BEGIN;\nSELECT 1;\nCOMMIT;\nCREATE INDEX CONCURRENTLY i ON t (a);") == []


def test_refresh_concurrently_is_allowed_in_a_transaction(codes):
    assert codes("BEGIN;\nREFRESH MATERIALIZED VIEW CONCURRENTLY m;\nCOMMIT;") == []


def test_validate_in_same_transaction(codes):
    sql = """
    BEGIN;
    ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0) NOT VALID;
    ALTER TABLE t VALIDATE CONSTRAINT c;
    COMMIT;
    """
    assert codes(sql) == ["PMG020"]


def test_validate_after_commit_is_the_right_pattern(codes):
    sql = """
    BEGIN;
    ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0) NOT VALID;
    COMMIT;
    ALTER TABLE t VALIDATE CONSTRAINT c;
    """
    assert codes(sql) == []


def test_two_tables_locked_in_one_transaction(codes):
    sql = "BEGIN;\nALTER TABLE a ADD COLUMN x int;\nALTER TABLE b ADD COLUMN x int;\nCOMMIT;"
    assert codes(sql) == ["PMG021"]


def test_one_table_twice_is_not_two_tables(codes):
    sql = "BEGIN;\nALTER TABLE a ADD COLUMN x int;\nALTER TABLE a ADD COLUMN y int;\nCOMMIT;"
    assert codes(sql) == []


def test_foreign_key_locks_its_parent_table_too(codes):
    sql = "BEGIN;\nALTER TABLE a ADD CONSTRAINT f FOREIGN KEY (x) REFERENCES b (id) NOT VALID;\n"
    assert codes(sql + "COMMIT;") == ["PMG021"]


def test_new_table_with_a_foreign_key_still_locks_the_parent():
    found = check_text("CREATE TABLE n (id bigint PRIMARY KEY, p bigint REFERENCES parent (id));")
    assert [f.code for f in found] == ["PMG019"]
    assert "parent" in found[0].message


def test_two_tables_outside_a_transaction_are_fine(codes):
    assert codes("ALTER TABLE a ADD COLUMN x int;\nALTER TABLE b ADD COLUMN x int;") == []


def test_multi_table_warning_appears_once_per_transaction():
    sql = (
        "BEGIN;\n" + "".join(f"ALTER TABLE t{i} ADD COLUMN x int;\n" for i in range(4)) + "COMMIT;"
    )
    assert [f.code for f in check_text(sql)].count("PMG021") == 1


def test_backfill_after_ddl_in_one_transaction(codes):
    sql = "BEGIN;\nALTER TABLE t ADD COLUMN x int;\nUPDATE t SET x = 1 WHERE id < 10;\nCOMMIT;"
    assert codes(sql) == ["PMG041"]


def test_backfill_after_commit_is_fine(codes):
    sql = "BEGIN;\nALTER TABLE t ADD COLUMN x int;\nCOMMIT;\nUPDATE t SET x = 1 WHERE id < 10;"
    assert codes(sql) == []


def test_enum_value_in_transaction(codes):
    assert codes("BEGIN;\nALTER TYPE s ADD VALUE 'x';\nCOMMIT;") == ["PMG022"]
    assert codes("ALTER TYPE s ADD VALUE 'x';") == []


def test_renaming_an_enum_value_is_not_adding_one(codes):
    assert codes("BEGIN;\nALTER TYPE s RENAME VALUE 'a' TO 'b';\nCOMMIT;") == []


class TestLockTimeout:
    @staticmethod
    def fired(sql: str) -> bool:
        return "PMG019" in [f.code for f in check_text(sql)]

    def test_blocking_ddl_without_timeout(self):
        assert self.fired("ALTER TABLE t ADD COLUMN a int")

    def test_timeout_before_ddl(self):
        assert not self.fired("SET lock_timeout = '2s';\nALTER TABLE t ADD COLUMN a int")

    def test_integer_timeout(self):
        assert not self.fired("SET lock_timeout = 2000;\nALTER TABLE t ADD COLUMN a int")

    def test_zero_means_no_timeout(self):
        assert self.fired("SET lock_timeout = 0;\nALTER TABLE t ADD COLUMN a int")
        assert self.fired("SET lock_timeout = '0';\nALTER TABLE t ADD COLUMN a int")

    def test_timeout_after_ddl_is_too_late(self):
        assert self.fired("ALTER TABLE t ADD COLUMN a int;\nSET lock_timeout = '2s';")

    def test_reset_removes_it(self):
        sql = "SET lock_timeout = '2s';\nRESET lock_timeout;\nALTER TABLE t ADD COLUMN a int"
        assert self.fired(sql)

    def test_set_local_ends_with_the_transaction(self):
        sql = """
        BEGIN; SET LOCAL lock_timeout = '2s'; ALTER TABLE t ADD COLUMN a int; COMMIT;
        ALTER TABLE u ADD COLUMN a int;
        """
        assert self.fired(sql)

    def test_set_local_covers_its_own_transaction(self):
        sql = "BEGIN; SET LOCAL lock_timeout = '2s'; ALTER TABLE t ADD COLUMN a int; COMMIT;"
        assert not self.fired(sql)

    def test_only_once_per_file(self):
        sql = "ALTER TABLE t ADD COLUMN a int;\nALTER TABLE u ADD COLUMN a int;"
        assert [f.code for f in check_text(sql)].count("PMG019") == 1

    def test_new_tables_need_no_timeout(self):
        assert not self.fired(TABLE + "ALTER TABLE fresh ADD COLUMN a int")

    def test_validate_takes_a_weak_lock(self):
        assert not self.fired("ALTER TABLE t VALIDATE CONSTRAINT c")


class TestIgnoreDirectives:
    def test_ignore_with_reason_silences_that_rule(self, codes):
        sql = "-- pgmg:ignore PMG001 lookup table, 40 rows\nCREATE INDEX i ON t (a);"
        assert codes(sql) == []

    def test_ignore_is_only_for_the_next_statement(self, codes):
        sql = "-- pgmg:ignore PMG001 small\nCREATE INDEX i ON t (a);\nCREATE INDEX j ON t (b);"
        assert codes(sql) == ["PMG001"]

    def test_ignore_without_reason_does_not_silence(self, codes):
        assert codes("-- pgmg:ignore PMG001\nCREATE INDEX i ON t (a);") == ["PMG000", "PMG001"]

    def test_ignore_with_no_code_is_reported(self, codes):
        assert codes("-- pgmg:ignore because\nSELECT 1;") == ["PMG000"]

    def test_unknown_code_is_reported(self, codes):
        assert codes("-- pgmg:ignore PMG777 oops\nSELECT 1;") == ["PMG000"]

    def test_stale_ignore_is_reported(self, codes):
        assert codes("-- pgmg:ignore PMG001 old\nCREATE INDEX CONCURRENTLY i ON t (a);") == [
            "PMG000"
        ]

    def test_ignored_rule_does_not_hide_the_others(self, codes):
        sql = "-- pgmg:ignore PMG030 column unused since v12\n"
        sql += "ALTER TABLE t DROP COLUMN a, ALTER COLUMN b SET NOT NULL;"
        assert codes(sql) == ["PMG006"]


def test_tables_created_in_an_earlier_file_are_new_in_the_next():
    from migration_guard.checker import check_sources

    found = check_sources(
        [
            ("1.sql", "CREATE TABLE n (id bigint PRIMARY KEY);"),
            ("2.sql", "CREATE INDEX i ON n (id);"),
        ],
        Options(),
    )
    assert found == []


def test_check_validated_in_a_later_file_still_proves_not_null():
    from migration_guard.checker import check_sources

    found = check_sources(
        [
            ("1.sql", "ALTER TABLE t ADD CONSTRAINT nn CHECK (a IS NOT NULL) NOT VALID;"),
            ("2.sql", "ALTER TABLE t VALIDATE CONSTRAINT nn;"),
            ("3.sql", "ALTER TABLE t ALTER COLUMN a SET NOT NULL;"),
        ],
        Options(),
    )
    assert "PMG006" not in [f.code for f in found]


def test_session_timeout_does_not_carry_into_the_next_file():
    from migration_guard.checker import check_sources

    found = check_sources(
        [("1.sql", "SET lock_timeout = '2s';"), ("2.sql", "ALTER TABLE t ADD COLUMN a int;")],
        Options(),
    )
    assert [f.path for f in found] == ["2.sql"]


def test_syntax_error_is_a_finding_and_later_files_still_run():
    from migration_guard.checker import check_sources

    found = check_sources(
        [("1.sql", "ALTER TABEL t;"), ("2.sql", "CREATE INDEX i ON t (a);")], Options()
    )
    assert [(f.code, f.path) for f in found][0] == ("PMG999", "1.sql")
    assert ("PMG001", "2.sql") in [(f.code, f.path) for f in found]


def test_disabled_rule_does_not_run(codes):
    assert codes("TRUNCATE t", disabled={"PMG014"}) == []


def test_severity_override():
    from migration_guard.model import Severity

    found = check_text("TRUNCATE t", Options(severity={"PMG014": Severity.INFO}))
    assert next(f for f in found if f.code == "PMG014").severity == Severity.INFO
