<!-- topics: postgresql, database-migrations, schema-migration, zero-downtime, linter, sql, ddl, devops, github-actions, python -->
# pg-migration-guard

PostgreSQL migration linter whose lock and rewrite claims are checked against real servers.

[![CI](https://github.com/AkibUllah/pg-migration-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/AkibUllah/pg-migration-guard/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14%20%7C%2016%20%7C%2017%20%7C%2018-336791)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Try it live

| | |
|---|---|
| Live demo | https://akibullah.github.io/pg-migration-guard/ : what each DDL statement locks, measured on four PostgreSQL versions |
| Run in your browser | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/AkibUllah/pg-migration-guard?quickstart=1) : starts a shadow PostgreSQL. Then run `make example`, or the probe command from Quick Start. |
| Local | `docker compose up -d shadow` (see Quick Start) |

## Why this exists

The migrations that cause outages rarely look dangerous. Adding a `NOT NULL`, a foreign key or a unique index reads like routine work in review, then takes a lock that stops a payment or ledger table for minutes. Linters catch these, but a linter is a list of claims about PostgreSQL, and the rules go stale as the server changes. So here every claim comes with a demo statement, and CI runs it on real servers and fails if the server disagrees.

## Features

- 31 rules for lock strength and duration, rolling-deploy compatibility, data changes inside DDL transactions, and column types.
- Parsed by the real PostgreSQL grammar (libpg_query), so dollar quoting and new syntax work, and a syntax error is reported rather than skipped.
- Understands the whole migration: a table created above is exempt, `SET NOT NULL` after a validated `CHECK` is fine, `VALIDATE` in the same transaction as `NOT VALID` is not.
- Fixes written for the statement in front of it, using your own spelling and names.
- `pgmg probe` runs a migration on a shadow database and shows the locks held, rewrites, scans, and whether a real read and write got through, then rolls back.
- `pgmg evidence` checks every claim and every recommended fix on your server version.
- Output as text, JSON, GitHub annotations, SARIF for code scanning, or Markdown for a PR comment.
- `-- pgmg:ignore PMG001 reason` needs a reason, and a stale ignore is reported.

## Architecture

```mermaid
flowchart LR
    F[Migration files] --> P[Parser: libpg_query]
    P --> S[State: new tables, transaction, lock_timeout, proven NOT NULL]
    S --> R[31 rules]
    R --> O[Text, JSON, GitHub, SARIF, Markdown]

    R -. each claim and demo .-> E[pgmg evidence]
    M[pgmg probe] --> PG[(Shadow PostgreSQL)]
    E --> M
    PG -->|pg_locks, filenodes, scan counters, second connection| J[evidence/pg*.json]
    J --> T[Tests and CI compare]
    J --> W[Pages site]
```

- **A rule is a claim.** A lock rule declares the lock mode, whether the table is rewritten or scanned, and a demo statement. `pgmg evidence` runs the demo and compares each field.
- **Measured, not inferred.** Locks come from `pg_locks`, rewrites from a changed filenode, scans from `pg_stat_xact_user_tables`. Blocking is tested by a second connection attempting a read and a write under a 100 ms `lock_timeout`.
- **Relations are tracked by oid.** After a `DROP` or `RENAME` the name inside the transaction is gone or different, so the observer resolves each oid to the name it can see.
- **State is applied after the rules run**, so a rule sees the migration as it stood before the current statement. Schema facts carry across files, and session and transaction state end with each file.
- **The probe always rolls back, but its locks are real while it runs.** It refuses standbys, and any database whose name does not look disposable, unless you pass `--allow-any-database`.

## Tech Stack

| Area | Choice |
|---|---|
| Language | Python 3.10+ |
| Parser | pglast (libpg_query, the PostgreSQL 18 grammar) |
| Server access | psycopg 3 (only for `probe` and `evidence`) |
| Tests | pytest, ruff |
| Delivery | GitHub Action, Docker, GitHub Pages |

## Quick Start

### Docker

```bash
docker compose up -d shadow
docker compose run --rm pgmg check examples/migrations/risky
docker compose run --rm pgmg probe --setup examples/schema.sql \
  examples/migrations/risky/0007_shipment_tracking.sql
```

### Local

```bash
make install
.venv/bin/pgmg check examples/migrations/risky
```

To probe, start the shadow database with `docker compose up -d shadow` and copy `.env.example` values into your shell:

```bash
export PGMG_DSN=postgresql://postgres:pgmg@localhost:55432/pgmg_shadow
.venv/bin/pgmg probe --setup examples/schema.sql \
  examples/migrations/risky/0007_shipment_tracking.sql
```

## Usage

### Check

```text
$ pgmg check examples/migrations/risky --no-fixes
0007_shipment_tracking.sql:4: error PMG009 add-column-volatile-default
    default gen_random_uuid() on shipment.public_id rewrites the table if it is volatile
0007_shipment_tracking.sql:6: error PMG004 foreign-key-validates-now
    shipment_hub_fk checks every row of shipment while blocking writes
0007_shipment_tracking.sql:11: error PMG001 add-index-not-concurrent
    index build on shipment blocks writes until done
0007_shipment_tracking.sql:13: error PMG041 backfill-under-lock
    this data change runs while the transaction blocks hub, shipment
```

(Shortened. Exit code 1 on findings at or above `--fail-on`, 2 on a usage problem.)

### Probe

Real output from PostgreSQL 17, trimmed to the last statement. Everything taken earlier in the transaction is still held:

```text
$ pgmg probe --setup examples/schema.sql examples/migrations/risky/0007_shipment_tracking.sql
line 13: UPDATE shipment SET status = 'pending' WHERE status = 'booked'
  272.9 ms
  holds ShareRowExclusiveLock on hub
  holds AccessExclusiveLock on shipment
  holds AccessExclusiveLock on shipment_tracking_code_key
  scanned shipment
  reads blocked on shipment
  writes blocked on hub, shipment
rolled back: nothing was changed
```

Statements that cannot run in a transaction, such as `CREATE INDEX CONCURRENTLY`, are reported as skipped. The probe proves what a statement locks on your schema, and it does not tell you how long the lock lasts on production data, unless the shadow holds a copy of that data.

### Other commands

| Command | Purpose |
|---|---|
| `pgmg check PATH...` | Lint files or directories, or `-` for stdin |
| `pgmg check --changed-since origin/main` | Only SQL files changed on this branch |
| `pgmg rules` / `pgmg explain PMG006` | List the rules, or explain one |
| `pgmg evidence --dsn ... -o out.json` | Check every claim on a server |

### Migration tools

Everything works on plain SQL. If your tool wraps each file in a transaction, as Flyway, Alembic and Django do by default, pass `--assume-in-transaction`.

```bash
python manage.py sqlmigrate shipments 0007 | pgmg check -
alembic upgrade head --sql | pgmg check -
```

### GitHub Action

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
- uses: AkibUllah/pg-migration-guard@main
  with:
    path: migrations
    base-ref: origin/${{ github.base_ref }}
```

Findings appear as annotations on the changed lines, with a table in the job summary. Set `sarif-file` to also upload to code scanning. Use it as `@main` for now: tagged releases start at v0.1.0.

### Config

Copy [.pgmg.toml.example](.pgmg.toml.example) to `.pgmg.toml`. Unknown keys and rule codes stop the run, so a typo cannot quietly switch a rule off.

## Rules

Measured results for each one are on the [live page](https://akibullah.github.io/pg-migration-guard/#rules).

| Code | Rule | Severity |
|---|---|---|
| PMG001 | CREATE INDEX without CONCURRENTLY | error |
| PMG002 | DROP INDEX without CONCURRENTLY | warning |
| PMG003 | CONCURRENTLY inside a transaction | error |
| PMG004 | Foreign key added without NOT VALID | error |
| PMG005 | CHECK constraint added without NOT VALID | error |
| PMG006 | SET NOT NULL with no validated CHECK behind it | error |
| PMG007 | UNIQUE constraint without USING INDEX | error |
| PMG008 | PRIMARY KEY without USING INDEX | error |
| PMG009 | New column whose default is evaluated per row | error |
| PMG010 | New serial, identity or stored generated column | error |
| PMG011 | NOT NULL column with no default | error |
| PMG012 | ALTER COLUMN TYPE | error |
| PMG013 | VACUUM FULL or CLUSTER | error |
| PMG014 | TRUNCATE | error |
| PMG015 | REINDEX without CONCURRENTLY | error |
| PMG016 | REFRESH MATERIALIZED VIEW without CONCURRENTLY | error |
| PMG017 | DETACH PARTITION without CONCURRENTLY | warning |
| PMG019 | Blocking DDL with no lock_timeout | warning |
| PMG020 | VALIDATE in the same transaction as NOT VALID | error |
| PMG021 | Strong locks on several tables in one transaction | warning |
| PMG022 | ALTER TYPE ADD VALUE inside a transaction | warning |
| PMG030 | DROP COLUMN | error |
| PMG031 | RENAME COLUMN | error |
| PMG032 | RENAME TABLE | error |
| PMG033 | DROP TABLE | error |
| PMG040 | UPDATE or DELETE with no WHERE | warning |
| PMG041 | Data change in a transaction that holds a strong lock | error |
| PMG050 | Primary key narrower than bigint | warning |
| PMG051 | timestamp without time zone | warning |
| PMG052 | json column | warning |
| PMG053 | char(n) column | info |

## Project Structure

```text
migration_guard/
  source.py       parse files, keep line numbers and ignore comments
  state.py        what the migration has done so far
  rules/          locking, compatibility, data, types
  checker.py      run the rules in order across files
  probe.py        run on a real server, measure, roll back
  evidence.py     compare each claim with the measurement
  report.py       text, JSON, GitHub, SARIF, Markdown
  cli.py          pgmg
evidence/         committed measurements per PostgreSQL version
examples/         a risky migration, and the same change done safely
scripts/          Pages site, generated from the evidence
action.yml        GitHub Action
```

## Testing

```bash
make test       # no server needed
make test-pg    # also runs the real-server tests against the shadow database
```

The default run covers every rule firing and staying quiet, table exemptions, transaction and `lock_timeout` tracking, ignore comments, each output format, and the CLI. `tests/test_evidence_files.py` fails if a rule changes without new evidence. CI also runs the real-server tests on PostgreSQL 14, 16, 17 and 18, and compares each server's measurements with the committed files, so a release that changes behaviour turns the build red.

## Limitations

- The linter sees SQL, not your data or your database. It cannot tell a 40-row table from a 4-billion-row one, so use a `pgmg:ignore` with a reason for small tables.
- It only knows about constraints in the files it is given. `SET NOT NULL` that relies on a `CHECK` from an older migration needs an ignore.
- Whether a function in a `DEFAULT` is volatile is not known without the database, so PMG009 fires on any function it does not recognise as stable.
- [squawk](https://github.com/sbdchd/squawk) and [strong_migrations](https://github.com/ankane/strong_migrations) are mature, and worth a look. This project's difference is the probe and the measured evidence.

## Roadmap

- Read table sizes from a shadow database, so small tables can be downgraded automatically.
- Read constraints and function volatility from a shadow database, to remove the first two guesses above.
- Parse Django and Alembic migration files directly.
- Measure lock duration against a generated table of a chosen size.

## License

MIT. See [LICENSE](LICENSE).
