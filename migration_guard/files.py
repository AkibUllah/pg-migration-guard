"""Find the migration files to check."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class SelectionError(ValueError):
    pass


def expand(paths: list[str]) -> list[str]:
    """Files as given, directories as their .sql files in name order."""
    found: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(str(p) for p in sorted(path.rglob("*.sql")))
        elif path.is_file():
            found.append(raw)
        else:
            raise SelectionError(f"no such file or directory: {raw}")
    return found


def changed_since(ref: str, paths: list[str]) -> list[str]:
    """SQL files added or changed since the merge base with `ref`, in name order."""
    if not ref or ref.startswith("-"):
        raise SelectionError(f"not a git ref: {ref!r}")
    command = ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD", "--"]
    command += [str(Path(p) / "*.sql") if Path(p).is_dir() else p for p in paths] or ["*.sql"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise SelectionError("git is not installed") from None
    except subprocess.CalledProcessError as exc:
        raise SelectionError(f"git diff failed: {exc.stderr.strip()}") from None
    return sorted(line for line in result.stdout.splitlines() if line.endswith(".sql"))


def read_sources(files: list[str]) -> list[tuple[str, str]]:
    if files == ["-"]:
        return [("<stdin>", sys.stdin.read())]
    return [(f, Path(f).read_text(encoding="utf-8")) for f in files]
