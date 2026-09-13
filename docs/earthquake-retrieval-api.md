# Earthquake retrieval API design

## Status

Accepted design for issue #18.

## Context

The IoT Hub route persists each streamed application envelope in the existing
Cosmos DB data container. The application envelope is stored under the Cosmos
document's `Body` property. Cosmos assigns its own root document ID, while the
stable USGS earthquake ID remains at `Body.id`.

Repeated acquisition runs can therefore create multiple Cosmos documents for
the same earthquake. These documents are useful as raw replay and backfill
data and must not be deleted or rewritten merely to support reads. `Body.id`
is used internally to identify those duplicates but is not part of the public
response.

The retrieval API will query this existing container, select one current
logical record per USGS earthquake ID, and return the selected Cosmos document
UUID with caller-facing metadata.
A separate curated container or relational read model is deferred until query
volume or cost demonstrates that one is needed.

## Goals

- Provide function-authorized `GET /api/earthquakes` collection queries and
  `GET /api/earthquakes/{id}` exact-record lookup.
- Read from the existing Cosmos data container without modifying raw records.
- Return the selected Cosmos document UUID and an explicit metadata allowlist.
- Handle duplicate raw documents deterministically.
- Support bounded time and location-radius queries.
- Support useful metadata filters without exposing arbitrary Cosmos queries.
- Return either JSON or CSV, selected by the `format` query parameter or its
  `fmt` alias.
- Keep Cosmos querying, HTTP parsing, and response formatting independently
  testable.

## Non-goals

- Returning the original USGS event stored at `Body.event`
- Exposing the USGS source ID at `Body.id` or `Body.metadata.source_url`
- Writing to Cosmos DB or deleting duplicate raw documents
- Creating a curated Cosmos container or relational read model
- Triggering earthquake acquisition from the retrieval endpoint
- Polygon, free-text, or arbitrary query-language support
- Anonymous public access

## Architecture

```text
Caller
  -> function-authorized GET /api/earthquakes or /api/earthquakes/{id}
  -> query parsing and validation
  -> Cosmos earthquake query service
  -> metadata validation and deduplication
  -> filtering and sorting
  -> JSON or CSV formatter
  -> HTTP response
```

The Cosmos service returns structured earthquake metadata records. It does not
serialize JSON or CSV. The HTTP adapter selects a formatter after the service
has produced the final result set.

## Source document shape

The retrieval code targets the envelope currently persisted under `Body`:

```json
{
  "id": "cosmos-generated-id",
  "Body": {
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
    "event": {}
  },
  "_ts": 1789130100
}
```

Queries should project only the fields required for retrieval and internal
deduplication:

- Cosmos root `id`, returned as the public `id` and used as the final tie-breaker
- Cosmos `_ts`, used only as an ingestion-time tie-breaker
- `Body.id`, used only to group duplicate logical earthquakes
- `Body.schema_version`
- The allowlisted `Body.metadata` fields used by the public response and filters

`Body.id`, `_ts`, system properties, `Body.metadata.source_url`, and
`Body.event` must not appear in caller responses. The Cosmos root UUID appears
as the public `id` without exposing its storage-specific role in the response
shape.

Before deployment, verify the deployed container's partition key, indexing
policy, and TTL. The first version uses cross-partition queries because the
caller cannot provide the raw container's partition key. The index must support
the bounded `Body.metadata.occurred_at_utc` query.

## Caller-facing record

Each result contains the selected Cosmos root UUID plus the approved metadata
fields:

```json
{
  "id": "4f91d6ea-99c7-4b22-b3a1-4eb7f2cb9e38",
  "event_type": "earthquake",
  "magnitude": 2.4,
  "place": "10 km NW of Example",
  "occurred_at_utc": "2026-09-11T12:30:00Z",
  "updated_at_utc": "2026-09-11T12:35:00Z",
  "longitude": -122.4,
  "latitude": 38.7,
  "depth_km": 4.2
}
```

Missing optional values remain `null` in JSON and become empty fields in CSV.
The UUID identifies the exact selected raw Cosmos document for later lookup or
debugging. It is not the logical earthquake identity and can change when a
newer duplicate becomes the selected record.

## Deduplication

The query service groups valid candidates by `Body.id` and selects one winner
using this descending priority:

1. Newest valid `Body.metadata.updated_at_utc`
2. Highest Cosmos `_ts`
3. Lexicographically greatest Cosmos root `id`

A missing `updated_at_utc` sorts before any valid update timestamp. Cosmos
`_ts` and the root ID participate in internal winner selection. The winning
document's root UUID becomes the public `id`; `_ts` and the grouping value from
`Body.id` remain internal.

Malformed documents are skipped and logged without their complete payload.
The service records candidate, malformed, duplicate, and selected counts for
operational visibility. It does not mutate or remove duplicate source data.

Deduplication occurs before magnitude and radius filters. This prevents an
older version of an event from matching when its newest version would not
match.

## Exact record lookup

`GET /api/earthquakes/{id}` retrieves the exact raw Cosmos document selected by
its root UUID and returns the same caller-facing metadata allowlist. This route
is intended for later retrieval and debugging; it does not rerun logical-event
deduplication.

The adapter validates `id` as a UUID. The service uses a Cosmos point read when
the deployed partition-key design permits one; otherwise it performs a
parameterized query for the exact root ID. A missing record returns HTTP 404.
The route supports the same `format=json|csv` behavior, with JSON as the
default. Other collection filters are invalid on the exact-record route.

## Query parameters

### Time range

| Parameter | Meaning | Default |
| --- | --- | --- |
| `start_time` | Inclusive event occurrence time | 2 hours before effective `end_time` |
| `end_time` | Exclusive event occurrence time | Current UTC time |

Times must be ISO 8601 values with an explicit offset or `Z`. The adapter
normalizes them to UTC. `start_time` must be earlier than `end_time`.

The two-hour default is a code constant, not an application setting. It covers
two scheduled hourly acquisition intervals without adding another operational
configuration value.

Each request is limited by `EARTHQUAKE_QUERY_MAX_WINDOW_HOURS`, when configured,
or the conservative code default of `720` hours (30 days). A configured value
must be a positive integer. Callers can retrieve longer history through
consecutive bounded requests. The time bounds apply to
`Body.metadata.occurred_at_utc`, not Cosmos `_ts`.

When neither time is supplied, the effective range is the previous two hours
through the current UTC time. When only `end_time` is supplied, `start_time`
defaults to two hours before it. When only `start_time` is supplied, `end_time`
defaults to the current UTC time and the resulting range must remain within the
configured maximum.

### Location and radius

| Parameter | Meaning |
| --- | --- |
| `latitude` | Center latitude from `-90` through `90` |
| `longitude` | Center longitude from `-180` through `180` |
| `radius_km` | Radius in kilometers |
| `radius_mi` | Radius in statute miles |

Location rules:

1. `latitude` and `longitude` must be supplied together.
2. A non-empty `radius_km` takes precedence when both radius parameters are
   supplied.
3. Otherwise, a non-empty `radius_mi` is converted using
   `1 mile = 1.609344 kilometers`.
4. When coordinates are present but both radius parameters are absent or
   empty, the effective radius is `5 km`.
5. When coordinates are absent, no location filter is applied and no default
   radius is created.
6. A radius without complete coordinates is invalid.
7. Radius values must be finite and greater than zero. The maximum comes from
   `EARTHQUAKE_QUERY_MAX_RADIUS_KM`, or its `1,000 km` code default, and is
   applied after conversion.

The service normalizes all radius calculations to kilometers. Empty query
values are treated as omitted; malformed non-empty values return HTTP 400.

The raw metadata stores longitude and latitude as scalar numbers rather than a
GeoJSON `Point`. The first implementation therefore uses a bounding-box check
followed by an exact Haversine-distance calculation with a mean Earth radius of
`6,371.0088 km`. It must handle longitude wraparound at the antimeridian.

### Magnitude filters

| Parameter | Meaning |
| --- | --- |
| `min_magnitude` | Inclusive minimum magnitude |
| `max_magnitude` | Inclusive maximum magnitude |

Magnitude values must be finite. `min_magnitude` cannot exceed
`max_magnitude`.

Magnitude and exact radius filtering occur after deduplication.

### Result controls

| Parameter | Meaning | Default |
| --- | --- | --- |
| `format` | `json` or `csv` | `json` |
| `fmt` | Alias for `format` | — |

`format` is normalized to lowercase. An empty format uses the JSON default;
any other non-empty value returns HTTP 400. When both parameters are non-empty,
`format` takes precedence over `fmt`.

Results use a fixed deterministic order:

1. `occurred_at_utc` descending
2. Public Cosmos document `id` descending

The API does not initially expose arbitrary sort expressions.

## Query execution

The service executes each request in this order:

1. Parse and validate the HTTP query into a typed query object.
2. Query Cosmos for projected candidates in the bounded occurrence-time range.
3. Validate candidate metadata and skip malformed documents safely.
4. Deduplicate candidates by `Body.id`.
5. Apply metadata and location filters to the selected latest versions.
6. Sort by the fixed public order.
7. Format the complete result set as JSON or CSV.

The initial implementation favors correct latest-version semantics over
pushing every filter into Cosmos. The bounded time window limits the candidate
set. Server-side projections reduce payload and RU consumption.

If observed candidate volume or request charge becomes excessive, optimize
only after measuring. Possible later optimizations include additional safe
server-side predicates, a GeoJSON point, or a curated container keyed by the
USGS event ID.

## Time-window paging and result safety

The public API does not expose cursors or caller-controlled result limits in
the first version. Each response contains every matching deduplicated record
within the requested time window.

Callers retrieve additional history by requesting adjacent, non-overlapping
time windows of no more than the configured 30-day maximum. Time ranges are
half-open: `start_time` is inclusive and `end_time` is exclusive. For example,
`[12:00, 13:00)` followed by `[11:00, 12:00)` has no gap or overlap, including
events exactly on the boundary. A frontend may paginate the returned records
locally without making additional API requests.

The Cosmos SDK may use continuation tokens internally while the service reads
all raw candidates for one bounded window. Those implementation tokens are
never exposed to callers.

The configured maximum time window is the public query-size control; there is
no separate result-count limit and results are never silently truncated. The
capacity assumption is fewer than 10 logical events per hour with about two
raw Cosmos documents per logical event. A maximum 30-day request therefore
examines approximately 14,400 raw candidates and returns at most approximately
7,200 deduplicated records under expected conditions. `count` represents the
complete number of matches in the requested window.

Candidate and response counts are monitored. If production volume materially
exceeds this assumption, the design must be revisited before increasing the
configured maximum window.

## JSON response

JSON responses use `application/json`:

```json
{
  "items": [
    {
      "id": "4f91d6ea-99c7-4b22-b3a1-4eb7f2cb9e38",
      "event_type": "earthquake",
      "magnitude": 2.4,
      "place": "10 km NW of Example",
      "occurred_at_utc": "2026-09-11T12:30:00Z",
      "updated_at_utc": "2026-09-11T12:35:00Z",
      "longitude": -122.4,
      "latitude": 38.7,
      "depth_km": 4.2
    }
  ],
  "count": 1,
  "time_range": {
    "start_utc": "2026-09-11T10:36:00Z",
    "end_utc": "2026-09-11T12:36:00Z"
  },
  "utc_now": "2026-09-11T12:36:00+00:00"
}
```

`time_range` reports the effective half-open range used by the query after
applying any default timestamps. No matches return HTTP 200 with an empty
`items` list and `count` equal to zero while retaining the effective range.

## CSV response

CSV responses use `text/csv; charset=utf-8` and the stable column order:

```text
id,event_type,magnitude,place,occurred_at_utc,updated_at_utc,longitude,latitude,depth_km
```

CSV rules:

- Use a standards-compliant CSV writer for quoting and embedded line breaks.
- Render null optional values as empty fields.
- Preserve timestamps as normalized ISO 8601 UTC text.
- Protect textual cells that could be interpreted as spreadsheet formulas.
- Set `Content-Disposition` with a safe generated filename.
- Return the complete result count in `X-Result-Count`.

Errors remain JSON even when the request asks for CSV. This gives callers one
consistent machine-readable error contract.

## HTTP errors

| Status | Condition |
| --- | --- |
| `400` | Invalid filter, unsupported format, or inconsistent parameters |
| `401` or `403` | Function-key authorization failure handled by Azure Functions |
| `404` | Requested Cosmos record UUID does not exist |
| `500` | Invalid application configuration or unexpected internal failure |
| `503` | Cosmos DB is unavailable or remains throttled after SDK retries |

Error responses contain a stable reason and UTC timestamp without credentials,
Cosmos queries, continuation tokens, or source payloads.

## Configuration and authentication

The endpoint uses `AuthLevel.FUNCTION`. Anonymous access is not permitted.

Application settings should identify the existing Cosmos account and data
container without embedding secrets in source control:

- `COSMOS_EARTHQUAKE_DB_CONNECTION_STRING`
- `COSMOS_EARTHQUAKE_DB_NAME`
- `COSMOS_EARTHQUAKE_DATA_CONTAINER_NAME`
- `EARTHQUAKE_QUERY_MAX_WINDOW_HOURS` (optional; defaults to `720`)
- `EARTHQUAKE_QUERY_MAX_RADIUS_KM` (optional; defaults to `1000`)

`EARTHQUAKE_QUERY_MAX_RADIUS_KM` must be finite and at least `5` so the
documented default radius is always valid. Radius values supplied in miles are
converted to kilometers before comparison with this setting.

The Cosmos connection string is a secret. Store it in an Azure Function App
setting, preferably through an Azure Key Vault reference, and keep local values
only in ignored `local.settings.json`. `local.settings.example.json` contains
safe public defaults and placeholders for sensitive or environment-specific
values.

The Cosmos client should be reused across warm Function App invocations rather
than constructed for every request.

## Observability

Structured logs should include:

- Normalized time-window duration
- Whether location and optional metadata filters were used
- Candidate, malformed, duplicate, selected, and returned counts
- Cosmos request charge when available
- Cosmos and total request latency
- Response format

Logs must not contain function keys, Cosmos credentials, complete source
events, or complete response bodies.

## Testing

Tests should cover:

- One-hour default and explicit time windows
- Inclusive start and exclusive end boundaries
- Invalid, reversed, naive, and excessive time windows
- Default, non-integer, and non-positive maximum-window configuration
- Latitude and longitude validation
- `radius_km`, `radius_mi`, kilometer precedence, conversion, and the `5 km`
  default
- Invalid and excessive radii
- Default, nonnumeric, non-finite, and less-than-default maximum-radius
  configuration
- Antimeridian and Haversine-distance behavior
- Minimum and maximum magnitude filters
- Deterministic duplicate selection and tie-breakers
- Public winner UUID with hidden `Body.id`, `source_url`, `_ts`, and raw event
- Exact-record lookup success, invalid UUID, and missing UUID
- Malformed raw Cosmos documents
- Fixed sorting and adjacent time windows
- Empty results
- JSON response shape
- CSV column order, escaping, nulls, spreadsheet protection, and count headers
- Cosmos configuration, throttling, and unexpected failures
- Function-level authorization metadata

All Cosmos interactions are mocked in unit tests. Integration testing against a
real test container may be added separately when deployment configuration is
available.

## Deferred decisions

- A curated Cosmos container keyed by USGS event ID
- A relational retrieval store
- GeoJSON point storage and native geospatial indexing
- Unbounded exports or asynchronous export jobs
- Additional sort modes, polygons, and free-text place search
