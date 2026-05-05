"""Tests for API key authentication."""
import pytest
from fastapi.testclient import TestClient

def test_missing_api_key(client):
    """Request without API key should be rejected."""
    # Use an endpoint that actually requires auth
    response = client.get("/api/v1/punch-types")
    assert response.status_code == 401

def test_invalid_api_key(client):
    """Request with invalid API key should be rejected."""
    response = client.get(
        "/api/v1/punch-types",
        headers={"X-API-Key": "invalid-key"}
    )
    assert response.status_code == 401

def test_valid_api_key(client, db_session):
    """Request with valid API key should succeed."""
    from app.database.models import ApiKey
    
    # Create a test API key (plain key_value, matching verify_api_key logic)
    api_key = "test-api-key-12345"
    api_key_obj = ApiKey(
        key_value=api_key,
        label="test-key",
        is_active=True
    )
    db_session.add(api_key_obj)
    db_session.commit()
    
    response = client.get(
        "/api/v1/punch-types",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200

def test_revoked_api_key(client, db_session):
    """Revoked (inactive) API key should be rejected."""
    from app.database.models import ApiKey
    
    api_key = "revoked-key-123"
    api_key_obj = ApiKey(
        key_value=api_key,
        label="revoked-test",
        is_active=False  # Revoked
    )
    db_session.add(api_key_obj)
    db_session.commit()
    
    response = client.get(
        "/api/v1/punch-types",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 401
