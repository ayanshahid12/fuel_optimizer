"""Geocode FuelStation rows that are missing coordinates.

Uses the shared OpenRouteService client. Duplicate physical addresses share one
ORS request; source CSV rows are never merged or deleted.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db.models import Q
from django.utils import timezone

from fuel.exceptions import LocationNotFoundError, OrsRateLimitError
from fuel.models import FuelStation
from fuel.services.ors import OpenRouteServiceClient

ProgressCallback = Callable[[str], None]
SleepFn = Callable[[float], None]

_WHITESPACE = re.compile(r"\s+")


@dataclass
class AddressGroup:
    key: str
    query: str
    stations: list[FuelStation] = field(default_factory=list)


@dataclass
class GeocodeRunStats:
    attempted: int = 0
    succeeded: int = 0
    rows_updated: int = 0
    not_found: int = 0
    errors: int = 0
    remaining_rows: int = 0
    stopped_early: str | None = None


def normalize_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def normalize_address_key(address: str, city: str, state: str) -> str | None:
    """Return a lowercase comparison key, or None if any part is missing."""
    parts = [normalize_whitespace(part).lower() for part in (address, city, state)]
    if any(not part for part in parts):
        return None
    return "|".join(parts)


def build_geocode_query(address: str, city: str, state: str) -> str | None:
    """Build ``\"{address}, {city}, {state}, USA\"`` or None if incomplete."""
    parts = [normalize_whitespace(part) for part in (address, city, state)]
    if any(not part for part in parts):
        return None
    return f"{parts[0]}, {parts[1]}, {parts[2]}, USA"


def missing_coordinates_q() -> Q:
    return Q(latitude__isnull=True) | Q(longitude__isnull=True)


def count_missing_coordinates() -> int:
    return FuelStation.objects.filter(missing_coordinates_q()).count()


def collect_address_groups(*, limit: int) -> tuple[list[AddressGroup], int]:
    """Collect up to ``limit`` unique incomplete-address groups from missing rows.

    Returns ``(groups, incomplete_row_count)``. Incomplete rows are counted but
    never sent to ORS.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    groups: dict[str, AddressGroup] = {}
    incomplete = 0
    queryset = (
        FuelStation.objects.filter(missing_coordinates_q())
        .only("id", "address", "city", "state", "latitude", "longitude")
        .order_by("id")
        .iterator(chunk_size=500)
    )

    for station in queryset:
        key = normalize_address_key(station.address, station.city, station.state)
        query = build_geocode_query(station.address, station.city, station.state)
        if key is None or query is None:
            incomplete += 1
            continue

        existing = groups.get(key)
        if existing is not None:
            existing.stations.append(station)
            continue
        if len(groups) >= limit:
            continue
        groups[key] = AddressGroup(key=key, query=query, stations=[station])

    return list(groups.values()), incomplete


def geocode_missing_stations(
    *,
    client: OpenRouteServiceClient | None = None,
    limit: int = 100,
    delay: float = 0.7,
    sleep: SleepFn = time.sleep,
    on_progress: ProgressCallback | None = None,
) -> GeocodeRunStats:
    """Geocode up to ``limit`` unique addresses that still lack coordinates."""
    client = client or OpenRouteServiceClient()
    groups, incomplete = collect_address_groups(limit=limit)
    stats = GeocodeRunStats(errors=incomplete)
    total = len(groups)

    for index, group in enumerate(groups, start=1):
        stats.attempted += 1
        label = group.query
        try:
            result = client.geocode(group.query, role="station")
        except LocationNotFoundError:
            stats.not_found += 1
            _emit(on_progress, f"[{index}/{total}] Not found: {label}")
        except OrsRateLimitError as exc:
            stats.errors += 1
            stats.stopped_early = "rate_limited"
            _emit(on_progress, f"[{index}/{total}] Rate limited: {exc.detail}")
            break
        except Exception as exc:  # noqa: BLE001 - keep batch running on provider errors
            stats.errors += 1
            _emit(on_progress, f"[{index}/{total}] Error: {label} ({exc})")
        else:
            updated = _apply_coordinates(group.stations, result.latitude, result.longitude)
            stats.succeeded += 1
            stats.rows_updated += updated
            _emit(on_progress, f"[{index}/{total}] Geocoded: {label}")

        if index < total and delay > 0 and stats.stopped_early is None:
            sleep(delay)

    stats.remaining_rows = count_missing_coordinates()
    return stats


def _apply_coordinates(stations: list[FuelStation], latitude: float, longitude: float) -> int:
    now = timezone.now()
    to_update: list[FuelStation] = []
    for station in stations:
        if station.latitude is not None and station.longitude is not None:
            continue
        station.latitude = latitude
        station.longitude = longitude
        station.updated_at = now
        to_update.append(station)

    if to_update:
        FuelStation.objects.bulk_update(to_update, ["latitude", "longitude", "updated_at"])
    return len(to_update)


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
