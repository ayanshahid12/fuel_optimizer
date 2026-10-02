# Fuel Route API (assessment)

Django 6.1 + DRF + PostgreSQL API that plans a driving route between two US
locations and picks cost-effective fuel stops along it (500-mile range, 10 MPG),
using fuel prices from the provided CSV and OpenRouteService for geocoding and
directions.

## Prerequisites

- Docker
- Docker Compose
- An [OpenRouteService API key](https://openrouteservice.org/dev/#/signup)

## Environment setup

```bash
cp .env.example .env
# edit .env: set Django/Postgres credentials and ORS_API_KEY
```

Get a free key at https://openrouteservice.org/dev/#/signup and set:

```bash
ORS_API_KEY=your_key_here
```

## Start Docker

```bash
docker compose up --build
```

Django listens on http://localhost:8000. PostgreSQL is the `db` service and
is only reachable from the Compose network (host `db`, port `5432`).

## Run migrations

```bash
docker compose exec web python manage.py migrate
```

## Load Prepared Assessment Data

`fuel_optimization_data.sql` is a **data-only** PostgreSQL dump of the
prepared `fuel_station` rows (prices plus validated coordinates) and
`location_geocode` rows (cached start/finish geocodes). It is not a schema
dump, so **run the migrations above first**.

```bash
docker compose exec db psql -U fuel -d fuel -c \
"TRUNCATE TABLE fuel_station, location_geocode RESTART IDENTITY CASCADE;"

docker compose exec -T db psql -U fuel -d fuel \
  < fuel_optimization_data.sql
```

- `TRUNCATE` removes any existing rows so the load does not fail with
  duplicate primary-key errors.
- The dump saves you from re-geocoding thousands of stations, which would cost
  several days of free-tier OpenRouteService credits.
- OpenRouteService is still required at request time: for start/finish
  geocoding when a location is not already cached, and for route directions.

### Alternative: build the data from the CSV

Only needed if you want to regenerate the data instead of loading the dump.

```bash
docker compose exec web \
  python manage.py import_fuel_prices \
  data/fuel-prices-for-be-assessment.csv
```

OPIS Truckstop ID is not unique in the source file, so every valid CSV row is
stored. Re-running the command replaces all existing fuel station rows with
the current file contents (deterministic full reload).

Station coordinates are filled separately (not during `/api/routes/`) so routing
stays fast. The command is resumable and deduplicates by `address + city + state`
so identical locations use one ORS request:

```bash
docker compose exec web \
  python manage.py geocode_fuel_stations --limit 2800 --delay 0.7
```

Each request asks for up to 5 candidates. A candidate is accepted only if it is
in the USA and its state matches the station's `state` (abbreviation or full
name, case-insensitive); a matching city is preferred but not required. If no
candidate qualifies, the station stays ungeocoded and is reported, for example
`Rejected geocode: Gillette, WY | returned region: Ohio`. Stations with
non-US state codes (Canadian provinces in the CSV) are skipped without a request.

Every row records a `geocode_status` (`pending`, `valid`, `rejected`,
`not_found`, `skipped`), so processed addresses are not requested again.
Coordinates stored before validation existed can be repaired with
`--revalidate`, which keeps coordinates only when the state matches.

ORS geocoding limits on the free tier are roughly **100 requests/minute** and
**3000/day**. Default `--delay 0.7` stays under the rate limit. Prefer modest
`--limit` values so route requests still have quota left.

Check how many stations still lack coordinates:

```bash
docker compose exec db \
  psql -U fuel -d fuel \
  -c "SELECT count(*) FROM fuel_station WHERE latitude IS NULL OR longitude IS NULL;"
```

## Run tests

Tests run against PostgreSQL inside Docker (no SQLite). OpenRouteService HTTP
calls are mocked; tests do not require a real `ORS_API_KEY`.

```bash
docker compose exec web python manage.py test
```

## Use the API

`POST /api/routes/` geocodes the start and finish locations (Pelias on
`api.heigit.org`), fetches one `driving-car` GeoJSON route from
OpenRouteService (`https://api.heigit.org/openrouteservice/v2/...`), and
returns the cheapest fuel stops for the trip.

Request body:

| Field | Type | Description |
|---|---|---|
| `start` | string | US start location, e.g. `"New York, NY"` |
| `finish` | string | US finish location, e.g. `"Chicago, IL"` |

## Example POST /api/routes/ request

```bash
curl -X POST http://localhost:8000/api/routes/ \
  -H "Content-Type: application/json" \
  -d '{
    "start": "New York, NY",
    "finish": "Chicago, IL"
  }'
```

Example response:

```json
{
  "start": {
    "input": "New York, NY",
    "label": "New York, NY, USA",
    "latitude": 40.7128,
    "longitude": -74.006
  },
  "finish": {
    "input": "Chicago, IL",
    "label": "Chicago, IL, USA",
    "latitude": 41.8781,
    "longitude": -87.6298
  },
  "route": {
    "distance_miles": 790.38,
    "duration_seconds": 45000.0,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-74.006, 40.7128], [-87.6298, 41.8781]]
    }
  },
  "vehicle": {
    "maximum_range_miles": 500,
    "fuel_efficiency_mpg": 10,
    "tank_capacity_gallons": 50,
    "starting_tank": "full"
  },
  "fuel_stops": [
    {
      "opis_truckstop_id": 72445,
      "truckstop_name": "SHEETZ #639",
      "address": "I-80 Exit 223",
      "city": "Youngstown",
      "state": "OH",
      "latitude": 41.098763,
      "longitude": -80.652215,
      "price_per_gallon": "3.059",
      "distance_from_start_miles": 395.48,
      "distance_from_route_miles": 3.7,
      "gallons_purchased": "29.04",
      "fuel_cost": "88.83"
    }
  ],
  "total_fuel_purchased_gallons": "29.04",
  "total_fuel_cost": "88.83"
}
```

Routes of 500 miles or less return `"fuel_stops": []` and totals of `"0.00"`.
If any stretch of the route has no usable station within 500 miles, the API
returns HTTP 422:

```json
{"error": {"code": "route_infeasible", "detail": "No fuel station between mile 120.4 and mile 655.1; the 534.7-mile gap exceeds the 500-mile range."}}
```

Other errors use the same `{"error": {"code", "detail"}}` shape, for example
`location_not_found` (404), `ors_timeout` (504) and `ors_request_failed` (502).

## Architecture and assumptions

### Request flow

1. Validate `start` and `finish`.
2. Resolve both locations from the `location_geocode` cache; cache misses are
   geocoded by ORS concurrently.
3. Fetch one driving route from ORS.
4. Match nearby fuel stations to the route in PostgreSQL + Shapely.
5. Choose fuel stops with the greedy optimizer.

One request makes at most three ORS calls (two geocodes, one directions call),
and only the directions call when both locations are cached.

### Location cache

Start and finish geocodes are cached in PostgreSQL (`location_geocode`), keyed
by the normalized input (case-folded, whitespace and commas standardized), so a
repeated location costs no ORS geocoding call. Failed lookups are not cached.
Cache misses for start and finish are geocoded concurrently; the directions
request waits for both.

### Matching stations to the route

Only stations with a validated geocode (`geocode_status = valid`) are
considered: those within `NEARBY_STATION_MAX_DISTANCE_MILES` (default `10`)
of the route, ordered by `distance_from_start_miles`.

- The route's bounding box, widened by `NEARBY_STATION_BBOX_MARGIN_MILES`
  (defaults to the distance threshold), prefilters stations in one SQL query.
- Route and stations are projected to an equal-area planar CRS (CONUS Albers)
  and measured with Shapely; error is about 1% across the contiguous US.
- `distance_from_route_miles` is the shortest distance to the route line.
- `distance_from_start_miles` is the distance along the route path to the
  point nearest the station (not a straight line from the start).
- Station positions are clamped to the ORS route distance, because the
  projected route line can be about 0.5% shorter.

### Fuel-stop optimization

- The vehicle has a 500-mile range at 10 MPG (a 50-gallon tank) and **starts
  with a full tank**. The cost of that starting fuel is **not included**,
  because no starting price is known.
- The CSV can list the same physical station several times with different
  prices. Rows sharing OPIS ID, address, city and state become one candidate at
  the **minimum listed retail price**. Source rows in PostgreSQL are never
  modified.
- No leg (start to first stop, stop to stop, last stop to destination) may
  exceed 500 miles; exactly 500 is allowed.
- Greedy strategy at each station: if a cheaper station is reachable within
  500 miles, buy only enough fuel to reach the first one. Otherwise, fill the
  tank, or buy just enough to finish if the destination is in range. Then
  continue to the cheapest station in range. No fuel is bought once the
  destination is reachable.
- Fuel stays between 0 and 50 gallons. Money uses `Decimal`; each stop's cost
  is rounded to cents, and `total_fuel_cost` is the sum of stop costs.
- No routing or geocoding calls are made after the initial route is fetched.
  Station matching and optimization use only PostgreSQL and local math.

## Known limitations

- Station coordinates come from ORS geocoding of the CSV address text, which is
  often a highway exit (e.g. `I-80, EXIT 223`). A geocode is accepted only if
  it is in the USA and in the station's own state. Validation is **state-level
  only**, so some accepted coordinates sit in the right state but the wrong
  town. Such a station can look closer to, or farther from, the route than it
  really is.
- Stations whose address could not be resolved in the correct state
  (`rejected`, `not_found`) or that are outside the USA (`skipped`, the
  Canadian rows in the CSV) have no coordinates and are never used. With the
  prepared dataset, 6,717 of 8,151 rows are usable.
- Geocodes are not refreshed automatically; re-running geocoding costs ORS
  quota.
- Distances along the route are measured to each station's nearest point on
  the route; the detour from the highway to the station (up to 10 miles) is not
  added to fuel use.
- The optimizer minimizes fuel cost only; it does not account for the number
  of stops, so a plan can include small top-ups.

## Stop and reset

```bash
docker compose down
```

Database data is kept in the named volume `postgres_data` across normal
`down` / `up` cycles. To start from an empty database:

```bash
docker compose down -v
docker compose up --build
docker compose exec web python manage.py migrate
```

`-v` deletes the Postgres volume; reload the prepared data afterwards.
