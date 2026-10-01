"""Parse the OPIS fuel price CSV and replace ``FuelStation`` rows.

The source feed is not unique on OPIS Truckstop ID, and we do not have enough
metadata to interpret duplicate IDs. Every valid CSV row is kept as its own
record. Re-import clears the table and reloads the file so results stay
deterministic without inventing a natural key.
"""
import csv
from collections.abc import Iterable
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import FuelStation

COLUMN_TO_FIELD: dict[str, str] = {
    "OPIS Truckstop ID": "opis_truckstop_id",
    "Truckstop Name": "truckstop_name",
    "Address": "address",
    "City": "city",
    "State": "state",
    "Rack ID": "rack_id",
    "Retail Price": "retail_price",
}
FIELD_TO_COLUMN: dict[str, str] = {field: column for column, field in COLUMN_TO_FIELD.items()}


class MissingHeadersError(ValueError):
    pass


@dataclass(frozen=True)
class RowError:
    row_number: int
    reason: str


@dataclass(frozen=True)
class ParseResult:
    stations: list[FuelStation]
    errors: list[RowError]
    rows_processed: int


@dataclass(frozen=True)
class ReplaceResult:
    deleted: int
    created: int


def parse_fuel_prices(lines: Iterable[str]) -> ParseResult:
    """Validate CSV rows into unsaved ``FuelStation`` instances.

    Row numbers are 1-based and count the header as row 1, matching how the
    file looks in a spreadsheet. Malformed rows are skipped; valid rows are
    kept even when OPIS Truckstop IDs repeat.
    """
    reader = csv.DictReader(lines)
    headers = [header.strip() for header in reader.fieldnames or []]
    missing = [column for column in COLUMN_TO_FIELD if column not in headers]
    if missing:
        raise MissingHeadersError(f"Missing required column(s): {', '.join(missing)}")
    reader.fieldnames = headers

    stations: list[FuelStation] = []
    errors: list[RowError] = []
    rows_processed = 0

    for row_number, row in enumerate(reader, start=2):
        rows_processed += 1
        try:
            stations.append(_build_station(row))
        except ValidationError as exc:
            errors.append(RowError(row_number, _format_error(exc)))

    return ParseResult(stations=stations, errors=errors, rows_processed=rows_processed)


@transaction.atomic
def replace_fuel_stations(stations: Iterable[FuelStation], batch_size: int = 1000) -> ReplaceResult:
    """Replace all fuel stations with the provided rows.

    Latitude and longitude remain null on import; geocoding is a later step.
    """
    station_list = list(stations)
    deleted, _ = FuelStation.objects.all().delete()
    FuelStation.objects.bulk_create(station_list, batch_size=batch_size)
    return ReplaceResult(deleted=deleted, created=len(station_list))


def _build_station(row: dict[str | None, str | None]) -> FuelStation:
    values = {field: (row.get(column) or "").strip() for column, field in COLUMN_TO_FIELD.items()}
    missing = [FIELD_TO_COLUMN[field] for field, value in values.items() if not value]
    if missing:
        raise ValidationError(f"Missing value for {', '.join(missing)}")

    values["state"] = values["state"].upper()
    station = FuelStation(**values)
    # Field validators cover malformed values; CheckConstraints are enforced on insert.
    station.full_clean(validate_constraints=False)
    return station


def _format_error(exc: ValidationError) -> str:
    if hasattr(exc, "error_dict"):
        return "; ".join(
            f"{FIELD_TO_COLUMN.get(field, field)}: {' '.join(messages)}"
            for field, messages in exc.message_dict.items()
        )
    return "; ".join(exc.messages)
