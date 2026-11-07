.PHONY: install lint test test-pg evidence site example

PY ?= .venv/bin/python
DSN ?= postgresql://postgres:pgmg@localhost:55432/pgmg_shadow

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

test:
	.venv/bin/pytest

test-pg:
	PGMG_TEST_DSN=$(DSN) .venv/bin/pytest

evidence:
	.venv/bin/pgmg evidence --dsn $(DSN) -o evidence/pg$$(docker compose exec -T shadow sh -c 'echo $$PG_MAJOR').json

site:
	$(PY) scripts/build_site.py _site

example:
	.venv/bin/pgmg check examples/migrations/risky
