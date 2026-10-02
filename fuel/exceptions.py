"""Application-level errors and a DRF exception handler for clean JSON responses."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class AppError(Exception):
    """Base error that maps to a controlled JSON API response."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "error"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class OrsConfigurationError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ors_not_configured"


class LocationNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "location_not_found"


class OrsTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "ors_timeout"


class OrsRequestError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ors_request_failed"


class OrsRateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "ors_rate_limited"


class OrsResponseError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ors_response_invalid"


class RouteInfeasibleError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "route_infeasible"


def api_exception_handler(exc, context):
    if isinstance(exc, AppError):
        return Response(
            {"error": {"code": exc.code, "detail": exc.detail}},
            status=exc.status_code,
        )
    return drf_exception_handler(exc, context)
