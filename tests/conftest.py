import pytest

from migration_guard.checker import Options, check_text


@pytest.fixture
def codes():
    """The rule codes that fire on a piece of SQL, without the lock_timeout reminder."""

    def run(sql: str, **options) -> list[str]:
        found = check_text(sql, Options(**options))
        return sorted({f.code for f in found if f.code != "PMG019"})

    return run
