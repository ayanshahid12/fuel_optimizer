from argparse import ArgumentTypeError

from django.core.management.base import BaseCommand, CommandError, CommandParser

from fuel.services.ors import OpenRouteServiceClient
from fuel.services.station_geocoding import geocode_stations


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
        "Geocode FuelStation rows via OpenRouteService, keeping only results in the USA and in "
        "the station's own state. Deduplicates by address+city+state so identical locations use "
        "one ORS request. Safe to interrupt and rerun; processed addresses are not requested again."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--limit",
            type=positive_int,
            default=100,
            help="Max unique addresses to send to ORS in this run (default: 100).",
        )
        parser.add_argument(
            "--delay",
            type=non_negative_float,
            default=0.7,
            help="Seconds to wait between ORS requests (default: 0.7; under 100 req/min).",
        )
        parser.add_argument(
            "--revalidate",
            action="store_true",
            help=(
                "Re-geocode addresses whose stored coordinates predate state validation; "
                "keep coordinates only when the returned state matches, otherwise clear them."
            ),
        )

    def handle(self, *args, limit: int, delay: float, revalidate: bool, **options) -> None:
        client = OpenRouteServiceClient()
        if not client.api_key:
            raise CommandError(
                "OpenRouteService API key is not configured. Set ORS_API_KEY in the environment."
            )

        mode = "Revalidating" if revalidate else "Geocoding"
        self.stdout.write(
            f"{mode} up to {limit} unique address(es) with {delay}s delay between requests."
        )

        stats = geocode_stations(
            client=client,
            limit=limit,
            delay=delay,
            revalidate=revalidate,
            on_progress=self.stdout.write,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Fuel station geocoding complete"))
        summary = [
            ("ORS requests", stats.requests),
            ("Valid geocodes", stats.valid),
            ("Rejected (wrong state)", stats.rejected_wrong_state),
            ("Rejected (outside USA)", stats.rejected_outside_us),
            ("Not found", stats.not_found),
            ("Skipped (non-US state)", stats.skipped_non_us_state),
            ("Skipped (incomplete)", stats.skipped_incomplete),
            ("Errors", stats.errors),
            ("Rows with valid coordinates", stats.rows_updated),
            ("Rows cleared", stats.rows_cleared),
            ("Remaining ungeocoded rows", stats.remaining_rows),
            ("Rows awaiting revalidation", stats.unvalidated_rows),
        ]
        for label, count in summary:
            self.stdout.write(f"{label + ':':<30}{count}")

        if stats.stopped_early == "rate_limited":
            self.stderr.write(
                self.style.WARNING(
                    "Stopped early due to OpenRouteService rate limiting (HTTP 429)."
                )
            )
