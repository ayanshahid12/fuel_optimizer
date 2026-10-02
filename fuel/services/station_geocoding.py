"""Geocode FuelStation rows with country and state validation.

Uses the shared OpenRouteService client. Duplicate physical addresses share one
ORS request; source CSV rows are never merged or deleted. Coordinates are only
stored when an ORS candidate is in the USA and in the station's own state.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.db.models import Q
from django.utils import timezone

from fuel.exceptions import OrsRateLimitError
from fuel.models import FuelStation
from fuel.services.ors import GeocodeCandidate, OpenRouteServiceClient
from fuel.services.us_states import normalize_us_state

ProgressCallback = Callable[[str], None]
SleepFn = Callable[[float], None]

Status = FuelStation.GeocodeStatus

# Pelias returns several candidates per request at no extra quota cost.
GEOCODE_CANDIDATES = 5

_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_USA_CODES = {"USA", "US"}
_USA_NAMES = {"united states", "united states of america"}
_STATION_FIELDS = ("id", "address", "city", "state", "latitude", "longitude", "geocode_status", "updated_at")


@dataclass
class AddressGroup:
    key: str
    query: str
    city: str
    state: str
    stations: list[FuelStation] = field(default_factory=list)


@dataclass
class GeocodeBatch:
    groups: list[AddressGroup]
    non_us_groups: list[AddressGroup]
    incomplete: list[FuelStation]


@dataclass(frozen=True)
class CandidateChoice:
    accepted: GeocodeCandidate | None
    rejection: str | None
    returned_regions: tuple[str, ...]


@dataclass
class GeocodeRunStats:
    requests: int = 0
    valid: int = 0
    rejected_wrong_state: int = 0
    rejected_outside_us: int = 0
    not_found: int = 0
    skipped_non_us_state: int = 0
    skipped_incomplete: int = 0
    errors: int = 0
    rows_updated: int = 0
    rows_cleared: int = 0
    remaining_rows: int = 0
    unvalidated_rows: int = 0
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


def count_unvalidated_coordinates() -> int:
    """Rows holding coordinates that predate state validation."""
    return (
        FuelStation.objects.filter(geocode_status=Status.PENDING)
        .exclude(missing_coordinates_q())
        .count()
    )


def is_usa(candidate: GeocodeCandidate) -> bool:
    if candidate.country_code.strip().upper() in _USA_CODES:
        return True
    return candidate.country.strip().lower() in _USA_NAMES


def candidate_state(candidate: GeocodeCandidate) -> str | None:
    return normalize_us_state(candidate.region_code) or normalize_us_state(candidate.region)


def choose_candidate(candidates: Iterable[GeocodeCandidate], *, state: str, city: str) -> CandidateChoice:
    """Pick the first US candidate in ``state``, preferring one whose locality matches ``city``.

    City is only a tie-breaker: highway exits often sit in a neighbouring municipality.
    """
    candidates = list(candidates)
    expected = normalize_us_state(state)
    in_usa = [c for c in candidates if is_usa(c)]
    in_state = [c for c in in_usa if expected is not None and candidate_state(c) == expected]

    if in_state:
        city_key = _city_key(city)
        accepted = next((c for c in in_state if city_key and _city_key(c.locality) == city_key), in_state[0])
        return CandidateChoice(accepted=accepted, rejection=None, returned_regions=())

    regions = tuple(dict.fromkeys(_describe_region(c) for c in candidates))
    rejection = "wrong_state" if in_usa else "outside_us"
    return CandidateChoice(accepted=None, rejection=rejection, returned_regions=regions)


def collect_address_groups(*, limit: int, revalidate: bool = False) -> GeocodeBatch:
    """Group pending rows by physical address.

    Normal mode selects addresses with rows lacking coordinates; ``revalidate``
    selects addresses with rows holding unvalidated coordinates. Every pending
    row sharing a selected address is included, so one request covers them all.
    Only addresses that need an ORS request count toward ``limit``.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    groups: dict[str, AddressGroup] = {}
    incomplete: list[FuelStation] = []
    queryset = (
        FuelStation.objects.filter(geocode_status=Status.PENDING)
        .only(*_STATION_FIELDS)
        .order_by("id")
        .iterator(chunk_size=500)
    )
    for station in queryset:
        key = normalize_address_key(station.address, station.city, station.state)
        query = build_geocode_query(station.address, station.city, station.state)
        if key is None or query is None:
            if _selected(station, revalidate):
                incomplete.append(station)
            continue
        group = groups.get(key)
        if group is None:
            group = groups[key] = AddressGroup(
                key=key,
                query=query,
                city=normalize_whitespace(station.city),
                state=normalize_whitespace(station.state),
            )
        group.stations.append(station)

    selected = [g for g in groups.values() if any(_selected(s, revalidate) for s in g.stations)]
    non_us = [g for g in selected if normalize_us_state(g.state) is None]
    requestable = [g for g in selected if normalize_us_state(g.state) is not None]
    return GeocodeBatch(groups=requestable[:limit], non_us_groups=non_us, incomplete=incomplete)


def geocode_stations(
    *,
    client: OpenRouteServiceClient | None = None,
    limit: int = 100,
    delay: float = 0.7,
    revalidate: bool = False,
    sleep: SleepFn = time.sleep,
    on_progress: ProgressCallback | None = None,
) -> GeocodeRunStats:
    """Geocode up to ``limit`` unique addresses, storing only state-validated coordinates."""
    client = client or OpenRouteServiceClient()
    batch = collect_address_groups(limit=limit, revalidate=revalidate)
    stats = GeocodeRunStats()

    if batch.incomplete:
        stats.skipped_incomplete = len(batch.incomplete)
        stats.rows_cleared += _save(batch.incomplete, Status.SKIPPED)
        _emit(on_progress, f"Skipped {len(batch.incomplete)} row(s) with incomplete addresses.")

    if batch.non_us_groups:
        stats.skipped_non_us_state = len(batch.non_us_groups)
        for group in batch.non_us_groups:
            stats.rows_cleared += _save(group.stations, Status.SKIPPED)
        codes = ", ".join(sorted({g.state.upper() for g in batch.non_us_groups}))
        _emit(
            on_progress,
            f"Skipped {len(batch.non_us_groups)} address(es) with non-US state codes ({codes}).",
        )

    total = len(batch.groups)
    for index, group in enumerate(batch.groups, start=1):
        if stats.requests and delay > 0:
            sleep(delay)
        stats.requests += 1
        prefix = f"[{index}/{total}]"
        try:
            candidates = client.search_candidates(group.query, size=GEOCODE_CANDIDATES)
        except OrsRateLimitError as exc:
            stats.errors += 1
            stats.stopped_early = "rate_limited"
            _emit(on_progress, f"{prefix} Rate limited: {exc.detail}")
            break
        except Exception as exc:  # noqa: BLE001 - keep batch running on provider errors
            stats.errors += 1
            _emit(on_progress, f"{prefix} Error: {group.query} ({exc})")
            continue

        if not candidates:
            stats.not_found += 1
            stats.rows_cleared += _save(group.stations, Status.NOT_FOUND)
            _emit(on_progress, f"{prefix} Not found: {group.query}")
            continue

        choice = choose_candidate(candidates, state=group.state, city=group.city)
        if choice.accepted is None:
            if choice.rejection == "outside_us":
                stats.rejected_outside_us += 1
            else:
                stats.rejected_wrong_state += 1
            stats.rows_cleared += _save(group.stations, Status.REJECTED)
            _emit(
                on_progress,
                f"{prefix} Rejected geocode: {group.city}, {group.state} | "
                f"returned region: {', '.join(choice.returned_regions)}",
            )
            continue

        stats.valid += 1
        stats.rows_updated += len(group.stations)
        _save(group.stations, Status.VALID, choice.accepted)
        _emit(on_progress, f"{prefix} Valid: {group.query} -> {choice.accepted.label}")

    stats.remaining_rows = count_missing_coordinates()
    stats.unvalidated_rows = count_unvalidated_coordinates()
    return stats


def _selected(station: FuelStation, revalidate: bool) -> bool:
    return station.has_coordinates if revalidate else not station.has_coordinates


def _save(
    stations: list[FuelStation],
    status: str,
    candidate: GeocodeCandidate | None = None,
) -> int:
    """Persist status (and coordinates when accepted); return rows whose coordinates were cleared."""
    now = timezone.now()
    cleared = 0
    for station in stations:
        if candidate is None and station.has_coordinates:
            cleared += 1
        station.latitude = candidate.latitude if candidate else None
        station.longitude = candidate.longitude if candidate else None
        station.geocode_status = status
        station.updated_at = now
    FuelStation.objects.bulk_update(
        stations, ["latitude", "longitude", "geocode_status", "updated_at"]
    )
    return cleared


def _city_key(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())


def _describe_region(candidate: GeocodeCandidate) -> str:
    region = candidate.region or candidate.region_code or "unknown region"
    if is_usa(candidate):
        return region
    country = candidate.country or candidate.country_code or "unknown country"
    return f"{region}, {country}"


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
