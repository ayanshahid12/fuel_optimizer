from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel.serializers import RouteRequestSerializer
from fuel.services.routing import plan_route


class RouteCreateAPIView(APIView):
    """Return a driving route with cost-optimized fuel stops."""

    def post(self, request: Request) -> Response:
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = plan_route(
            serializer.validated_data["start"],
            serializer.validated_data["finish"],
        )
        return Response(result)
