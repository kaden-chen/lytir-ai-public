# Validation case format

`cases.json` is the tracked source of truth for the live validation suite. Each
case has these fields:

- `id`: unique kebab-case artifact name.
- `question`: complete request sent to `POST /api/ai/qa`.
- `expected_answer`: human-readable semantic expectation shown in the report.
- `expected_skills`: acceptable debug-mode skill values.
- `expected_outcomes`: acceptable public API outcome values.
- `expected_fields`: optional top-level response fields and exact values that
  must be present, such as provenance markers.
- `retrieval`: `none` or `required`.
- `answer_term_groups`: groups of equivalent terms. At least one term from each
  group must occur in the answer, case-insensitively.
- `semantic_check`: one of the checks below.
- `semantic_value`: numeric input required by checks such as `magnitude_floor`.

Available semantic checks:

- `none`: use only the contract, route, outcome, retrieval, and term checks.
- `retrieval_count`: require the answer to state the independently replayed
  count.
- `retrieval_presence`: require the answer's positive or negative conclusion to
  agree with whether replay returned any events.
- `magnitude_floor`: require every emitted query and replayed event to honor the
  magnitude in `semantic_value`.
- `strongest_event`: require the answer to identify the maximum replayed
  magnitude and its place. Common compass abbreviations and expanded names are
  treated as equivalent.

Prefer dynamic checks based on replayed evidence for live earthquake data. Use
answer terms only for stable concepts, not exact sentences, counts, dates, or
event details that change over time.
