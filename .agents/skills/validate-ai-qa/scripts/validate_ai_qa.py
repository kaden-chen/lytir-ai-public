#!/usr/bin/env python3
"""Validate the live Lytir Q&A endpoint and produce timestamped evidence."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:7071/api"
DEFAULT_CASES = Path(__file__).resolve().parents[1] / "references" / "cases.json"
DEFAULT_OUTPUT_ROOT = Path("tmp/qa-validation/runs")
DEFAULT_CREDENTIALS_FILE = Path("tmp/qa-validation/remote-services.md")
DEFAULT_FUNCTION_KEY_ENV = "LYTIR_FUNCTION_KEY"
DEFAULT_TARGET = "local"
AZURE_TARGET_ALIAS = "azure-dev"
DEFAULT_TIMEOUT_SECONDS = 220.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
NUMBER_WORDS = {
    0: ("zero", "no", "none"),
    1: ("one",),
    2: ("two",),
    3: ("three",),
    4: ("four",),
    5: ("five",),
    6: ("six",),
    7: ("seven",),
    8: ("eight",),
    9: ("nine",),
    10: ("ten",),
    11: ("eleven",),
    12: ("twelve",),
    13: ("thirteen",),
    14: ("fourteen",),
    15: ("fifteen",),
    16: ("sixteen",),
    17: ("seventeen",),
    18: ("eighteen",),
    19: ("nineteen",),
    20: ("twenty",),
}
DIRECTION_NAMES = {
    "nne": "north northeast",
    "ene": "east northeast",
    "ese": "east southeast",
    "sse": "south southeast",
    "ssw": "south southwest",
    "wsw": "west southwest",
    "wnw": "west northwest",
    "nnw": "north northwest",
    "ne": "northeast",
    "se": "southeast",
    "sw": "southwest",
    "nw": "northwest",
    "n": "north",
    "e": "east",
    "s": "south",
    "w": "west",
}


class ValidationSetupError(ValueError):
    """Raised when the suite cannot be configured safely."""


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: object
    execution_seconds: float


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    question: str
    expected_answer: str
    request_body: dict[str, object]
    response: HTTPResult
    replays: list[dict[str, object]]
    correct: bool
    reasons: list[str]


@dataclass(frozen=True)
class StartedLocalHost:
    """Exact process group created by this validation run."""

    process: subprocess.Popen[bytes]


@dataclass(frozen=True)
class ServiceTarget:
    """Resolved validation target without reportable credentials."""

    name: str
    diag_url: str = dataclass_field(repr=False)
    ai_qa_url: str = dataclass_field(repr=False)
    earthquakes_url: str = dataclass_field(repr=False)
    function_key: str | None = dataclass_field(repr=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a running Lytir AI Q&A API and write a report."
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=(
            "Service target. Defaults to local; azure is an alias for azure-dev. "
            "Other values select a named profile from the credentials file."
        ),
    )
    parser.add_argument(
        "--base-url",
        help="Override the local target's base URL.",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=DEFAULT_CREDENTIALS_FILE,
        help="Ignored Markdown file containing remote service profiles.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run only this case ID; repeat to select multiple cases.",
    )
    parser.add_argument(
        "--function-key-env",
        default=DEFAULT_FUNCTION_KEY_ENV,
        help="Environment variable containing an optional Functions host key.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--start-local",
        action="store_true",
        help="Start and later stop a local Functions host when none is running.",
    )
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
        help="Maximum time to wait for a host started with --start-local.",
    )
    return parser.parse_args()


def load_cases(path: Path, selected: list[str] | None) -> list[dict[str, Any]]:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationSetupError(f"Case manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationSetupError(
            f"Invalid case manifest JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValidationSetupError("Case manifest must use schema_version 1")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValidationSetupError("Case manifest must contain a non-empty cases list")

    cases: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValidationSetupError("Every case must be a JSON object")
        case = dict(raw_case)
        case_id = case.get("id")
        question = case.get("question")
        expected = case.get("expected_answer")
        if not isinstance(case_id, str) or not re.fullmatch(r"[a-z0-9-]+", case_id):
            raise ValidationSetupError("Every case requires a kebab-case id")
        if case_id in known_ids:
            raise ValidationSetupError(f"Duplicate case id: {case_id}")
        if not isinstance(question, str) or not question.strip():
            raise ValidationSetupError(f"Case {case_id} requires a question")
        if not isinstance(expected, str) or not expected.strip():
            raise ValidationSetupError(f"Case {case_id} requires an expected_answer")
        known_ids.add(case_id)
        cases.append(case)

    if selected:
        unknown = sorted(set(selected) - known_ids)
        if unknown:
            raise ValidationSetupError(f"Unknown case id(s): {', '.join(unknown)}")
        selected_set = set(selected)
        cases = [case for case in cases if case["id"] in selected_set]
    return cases


def resolve_service_target(
    target: str,
    *,
    base_url_override: str | None,
    credentials_file: Path,
    function_key_env: str,
) -> ServiceTarget:
    target_name = target.strip().lower()
    if target_name == "azure":
        target_name = AZURE_TARGET_ALIAS
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", target_name):
        raise ValidationSetupError(
            "Target must contain only lowercase letters, numbers, and hyphens"
        )

    environment_key = os.environ.get(function_key_env) or None
    if target_name == DEFAULT_TARGET:
        base_url = validate_base_url(base_url_override or DEFAULT_BASE_URL)
        return ServiceTarget(
            name=target_name,
            diag_url=f"{base_url}/diag",
            ai_qa_url=f"{base_url}/ai/qa",
            earthquakes_url=f"{base_url}/earthquakes",
            function_key=environment_key,
        )

    if base_url_override is not None:
        raise ValidationSetupError("--base-url can only be used with the local target")

    profile = load_remote_profile(credentials_file, target_name)
    api_urls = profile.get("api_urls")
    if not isinstance(api_urls, dict):
        raise ValidationSetupError(
            f"Remote profile {target_name!r} requires an api_urls object"
        )
    resolved_urls = {
        api_name: _remote_api_url(api_urls, target_name, api_name)
        for api_name in ("diag", "ai_qa", "earthquakes")
    }
    return ServiceTarget(
        name=target_name,
        diag_url=resolved_urls["diag"],
        ai_qa_url=resolved_urls["ai_qa"],
        earthquakes_url=resolved_urls["earthquakes"],
        function_key=environment_key,
    )


def _remote_api_url(
    api_urls: dict[object, object], target_name: str, api_name: str
) -> str:
    value = api_urls.get(api_name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationSetupError(
            f"Remote profile {target_name!r} requires api_urls.{api_name}"
        )
    if _contains_placeholder(value):
        raise ValidationSetupError(
            f"Remote profile {target_name!r} api_urls.{api_name} still contains "
            "a placeholder"
        )
    return validate_api_url(value)


def load_remote_profile(path: Path, target_name: str) -> dict[str, object]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationSetupError(
            f"Remote service credentials file not found: {path}"
        ) from exc
    match = re.search(r"```json\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
    if match is None:
        raise ValidationSetupError(
            f"Remote service credentials file lacks a fenced JSON object: {path}"
        )
    try:
        document: object = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationSetupError(
            "Invalid remote service credentials JSON at "
            f"line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(document, dict):
        raise ValidationSetupError("Remote service credentials must be a JSON object")
    profile = document.get(target_name)
    if not isinstance(profile, dict):
        raise ValidationSetupError(
            f"Remote service profile {target_name!r} was not found in {path}"
        )
    return dict(profile)


def _contains_placeholder(value: str) -> bool:
    folded = value.casefold()
    return "<" in value or ">" in value or "replace-with-" in folded


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationSetupError("Base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationSetupError(
            "Base URL cannot contain credentials, a query, or a fragment"
        )
    return value.rstrip("/")


def validate_api_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationSetupError("API URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValidationSetupError(
            "API URL cannot contain user credentials or a fragment"
        )
    query_names = {
        name.casefold()
        for name, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    if query_names - {"code"}:
        raise ValidationSetupError(
            "Configured API URLs may contain only the function-key code parameter"
        )
    return value


def redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.urlencode(
        [
            (name, "***" if name.casefold() == "code" else item_value)
            for name, item_value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
        ],
        safe="*",
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
    )


def request_json(
    method: str,
    url: str,
    *,
    function_key: str | None,
    timeout_seconds: float,
    payload: dict[str, object] | None = None,
    params: dict[str, object] | None = None,
) -> HTTPResult:
    if params:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode(
            [
                *urllib.parse.parse_qsl(parsed.query, keep_blank_values=True),
                *((key, value) for key, value in params.items() if value is not None),
            ]
        )
        url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
        )
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if function_key:
        headers["x-functions-key"] = function_key
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = _decode_body(response.read())
            return HTTPResult(
                status=response.status,
                body=body,
                execution_seconds=time.perf_counter() - started,
            )
    except urllib.error.HTTPError as exc:
        return HTTPResult(
            status=exc.code,
            body=_decode_body(exc.read()),
            execution_seconds=time.perf_counter() - started,
        )
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        message = (
            str(exc.reason) if isinstance(exc, urllib.error.URLError) else str(exc)
        )
        return HTTPResult(
            status=0,
            body={"request_error": type(exc).__name__, "message": message},
            execution_seconds=time.perf_counter() - started,
        )


def ensure_service(
    diag_url: str,
    *,
    target_name: str,
    function_key: str | None,
    start_local: bool,
    startup_timeout_seconds: float,
) -> StartedLocalHost | None:
    """Verify service health, optionally starting one loopback Functions host."""
    health = _health_check(diag_url, function_key)
    if health.status == 200:
        return None
    if health.status != 0:
        raise ValidationSetupError(_health_failure_message(health.status))
    if not start_local:
        raise ValidationSetupError(
            f"{target_name} service is not reachable."
            + (
                " Start it first or use --start-local."
                if target_name == "local"
                else ""
            )
        )

    parsed = urllib.parse.urlsplit(diag_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValidationSetupError("--start-local requires an HTTP loopback base URL")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ValidationSetupError("Base URL contains an invalid port") from exc
    executable = shutil.which("func")
    if executable is None:
        raise ValidationSetupError(
            "Azure Functions Core Tools command 'func' was not found"
        )

    environment = os.environ.copy()
    virtual_env_bin = Path.cwd() / ".venv" / "bin"
    if (virtual_env_bin / "python").is_file():
        environment["PATH"] = f"{virtual_env_bin}{os.pathsep}{environment['PATH']}"

    process = subprocess.Popen(
        [executable, "start", "--port", str(port)],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started_host = StartedLocalHost(process)
    try:
        deadline = time.monotonic() + startup_timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValidationSetupError(
                    "The local Functions host exited before becoming healthy"
                )
            health = _health_check(diag_url, function_key)
            if health.status == 200:
                return started_host
            if health.status != 0:
                raise ValidationSetupError(_health_failure_message(health.status))
            time.sleep(0.5)

        raise ValidationSetupError(
            "The local Functions host was not healthy within "
            f"{startup_timeout_seconds:g} seconds"
        )
    except BaseException:
        stop_local_host(started_host)
        raise


def stop_local_host(started_host: StartedLocalHost) -> None:
    """Stop only the process group created by ensure_service."""
    process = started_host.process
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def _health_check(diag_url: str, function_key: str | None) -> HTTPResult:
    return request_json(
        "GET",
        diag_url,
        function_key=function_key,
        timeout_seconds=HEALTH_CHECK_TIMEOUT_SECONDS,
    )


def _health_failure_message(status: int) -> str:
    if status in {401, 403}:
        return (
            f"Service health check returned HTTP {status}; provide a valid "
            f"function key through {DEFAULT_FUNCTION_KEY_ENV}"
        )
    return f"Service health check returned HTTP {status}"


def _decode_body(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"non_json_body": text}


def replay_queries(
    earthquakes_url: str,
    payload: dict[str, Any],
    *,
    function_key: str | None,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    data_context = payload.get("data_context")
    if not isinstance(data_context, dict):
        return []
    queries = data_context.get("queries")
    if not isinstance(queries, list):
        return []

    results: list[dict[str, object]] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        params = {
            "start_time": query.get("start_utc"),
            "end_time": query.get("end_utc"),
            "latitude": query.get("latitude"),
            "longitude": query.get("longitude"),
            "radius_km": query.get("radius_km"),
            "min_magnitude": query.get("min_magnitude"),
            "max_magnitude": query.get("max_magnitude"),
        }
        response = request_json(
            "GET",
            earthquakes_url,
            function_key=function_key,
            timeout_seconds=timeout_seconds,
            params=params,
        )
        results.append(
            {
                "query": params,
                "status": response.status,
                "execution_seconds": round(response.execution_seconds, 6),
                "response_body": response.body,
            }
        )
    return results


def evaluate_case(
    case: dict[str, Any],
    response: HTTPResult,
    replays: list[dict[str, object]],
) -> list[str]:
    failures: list[str] = []
    if response.status != 200:
        return [f"Q&A returned HTTP {response.status}"]
    if not isinstance(response.body, dict):
        return ["Q&A response was not a JSON object"]
    payload: dict[str, Any] = response.body

    for field in ("question", "outcome", "answer", "answer_format", "model"):
        if field not in payload:
            failures.append(f"missing response field: {field}")
    if payload.get("question") != case["question"]:
        failures.append("response question did not match the request")
    if payload.get("answer_format") != "markdown":
        failures.append("answer_format was not markdown")

    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        failures.append("answer was missing or empty")
        answer = ""
    answer_folded = answer.casefold()

    expected_skills = case.get("expected_skills", [])
    if payload.get("skill") not in expected_skills:
        failures.append(
            f"skill {payload.get('skill')!r} was not one of {expected_skills!r}"
        )
    expected_outcomes = case.get("expected_outcomes", [])
    if payload.get("outcome") not in expected_outcomes:
        failures.append(
            f"outcome {payload.get('outcome')!r} was not one of {expected_outcomes!r}"
        )

    expected_fields = case.get("expected_fields", {})
    if not isinstance(expected_fields, dict):
        failures.append("case expected_fields was invalid")
    else:
        for field_name, expected_value in expected_fields.items():
            if not isinstance(field_name, str):
                failures.append("case expected_fields contained a non-string key")
            elif payload.get(field_name) != expected_value:
                failures.append(
                    f"response field {field_name!r} was {payload.get(field_name)!r}, "
                    f"expected {expected_value!r}"
                )

    usage = payload.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("requests"), int):
        failures.append("debug usage was missing; run the service in debug mode")
    elif usage["requests"] < 1:
        failures.append("usage.requests was not positive")

    _check_answer_terms(case, answer_folded, failures)
    _check_retrieval_expectation(case, payload, replays, failures)
    _check_semantics(case, answer_folded, replays, failures)
    return failures


def _check_answer_terms(case: dict[str, Any], answer: str, failures: list[str]) -> None:
    groups = case.get("answer_term_groups", [])
    if not isinstance(groups, list):
        failures.append("case answer_term_groups was invalid")
        return
    for group in groups:
        if not isinstance(group, list) or not all(
            isinstance(term, str) for term in group
        ):
            failures.append("case contained an invalid answer term group")
            continue
        if not any(term.casefold() in answer for term in group):
            failures.append(f"answer did not contain any expected term from {group!r}")


def _check_retrieval_expectation(
    case: dict[str, Any],
    payload: dict[str, Any],
    replays: list[dict[str, object]],
    failures: list[str],
) -> None:
    expectation = case.get("retrieval")
    data_context = payload.get("data_context")
    queries = data_context.get("queries") if isinstance(data_context, dict) else None
    query_count = len(queries) if isinstance(queries, list) else 0

    if expectation == "none" and query_count:
        failures.append(
            f"expected no retrieval but received {query_count} query call(s)"
        )
    elif expectation == "required":
        if query_count == 0:
            failures.append(
                "retrieval query context was missing; run the service in debug mode"
            )
        if len(replays) != query_count:
            failures.append("not every emitted retrieval query was replayed")

    replay_counts: list[int] = []
    for replay in replays:
        if replay.get("status") != 200:
            failures.append(
                f"independent retrieval returned HTTP {replay.get('status')}"
            )
            continue
        body = replay.get("response_body")
        if not isinstance(body, dict) or not isinstance(body.get("count"), int):
            failures.append("independent retrieval lacked an integer count")
            continue
        replay_counts.append(body["count"])

    if replay_counts and isinstance(data_context, dict):
        matched_records = data_context.get("matched_records")
        if matched_records != sum(replay_counts):
            failures.append(
                "independent retrieval count differed from data_context.matched_records"
            )


def _check_semantics(
    case: dict[str, Any],
    answer: str,
    replays: list[dict[str, object]],
    failures: list[str],
) -> None:
    check = case.get("semantic_check", "none")
    items = _replayed_items(replays)
    if check == "none":
        return
    if check == "retrieval_count":
        count = len(items)
        if not _number_is_mentioned(answer, count):
            failures.append(f"answer did not report the replayed count of {count}")
        return
    if check == "retrieval_presence":
        if items and not _has_positive_conclusion(answer, len(items)):
            failures.append("answer did not affirm the replayed matching events")
        if not items and not _has_negative_conclusion(answer):
            failures.append("answer did not state that replay found no matching events")
        return
    if check == "magnitude_floor":
        floor = case.get("semantic_value")
        if not isinstance(floor, (int, float)):
            failures.append("magnitude_floor case lacked a numeric semantic_value")
            return
        for replay in replays:
            query = replay.get("query")
            if not isinstance(query, dict) or query.get("min_magnitude") != floor:
                failures.append(f"retrieval did not apply minimum magnitude {floor:g}")
        magnitudes = [item.get("magnitude") for item in items]
        if any(
            not isinstance(value, (int, float)) or value < floor for value in magnitudes
        ):
            failures.append(f"retrieval returned an event below magnitude {floor:g}")
        return
    if check == "strongest_event":
        if not items:
            if not _has_negative_conclusion(answer):
                failures.append("answer did not explain that no strongest event exists")
            return
        numeric_items = [
            item for item in items if isinstance(item.get("magnitude"), (int, float))
        ]
        if not numeric_items:
            failures.append("replayed events lacked numeric magnitudes")
            return
        strongest = max(numeric_items, key=lambda item: float(item["magnitude"]))
        magnitude = float(strongest["magnitude"])
        place = strongest.get("place")
        if not _magnitude_is_mentioned(answer, magnitude):
            failures.append(
                f"answer did not identify strongest magnitude {magnitude:g}"
            )
        if isinstance(place, str) and not _place_is_mentioned(answer, place):
            failures.append(f"answer did not identify strongest-event place {place!r}")
        return
    failures.append(f"unsupported semantic_check: {check!r}")


def _replayed_items(replays: list[dict[str, object]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for replay in replays:
        body = replay.get("response_body")
        raw_items = body.get("items") if isinstance(body, dict) else None
        if isinstance(raw_items, list):
            items.extend(item for item in raw_items if isinstance(item, dict))
    return items


def _number_is_mentioned(answer: str, number: int) -> bool:
    if re.search(rf"(?<![\d.]){number}(?![\d.])", answer):
        return True
    return any(
        re.search(rf"\b{re.escape(word)}\b", answer)
        for word in NUMBER_WORDS.get(number, ())
    )


def _has_positive_conclusion(answer: str, count: int) -> bool:
    return _number_is_mentioned(answer, count) or any(
        phrase in answer
        for phrase in ("yes", "there was", "there were", "observed", "found")
    )


def _has_negative_conclusion(answer: str) -> bool:
    return any(
        phrase in answer
        for phrase in (
            "no earthquake",
            "no matching",
            "none",
            "zero",
            "did not observe",
            "didn't observe",
            "there were no",
            "there was no",
        )
    )


def _magnitude_is_mentioned(answer: str, magnitude: float) -> bool:
    candidates = {f"{magnitude:g}", f"{magnitude:.1f}", f"{magnitude:.2f}"}
    return any(
        re.search(rf"(?<![\d.]){re.escape(value)}(?![\d.])", answer)
        for value in candidates
    )


def _place_is_mentioned(answer: str, place: str) -> bool:
    return _normalize_place(place) in _normalize_place(answer)


def _normalize_place(value: str) -> str:
    normalized = value.casefold()
    for abbreviation, expanded in DIRECTION_NAMES.items():
        normalized = re.sub(rf"\b{abbreviation}\b", expanded, normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def run_case(
    case: dict[str, Any],
    *,
    ai_qa_url: str,
    earthquakes_url: str,
    function_key: str | None,
    timeout_seconds: float,
) -> CaseResult:
    question = str(case["question"])
    request_body: dict[str, object] = {"question": question}
    response = request_json(
        "POST",
        ai_qa_url,
        function_key=function_key,
        timeout_seconds=timeout_seconds,
        payload=request_body,
    )
    payload = response.body if isinstance(response.body, dict) else {}
    replays = replay_queries(
        earthquakes_url,
        payload,
        function_key=function_key,
        timeout_seconds=timeout_seconds,
    )
    reasons = evaluate_case(case, response, replays)
    return CaseResult(
        case_id=str(case["id"]),
        question=question,
        expected_answer=str(case["expected_answer"]),
        request_body=request_body,
        response=response,
        replays=replays,
        correct=not reasons,
        reasons=reasons or ["All configured semantic and retrieval checks passed."],
    )


def write_results(
    output_dir: Path,
    results: list[CaseResult],
    generated: str,
    *,
    target_name: str,
    api_urls: dict[str, str],
) -> None:
    cases_dir = output_dir / "cases"
    cases_dir.mkdir(parents=True)
    summaries: list[dict[str, object]] = []
    for result in results:
        artifact: dict[str, object] = {
            "case_id": result.case_id,
            "question": result.question,
            "expected_answer": result.expected_answer,
            "request_body": result.request_body,
            "http_status": result.response.status,
            "client_execution_seconds": round(result.response.execution_seconds, 6),
            "response_body": result.response.body,
            "independent_retrievals": result.replays,
            "correct": result.correct,
            "reason": " ".join(result.reasons),
        }
        (cases_dir / f"{result.case_id}.json").write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summaries.append(
            {
                "case_id": result.case_id,
                "correct": result.correct,
                "http_status": result.response.status,
                "reason": " ".join(result.reasons),
            }
        )

    summary: dict[str, object] = {
        "generated_at_utc": generated,
        "target": target_name,
        "api_urls": api_urls,
        "passed": sum(result.correct for result in results),
        "failed": sum(not result.correct for result in results),
        "cases": summaries,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "REPORT.md").write_text(
        render_report(
            results,
            generated,
            target_name=target_name,
            api_urls=api_urls,
        ),
        encoding="utf-8",
    )


def render_report(
    results: list[CaseResult],
    generated: str,
    *,
    target_name: str,
    api_urls: dict[str, str],
) -> str:
    passed = sum(result.correct for result in results)
    lines = [
        "# Lytir AI Q&A validation report",
        "",
        f"Generated at: `{generated}`",
        "",
        f"Target: `{target_name}`",
        "",
        "API URLs:",
        "",
        *(f"- `{name}`: `{url}`" for name, url in api_urls.items()),
        "",
        f"Result: **{'PASS' if passed == len(results) else 'FAIL'}** "
        f"({passed}/{len(results)} cases passed)",
        "",
    ]
    for result in results:
        lines.extend(
            [
                f"## {result.case_id}",
                "",
                f"Question: {result.question}",
                "",
                f"Expected answer: {result.expected_answer}",
                "",
                f"Correct: **{'yes' if result.correct else 'no'}**",
                "",
                f"Reason: {' '.join(result.reasons)}",
                "",
                "Rendered answer (Markdown):",
                "",
                _render_markdown_answer(result.response.body),
                "",
                "Request body (JSON):",
                "",
                _indent_json(result.request_body),
                "",
                "Response body (JSON):",
                "",
                _indent_json(result.response.body),
                "",
                "Independent retrievals (JSON):",
                "",
                _indent_json(result.replays),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _indent_json(value: object) -> str:
    return "\n".join(
        f"    {line}"
        for line in json.dumps(value, indent=2, ensure_ascii=False).splitlines()
    )


def _render_markdown_answer(response_body: object) -> str:
    answer = response_body.get("answer") if isinstance(response_body, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        return "> _No answer returned._"
    safe_answer = html.escape(answer, quote=False)
    return "\n".join(f"> {line}" if line else ">" for line in safe_answer.splitlines())


def output_directory(root: Path, now: datetime, target_name: str) -> Path:
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / f"{timestamp}-{target_name}"
    sequence = 1
    while candidate.exists():
        candidate = root / f"{timestamp}-{target_name}-{sequence}"
        sequence += 1
    return candidate


def main() -> int:
    args = parse_args()
    try:
        target = resolve_service_target(
            args.target,
            base_url_override=args.base_url,
            credentials_file=args.credentials_file,
            function_key_env=args.function_key_env,
        )
        if args.start_local and target.name != DEFAULT_TARGET:
            raise ValidationSetupError(
                "--start-local can only be used with the local target"
            )
        if args.timeout_seconds <= 0:
            raise ValidationSetupError("timeout-seconds must be positive")
        if args.startup_timeout_seconds <= 0:
            raise ValidationSetupError("startup-timeout-seconds must be positive")
        cases = load_cases(args.cases, args.case_ids)
    except (OSError, ValidationSetupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    started_host: StartedLocalHost | None = None
    try:
        started_host = ensure_service(
            target.diag_url,
            target_name=target.name,
            function_key=target.function_key,
            start_local=args.start_local,
            startup_timeout_seconds=args.startup_timeout_seconds,
        )
    except ValidationSetupError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    generated_at = datetime.now(UTC)
    try:
        results: list[CaseResult] = []
        for case in cases:
            case_id = str(case["id"])
            print(f"Running {case_id}...", flush=True)
            result = run_case(
                case,
                ai_qa_url=target.ai_qa_url,
                earthquakes_url=target.earthquakes_url,
                function_key=target.function_key,
                timeout_seconds=args.timeout_seconds,
            )
            results.append(result)
            print(f"{case_id}: {'PASS' if result.correct else 'FAIL'}", flush=True)

        output_dir = output_directory(args.output_root, generated_at, target.name)
        write_results(
            output_dir,
            results,
            generated_at.isoformat().replace("+00:00", "Z"),
            target_name=target.name,
            api_urls={
                "diag": redact_url(target.diag_url),
                "ai_qa": redact_url(target.ai_qa_url),
                "earthquakes": redact_url(target.earthquakes_url),
            },
        )
        print(f"Report: {output_dir / 'REPORT.md'}")
        return 0 if all(result.correct for result in results) else 1
    finally:
        if started_host is not None:
            stop_local_host(started_host)


if __name__ == "__main__":
    raise SystemExit(main())
