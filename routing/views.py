import logging

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.serializers import RouteRequestSerializer
from routing.services.fuel_optimizer import plan_fuel_stops
from routing.services.geocoding import GeocodingError, geocode
from routing.services.osrm_routing import RoutingError, get_route

logger = logging.getLogger(__name__)


class FuelRouteView(APIView):
    """
    POST /api/route/
    Body: {"start": "Chicago, IL", "finish": "Dallas, TX"}

    Returns the driving route between two US locations, along with the
    optimal (cheapest) places to refuel given a 500-mile vehicle range,
    and the total estimated fuel cost for the trip at 10 MPG.

    External API budget per request: usually 1 call (routing only)
      - `start`/`finish` geocoding tries the Census geocoder, then an
        offline local city-name lookup (handles plain "City, ST"
        inputs with zero network calls), then Nominatim as a last
        resort -- so common city-level requests need 0 geocoding
        network calls.
      - 1x OSRM route call.
    Station selection uses a locally pre-geocoded, offline index -- no
    extra network calls per station lookup.
    """

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start_input = serializer.validated_data["start"]
        finish_input = serializer.validated_data["finish"]

        try:
            start_lat, start_lon, start_display = geocode(start_input)
            finish_lat, finish_lon, finish_display = geocode(finish_input)
        except GeocodingError as exc:
            return Response({"error": str(exc)}, status=400)

        try:
            route = get_route(start_lat, start_lon, finish_lat, finish_lon)
        except RoutingError as exc:
            return Response({"error": str(exc)}, status=502)

        fuel_plan = plan_fuel_stops(route["geometry"], route["distance_miles"])

        response_payload = {
            "start": {
                "input": start_input,
                "resolved_address": start_display,
                "lat": start_lat,
                "lon": start_lon,
            },
            "finish": {
                "input": finish_input,
                "resolved_address": finish_display,
                "lat": finish_lat,
                "lon": finish_lon,
            },
            "route": {
                "distance_miles": round(route["distance_miles"], 1),
                "duration_minutes": round(route["duration_seconds"] / 60, 1),
                "geometry_geojson": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lat, lon in route["geometry"]],
                },
                "google_maps_url": (
                    "https://www.google.com/maps/dir/"
                    f"{start_lat},{start_lon}/{finish_lat},{finish_lon}"
                ),
            },
            "vehicle": {
                "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
            },
            "fuel_stops": fuel_plan["fuel_stops"],
            "total_gallons": fuel_plan["total_gallons"],
            "total_fuel_cost_usd": fuel_plan["total_fuel_cost"],
        }
        return Response(response_payload)
