"""
Geofencing service with support for:
- Single branch center-point validation
- Multiple branch validation (a device assigned to multiple branches)
- Branch checkpoint validation (multiple clock-in points per branch)
"""
import math
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


def is_within_fence(lat: float, lon: float, branch) -> tuple[bool, float]:
    """
    Check if the given coordinates are within the specified branch's geofence.
    Also checks any active checkpoints belonging to this branch.
    
    Returns: (is_within: bool, distance_meters: float)
    """
    if not branch:
        return False, float("inf")

    # First: check branch center point
    center_dist = haversine(lat, lon, branch.latitude, branch.longitude)
    if center_dist <= branch.radius_meters:
        return True, center_dist

    # Second: check all active checkpoints for this branch
    # (checkpoints are fetched from DB — this function receives the branch object
    #  and we look up checkpoints lazily if not pre-loaded)
    if hasattr(branch, 'checkpoints') and branch.checkpoints:
        for cp in branch.checkpoints:
            if not cp.is_active:
                continue
            cp_dist = haversine(lat, lon, cp.latitude, cp.longitude)
            if cp_dist <= cp.radius_meters:
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

        # Check branch center point
        dist = haversine(lat, lon, branch.latitude, branch.longitude)
        if dist <= branch.radius_meters:
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
            cp_dist = haversine(lat, lon, cp.latitude, cp.longitude)
            if cp_dist <= cp.radius_meters:
                return True, cp_dist, f"{branch.name} ({cp.name})"

        # Track closest for error message
        if dist < best_dist:
            best_dist, best_branch = dist, branch.name

    return False, best_dist, best_branch
