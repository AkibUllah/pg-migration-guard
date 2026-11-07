"""Build the GitHub Pages site from the committed evidence.

    python scripts/build_site.py _site

Nothing on the page is typed by hand: the matrix comes from evidence/pg*.json, the rule text from
the rule registry, and the worked example from the real output of pgmg check and pgmg probe.
"""

from __future__ import annotations

import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migration_guard import __version__  # noqa: E402
from migration_guard.checker import Options, check_sources  # noqa: E402
from migration_guard.model import RULES  # noqa: E402
from migration_guard.probe import StatementResult  # noqa: E402
from migration_guard.probe import to_text as probe_text  # noqa: E402
from migration_guard.report import to_text  # noqa: E402

REPO = "https://github.com/AkibUllah/pg-migration-guard"
TEMPLATE = Path(__file__).with_name("site_template.html")
EVIDENCE = ROOT / "evidence"
EXAMPLES = ROOT / "examples" / "migrations"
RISKY = EXAMPLES / "risky" / "0007_shipment_tracking.sql"
SAFE = EXAMPLES / "safe" / "0007_add_public_id.sql"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def mark(value: bool) -> str:
    return '<td class="mark yes">yes</td>' if value else '<td class="mark no">no</td>'


def severity(label: str) -> str:
    return f'<span class="sev {esc(label)}">{esc(label)}</span>'


def row(first: str, entry: dict) -> str:
    m = entry["measured"]
    rule = RULES.get(entry["code"])
    # A rule with no scan claim saw only a counter tick, such as an index rebuild on an empty heap.
    scan = '<td class="mark no">n/a</td>' if rule and rule.claim.scans is None else mark(m["scans"])
    return (
        f"<tr><td>{first}</td><td><code>{esc(entry['sql'])}</code></td>"
        f'<td class="lock">{esc(m["lock"])}</td>{mark(m["rewrites"])}{scan}'
        f"{mark(m['blocks_reads'])}{mark(m['blocks_writes'])}</tr>"
    )


def versions_differ(reports: list[dict]) -> int:
    def key(report):
        return [e["measured"] for e in report["results"]]

    return sum(
        1
        for entries in zip(*(key(r) for r in reports), strict=True)
        if any(e != entries[0] for e in entries)
    )


def catalog() -> str:
    rows = []
    for code in sorted(RULES):
        r = RULES[code]
        rows.append(
            f"<details><summary><b>{esc(code)}</b> {severity(r.severity.label)} "
            f"{esc(r.title)}</summary><p>{esc(r.why)}</p><p><b>Fix:</b> {esc(r.fix)}</p></details>"
        )
    return "\n".join(rows)


def probe_output(path: Path) -> str:
    results = [StatementResult(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
    for result in results:
        result.ms = 0
    lines = [line for line in probe_text(results).splitlines() if not line.endswith(" ms")]
    return "\n".join(lines)


def build(out: Path) -> dict:
    reports = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(EVIDENCE.glob("pg[0-9]*.json"))
    ]
    latest = reports[-1]
    entries = latest["results"]
    flagged = [e for e in entries if e["kind"] == "rule" and not e["measured"]["fails_with"]]
    failing = [e for e in entries if e["kind"] == "rule" and e["measured"]["fails_with"]]
    safe = [e for e in entries if e["kind"] == "safe"]
    checked = sum(len(r["results"]) for r in reports)
    problems = sum(len(e["mismatches"]) for r in reports for e in r["results"])
    if problems:
        raise SystemExit(f"{problems} claims are contradicted by the evidence")

    risky = check_sources([(RISKY.name, RISKY.read_text(encoding="utf-8"))], Options())
    values = {
        "repo": REPO,
        "version": esc(__version__),
        "rules": str(len(RULES)),
        "checked": str(checked),
        "servers": str(len(reports)),
        "differences": str(versions_differ(reports)),
        "latest": str(latest["major"]),
        "server_list": esc(", ".join(r["server_version"] for r in reports)),
        "probe_version": "17",
        "flagged": "\n".join(row(f"<b>{esc(e['code'])}</b>", e) for e in flagged),
        "safe": "\n".join(row(severity("safe") + f" {esc(e['name'])}", e) for e in safe),
        "failing": "\n".join(
            f"<tr><td><b>{esc(e['code'])}</b> {esc(e['name'])}</td>"
            f"<td><code>{esc(e['sql'])}</code></td>"
            f'<td class="lock">{esc(e["measured"]["fails_with"])}</td></tr>'
            for e in failing
        ),
        "check_risky": esc(to_text(risky, fixes=False)),
        "probe_risky": esc(probe_output(EVIDENCE / "example-risky.json")),
        "safe_sql": esc(SAFE.read_text(encoding="utf-8")),
        "probe_safe": esc(probe_output(EVIDENCE / "example-safe.json")),
        "catalog": catalog(),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        page = page.replace(f"@@{key}@@", value)
    if "@@" in page:
        raise SystemExit("the template has a placeholder that was not filled")
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    for report in EVIDENCE.glob("*.json"):
        shutil.copy(report, out / "evidence" / report.name)
    return {"rules": len(RULES), "checked": checked, "flagged": len(flagged)}


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
    print(f"wrote {target / 'index.html'}: {build(target)}")
