"""Tests for geofence service."""
import pytest
from unittest.mock import Mock
from app.services.geo import is_within_fence, is_within_any_fence


def _make_branch(lat, lon, radius, name="Test Branch", is_active=True):
    """Helper to create a branch-like object matching Branch model attributes used by geo service."""
    branch = Mock()
    branch.latitude = lat
    branch.longitude = lon
    branch.radius_meters = radius
    branch.name = name
    branch.is_active = is_active
    return branch


class TestGeoService:
    def test_within_radius(self):
        """Point within 100m radius should return True."""
        branch = _make_branch(-6.2, 106.8, 100)
        # Test point ~50m away
        result = is_within_fence(-6.2005, 106.8005, branch)
        assert result[0] is True
        assert result[1] < 100  # Distance less than radius

    def test_outside_radius(self):
        """Point far away should return False."""
        branch = _make_branch(-6.2, 106.8, 100)
        result = is_within_fence(-6.3, 106.9, branch)
        assert result[0] is False
        assert result[1] > 100

    def test_exactly_at_center(self):
        """Point at exact center should be within fence."""
        branch = _make_branch(-6.2, 106.8, 100)
        result = is_within_fence(-6.2, 106.8, branch)
        assert result[0] is True
        assert result[1] == 0

    def test_multiple_branches(self):
        """Point should be within any of the provided branches."""
        branches = [
            _make_branch(-6.2, 106.8, 100, name="Branch A"),
            _make_branch(-6.3, 106.9, 500, name="Branch B"),
        ]
        # Point near second branch
        result = is_within_any_fence(-6.301, 106.901, branches)
        assert result[0] is True
        
        # Point far from all branches
        result = is_within_any_fence(-7.0, 107.0, branches)
        assert result[0] is False
