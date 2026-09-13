---
name: sync-local-settings-example
description: Safely synchronize Azure Functions setting names and allowlisted public values from local.settings.json into the tracked local.settings.example.json without exposing secrets.
---

# Sync the Local Settings Example

Use this skill when local Azure Functions settings have changed and the tracked
example must expose the same keys without exposing sensitive values.

## Safety requirements

- Run from the repository root.
- Treat `local.settings.json` as sensitive. Never print, summarize, copy, or
  otherwise expose values except for the public settings explicitly allowlisted
  by the helper.
- Verify that `local.settings.json` is ignored with
  `git check-ignore --no-index local.settings.json`. Stop if it is not ignored.
- Modify only `local.settings.example.json` unless the user separately requests
  documentation changes.
- Do not stage, commit, or push unless the user explicitly requests that work.

## Synchronize

Use [scripts/sync_local_settings_example.py](scripts/sync_local_settings_example.py)
instead of manually handling settings values.

1. Preview the sanitized candidate without writing. The preview may display
   only static development defaults and explicitly allowlisted public values:

   ```bash
   python3 .agents/skills/sync-local-settings-example/scripts/sync_local_settings_example.py
   ```

2. Review the candidate key names. The result intentionally mirrors the
   current `Values` keys: new keys are added and stale keys are removed. Keys
   ending in `_deprecated` or `_old`, matched case-insensitively, are omitted
   and removed from an existing example. If any other unexpected removal
   appears, report it and stop for user confirmation.
3. Write the sanitized example:

   ```bash
   python3 .agents/skills/sync-local-settings-example/scripts/sync_local_settings_example.py --write
   ```

4. Verify the result:

   ```bash
   python3 .agents/skills/sync-local-settings-example/scripts/sync_local_settings_example.py --check
   python3 -m json.tool local.settings.example.json
   git diff --check -- local.settings.example.json
   ```

The helper retains safe development defaults for `AzureWebJobsStorage`,
`FUNCTIONS_WORKER_RUNTIME`, and `JSON_FILE_ENCODING`. It copies the local values
of these explicitly public settings:

- `EARTHQUAKE_QUERY_MAX_RADIUS_KM`
- `EARTHQUAKE_QUERY_MAX_WINDOW_HOURS`
- `USGS_CRON_JOB_SCHEDULE`
- `USGS_EARTHQUAKE_API_URL`
- `AI_QA_MODEL_*_ASSET_NAME`
- `AI_QA_MODEL_*_ENABLED`
- `AI_QA_MODEL_*_ENDPOINT_URL`
- `AI_QA_MODEL_*_MAX_TOKENS`
- `AI_QA_MODEL_*_MODEL_NAME`
- `AI_QA_MODEL_*_TIMEOUT_SECONDS`

An empty `AI_QA_MODEL_*_API_KEY` remains empty so the example can represent a
managed-identity profile without suggesting that a key should be added. A
non-empty API key is always replaced. Every other setting gets the literal placeholder
`<set-in-local.settings.json>`. Adding another copied value requires deliberate
inclusion in the helper's public allowlist. The helper ignores keys with the
`_deprecated` or `_old` suffix, reads only the `Values` object, and always
writes `IsEncrypted` as `false`.
