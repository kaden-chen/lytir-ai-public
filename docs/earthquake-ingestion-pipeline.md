# Earthquake ingestion pipeline

## Scope

The earthquake pipeline has three independent stages:

```text
USGSClient -> EarthquakeNormalizer -> IoTHubPublisher
```

- `USGSClient` downloads the configured USGS GeoJSON feed in memory.
- `EarthquakeNormalizer` validates each feature and adds compact metadata.
- `IoTHubPublisher` sends one JSON message per earthquake to IoT Hub.

`EarthquakeIngestionPipeline` orchestrates these stages directly, and
`run_earthquake_ingestion()` builds it from the application environment. The
`POST /api/earthquakes/acquire` and the timer trigger both call that pipeline
entry point directly. The timer does not call the HTTP endpoint. Its schedule
comes from the `USGS_CRON_JOB_SCHEDULE` application setting; use
`0 0 * * * *` to run at the first second of every hour.

## Code organization

- `blueprints/` contains Azure Functions trigger adapters. A blueprint handles
  its trigger input, calls application code, and converts the result into the
  trigger-specific response.
- `pipelines/` coordinates complete workflows. The reusable
  `run_earthquake_ingestion()` entry point constructs and runs the earthquake
  pipeline for both the HTTP and timer triggers.
- `services/` contains the individual download, normalization, and publishing
  operations used by the pipeline.
- `models/` contains the shared data shapes passed between those operations.

The name `pipeline` is used instead of `orchestrator` to avoid implying that
the application uses Azure Durable Functions orchestration.

## HTTP response

The acquisition endpoint returns each normalized event's metadata with its
USGS event ID and aggregate processing counts. It does not return the raw
`event` payload:

```json
{
  "counts": {
    "retrieved": 1,
    "normalized": 1,
    "skipped": 0,
    "published": 1,
    "failed": 0
  },
  "metadata": [
    {
      "id": "us7000abcd",
      "event_type": "earthquake",
      "magnitude": 2.4,
      "place": "10 km NW of Example",
      "occurred_at_utc": "2026-09-11T12:30:00Z",
      "updated_at_utc": "2026-09-11T12:35:00Z",
      "longitude": -122.4,
      "latitude": 38.7,
      "depth_km": 4.2,
      "source_url": "https://earthquake.usgs.gov/earthquakes/eventpage/example"
    }
  ],
  "utc_now": "2026-09-11T12:36:00+00:00",
  "reason": "OK"
}
```

`retrieved` is the number of source features examined and equals `normalized`
plus `skipped`. Dry runs report `published` and `failed` as zero. Responses
produced before the feed is downloaded, such as invalid request errors, report
all counts as zero.

If the pipeline fails after normalization, the response retains the metadata
that was already produced. Before normalization succeeds, `metadata` is an
empty list.

The optional POST body enables a preview without publishing:

```json
{
  "dry_run": true
}
```

When `dry_run` is true, the pipeline skips IoT Hub and the response adds an
`events` list containing each complete envelope, including `metadata` and the
original USGS `event`. Missing `dry_run` defaults to false. JSON booleans, `1`,
and case-insensitive `"true"`, `"1"`, `"yes"`, `"enabled"`, `"allowed"`, or
`"approved"` strings are truthy. All other values are treated as false.
Invalid JSON or a non-object request body returns HTTP 400 without running the
pipeline.

## Event envelope

Each IoT Hub message uses a versioned envelope:

```json
{
  "id": "us7000abcd",
  "schema_version": 1,
  "source": "usgs",
  "metadata": {
    "event_type": "earthquake",
    "magnitude": 2.4,
    "place": "10 km NW of Example",
    "occurred_at_utc": "2026-09-11T12:30:00Z",
    "updated_at_utc": "2026-09-11T12:35:00Z",
    "longitude": -122.4,
    "latitude": 38.7,
    "depth_km": 4.2,
    "source_url": "https://earthquake.usgs.gov/earthquakes/eventpage/example"
  },
  "event": {
    "type": "Feature",
    "id": "us7000abcd"
  }
}
```

`metadata` is the compact, stable view used for common Cosmos DB queries and AI
context. `event` is the original parsed USGS GeoJSON feature and is preserved
without mutation so less common source fields remain available.

The top-level `id` is the USGS event ID. It provides a stable identity for
downstream duplicate handling. `schema_version` allows the envelope to evolve
without confusing consumers.

## Cosmos DB persistence

The current IoT Hub route stores the application envelope under the Cosmos DB
document's `Body` property. IoT Hub message details, including the USGS event
ID assigned as `message-id`, appear under `SystemProperties`.

Cosmos DB currently generates its own root-level document `id`; the stable USGS
ID remains at `Body.id`. Repeated acquisition runs can therefore create
multiple Cosmos documents for the same earthquake. A future storage-ingestion
change must define the duplicate-handling policy before relying on the USGS ID
for idempotency.

## Decisions

- Data moves between stages in memory; this service does not archive temporary
  USGS files.
- Timestamps are converted from USGS epoch milliseconds to UTC ISO 8601 text.
- The third GeoJSON coordinate is named `depth_km`, not altitude.
- Malformed individual features are skipped and counted; an invalid top-level
  feed fails normalization.
- Missing optional magnitude, place, update time, and source URL values remain
  `null`.
- Publishing continues after an individual message failure and reports
  published and failed counts.
- The pipeline catches and logs exceptions with the active stage name. Its
  result carries success state, diagnostic reason, and processing counts so
  HTTP and timer triggers share the same outcome handling.
- Logs identify failed event IDs but do not include connection strings or full
  event payloads.

## Out of scope

- Retry and duplicate-storage policy
- Cosmos DB routing changes
- Time-zone lookup, alerts, and local file exports from the legacy `Lytir`
  project
