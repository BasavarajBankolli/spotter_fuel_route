from django.urls import path

from routing.views import FuelRouteView

urlpatterns = [
    path("route/", FuelRouteView.as_view(), name="fuel-route"),
]
