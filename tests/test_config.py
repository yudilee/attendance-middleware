"""Tests for configuration endpoints."""
from fastapi.testclient import TestClient
from app.database.models import ApiKey


def test_get_device_config(client, db_session):
    """Device config should return branch and punch type info."""
    from app.database.models import Branch, PunchType, DeviceBinding, Employee, BindingBranch
    
    # Setup
    api_key = "config-test-key"
    db_session.add(ApiKey(key_value=api_key, label="config-test", is_active=True))
    emp = Employee(employee_id="CFG001", full_name="Config Test", is_active=True)
    db_session.add(emp)
    binding = DeviceBinding(
        employee_id="CFG001",
        device_uuid="cfg-uuid",
        device_label="Config Device",
        registration_status="active",
        is_active=True
    )
    db_session.add(binding)
    db_session.flush()
    
    branch = Branch(name="Config Branch", latitude=-6.2, longitude=106.8, radius_meters=100)
    db_session.add(branch)
    db_session.flush()
    
    db_session.add(BindingBranch(binding_id=binding.id, branch_id=branch.id))
    db_session.add(PunchType(code="in", label="Clock In", color_hex="#00ff00", requires_geofence=False))
    db_session.commit()
    
    response = client.get(
        "/api/v1/device-config?device_uuid=cfg-uuid",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "branches" in data
