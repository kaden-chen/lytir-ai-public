#!/usr/bin/env python3
"""Create a sanitized Azure Functions local settings example."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SAFE_DEFAULTS = {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "JSON_FILE_ENCODING": "utf-8",
}
PUBLIC_VALUE_KEYS = {
    "EARTHQUAKE_QUERY_MAX_RADIUS_KM",
    "EARTHQUAKE_QUERY_MAX_WINDOW_HOURS",
    "USGS_CRON_JOB_SCHEDULE",
    "USGS_EARTHQUAKE_API_URL",
}
PUBLIC_AI_VALUE_SUFFIXES = (
    "_ASSET_NAME",
    "_ENABLED",
    "_ENDPOINT_URL",
    "_MAX_TOKENS",
    "_MODEL_NAME",
    "_TIMEOUT_SECONDS",
)
PLACEHOLDER = "<set-in-local.settings.json>"
IGNORED_SUFFIXES = ("_deprecated", "_old")


class SettingsError(ValueError):
    """Raised when a local settings file cannot be safely processed."""


def is_ignored_setting(key: str) -> bool:
    """Return whether a setting is retired and should stay out of the example."""
    return key.casefold().endswith(IGNORED_SUFFIXES)


def is_public_value_setting(key: str) -> bool:
    """Return whether a setting value is explicitly safe for the example."""
    return key in PUBLIC_VALUE_KEYS or (
        key.startswith("AI_QA_MODEL_") and key.endswith(PUBLIC_AI_VALUE_SUFFIXES)
    )


def load_settings(source: Path) -> tuple[list[str], dict[str, str]]:
    """Load setting names and explicitly public values from the source."""
    try:
        document: Any = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SettingsError(f"Settings file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"Invalid JSON in {source} at line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(document, dict):
        raise SettingsError(f"Expected a JSON object in {source}")

    values = document.get("Values")
    if not isinstance(values, dict):
        raise SettingsError(f"Expected {source} to contain a Values object")

    keys = list(values)
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise SettingsError("All entries in Values must have non-empty string keys")

    keys = [key for key in keys if not is_ignored_setting(key)]
    public_values: dict[str, str] = {}
    copied_value_keys = {
        key
        for key in keys
        if is_public_value_setting(key)
        or (key.endswith("_API_KEY") and values[key] == "")
    }
    for key in copied_value_keys:
        value = values[key]
        if not isinstance(value, str):
            raise SettingsError(f"Expected copied setting {key} to have a string value")
        public_values[key] = value

    default_keys = [key for key in SAFE_DEFAULTS if key in values]
    custom_keys = sorted(key for key in keys if key not in SAFE_DEFAULTS)
    return default_keys + custom_keys, public_values


def build_example(keys: list[str], public_values: dict[str, str]) -> dict[str, Any]:
    """Build an example using static defaults, public values, or placeholders."""
    example_values = {
        key: SAFE_DEFAULTS.get(key, public_values.get(key, PLACEHOLDER)) for key in keys
    }
    return {
        "IsEncrypted": False,
        "Values": example_values,
    }


def serialize(document: dict[str, Any]) -> str:
    """Serialize the example in the repository's JSON style."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize local setting names into a sanitized example."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("local.settings.json"),
        help="Source settings file (default: local.settings.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("local.settings.example.json"),
        help="Example settings file (default: local.settings.example.json)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero when the example is missing or out of date",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Write the sanitized example",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.source.resolve() == args.output.resolve():
        print("Source and output paths must be different.", file=sys.stderr)
        return 2

    try:
        keys, public_values = load_settings(args.source)
        candidate = serialize(build_example(keys, public_values))
    except (OSError, SettingsError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        try:
            current = args.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"{args.output} is missing", file=sys.stderr)
            return 1
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if current != candidate:
            print(f"{args.output} is out of date", file=sys.stderr)
            return 1

        print(f"{args.output} is up to date")
        return 0

    if args.write:
        try:
            args.output.write_text(candidate, encoding="utf-8")
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Synchronized {args.output}")
        return 0

    print(candidate, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
