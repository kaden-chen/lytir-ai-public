"""Helpers for parsing loosely typed input values."""

TRUTHY_VALUES = frozenset({"true", "1", "yes", "enabled", "allowed", "approved"})


def is_truthy(value: object) -> bool:
    """Return whether a value matches a supported truthy representation."""
    return str(value).strip().lower() in TRUTHY_VALUES
