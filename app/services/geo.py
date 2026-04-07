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


def is_within_fence(lat: float, lon: float, db: Session):
    """
    Check if the given coordinates are within the configured geofence zone.
    Loads the active zone from the database, so updates through the UI take
    effect immediately without a server restart.

    Returns: (is_within: bool, distance_meters: float)
    """
    from app.database.models import GeofenceZone

    zone = db.query(GeofenceZone).filter(GeofenceZone.is_active == True).first()

    if not zone:
        # If no zone is configured, fail-safe: reject all punches
        return False, float("inf")

    distance = haversine(lat, lon, zone.latitude, zone.longitude)
    return distance <= zone.radius_meters, distance
