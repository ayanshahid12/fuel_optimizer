"""Resolve user-entered route locations, checking the PostgreSQL cache before ORS.

Cache reads and writes happen on the calling thread; worker threads only make
ORS HTTP requests, so they never need their own database connection.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from fuel.models import LocationGeocode
from fuel.services.ors import GeocodedLocation, OpenRouteServiceClient

_COMMA = re.compile(r"\s*,\s*")
_ROLES = ("start", "finish")


def normalize_location(text: str) -> str:
    """Case-fold, collapse whitespace, and standardize comma spacing."""
    value = " ".join(text.split()).casefold()
    return _COMMA.sub(", ", value).strip(" ,.")


def resolve_route_locations(
    client: OpenRouteServiceClient,
    start: str,
    finish: str,
) -> tuple[GeocodedLocation, GeocodedLocation]:
    """Return start and finish coordinates, geocoding cache misses concurrently.

    Only successful lookups are cached. If both fail, the start error is raised.
    """
    inputs = {"start": start, "finish": finish}
    keys = {role: normalize_location(text) for role, text in inputs.items()}

    found: dict[str, tuple[str, float, float]] = {
        row.normalized_query: (row.label, row.latitude, row.longitude)
        for row in LocationGeocode.objects.filter(normalized_query__in=set(keys.values()))
    }
    misses: dict[str, str] = {}
    for role in _ROLES:
        if keys[role] not in found:
            misses.setdefault(keys[role], role)

    def lookup(role: str) -> GeocodedLocation:
        return client.geocode(inputs[role], role=role)

    errors: dict[str, Exception] = {}
    if len(misses) == 1:
        [(key, role)] = misses.items()
        try:
            fetched = {key: lookup(role)}
        except Exception as exc:  # noqa: BLE001 - re-raised below in role order
            fetched, errors[key] = {}, exc
    elif misses:
        with ThreadPoolExecutor(max_workers=len(misses)) as pool:
            futures = {key: pool.submit(lookup, role) for key, role in misses.items()}
        fetched = {}
        for key, future in futures.items():
            if future.exception() is None:
                fetched[key] = future.result()
            else:
                errors[key] = future.exception()
    else:
        fetched = {}

    for key, location in fetched.items():
        LocationGeocode.objects.update_or_create(
            normalized_query=key,
            defaults={
                "query": inputs[misses[key]],
                "label": location.label[:500],
                "latitude": location.latitude,
                "longitude": location.longitude,
            },
        )
        found[key] = (location.label, location.latitude, location.longitude)

    for role in _ROLES:
        if keys[role] in errors:
            raise errors[keys[role]]

    def located(role: str) -> GeocodedLocation:
        label, latitude, longitude = found[keys[role]]
        return GeocodedLocation(input=inputs[role], label=label, latitude=latitude, longitude=longitude)

    return located("start"), located("finish")
