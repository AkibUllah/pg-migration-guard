"""Findings as terminal text, JSON, GitHub annotations, SARIF or a Markdown PR comment."""

from __future__ import annotations

import json
from collections import Counter

from migration_guard import __version__
from migration_guard.model import RULES, Finding, Severity

REPO = "https://github.com/AkibUllah/pg-migration-guard"
SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}
GITHUB_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "notice"}


def ordered(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.path, f.line, -f.severity, f.code))


def totals(findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings)
    parts = [
        f"{counts[s]} {s.label}{'' if counts[s] == 1 else 's'}"
        for s in sorted(counts, reverse=True)
    ]
    return ", ".join(parts) if parts else "no findings"


def to_text(findings: list[Finding], *, fixes: bool = True) -> str:
    lines = []
    for f in ordered(findings):
        lines.append(f"{f.path}:{f.line}: {f.severity.label} {f.code} {f.name}")
        lines.append(f"    {f.message}")
        if fixes and f.fix:
            lines.extend(
                f"    fix: {row}" if i == 0 else f"         {row}"
                for i, row in enumerate(f.fix.splitlines())
            )
    lines.append(totals(findings))
    return "\n".join(lines)


def to_json(findings: list[Finding]) -> str:
    return json.dumps(
        {"version": __version__, "findings": [f.to_dict() for f in ordered(findings)]},
        indent=2,
        ensure_ascii=False,
    )


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def to_github(findings: list[Finding]) -> str:
    """Workflow commands, which GitHub turns into annotations on the changed lines."""
    rows = []
    for f in ordered(findings):
        title = _escape_property(f"{f.code} {f.name}")
        body = f.message + (f"\n\nFix:\n{f.fix}" if f.fix else "")
        rows.append(
            f"::{GITHUB_LEVEL[f.severity]} file={_escape_property(f.path)},line={f.line},"
            f"title={title}::{_escape_data(body)}"
        )
    return "\n".join(rows)


def to_sarif(findings: list[Finding]) -> str:
    used = sorted({f.code for f in findings})
    rules = []
    for code in used:
        rule = RULES.get(code)
        name = rule.name if rule else next(f.name for f in findings if f.code == code)
        rules.append(
            {
                "id": code,
                "name": name,
                "shortDescription": {"text": rule.title if rule else name},
                "fullDescription": {"text": rule.why if rule else name},
                "help": {"text": rule.fix if rule else "See the message."},
                "helpUri": f"{REPO}#rules",
            }
        )
    results = [
        {
            "ruleId": f.code,
            "ruleIndex": used.index(f.code),
            "level": SARIF_LEVEL[f.severity],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.path},
                        "region": {"startLine": f.line},
                    }
                }
            ],
        }
        for f in ordered(findings)
    ]
    return json.dumps(
        {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "pg-migration-guard",
                            "version": __version__,
                            "informationUri": REPO,
                            "rules": rules,
                        }
                    },
                    "results": results,
                }
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


def to_markdown(findings: list[Finding]) -> str:
    if not findings:
        return "**pg-migration-guard:** no findings."
    lines = [
        f"**pg-migration-guard:** {totals(findings)}",
        "",
        "| Severity | Where | Rule | Problem |",
        "|---|---|---|---|",
    ]
    for f in ordered(findings):
        message = f.message.replace("|", "\\|")
        lines.append(f"| {f.severity.label} | `{f.path}:{f.line}` | {f.code} | {message} |")
    return "\n".join(lines)


FORMATS = {
    "text": to_text,
    "json": to_json,
    "github": to_github,
    "sarif": to_sarif,
    "markdown": to_markdown,
}
