"""Importing this package registers every rule in model.RULES."""

from migration_guard.rules import compatibility, data, locking, types

__all__ = ["compatibility", "data", "locking", "types"]
