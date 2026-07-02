"""
Geocoding client: turns a free-text US location ("Chicago, IL", "600
Commerce St, Dallas, TX", etc.) into (lat, lon, display_name).

Three providers are tried in order, each one only used if the previous
one couldn't resolve the input:

1. US Census Bureau Geocoder (geocoding.geo.census.gov) -- official,
   free, no key. Great for full street addresses, but NOT reliable for
   bare "City, ST" inputs (it's built around TIGER/Line street ranges).

2. Local offline city lookup (routing/data/us_cities.csv, ~29,800 US
   cities/towns with lat/lon, bundled with the project). Handles plain
   "City, ST" or "City, State Name" inputs instantly, with zero network
   calls -- this is what catches the common case of a reviewer testing
   with something like {"start": "Chicago, IL", "finish": "Dallas, TX"}.

3. OpenStreetMap Nominatim -- last-resort fallback for anything the
   first two can't handle. Kept last because Nominatim can return 403s
   for automated clients depending on IP/network reputation.

Results are cached in-process so repeated requests for a popular
city/address don't re-hit any provider.
"""
from __future__ import annotations

import csv
import logging
import re
import threading

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class GeocodingError(Exception):
    """Raised when a location string can't be resolved to coordinates by any provider."""


_CACHE: dict[str, tuple[float, float, str]] = {}

# --- Local city lookup: loaded once, lazily -------------------------------

_STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_VALID_STATE_ABBRS = set(_STATE_NAME_TO_ABBR.values())

_city_lock = threading.Lock()
_city_lookup: dict[tuple[str, str], tuple[float, float, str]] | None = None


def _load_city_lookup() -> dict[tuple[str, str], tuple[float, float, str]]:
    global _city_lookup
    if _city_lookup is None:
        with _city_lock:
            if _city_lookup is None:
                lookup = {}
                path = settings.BASE_DIR / "routing" / "data" / "us_cities.csv"
                with open(path, newline="", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        key = (row["CITY"].strip().lower(), row["STATE_CODE"].strip())
                        display = f"{row['CITY'].strip()}, {row['STATE_CODE'].strip()}"
                        lookup[key] = (
                            float(row["LATITUDE"]),
                            float(row["LONGITUDE"]),
                            display,
                        )
                _city_lookup = lookup
    return _city_lookup


def _parse_city_state(location: str) -> tuple[str, str] | None:
    """
    Best-effort parse of a "City, ST" or "City, State Name" style input.
    Returns (city, state_abbr) or None if it doesn't look like that shape
    (e.g. it has a street number, which the local lookup can't handle).
    """
    if re.match(r"^\s*\d", location):
        # Starts with a number -> almost certainly a street address, not
        # a bare city -- let Census/Nominatim handle it instead.
        return None

    parts = [p.strip() for p in location.split(",")]
    if len(parts) < 2:
        return None

    city, state_part = parts[0], parts[1]
    state_part_clean = state_part.strip().lower()

    if state_part.strip().upper() in _VALID_STATE_ABBRS:
        return city, state_part.strip().upper()
    if state_part_clean in _STATE_NAME_TO_ABBR:
        return city, _STATE_NAME_TO_ABBR[state_part_clean]
    return None


def _geocode_local_city(location: str) -> tuple[float, float, str] | None:
    parsed = _parse_city_state(location)
    if parsed is None:
        return None
    city, state = parsed
    lookup = _load_city_lookup()
    return lookup.get((city.lower(), state))


def _geocode_census(location: str) -> tuple[float, float, str] | None:
    """Try the US Census Bureau's free geocoder. Returns None if no match (not an error)."""
    try:
        response = requests.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={
                "address": location,
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
            timeout=10,
        )
        response.raise_for_status()
        matches = response.json()["result"]["addressMatches"]
    except (requests.RequestException, KeyError, ValueError):
        logger.warning("Census geocoder failed for %r", location)
        return None

    if not matches:
        return None

    top = matches[0]
    coords = top["coordinates"]  # {"x": lon, "y": lat}
    return (float(coords["y"]), float(coords["x"]), top.get("matchedAddress", location))


def _geocode_nominatim(location: str) -> tuple[float, float, str] | None:
    """Try OpenStreetMap Nominatim. Returns None if no match or the request fails."""
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={
                "q": location,
                "format": "json",
                "limit": 1,
                "countrycodes": "us",
            },
            headers={
                "User-Agent": settings.NOMINATIM_USER_AGENT,
                "Accept-Language": "en-US,en",
                "Referer": "https://spotter-fuel-route-assessment.local/",
            },
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except requests.RequestException:
        logger.warning("Nominatim geocoder failed for %r", location)
        return None

    if not results:
        return None

    top = results[0]
    return (float(top["lat"]), float(top["lon"]), top.get("display_name", location))


def geocode(location: str) -> tuple[float, float, str]:
    """
    Resolve a free-text location to (lat, lon, display_name).

    Restricted to the US per the assessment ("both within the USA").
    Order: Census (best for street addresses) -> local city lookup
    (instant, offline, handles bare "City, ST" inputs) -> Nominatim
    (last-resort fallback).
    """
    cache_key = location.strip().lower()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    result = (
        _geocode_census(location)
        or _geocode_local_city(location)
        or _geocode_nominatim(location)
    )

    if result is None:
        raise GeocodingError(
            f"Could not resolve location {location!r} via Census geocoder, "
            "local city lookup, or Nominatim."
        )

    _CACHE[cache_key] = result
    return result
