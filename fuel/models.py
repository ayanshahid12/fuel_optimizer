from decimal import Decimal

from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models


class FuelStation(models.Model):
    """A fuel-price source row from the OPIS assessment CSV.

    OPIS Truckstop ID is not unique in the feed: the same ID can appear with
    different names and prices. Each CSV row is stored as its own record.
    """

    class GeocodeStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        REJECTED = "rejected", "Rejected"
        NOT_FOUND = "not_found", "Not found"
        SKIPPED = "skipped", "Skipped"

    opis_truckstop_id = models.PositiveIntegerField()
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(
        max_length=2,
        validators=[RegexValidator(r"^[A-Z]{2}$", "State must be a 2-letter uppercase code.")],
    )
    rack_id = models.PositiveIntegerField()
    retail_price = models.DecimalField(
        max_digits=12,
        decimal_places=8,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    latitude = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    geocode_status = models.CharField(
        max_length=16,
        choices=GeocodeStatus.choices,
        default=GeocodeStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_station"
        ordering = ["opis_truckstop_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(retail_price__gt=0),
                name="fuel_station_retail_price_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["opis_truckstop_id"], name="fuel_station_opis_id_idx"),
            models.Index(fields=["state"], name="fuel_station_state_idx"),
            models.Index(fields=["latitude", "longitude"], name="fuel_station_coords_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.truckstop_name} ({self.city}, {self.state}) #{self.opis_truckstop_id}"

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class LocationGeocode(models.Model):
    """Cached ORS geocode for a user-entered route location (successful lookups only)."""

    normalized_query = models.CharField(max_length=255, unique=True)
    query = models.CharField(max_length=255)
    label = models.CharField(max_length=500)
    latitude = models.FloatField(validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField(validators=[MinValueValidator(-180), MaxValueValidator(180)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "location_geocode"

    def __str__(self) -> str:
        return f"{self.normalized_query} -> ({self.latitude}, {self.longitude})"
