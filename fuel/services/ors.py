"""OpenRouteService HTTP client for geocoding and driving directions."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from django.conf import settings

from fuel.exceptions import (
    LocationNotFoundError,
    OrsConfigurationError,
    OrsRateLimitError,
    OrsRequestError,
    OrsResponseError,
    OrsTimeoutError,
)

logger = logging.getLogger(__name__)

METERS_PER_MILE = settings.METERS_PER_MILE

GEOCODE_PATH = "/pelias/v1/search"
DIRECTIONS_PATH = "/openrouteservice/v2/directions/driving-car/geojson"


@dataclass(frozen=True)
class GeocodedLocation:
    input: str
    label: str
    latitude: float
    longitude: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def coordinates(self) -> list[float]:
        """ORS expects [longitude, latitude]."""
        return [self.longitude, self.latitude]


@dataclass(frozen=True)
class GeocodeCandidate:
    """One Pelias search result with the administrative fields used for validation."""

    label: str
    latitude: float
    longitude: float
    country: str
    country_code: str
    region: str
    region_code: str
    locality: str
    layer: str


@dataclass(frozen=True)
class RouteResult:
    distance_meters: float
    distance_miles: float
    duration_seconds: float
    geometry: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "distance_miles": self.distance_miles,
            "duration_seconds": self.duration_seconds,
            "geometry": self.geometry,
        }


def meters_to_miles(meters: float) -> float:
    return round(meters / METERS_PER_MILE, 2)


class OpenRouteServiceClient:
    """Thin httpx wrapper around HeiGIT OpenRouteService / Pelias endpoints."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.ORS_API_KEY).strip()
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.ORS_TIMEOUT_SECONDS
        self._transport = transport

    @property
    def geocode_url(self) -> str:
        return f"{self.base_url}{GEOCODE_PATH}"

    @property
    def directions_url(self) -> str:
        return f"{self.base_url}{DIRECTIONS_PATH}"

    def geocode(self, location: str, *, role: str = "location") -> GeocodedLocation:
        self._require_api_key()
        payload = self._request(
            "GET",
            self.geocode_url,
            params={
                "text": location,
                "size": 1,
                "boundary.country": "US",
            },
            accept="application/json",
        )
        features = payload.get("features")
        if not isinstance(features, list) or not features:
            raise LocationNotFoundError(f"No results found for {role}: {location}")

        feature = features[0]
        try:
            coordinates = feature["geometry"]["coordinates"]
            longitude, latitude = float(coordinates[0]), float(coordinates[1])
            properties = feature.get("properties") or {}
            label = str(properties.get("label") or location)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise OrsResponseError("Geocoding response from OpenRouteService was malformed.") from exc

        return GeocodedLocation(
            input=location,
            label=label,
            latitude=latitude,
            longitude=longitude,
        )

    def search_candidates(self, text: str, *, size: int = 5) -> list[GeocodeCandidate]:
        """Return up to ``size`` geocoding candidates; an empty list means no match.

        Malformed features are skipped. One call is one ORS request regardless of ``size``.
        """
        self._require_api_key()
        payload = self._request(
            "GET",
            self.geocode_url,
            params={
                "text": text,
                "size": size,
                "boundary.country": "US",
            },
            accept="application/json",
        )
        features = payload.get("features")
        if not isinstance(features, list):
            raise OrsResponseError("Geocoding response from OpenRouteService was malformed.")

        candidates: list[GeocodeCandidate] = []
        for feature in features:
            try:
                coordinates = feature["geometry"]["coordinates"]
                longitude, latitude = float(coordinates[0]), float(coordinates[1])
                properties = feature.get("properties") or {}
            except (KeyError, TypeError, ValueError, IndexError):
                continue

            def text_prop(*keys: str) -> str:
                for key in keys:
                    value = properties.get(key)
                    if value:
                        return str(value)
                return ""

            candidates.append(
                GeocodeCandidate(
                    label=text_prop("label") or text,
                    latitude=latitude,
                    longitude=longitude,
                    country=text_prop("country"),
                    country_code=text_prop("country_a", "country_code"),
                    region=text_prop("region"),
                    region_code=text_prop("region_a"),
                    locality=text_prop("locality", "localadmin"),
                    layer=text_prop("layer"),
                )
            )
        return candidates

    def get_route(self, start: GeocodedLocation, finish: GeocodedLocation) -> RouteResult:
        self._require_api_key()
        payload = self._request(
            "POST",
            self.directions_url,
            json={"coordinates": [start.coordinates, finish.coordinates]},
            accept="application/geo+json",
        )
        try:
            feature = payload["features"][0]
            summary = feature["properties"]["summary"]
            distance_meters = float(summary["distance"])
            duration_seconds = float(summary["duration"])
            geometry = feature["geometry"]
            if not isinstance(geometry, dict) or "type" not in geometry:
                raise TypeError("geometry missing")
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise OrsResponseError("Routing response from OpenRouteService was malformed.") from exc

        return RouteResult(
            distance_meters=distance_meters,
            distance_miles=meters_to_miles(distance_meters),
            duration_seconds=duration_seconds,
            geometry=geometry,
        )

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise OrsConfigurationError(
                "OpenRouteService API key is not configured. Set ORS_API_KEY in the environment."
            )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        accept: str,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": self.api_key,
            "Accept": accept,
        }
        if json is not None:
            headers["Content-Type"] = "application/json"

        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                response = client.request(method, url, params=params, json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise OrsTimeoutError("OpenRouteService request timed out.") from exc
        except httpx.HTTPError as exc:
            raise OrsRequestError("Unable to reach OpenRouteService.") from exc

        if response.status_code >= 400:
            logger.warning(
                "ORS request failed status=%s url=%s body=%s",
                response.status_code,
                str(response.request.url),
                _sanitize_response_body(response.text),
            )
            if response.status_code == 403:
                raise OrsRequestError(
                    "OpenRouteService rejected the API key (HTTP 403). "
                    "Check ORS_API_KEY in .env and restart the containers."
                )
            if response.status_code == 429:
                raise OrsRateLimitError(
                    "OpenRouteService rate limit exceeded (HTTP 429). Stopping to avoid further requests."
                )
            raise OrsRequestError(
                f"OpenRouteService returned HTTP {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OrsResponseError("OpenRouteService returned a non-JSON response.") from exc

        if not isinstance(payload, dict):
            raise OrsResponseError("OpenRouteService returned an unexpected JSON payload.")
        return payload


def _sanitize_response_body(body: str, *, limit: int = 500) -> str:
    """Truncate provider error bodies for logs; never echo credentials."""
    cleaned = body.replace("\n", " ").strip()
    if len(cleaned) > limit:
        return f"{cleaned[:limit]}…"
    return cleaned
