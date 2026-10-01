import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError, CommandParser

from fuel.importers import MissingHeadersError, parse_fuel_prices, replace_fuel_stations


class Command(BaseCommand):
    help = (
        "Import fuel stations and retail prices from an OPIS fuel price CSV file. "
        "Replaces all existing fuel station rows with the valid rows from the file."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("csv_path", type=Path, help="Path to the fuel prices CSV file.")

    def handle(self, *args, csv_path: Path, **options) -> None:
        try:
            # utf-8-sig tolerates a BOM written by spreadsheet tools.
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                parsed = parse_fuel_prices(csv_file)
        except FileNotFoundError:
            raise CommandError(f"File not found: {csv_path}")
        except MissingHeadersError as exc:
            raise CommandError(str(exc))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise CommandError(f"Could not read {csv_path}: {exc}")

        for error in parsed.errors:
            self.stderr.write(self.style.WARNING(f"Row {error.row_number}: {error.reason}"))

        replaced = replace_fuel_stations(parsed.stations)

        summary = [
            ("Processed rows", parsed.rows_processed),
            ("Stations deleted", replaced.deleted),
            ("Stations created", replaced.created),
            ("Skipped rows (errors)", len(parsed.errors)),
        ]
        for label, count in summary:
            self.stdout.write(f"{label + ':':<24}{count}")

        style = self.style.WARNING if parsed.errors else self.style.SUCCESS
        self.stdout.write(style("Import finished."))
