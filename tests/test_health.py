"""Tests for health check endpoint."""
from fastapi.testclient import TestClient

def test_health_endpoint(client):
    """Health endpoint should return 200 with status info."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "database" in data
    assert "timestamp" in data

def test_health_response_structure(client):
    """Health response should have correct structure."""
    response = client.get("/health")
    data = response.json()
    assert data["status"] in ["healthy", "degraded"]
    assert data["database"] in ["connected", "disconnected"]
