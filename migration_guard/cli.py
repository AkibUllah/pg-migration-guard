"""pgmg: check migrations, and show what the rules are based on."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from migration_guard import __version__
from migration_guard.checker import Options, check_sources
from migration_guard.config import ConfigError, check_version, load_config
from migration_guard.evidence import collect, mismatches
from migration_guard.files import SelectionError, changed_since, expand, read_sources
from migration_guard.model import RULES, Severity
from migration_guard.probe import ProbeError, probe_sql
from migration_guard.probe import to_text as probe_text
from migration_guard.report import FORMATS, to_text
from migration_guard.source import SourceError

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pgmg", description=__doc__)
    parser.add_argument("--version", action="version", version=f"pgmg {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="lint migration files")
    check.add_argument("paths", nargs="*", help="files, directories, or - for stdin")
    check.add_argument("--format", choices=sorted(FORMATS), default="text")
    check.add_argument("--config", help="config file (default: .pgmg.toml if present)")
    check.add_argument("--target-version", type=int, help="PostgreSQL major version")
    check.add_argument(
        "--assume-in-transaction",
        action="store_true",
        help="the tool wraps each file in a transaction (Flyway, Alembic, Django)",
    )
    check.add_argument("--disable", action="append", default=[], metavar="CODE")
    check.add_argument("--fail-on", choices=["info", "warning", "error", "never"])
    check.add_argument("--changed-since", metavar="REF", help="only files changed since this ref")
    check.add_argument("--no-fixes", action="store_true", help="leave fixes out of text output")

    rules = commands.add_parser("rules", help="list the rules")
    rules.add_argument("--format", choices=["text", "json"], default="text")

    explain = commands.add_parser("explain", help="explain one rule")
    explain.add_argument("code")

    probe = commands.add_parser("probe", help="run a migration on a shadow database, roll back")
    probe.add_argument("file", help="migration file, or - for stdin")
    probe.add_argument("--dsn", default=os.environ.get("PGMG_DSN"), help="or set PGMG_DSN")
    probe.add_argument("--setup", help="schema to load first, in a scratch schema that is dropped")
    probe.add_argument("--allow-any-database", action="store_true")
    probe.add_argument("--format", choices=["text", "json"], default="text")

    evidence = commands.add_parser("evidence", help="check every rule's claim on a real server")
    evidence.add_argument("--dsn", default=os.environ.get("PGMG_DSN"), help="or set PGMG_DSN")
    evidence.add_argument("--allow-any-database", action="store_true")
    evidence.add_argument("-o", "--output", help="write the report here as JSON")
    return parser


def _options(args: argparse.Namespace) -> tuple[Options, Severity | None]:
    options, fail_on = load_config(args.config)
    if args.target_version is not None:
        options.target_version = check_version(args.target_version)
    if args.assume_in_transaction:
        options.assume_in_transaction = True
    unknown = sorted(set(args.disable) - set(RULES))
    if unknown:
        raise ConfigError(f"unknown rule {', '.join(unknown)}")
    options.disabled |= set(args.disable)
    if args.fail_on == "never":
        return options, None
    if args.fail_on:
        fail_on = Severity.parse(args.fail_on)
    return options, fail_on


def _files(args: argparse.Namespace) -> list[str]:
    if args.changed_since:
        return changed_since(args.changed_since, args.paths)
    if not args.paths:
        raise SelectionError("give a file, a directory, - for stdin, or --changed-since")
    return ["-"] if args.paths == ["-"] else expand(args.paths)


def cmd_check(args: argparse.Namespace) -> int:
    options, fail_on = _options(args)
    findings = check_sources(read_sources(_files(args)), options)
    if args.format == "text":
        print(to_text(findings, fixes=not args.no_fixes))
    else:
        output = FORMATS[args.format](findings)
        if output:
            print(output)
    if fail_on is not None and any(f.severity >= fail_on for f in findings):
        return EXIT_FINDINGS
    return EXIT_OK


def rule_dict(code: str) -> dict:
    rule = RULES[code]
    claim = rule.claim
    return {
        "code": rule.code,
        "name": rule.name,
        "severity": rule.severity.label,
        "title": rule.title,
        "why": rule.why,
        "fix": rule.fix,
        "claim": None
        if claim is None
        else {
            "lock": claim.lock,
            "rewrites": claim.rewrites,
            "scans": claim.scans,
            "fails_with": claim.fails_with,
            "blocks_reads": claim.blocks_reads,
            "blocks_writes": claim.blocks_writes,
        },
    }


def cmd_rules(args: argparse.Namespace) -> int:
    if args.format == "json":
        print(json.dumps([rule_dict(c) for c in sorted(RULES)], indent=2))
        return EXIT_OK
    for code in sorted(RULES):
        rule = RULES[code]
        print(f"{code}  {rule.severity.label:<8} {rule.name:<34} {rule.title}")
    return EXIT_OK


def cmd_explain(args: argparse.Namespace) -> int:
    code = args.code.upper()
    if code not in RULES:
        print(f"pgmg: unknown rule {args.code}", file=sys.stderr)
        return EXIT_USAGE
    rule = RULES[code]
    print(f"{rule.code} {rule.name} ({rule.severity.label})\n{rule.title}\n\n{rule.why}\n")
    print(f"Fix: {rule.fix}")
    if rule.demo:
        print(f"\nExample:\n  {rule.demo.sql}")
    return EXIT_OK


def _dsn(args: argparse.Namespace) -> str:
    if not args.dsn:
        raise ProbeError("give --dsn or set PGMG_DSN")
    return args.dsn


def cmd_probe(args: argparse.Namespace) -> int:
    dsn = _dsn(args)
    text = read_sources([args.file])[0][1]
    setup = Path(args.setup).read_text(encoding="utf-8") if args.setup else None
    results = probe_sql(dsn, text, setup=setup, allow_any_database=args.allow_any_database)
    if args.format == "json":
        print(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        print(probe_text(results))
    return EXIT_FINDINGS if any(r.status == "failed" for r in results) else EXIT_OK


def cmd_evidence(args: argparse.Namespace) -> int:
    report = collect(_dsn(args), allow_any_database=args.allow_any_database)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    problems = mismatches(report)
    for problem in problems:
        print(f"mismatch: {problem}", file=sys.stderr)
    checked = len(report["results"])
    print(f"PostgreSQL {report['server_version']}: {checked - len(problems)}/{checked} claims hold")
    return EXIT_FINDINGS if problems else EXIT_OK


HANDLERS = {
    "check": cmd_check,
    "rules": cmd_rules,
    "explain": cmd_explain,
    "probe": cmd_probe,
    "evidence": cmd_evidence,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except SourceError as exc:
        print(f"pgmg: line {exc.line}: {exc.message}", file=sys.stderr)
        return EXIT_USAGE
    except (ConfigError, ProbeError, SelectionError, OSError, UnicodeDecodeError) as exc:
        print(f"pgmg: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
