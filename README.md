# Fuel Route API (assessment)

Django 6.1 + DRF + PostgreSQL scaffolding for a USA fuel-stop route planner.
Step 1 covers project setup, `FuelStation`, CSV import, Docker, and tests.
Step 2 adds geocoding and driving directions via OpenRouteService.
Fuel-stop matching and cost optimization are not implemented yet.

## Prerequisites

- Docker
- Docker Compose
- An [OpenRouteService API key](https://openrouteservice.org/dev/#/signup)

## Setup

```bash
cp .env.example .env
# edit .env: set Django/Postgres credentials and ORS_API_KEY
```

Get a free key at https://openrouteservice.org/dev/#/signup and set:

```bash
ORS_API_KEY=your_key_here
```

## Start

```bash
docker compose up --build
```

Django listens on http://localhost:8000. PostgreSQL is the `db` service and
is only reachable from the Compose network (host `db`, port `5432`).

## Migrate

```bash
docker compose exec web python manage.py migrate
```

## Import fuel prices

The CSV lives at `data/fuel-prices-for-be-assessment.csv` and is available
inside the container through the project bind mount (it is not baked into the
image).

```bash
docker compose exec web \
  python manage.py import_fuel_prices \
  data/fuel-prices-for-be-assessment.csv
```

OPIS Truckstop ID is not unique in the source file, so every valid CSV row is
stored. Re-running the command replaces all existing fuel station rows with
the current file contents (deterministic full reload).

## Geocode fuel stations

Station coordinates are filled separately (not during `/api/routes/`) so routing
stays fast. The command is resumable and deduplicates by `address + city + state`
so identical locations use one ORS request.

ORS geocoding limits on the free tier are roughly **100 requests/minute** and
**3000/day**. Default `--delay 0.7` stays under the rate limit. Prefer modest
`--limit` values (for example `2800`) so route requests still have quota left.

```bash
# 10 unique addresses
docker compose exec web \
  python manage.py geocode_fuel_stations --limit 10

# 100 unique addresses
docker compose exec web \
  python manage.py geocode_fuel_stations --limit 100 --delay 0.7

# Larger daily batch (leave headroom for routing)
docker compose exec web \
  python manage.py geocode_fuel_stations --limit 2800 --delay 0.7
```

Check how many stations still lack coordinates:

```bash
docker compose exec db \
  psql -U fuel -d fuel \
  -c "SELECT count(*) FROM fuel_station WHERE latitude IS NULL OR longitude IS NULL;"
```

## Route API

`POST /api/routes/` geocodes the start and finish locations (Pelias on
`api.heigit.org`), then returns a `driving-car` GeoJSON route from
OpenRouteService (`https://api.heigit.org/openrouteservice/v2/...`).

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
  }
}
```

## Tests

Tests run against PostgreSQL inside Docker (no SQLite). OpenRouteService HTTP
calls are mocked; tests do not require a real `ORS_API_KEY`.

```bash
docker compose exec web python manage.py test
```

## Stop

```bash
docker compose down
```

Database data is kept in the named volume `postgres_data` across normal
`down` / `up` cycles.

## Reset the database

```bash
docker compose down -v
docker compose up --build
docker compose exec web python manage.py migrate
```

`-v` deletes the Postgres volume. Recreate schema and re-import data afterward.
