# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-07

### Added

- `pgmg check`: 31 rules for lock strength and duration, rolling-deploy compatibility, data
  changes inside DDL transactions, and column types, parsed with the real PostgreSQL grammar.
- Text, JSON, GitHub annotation, SARIF and Markdown output.
- `.pgmg.toml`, `--changed-since`, and `-- pgmg:ignore` comments that require a reason.
- `pgmg probe`: run a migration on a shadow database in a rolled-back transaction, and report
  locks held, rewrites, scans, and whether a read and a write got through.
- `pgmg evidence`: check every rule claim and recommended pattern on a real server. Results for
  PostgreSQL 14, 16, 17 and 18 are committed, and CI compares them with live servers.
- GitHub Action, container image, and a Pages site generated from the evidence.
