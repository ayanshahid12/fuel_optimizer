from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.serializers import RouteRequestSerializer
from fuel.services.ors import OpenRouteServiceClient


class RouteCreateAPIView(APIView):
    """Geocode start/finish and return a driving route via OpenRouteService."""

    def post(self, request: Request) -> Response:
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = OpenRouteServiceClient().create_route(
            serializer.validated_data["start"],
            serializer.validated_data["finish"],
        )
        return Response(result)
