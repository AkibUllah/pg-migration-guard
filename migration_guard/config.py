"""Read .pgmg.toml. Command-line flags win over the file."""

from __future__ import annotations

import sys
from pathlib import Path

from migration_guard.checker import Options
from migration_guard.model import RULES, Severity

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

DEFAULT_FILE = ".pgmg.toml"
KNOWN_KEYS = {"target_version", "assume_in_transaction", "disable", "severity", "fail_on"}
MIN_VERSION, MAX_VERSION = 12, 18


class ConfigError(ValueError):
    pass


def check_version(version: object) -> int:
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError("target_version must be a major version number, such as 16")
    if not MIN_VERSION <= version <= MAX_VERSION:
        raise ConfigError(f"target_version must be between {MIN_VERSION} and {MAX_VERSION}")
    return version


def _codes(values: object, key: str) -> set[str]:
    if not isinstance(values, list):
        raise ConfigError(f"{key} must be a list of rule codes")
    unknown = sorted(str(v) for v in values if v not in RULES)
    if unknown:
        raise ConfigError(f"{key}: unknown rule {', '.join(unknown)}")
    return set(values)


def _severity(value: object) -> Severity:
    try:
        return Severity.parse(str(value))
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def load_config(path: str | None = None) -> tuple[Options, Severity]:
    """Return the options and the severity that fails the run."""
    options = Options()
    fail_on = Severity.ERROR
    file = Path(path or DEFAULT_FILE)
    if not file.exists():
        if path:
            raise ConfigError(f"config file not found: {path}")
        return options, fail_on
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{file}: {exc}") from None

    unknown = sorted(set(data) - KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"{file}: unknown key {', '.join(unknown)}")
    if "target_version" in data:
        options.target_version = check_version(data["target_version"])
    if "assume_in_transaction" in data:
        if not isinstance(data["assume_in_transaction"], bool):
            raise ConfigError("assume_in_transaction must be true or false")
        options.assume_in_transaction = data["assume_in_transaction"]
    if "disable" in data:
        options.disabled = _codes(data["disable"], "disable")
    if "severity" in data:
        if not isinstance(data["severity"], dict):
            raise ConfigError("severity must be a table of rule code = level")
        _codes(list(data["severity"]), "severity")
        options.severity = {code: _severity(v) for code, v in data["severity"].items()}
    if "fail_on" in data:
        fail_on = _severity(data["fail_on"])
    return options, fail_on
