from django.urls import path

from fuel.views import RouteCreateAPIView

urlpatterns = [
    path("routes/", RouteCreateAPIView.as_view(), name="route-create"),
]
