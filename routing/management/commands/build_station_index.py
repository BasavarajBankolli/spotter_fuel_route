"""
Management command: build_station_index

Pre-processes the raw OPIS fuel-price CSV into a geocoded, de-duplicated
station index that the live API can load instantly.

Why this exists
----------------
The assessment requires the live API to call the external map/routing API
as few times as possible (ideally once, two or three at most). The fuel
price CSV has ~8,000 rows but no coordinates, and there is no way to
serve "nearest station along the route" queries without *some* notion of
where each station is.

The trick: geocoding happens ONCE, offline, as a build step -- not on
every API request. We approximate each station's location by matching its
City + State against a bundled, offline dataset of ~29,800 US cities/towns
(routing/data/us_cities.csv, sourced from a public domain-ish US cities
database with lat/lon per city). No network calls, no API key, no
per-station geocoding at request time. A smaller ~3,300-city dataset
(geonamescache) and a state-centroid are used as fallbacks for the rare
city that isn't in the primary list.

Run with:
    python manage.py build_station_index
"""
import csv
import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Pre-geocode the fuel price CSV into routing/data/stations_geocoded.json"

    def handle(self, *args, **options):
        import geonamescache

        # --- Primary lookup: bundled ~29,800-city US database ---------------
        primary_lookup = {}
        us_cities_csv = settings.BASE_DIR / "routing" / "data" / "us_cities.csv"
        with open(us_cities_csv, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["CITY"].strip().lower(), row["STATE_CODE"].strip())
                primary_lookup[key] = (float(row["LATITUDE"]), float(row["LONGITUDE"]))

        # --- Secondary lookup: geonamescache (~3,300 larger cities) --------
        gc = geonamescache.GeonamesCache()
        us_cities = [c for c in gc.get_cities().values() if c["countrycode"] == "US"]
        secondary_lookup = {}
        for c in us_cities:
            key = (c["name"].strip().lower(), c["admin1code"])
            if key not in secondary_lookup or c["population"] > secondary_lookup[key][2]:
                secondary_lookup[key] = (c["latitude"], c["longitude"], c["population"])

        # --- Fallback: state centroid ---------------------------------------
        state_points = {}
        for c in us_cities:
            state_points.setdefault(c["admin1code"], []).append((c["latitude"], c["longitude"]))
        state_centroid = {
            state: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            for state, pts in state_points.items()
        }

        stations = []
        seen = set()
        matched_primary, matched_secondary, fallback, skipped = 0, 0, 0, 0

        with open(settings.FUEL_PRICES_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row["Truckstop Name"].strip()
                city = row["City"].strip()
                state = row["State"].strip()
                address = row["Address"].strip()
                try:
                    price = float(row["Retail Price"])
                except (TypeError, ValueError):
                    continue

                dedupe_key = (name, city, state, address, round(price, 4))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                key = (city.lower(), state)
                if key in primary_lookup:
                    lat, lon = primary_lookup[key]
                    matched_primary += 1
                elif key in secondary_lookup:
                    lat, lon, _ = secondary_lookup[key]
                    matched_secondary += 1
                elif state in state_centroid:
                    lat, lon = state_centroid[state]
                    fallback += 1
                else:
                    skipped += 1
                    continue

                stations.append(
                    {
                        "id": row["OPIS Truckstop ID"],
                        "name": name,
                        "address": address,
                        "city": city,
                        "state": state,
                        "price": price,
                        "lat": lat,
                        "lon": lon,
                    }
                )

        settings.STATION_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(settings.STATION_INDEX_PATH, "w", encoding="utf-8") as out:
            json.dump(stations, out)

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(stations)} stations to {settings.STATION_INDEX_PATH} "
                f"({matched_primary} matched via primary city db, "
                f"{matched_secondary} via geonamescache, "
                f"{fallback} via state centroid, {skipped} skipped)"
            )
        )
