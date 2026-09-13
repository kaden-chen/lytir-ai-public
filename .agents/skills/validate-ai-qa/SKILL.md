---
name: validate-ai-qa
description: Run repeatable live validation of the Lytir AI Q&A API, including semantic expectations and independent replay of earthquake queries. Use when testing Q&A routing, answers, grounding, or regressions against a running service.
---

# Validate Lytir AI Q&A

Use the tracked cases in [references/cases.json](references/cases.json) and the
deterministic runner. It supports local and named deployed targets. Generated
results belong only under the ignored `tmp/qa-validation/runs/` directory.

## Safety and scope

- Run from the repository root. The default target is `local`.
- Treat `azure` as an alias for `azure-dev`. Use any other deployed profile only
  when the user identifies it as the intended validation target.
- Never inspect, print, copy, or include `local.settings.json`, credentials, API
  keys, connection strings, or unredacted API URLs in reports. Let the runner
  read only the selected profile from the ignored credentials file.
- Deployed profiles may use a different `code` query parameter in each complete
  API URL. The runner uses those URLs directly and redacts every `code` value in
  generated output. `LYTIR_FUNCTION_KEY` remains available as an optional
  shared-key header override.
- Do not retry failed cases automatically. Each retry can cause another model
  request and must be intentional.
- Do not change application code merely to make a variable model answer match
  exact wording. Judge the semantic criteria and supporting retrieval evidence.

## Run the suite

The Q&A service should run with `SERVICE_MODE=debug`; otherwise the response
omits the skill, usage, and query context needed for complete validation.

```bash
python3 .agents/skills/validate-ai-qa/scripts/validate_ai_qa.py
```

To validate the deployed development service, use:

```bash
python3 .agents/skills/validate-ai-qa/scripts/validate_ai_qa.py --target azure
```

This reads the `azure-dev` profile from the ignored
`tmp/qa-validation/remote-services.md` file. The file contains a fenced JSON
object with complete endpoint URLs for each remote profile. Never commit this
file or place its URLs on the command line because their `code` parameters are
credentials.

The selected profile must provide these three function-authorized APIs. Each URL
may use a different function code:

- `GET api_urls.diag` for the preflight check;
- `POST api_urls.ai_qa` for each validation question;
- `GET api_urls.earthquakes` for independent replay of data-backed answers.

Use this file shape when the local credentials file is missing:

````markdown
```json
{
  "azure-dev": {
    "api_urls": {
      "diag": "https://replace-with-azure-dev-function-app.azurewebsites.net/api/diag?code=<diag-function-key>",
      "ai_qa": "https://replace-with-azure-dev-function-app.azurewebsites.net/api/ai/qa?code=<ai-qa-function-key>",
      "earthquakes": "https://replace-with-azure-dev-function-app.azurewebsites.net/api/earthquakes?code=<earthquakes-function-key>"
    }
  }
}
```
````

Use `--target azure-dev` explicitly for the same profile, or select another
named profile such as `--target azure-prod`. Placeholder values are rejected
before any network request is made.

The runner first calls the inexpensive `GET /api/diag` endpoint. It stops
before making model requests when the service is unavailable, unhealthy, or
requires a missing function key.

To start the local Functions host only when that preflight cannot connect, use:

```bash
python3 .agents/skills/validate-ai-qa/scripts/validate_ai_qa.py --start-local
```

`--start-local` is accepted only for a loopback URL. The runner uses the
repository's `.venv` when present, waits for the diagnostic endpoint, and stops
only the host process group that it started. It leaves an already-running host
untouched.

Useful options:

```bash
python3 .agents/skills/validate-ai-qa/scripts/validate_ai_qa.py \
  --target local \
  --start-local \
  --base-url http://127.0.0.1:7071/api \
  --case general-capital \
  --case strongest-recent
```

The runner creates
`tmp/qa-validation/runs/<UTC-timestamp>-<resolved-target>/` (for example,
`20260912T035024Z-local` or `20260912T035024Z-azure-dev`) containing:

- `REPORT.md`, with the timestamp and, for every case, the question, expected
  answer, rendered Markdown answer, request JSON, raw response JSON,
  correctness, reason, resolved target, and sanitized API URLs;
- `summary.json`, with target metadata and compact pass/fail information;
- `cases/<case-id>.json`, with the complete response and independent retrieval
  evidence.

It exits `0` only when every selected case passes, `1` for validation failures,
and `2` for invalid input or setup.

## Interpret the result

The automated verdict checks the public response contract, expected route and
outcome, semantic answer indicators, and retrieval invariants. For data-backed
answers it replays every validated query through `GET /api/earthquakes` and
checks the returned evidence independently.

Read `REPORT.md` after every run. Review any failure against the natural-language
expected answer, the raw response, and the replayed data before concluding that
the application is defective. Counts and strongest events are time-dependent;
the manifest deliberately specifies dynamic consistency rules instead of fixed
values.

When adding a regression case, give it a stable ID, a self-contained question,
a human-readable expected answer, and the narrowest machine checks that prove
the behavior. Do not add secrets, unstable exact prose, or fixed live-data
counts. Read [references/case-format.md](references/case-format.md) before
editing the manifest.
