"""
Geofencing service with support for:
- Single branch center-point validation
- Multiple branch validation (a device assigned to multiple branches)
- Branch checkpoint validation (multiple clock-in points per branch)
"""
import math
import json
from typing import Optional
from sqlalchemy.orm import Session

from app.database.models import Branch, BranchCheckpoint


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


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """
    Check if a coordinate (lat, lon) is inside a polygon using Ray-Casting algorithm.
    polygon is a list of [lat, lon] coordinates, e.g. [[lat1, lon1], [lat2, lon2], ...]
    """
    num_points = len(polygon)
    if num_points < 3:
        return False
        
    inside = False
    j = num_points - 1
    
    for i in range(num_points):
        lat_i, lon_i = polygon[i][0], polygon[i][1]
        lat_j, lon_j = polygon[j][0], polygon[j][1]
        
        # Ray casting check
        intersect = ((lon_i > lon) != (lon_j > lon)) and \
                    (lat < (lat_j - lat_i) * (lon - lon_i) / (lon_j - lon_i) + lat_i)
        if intersect:
            inside = not inside
        j = i
        
    return inside


def is_within_fence(lat: float, lon: float, branch) -> tuple[bool, float]:
    """
    Check if the given coordinates are within the specified branch's geofence.
    Also checks any active checkpoints belonging to this branch.
    
    Returns: (is_within: bool, distance_meters: float)
    """
    if not branch:
        return False, float("inf")

    # First: check branch perimeter
    is_inside = False
    center_dist = float("inf")
    
    if getattr(branch, "geofence_type", "circle") == "polygon" and getattr(branch, "polygon_coordinates", None):
        try:
            coords = json.loads(branch.polygon_coordinates)
            is_inside = point_in_polygon(lat, lon, coords)
            center_dist = 0.0 if is_inside else haversine(lat, lon, branch.latitude, branch.longitude)
        except Exception:
            pass
    else:
        center_dist = haversine(lat, lon, branch.latitude, branch.longitude)
        is_inside = center_dist <= branch.radius_meters

    if is_inside:
        return True, center_dist

    # Second: check all active checkpoints for this branch
    if hasattr(branch, 'checkpoints') and branch.checkpoints:
        for cp in branch.checkpoints:
            if not cp.is_active:
                continue
            
            cp_inside = False
            cp_dist = float("inf")
            
            if getattr(cp, "geofence_type", "circle") == "polygon" and getattr(cp, "polygon_coordinates", None):
                try:
                    cp_coords = json.loads(cp.polygon_coordinates)
                    cp_inside = point_in_polygon(lat, lon, cp_coords)
                    cp_dist = 0.0 if cp_inside else haversine(lat, lon, cp.latitude, cp.longitude)
                except Exception:
                    pass
            else:
                cp_dist = haversine(lat, lon, cp.latitude, cp.longitude)
                cp_inside = cp_dist <= cp.radius_meters
                
            if cp_inside:
                return True, cp_dist

    return False, center_dist


def is_within_any_fence(
    lat: float, lon: float, branches: list,
    db: Optional[Session] = None,
) -> tuple[bool, float, str | None]:
    """
    Check if GPS coordinates are within ANY of the given branches.
    For each branch, also checks its active checkpoints (if any).
    
    Args:
        lat: GPS latitude
        lon: GPS longitude
        branches: List of Branch objects
        db: Optional DB session for loading checkpoints (if not pre-loaded)
    
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

        # Check branch perimeter
        is_inside = False
        dist = float("inf")
        
        if getattr(branch, "geofence_type", "circle") == "polygon" and getattr(branch, "polygon_coordinates", None):
            try:
                coords = json.loads(branch.polygon_coordinates)
                is_inside = point_in_polygon(lat, lon, coords)
                dist = 0.0 if is_inside else haversine(lat, lon, branch.latitude, branch.longitude)
            except Exception:
                dist = haversine(lat, lon, branch.latitude, branch.longitude)
        else:
            dist = haversine(lat, lon, branch.latitude, branch.longitude)
            is_inside = dist <= branch.radius_meters

        if is_inside:
            return True, dist, branch.name

        # Check branch checkpoints
        if db is not None:
            checkpoints = db.query(BranchCheckpoint).filter(
                BranchCheckpoint.branch_id == branch.id,
                BranchCheckpoint.is_active == True,
            ).all()
        elif hasattr(branch, 'checkpoints') and branch.checkpoints:
            checkpoints = [cp for cp in branch.checkpoints if cp.is_active]
        else:
            checkpoints = []

        for cp in checkpoints:
            cp_inside = False
            cp_dist = float("inf")
            
            if getattr(cp, "geofence_type", "circle") == "polygon" and getattr(cp, "polygon_coordinates", None):
                try:
                    cp_coords = json.loads(cp.polygon_coordinates)
                    cp_inside = point_in_polygon(lat, lon, cp_coords)
                    cp_dist = 0.0 if cp_inside else haversine(lat, lon, cp.latitude, cp.longitude)
                except Exception:
                    cp_dist = haversine(lat, lon, cp.latitude, cp.longitude)
            else:
                cp_dist = haversine(lat, lon, cp.latitude, cp.longitude)
                cp_inside = cp_dist <= cp.radius_meters
                
            if cp_inside:
                return True, cp_dist, f"{branch.name} ({cp.name})"

        # Track closest for error message
        # Use center point distance as reference for polygons if outside
        ref_dist = dist if dist != 0.0 else haversine(lat, lon, branch.latitude, branch.longitude)
        if ref_dist < best_dist:
            best_dist, best_branch = ref_dist, branch.name

    return False, best_dist, best_branch
