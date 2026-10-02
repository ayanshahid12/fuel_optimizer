from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fuel.exceptions import RouteInfeasibleError
from fuel.services.route_stations import NearbyFuelStation

MAX_RANGE_MILES = Decimal("500")
FUEL_EFFICIENCY_MPG = Decimal("10")
TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / FUEL_EFFICIENCY_MPG

VEHICLE = {
    "maximum_range_miles": int(MAX_RANGE_MILES),
    "fuel_efficiency_mpg": int(FUEL_EFFICIENCY_MPG),
    "tank_capacity_gallons": int(TANK_CAPACITY_GALLONS),
    "starting_tank": "full",
}

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class FuelCandidate:
    """One physical station, priced at the minimum of its source rows."""

    opis_truckstop_id: int
    truckstop_name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal
    distance_from_start_miles: Decimal
    distance_from_route_miles: float


@dataclass(frozen=True)
class FuelStop:
    station: FuelCandidate
    gallons_purchased: Decimal
    fuel_on_arrival_gallons: Decimal
    fuel_after_purchase_gallons: Decimal

    @property
    def fuel_cost(self) -> Decimal:
        return _money(self.gallons_purchased * self.station.price_per_gallon)

    def to_dict(self) -> dict[str, Any]:
        station = self.station
        return {
            "opis_truckstop_id": station.opis_truckstop_id,
            "truckstop_name": station.truckstop_name,
            "address": station.address,
            "city": station.city,
            "state": station.state,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "price_per_gallon": format(station.price_per_gallon.normalize(), "f"),
            "distance_from_start_miles": float(station.distance_from_start_miles),
            "distance_from_route_miles": station.distance_from_route_miles,
            "gallons_purchased": str(_money(self.gallons_purchased)),
            "fuel_cost": str(self.fuel_cost),
        }


@dataclass(frozen=True)
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)

    @property
    def total_gallons(self) -> Decimal:
        return sum((_money(stop.gallons_purchased) for stop in self.stops), ZERO).quantize(CENT)

    @property
    def total_cost(self) -> Decimal:
        return sum((stop.fuel_cost for stop in self.stops), ZERO).quantize(CENT)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fuel_stops": [stop.to_dict() for stop in self.stops],
            "total_fuel_purchased_gallons": str(self.total_gallons),
            "total_fuel_cost": str(self.total_cost),
        }


def deduplicate_stations(
    stations: Iterable[NearbyFuelStation],
    *,
    route_distance_miles: Decimal | None = None,
) -> list[FuelCandidate]:
    """Collapse rows of the same physical station to one candidate at its minimum price.

    Physical identity is OPIS ID + address + city + state (case/whitespace-insensitive).
    Progress is clamped to ``route_distance_miles`` when given. Result is ordered by
    progress, then price.
    """
    best: dict[tuple, NearbyFuelStation] = {}
    for station in stations:
        key = (
            station.opis_truckstop_id,
            _norm(station.address),
            _norm(station.city),
            _norm(station.state),
        )
        current = best.get(key)
        if current is None or station.retail_price < current.retail_price:
            best[key] = station

    candidates = []
    for station in best.values():
        progress = max(_decimal(station.distance_from_start_miles), ZERO)
        if route_distance_miles is not None:
            progress = min(progress, route_distance_miles)
        candidates.append(
            FuelCandidate(
                opis_truckstop_id=station.opis_truckstop_id,
                truckstop_name=station.truckstop_name,
                address=station.address,
                city=station.city,
                state=station.state,
                latitude=station.latitude,
                longitude=station.longitude,
                price_per_gallon=station.retail_price,
                distance_from_start_miles=progress,
                distance_from_route_miles=station.distance_from_route_miles,
            )
        )
    candidates.sort(key=lambda c: (c.distance_from_start_miles, c.price_per_gallon, c.opis_truckstop_id))
    return candidates


def check_feasibility(candidates: Sequence[FuelCandidate], route_distance_miles: Decimal) -> None:
    """Raise ``RouteInfeasibleError`` if any leg between refuelling points exceeds the range."""
    if route_distance_miles <= MAX_RANGE_MILES:
        return
    if not candidates:
        raise RouteInfeasibleError(
            f"No fuel stations were found near the route, and the {route_distance_miles}-mile "
            f"trip exceeds the {MAX_RANGE_MILES}-mile range."
        )

    first = candidates[0].distance_from_start_miles
    if first > MAX_RANGE_MILES:
        raise RouteInfeasibleError(
            f"The first fuel station near the route is at mile {first}, beyond the "
            f"{MAX_RANGE_MILES}-mile range of a full tank."
        )
    for previous, following in zip(candidates, candidates[1:]):
        gap = following.distance_from_start_miles - previous.distance_from_start_miles
        if gap > MAX_RANGE_MILES:
            raise RouteInfeasibleError(
                f"No fuel station between mile {previous.distance_from_start_miles} and mile "
                f"{following.distance_from_start_miles}; the {gap}-mile gap exceeds the "
                f"{MAX_RANGE_MILES}-mile range."
            )
    last = candidates[-1].distance_from_start_miles
    if route_distance_miles - last > MAX_RANGE_MILES:
        raise RouteInfeasibleError(
            f"The destination is {route_distance_miles - last} miles past the last fuel station "
            f"(mile {last}), exceeding the {MAX_RANGE_MILES}-mile range."
        )


def plan_fuel_stops(
    stations: Iterable[NearbyFuelStation],
    *,
    route_distance_miles: float | Decimal,
) -> FuelPlan:
    """Return the cheapest set of fuel purchases for the route."""
    total = _decimal(route_distance_miles)
    if total <= MAX_RANGE_MILES:
        return FuelPlan()

    candidates = deduplicate_stations(stations, route_distance_miles=total)
    check_feasibility(candidates, total)

    stops: list[FuelStop] = []
    position = ZERO
    fuel = TANK_CAPACITY_GALLONS
    current: int | None = None

    while (total - position) / FUEL_EFFICIENCY_MPG > fuel:
        start_index = 0 if current is None else current + 1
        reachable = [
            index
            for index in range(start_index, len(candidates))
            if candidates[index].distance_from_start_miles - position <= MAX_RANGE_MILES
        ]

        if current is None:
            # We are at the trip start, so use the full starting tank before buying fuel.
            if not reachable:
                raise RouteInfeasibleError("No fuel station is reachable from the start.")

            # Move to the first reachable station without purchasing fuel.
            target = reachable[0]
            purchase = ZERO

        else:
            here = candidates[current]

            # Look ahead for the first reachable station with a cheaper fuel price.
            cheaper = next(
                (i for i in reachable if candidates[i].price_per_gallon < here.price_per_gallon),
                None,
            )

            if cheaper is not None:
                # Buy only enough fuel to reach the first cheaper station.
                target = cheaper
                needed = (
                    candidates[target].distance_from_start_miles - position
                ) / FUEL_EFFICIENCY_MPG
                purchase = max(needed - fuel, ZERO)

            elif total - position <= MAX_RANGE_MILES:
                # No cheaper stop is needed because the destination is within tank range.
                target = None
                purchase = (total - position) / FUEL_EFFICIENCY_MPG - fuel

            elif reachable:
                # No cheaper station is reachable, so fill up and head to the cheapest reachable option.
                target = min(
                    reachable,
                    key=lambda i: (
                        candidates[i].price_per_gallon,
                        -candidates[i].distance_from_start_miles,
                    ),
                )
                purchase = TANK_CAPACITY_GALLONS - fuel

            else:
                # The trip cannot continue because no station is reachable within 500 miles.
                raise RouteInfeasibleError(
                    f"No fuel station is reachable after mile {position}."
                )

            if purchase > ZERO:
                # Record the refueling stop and update the fuel currently in the tank.
                stops.append(
                    FuelStop(
                        station=here,
                        gallons_purchased=purchase,
                        fuel_on_arrival_gallons=fuel,
                        fuel_after_purchase_gallons=fuel + purchase,
                    )
                )
                fuel += purchase

            if target is None:
                # We now have enough fuel to reach the destination, so stop planning.
                break

        # Move the vehicle to the selected next station.
        next_position = candidates[target].distance_from_start_miles

        # Consume fuel based on the miles driven to the next station.
        fuel -= (next_position - position) / FUEL_EFFICIENCY_MPG

        # Update the vehicle's current route position.
        position = next_position

        # Mark the selected station as the current station for the next iteration.
        current = target

        # Sanity-check that fuel remains between 0 and tank capacity.
        _check_tank(fuel)

    return FuelPlan(stops=stops)


def _check_tank(fuel: Decimal) -> None:
    if fuel < ZERO or fuel > TANK_CAPACITY_GALLONS:
        raise ArithmeticError(f"Fuel level {fuel} outside 0..{TANK_CAPACITY_GALLONS} gallons.")


def _decimal(value: float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _norm(value: str) -> str:
    return " ".join(value.split()).lower()
