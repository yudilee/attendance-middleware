"""Tests for punch submission endpoints."""
import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient


@pytest.fixture
def auth_headers(client, db_session):
    """Create test API key and return auth headers."""
    from app.database.models import ApiKey, DeviceBinding, Employee, PunchType, Branch, BindingBranch
    
    api_key = "punch-test-key"
    api_key_obj = ApiKey(
        key_value=api_key,
        label="punch-test",
        is_active=True
    )
    db_session.add(api_key_obj)
    
    # Create test employee
    emp = Employee(employee_id="TEST001", full_name="Test Employee", is_active=True)
    db_session.add(emp)
    
    # Create test device binding
    binding = DeviceBinding(
        employee_id="TEST001",
        device_uuid="test-uuid-123",
        device_label="Test Device",
        registration_status="active",
        is_active=True
    )
    db_session.add(binding)
    db_session.flush()  # Get binding.id
    
    # Create test punch type
    pt = PunchType(code="in", label="Clock In", color_hex="#00ff00", requires_geofence=False)
    db_session.add(pt)
    
    # Create test branch
    branch = Branch(
        name="Test Branch",
        latitude=-6.2,
        longitude=106.8,
        radius_meters=100
    )
    db_session.add(branch)
    db_session.flush()  # Get branch.id
    
    # Assign branch to device via BindingBranch
    bb = BindingBranch(binding_id=binding.id, branch_id=branch.id)
    db_session.add(bb)
    
    db_session.commit()
    
    return {"X-API-Key": api_key}


def test_submit_punch_success(client, auth_headers):
    """Valid punch submission should succeed."""
    now = datetime.utcnow()
    response = client.post(
        "/api/v1/punch",
        json={
            "employee_id": "TEST001",
            "timestamp": now.isoformat() + "Z",
            "punch_type": "in",
            "latitude": -6.2,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": "test-client-id-001",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,  # UTC timestamp
        },
        headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "log_id" in data


def test_submit_duplicate_punch(client, auth_headers):
    """Same client_punch_id should be treated as duplicate (idempotency)."""
    now = datetime.utcnow()
    payload = {
        "employee_id": "TEST001",
        "timestamp": now.isoformat() + "Z",
        "punch_type": "in",
        "latitude": -6.2,
        "longitude": 106.8,
        "device_uuid": "test-uuid-123",
        "client_punch_id": "duplicate-id-001",
        "biometric_verified": True,
        "is_mock_location": False,
        "gps_time_validated": True,
        "tz_offset_minutes": 0,  # UTC timestamp
    }
    
    # First should succeed
    r1 = client.post("/api/v1/punch", json=payload, headers=auth_headers)
    assert r1.status_code == 200
    
    # Second should be accepted as duplicate (returns success, not 409)
    r2 = client.post("/api/v1/punch", json=payload, headers=auth_headers)
    assert r2.status_code == 200
    data = r2.json()
    assert "duplicate" in data.get("message", "").lower()


def test_submit_punch_future_timestamp(client, auth_headers):
    """Punch with timestamp >5 minutes in the future should be rejected."""
    future = datetime.utcnow() + timedelta(minutes=10)
    response = client.post(
        "/api/v1/punch",
        json={
            "employee_id": "TEST001",
            "timestamp": future.isoformat() + "Z",
            "punch_type": "in",
            "latitude": -6.2,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": "future-test",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,  # UTC timestamp
        },
        headers=auth_headers
    )
    assert response.status_code == 422  # Time deviation too large


def test_batch_punch_submission(client, auth_headers):
    """Batch punch submission should process all punches."""
    now = datetime.utcnow()
    punches = []
    for i in range(3):
        punches.append({
            "employee_id": "TEST001",
            "timestamp": (now - timedelta(hours=i)).isoformat() + "Z",
            "punch_type": "in" if i % 2 == 0 else "out",
            "latitude": -6.2,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": f"batch-test-{i}",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,  # UTC timestamp
        })
    
    response = client.post(
        "/api/v1/punch/batch",
        json={"punches": punches},
        headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["synced"] >= 0
    assert data["failed"] >= 0
    assert len(data["results"]) == 3


def test_submit_punch_geofencing_with_checkpoints(client, db_session, auth_headers):
    """Test geofencing validation against branch center and checkpoints."""
    from app.database.models import Branch, BranchCheckpoint, PunchType

    # Update the "in" punch type to require geofence
    pt = db_session.query(PunchType).filter(PunchType.code == "in").first()
    pt.requires_geofence = True

    # Main branch is at latitude=-6.2, longitude=106.8, radius=100m.
    # 1. A punch at exact center (-6.2, 106.8) should succeed.
    now = datetime.utcnow()
    res1 = client.post(
        "/api/v1/punch",
        json={
            "employee_id": "TEST001",
            "timestamp": now.isoformat() + "Z",
            "punch_type": "in",
            "latitude": -6.2,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": "geo-center-test",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,
        },
        headers=auth_headers
    )
    assert res1.status_code == 200

    # 2. A punch outside the branch center radius (e.g., ~1.1km away at -6.19, 106.8) should fail.
    res2 = client.post(
        "/api/v1/punch",
        json={
            "employee_id": "TEST001",
            "timestamp": now.isoformat() + "Z",
            "punch_type": "in",
            "latitude": -6.19,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": "geo-outside-test",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,
        },
        headers=auth_headers
    )
    assert res2.status_code == 403
    assert "outside assigned branches" in res2.json()["detail"].lower()

    # 3. Create a checkpoint at -6.19, 106.8 (radius 100m) for the branch.
    branch = db_session.query(Branch).filter(Branch.name == "Test Branch").first()
    checkpoint = BranchCheckpoint(
        branch_id=branch.id,
        name="Test Checkpoint A",
        latitude=-6.19,
        longitude=106.8,
        radius_meters=100.0,
        is_active=True
    )
    db_session.add(checkpoint)
    db_session.commit()

    # 4. Now, the same punch at -6.19, 106.8 should succeed because it lies within the active checkpoint!
    res3 = client.post(
        "/api/v1/punch",
        json={
            "employee_id": "TEST001",
            "timestamp": now.isoformat() + "Z",
            "punch_type": "in",
            "latitude": -6.19,
            "longitude": 106.8,
            "device_uuid": "test-uuid-123",
            "client_punch_id": "geo-checkpoint-test",
            "biometric_verified": True,
            "is_mock_location": False,
            "gps_time_validated": True,
            "tz_offset_minutes": 0,
        },
        headers=auth_headers
    )
    assert res3.status_code == 200

