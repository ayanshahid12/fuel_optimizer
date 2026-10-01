from argparse import ArgumentTypeError

from django.core.management.base import BaseCommand, CommandError, CommandParser

from fuel.services.ors import OpenRouteServiceClient
from fuel.services.station_geocoding import geocode_missing_stations


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ArgumentTypeError("must be an integer >= 1")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise ArgumentTypeError("must be >= 0")
    return number


class Command(BaseCommand):
    help = (
        "Geocode FuelStation rows that are missing latitude/longitude via OpenRouteService. "
        "Deduplicates by address+city+state so identical locations use one ORS request. "
        "Safe to interrupt and rerun; already-populated coordinates are skipped."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--limit",
            type=positive_int,
            default=100,
            help="Max unique addresses to geocode in this run (default: 100).",
        )
        parser.add_argument(
            "--delay",
            type=non_negative_float,
            default=0.7,
            help="Seconds to wait between ORS requests (default: 0.7; under 100 req/min).",
        )

    def handle(self, *args, limit: int, delay: float, **options) -> None:
        client = OpenRouteServiceClient()
        if not client.api_key:
            raise CommandError(
                "OpenRouteService API key is not configured. Set ORS_API_KEY in the environment."
            )

        self.stdout.write(
            f"Geocoding up to {limit} unique address(es) with {delay}s delay between requests."
        )

        stats = geocode_missing_stations(
            client=client,
            limit=limit,
            delay=delay,
            on_progress=self.stdout.write,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Fuel station geocoding complete"))
        summary = [
            ("Unique addresses attempted", stats.attempted),
            ("Successfully geocoded", stats.succeeded),
            ("Rows updated", stats.rows_updated),
            ("Not found", stats.not_found),
            ("Errors", stats.errors),
            ("Remaining ungeocoded rows", stats.remaining_rows),
        ]
        for label, count in summary:
            self.stdout.write(f"{label + ':':<28}{count}")

        if stats.stopped_early == "rate_limited":
            self.stderr.write(
                self.style.WARNING(
                    "Stopped early due to OpenRouteService rate limiting (HTTP 429)."
                )
            )
