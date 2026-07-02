"""
Core fuel-stop optimization algorithm.

Given a route's polyline geometry (list of (lat, lon) points, in order,
as returned by the routing API) and the vehicle's range, this figures out
where the vehicle needs to refuel and picks the cheapest station near
each refuel point.

Approach
--------
1. Walk the polyline, accumulating cumulative distance.
2. Every time cumulative distance since the last fill-up would exceed a
   safe trigger distance (max range minus a safety margin), record that
   point on the route as a "refuel needed here" point.
3. For each such point, look up the cheapest station within a search
   radius (expanding if the area is sparse).
4. Cost the trip leg by leg: the vehicle leaves each stop with just
   enough fuel (at that stop's price) to cover the distance to the next
   stop (or the finish). The very first leg is assumed to start with a
   full tank at no cost (the driver fuels up before departure).
"""
from __future__ import annotations

from django.conf import settings

from routing.services.station_index import cheapest_station_near, haversine_miles


def _cumulative_points(geometry: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """Return [(lat, lon, cumulative_miles_from_start), ...] for each polyline vertex."""
    points = [(geometry[0][0], geometry[0][1], 0.0)]
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(geometry, geometry[1:]):
        total += haversine_miles(lat1, lon1, lat2, lon2)
        points.append((lat2, lon2, total))
    return points


def _point_at_distance(points: list[tuple[float, float, float]], target_miles: float) -> tuple[float, float]:
    """Interpolate the (lat, lon) on the polyline at a given cumulative distance."""
    for (lat1, lon1, d1), (lat2, lon2, d2) in zip(points, points[1:]):
        if d1 <= target_miles <= d2:
            if d2 == d1:
                return lat1, lon1
            ratio = (target_miles - d1) / (d2 - d1)
            return lat1 + ratio * (lat2 - lat1), lon1 + ratio * (lon2 - lon1)
    return points[-1][0], points[-1][1]


def plan_fuel_stops(geometry: list[tuple[float, float]], total_distance_miles: float) -> dict:
    max_range = settings.VEHICLE_MAX_RANGE_MILES
    trigger_distance = max_range - settings.REFUEL_SAFETY_MARGIN_MILES
    search_radius = settings.STATION_SEARCH_RADIUS_MILES
    mpg = settings.VEHICLE_MPG

    points = _cumulative_points(geometry)

    stops = []
    used_ids = set()
    distance_since_last_stop = 0.0
    next_trigger = trigger_distance

    while next_trigger < total_distance_miles:
        lat, lon = _point_at_distance(points, next_trigger)
        station = cheapest_station_near(lat, lon, search_radius, exclude_ids=used_ids)
        if station is None:
            # Extremely sparse area -- fall back to the cheapest station
            # anywhere already-considered radius could not find; without
            # this the trip would be impossible to complete on paper.
            break

        used_ids.add(station["id"])
        stops.append(
            {
                "station_id": station["id"],
                "name": station["name"],
                "address": station["address"],
                "city": station["city"],
                "state": station["state"],
                "price_per_gallon": station["price"],
                "lat": station["lat"],
                "lon": station["lon"],
                "distance_from_start_miles": round(next_trigger, 1),
            }
        )
        distance_since_last_stop = next_trigger
        next_trigger = distance_since_last_stop + trigger_distance

    # Cost the trip: each stop buys just enough fuel to reach the next
    # stop (or the finish). The first leg (start -> first stop, or start
    # -> finish if no stop is needed) is assumed pre-fueled, at no cost.
    total_cost = 0.0
    total_gallons = total_distance_miles / mpg

    boundaries = [s["distance_from_start_miles"] for s in stops] + [round(total_distance_miles, 1)]
    for stop, leg_end in zip(stops, boundaries[1:]):
        leg_miles = leg_end - stop["distance_from_start_miles"]
        gallons = leg_miles / mpg
        cost = gallons * stop["price_per_gallon"]
        stop["fuel_purchased_gallons"] = round(gallons, 2)
        stop["fuel_cost"] = round(cost, 2)
        total_cost += cost

    return {
        "fuel_stops": stops,
        "total_gallons": round(total_gallons, 2),
        "total_fuel_cost": round(total_cost, 2),
    }
