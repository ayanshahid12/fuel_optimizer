"""Route planning: resolve locations, fetch one ORS route, then choose fuel stops locally."""
from __future__ import annotations

from typing import Any

from fuel.services.fuel_optimizer import MAX_RANGE_MILES, VEHICLE, plan_fuel_stops
from fuel.services.location_geocoding import resolve_route_locations
from fuel.services.ors import OpenRouteServiceClient
from fuel.services.route_stations import find_stations_near_route


def plan_route(
    start: str,
    finish: str,
    *,
    client: OpenRouteServiceClient | None = None,
) -> dict[str, Any]:
    client = client or OpenRouteServiceClient()
    start_location, finish_location = resolve_route_locations(client, start, finish)
    route = client.get_route(start_location, finish_location)

    nearby = []
    if route.distance_miles > MAX_RANGE_MILES:
        nearby = find_stations_near_route(route.geometry.get("coordinates", []))
    plan = plan_fuel_stops(nearby, route_distance_miles=route.distance_miles)

    return {
        "start": start_location.to_dict(),
        "finish": finish_location.to_dict(),
        "route": route.to_dict(),
        "vehicle": VEHICLE,
        **plan.to_dict(),
    }
