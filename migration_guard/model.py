"""What a rule is, what it claims about PostgreSQL, and what it reports."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum

BLOCKS_WRITES = {"ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"}


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> Severity:
        try:
            return cls[value.upper()]
        except KeyError:
            raise ValueError(f"unknown severity {value!r}") from None


@dataclass(frozen=True)
class Claim:
    """What a rule says the server does. `pgmg evidence` checks each field on a real server.

    `lock` is the strongest mode taken on the demo's target table. `fails_with` is a SQLSTATE,
    for rules about statements that do not run at all. `reads_blocked` overrides the value that
    follows from the lock mode, for the cases where an index lock blocks readers on its own.
    """

    lock: str | None = None
    rewrites: bool = False
    scans: bool = False
    fails_with: str | None = None
    reads_blocked: bool | None = None

    @property
    def blocks_reads(self) -> bool:
        if self.reads_blocked is not None:
            return self.reads_blocked
        return self.lock == "AccessExclusiveLock"

    @property
    def blocks_writes(self) -> bool:
        return self.lock in BLOCKS_WRITES


@dataclass(frozen=True)
class Demo:
    """The smallest statement that shows the claim, run on top of evidence.FIXTURE."""

    sql: str
    target: str = "demo"
    setup: str = ""


@dataclass(frozen=True)
class Hit:
    message: str
    fix: str | None = None
    severity: Severity | None = None


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    severity: Severity
    title: str
    why: str
    fix: str
    check: Callable[..., Iterable[Hit]]
    claim: Claim | None = None
    demo: Demo | None = None


@dataclass(frozen=True)
class Finding:
    code: str
    name: str
    severity: Severity
    message: str
    path: str
    line: int
    sql: str = ""
    fix: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "severity": self.severity.label,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "sql": self.sql,
            "fix": self.fix,
        }


RULES: dict[str, Rule] = {}


def rule(
    code: str,
    name: str,
    severity: Severity,
    title: str,
    why: str,
    fix: str,
    claim: Claim | None = None,
    demo: Demo | None = None,
):
    def register(check: Callable[..., Iterable[Hit]]):
        if code in RULES:
            raise ValueError(f"duplicate rule {code}")
        RULES[code] = Rule(code, name, severity, title, why, fix, check, claim, demo)
        return check

    return register
