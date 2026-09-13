"""Externally safe, version-controlled product context supplied to the model."""

LYTIR_PUBLIC_CONTEXT_VERSION = "lytir-public-v1"

LYTIR_PUBLIC_CONTEXT = """
Lytir is a service for acquiring, retrieving, and explaining earthquake
observations. It exposes deduplicated earthquake metadata and can answer
natural-language questions using that metadata.

The available earthquake fields are occurrence time, last-updated time,
magnitude, place description, longitude, latitude, and depth in kilometers.
Lytir can filter observations by a bounded time range, a geographic center and
radius, and minimum or maximum magnitude. Its data bounds describe the period
the service supports and the latest event it has observed; they do not
guarantee uninterrupted or complete coverage.

The Q&A API handles one independent question per request. It does not retain a
conversation or remember clarification responses. A caller answering a
clarification must submit a new, complete question. Answers may contain
Markdown. Locations, proximity, and local-time expressions may be interpreted
by the model and are reported as such rather than presented as authoritative
geocoding.
""".strip()
