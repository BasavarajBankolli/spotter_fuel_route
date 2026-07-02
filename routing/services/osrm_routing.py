"""
Thin client for the free OSRM public routing API (no key required).

We make exactly ONE call here per request: the "driving" route between
start and finish, requesting full geometry so we can walk the polyline to
figure out where the 500-mile range runs out.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    """Raised when OSRM can't produce a route between the two points."""


def get_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict:
    """
    Fetch a driving route from OSRM.

    Returns a dict with:
        geometry: list of (lat, lon) points describing the route
        distance_miles: total route distance in miles
        duration_seconds: total route duration in seconds
    """
    # OSRM expects "lon,lat" ordering.
    coords = f"{start_lon},{start_lat};{end_lon},{end_lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords}"

    try:
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.exception("OSRM routing request failed")
        raise RoutingError(f"Could not reach routing service: {exc}") from exc

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"No route found: {data.get('message', data.get('code'))}")

    route = data["routes"][0]
    geometry = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]

    return {
        "geometry": geometry,
        "distance_miles": route["distance"] / METERS_PER_MILE,
        "duration_seconds": route["duration"],
    }
