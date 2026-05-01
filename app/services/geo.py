import math
from sqlalchemy.orm import Session


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two GPS coordinates (in meters).
    Uses the Haversine formula.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return c * 6371000  # Earth radius in meters


def is_within_fence(lat: float, lon: float, active_branch) -> tuple[bool, float]:
    """
    Check if the given coordinates are within the specified branch's geofence.
    Returns: (is_within: bool, distance_meters: float)
    """
    if not active_branch:
        return False, float("inf")

    distance = haversine(lat, lon, active_branch.latitude, active_branch.longitude)
    return distance <= active_branch.radius_meters, distance


def is_within_any_fence(lat: float, lon: float, branches: list) -> tuple[bool, float, str | None]:
    """
    Check if GPS coordinates are within ANY of the given branches.
    Returns: (is_within: bool, best_distance_meters: float, best_branch_name: str | None)
    
    Short-circuits on first match for performance.
    If no match, returns the closest branch and distance (for error messages).
    """
    if not branches:
        return False, float("inf"), None

    best_dist = float("inf")
    best_branch = None
    for branch in branches:
        if not branch.is_active:
            continue
        dist = haversine(lat, lon, branch.latitude, branch.longitude)
        if dist <= branch.radius_meters:
            return True, dist, branch.name  # Short-circuit on first match
        if dist < best_dist:
            best_dist, best_branch = dist, branch.name

    return False, best_dist, best_branch
