"""
Loads the pre-built (offline-geocoded) station index into memory once per
worker process, and provides a "cheapest station near this point" lookup.

With ~8,000 stations, a brute-force haversine scan per lookup is trivial
(a handful of lookups per request x 8,000 comparisons = microseconds), so
no spatial index (KD-tree etc.) is needed for this dataset size -- keeping
the code simple and dependency-free.
"""
from __future__ import annotations

import json
import math
import threading

from django.conf import settings

EARTH_RADIUS_MILES = 3958.8

_lock = threading.Lock()
_stations_cache: list[dict] | None = None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def load_stations() -> list[dict]:
    global _stations_cache
    if _stations_cache is None:
        with _lock:
            if _stations_cache is None:  # re-check inside the lock
                if not settings.STATION_INDEX_PATH.exists():
                    raise RuntimeError(
                        "Station index not found. Run "
                        "`python manage.py build_station_index` first."
                    )
                with open(settings.STATION_INDEX_PATH, encoding="utf-8") as f:
                    _stations_cache = json.load(f)
    return _stations_cache


def cheapest_station_near(
    lat: float,
    lon: float,
    max_radius_miles: float,
    exclude_ids: set[str] | None = None,
) -> dict | None:
    """
    Find the cheapest station within `max_radius_miles` of (lat, lon).
    Expands the search radius (doubling) up to a hard cap if nothing is
    found nearby, since station density varies a lot by region.
    """
    exclude_ids = exclude_ids or set()
    stations = load_stations()

    radius = max_radius_miles
    hard_cap = max_radius_miles * 8

    while radius <= hard_cap:
        candidates = [
            s
            for s in stations
            if s["id"] not in exclude_ids
            and haversine_miles(lat, lon, s["lat"], s["lon"]) <= radius
        ]
        if candidates:
            return min(candidates, key=lambda s: s["price"])
        radius *= 2

    return None
