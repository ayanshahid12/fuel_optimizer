"""Find geocoded fuel stations near a route polyline, ordered by route progress.

Coordinates are projected to a spherical Albers equal-area conic using the
standard CONUS parameters (EPSG:5070: standard parallels 29.5°N and 45.5°N,
origin 23°N 96°W). Distance error stays within ~1.5% across the lower 48, so
planar meters in that space approximate ground distance. GeoJSON coordinates
are [longitude, latitude].
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import numpy as np
import shapely
from django.conf import settings

from fuel.models import FuelStation

EARTH_RADIUS_M = 6_371_008.8
# Shortest length of one degree of latitude; keeps the bounding-box margin conservative.
MIN_MILES_PER_DEGREE = 68.7

_PHI1 = math.radians(29.5)
_PHI2 = math.radians(45.5)
_PHI0 = math.radians(23.0)
_LAMBDA0 = math.radians(-96.0)
_N = (math.sin(_PHI1) + math.sin(_PHI2)) / 2
_C = math.cos(_PHI1) ** 2 + 2 * _N * math.sin(_PHI1)
_RHO0 = EARTH_RADIUS_M * math.sqrt(_C - 2 * _N * math.sin(_PHI0)) / _N

_STATION_FIELDS = (
    "id",
    "opis_truckstop_id",
    "truckstop_name",
    "address",
    "city",
    "state",
    "retail_price",
    "latitude",
    "longitude",
)

BoundingBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class NearbyFuelStation:
    fuel_station_id: int
    opis_truckstop_id: int
    truckstop_name: str
    address: str
    city: str
    state: str
    retail_price: Decimal
    latitude: float
    longitude: float
    distance_from_route_miles: float
    distance_from_start_miles: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["retail_price"] = format(self.retail_price.normalize(), "f")
        return data


def project_lon_lat(lon_lat: np.ndarray) -> np.ndarray:
    """Project an (N, 2) array of [longitude, latitude] degrees to Albers meters."""
    lon = np.radians(lon_lat[:, 0])
    lat = np.radians(lon_lat[:, 1])
    rho = EARTH_RADIUS_M * np.sqrt(_C - 2 * _N * np.sin(lat)) / _N
    theta = _N * (lon - _LAMBDA0)
    return np.column_stack((rho * np.sin(theta), _RHO0 - rho * np.cos(theta)))


def route_bounding_box(lon_lat: np.ndarray, margin_miles: float) -> BoundingBox:
    """Return ``(min_lon, min_lat, max_lon, max_lat)`` expanded by ``margin_miles``."""
    min_lon, min_lat = (float(v) for v in lon_lat.min(axis=0))
    max_lon, max_lat = (float(v) for v in lon_lat.max(axis=0))
    lat_margin = margin_miles / MIN_MILES_PER_DEGREE
    widest_lat = min(max(abs(min_lat), abs(max_lat)) + lat_margin, 89.0)
    lon_margin = margin_miles / (MIN_MILES_PER_DEGREE * math.cos(math.radians(widest_lat)))
    return (
        min_lon - lon_margin,
        min_lat - lat_margin,
        max_lon + lon_margin,
        max_lat + lat_margin,
    )


def stations_in_bounding_box(bbox: BoundingBox):
    min_lon, min_lat, max_lon, max_lat = bbox
    return FuelStation.objects.filter(
        geocode_status=FuelStation.GeocodeStatus.VALID,
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__range=(min_lat, max_lat),
        longitude__range=(min_lon, max_lon),
    ).values(*_STATION_FIELDS)


def find_stations_near_route(
    coordinates: Sequence[Sequence[float]],
    *,
    max_distance_miles: float | None = None,
    bbox_margin_miles: float | None = None,
) -> list[NearbyFuelStation]:
    """Return geocoded stations within ``max_distance_miles`` of the route.

    ``distance_from_start_miles`` is measured along the route path to the
    station's nearest point on the route.
    """
    if max_distance_miles is None:
        max_distance_miles = settings.NEARBY_STATION_MAX_DISTANCE_MILES
    if bbox_margin_miles is None:
        bbox_margin_miles = settings.NEARBY_STATION_BBOX_MARGIN_MILES

    lon_lat = _route_array(coordinates)
    if lon_lat is None:
        return []

    candidates = list(stations_in_bounding_box(route_bounding_box(lon_lat, bbox_margin_miles)))
    if not candidates:
        return []

    route_line = shapely.linestrings(project_lon_lat(lon_lat))
    shapely.prepare(route_line)
    station_lon_lat = np.array([[c["longitude"], c["latitude"]] for c in candidates], dtype=float)
    points = shapely.points(project_lon_lat(station_lon_lat))

    meters_per_mile = settings.METERS_PER_MILE
    near = shapely.dwithin(route_line, points, max_distance_miles * meters_per_mile)
    if not near.any():
        return []

    near_points = points[near]
    offsets_m = shapely.distance(route_line, near_points)
    progress_m = shapely.line_locate_point(route_line, near_points)
    near_candidates = [c for c, keep in zip(candidates, near) if keep]

    ranked = sorted(
        zip(progress_m.tolist(), offsets_m.tolist(), near_candidates),
        key=lambda item: (item[0], item[2]["id"]),
    )
    return [
        NearbyFuelStation(
            fuel_station_id=station["id"],
            opis_truckstop_id=station["opis_truckstop_id"],
            truckstop_name=station["truckstop_name"],
            address=station["address"],
            city=station["city"],
            state=station["state"],
            retail_price=station["retail_price"],
            latitude=station["latitude"],
            longitude=station["longitude"],
            distance_from_route_miles=round(offset / meters_per_mile, 2),
            distance_from_start_miles=round(progress / meters_per_mile, 2),
        )
        for progress, offset, station in ranked
    ]


def _route_array(coordinates: Sequence[Sequence[float]]) -> np.ndarray | None:
    try:
        array = np.asarray(coordinates, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.ndim != 2 or array.shape[0] < 2 or array.shape[1] < 2:
        return None
    return array[:, :2]
