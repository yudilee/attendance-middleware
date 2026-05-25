"""Tests for configuration endpoints."""
from fastapi.testclient import TestClient
from app.database.models import ApiKey


def test_get_device_config(client, db_session):
    """Device config should return branch and punch type info."""
    from app.database.models import Branch, PunchType, DeviceBinding, Employee, BindingBranch
    from app.services.auth import hash_api_key
    
    # Setup
    api_key = "config-test-key"
    db_session.add(ApiKey(key_value=hash_api_key(api_key), label="config-test", is_active=True))
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


def test_smtp_settings(client, db_session):
    """Admin should be able to get and set SMTP configurations."""
    from app.services.auth_ui import create_access_token
    from app.database.models import AdminUser, AppConfig
    
    # Create admin
    admin = AdminUser(username="settingsadmin", hashed_password="hashed_placeholder")
    db_session.add(admin)
    db_session.commit()
    
    # Authenticate via cookie
    token = create_access_token(data={"sub": "settingsadmin"})
    client.cookies.set("dashboard_session", token)
    
    # 1. Post new SMTP settings
    smtp_payload = {
        "smtp_host": "smtp.test.com",
        "smtp_port": 587,
        "smtp_user": "test-user@test.com",
        "smtp_password": "supersecretpassword",
        "hr_email_recipients": "hr1@test.com,hr2@test.com"
    }
    
    post_res = client.post("/ui/app-settings/smtp", json=smtp_payload)
    assert post_res.status_code == 200
    assert post_res.json() == {"status": "success"}
    
    # 2. Get SMTP settings and verify password is NOT returned directly but indicates set=True
    get_res = client.get("/ui/app-settings/smtp")
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert get_data["smtp_host"] == "smtp.test.com"
    assert get_data["smtp_port"] == 587
    assert get_data["smtp_user"] == "test-user@test.com"
    assert get_data["smtp_password_set"] is True
    assert get_data["hr_email_recipients"] == "hr1@test.com,hr2@test.com"
    
    # 3. Modify settings without changing password (using __UNCHANGED__ value)
    smtp_payload_modified = {
        "smtp_host": "smtp.new.com",
        "smtp_port": 465,
        "smtp_user": "new-user@test.com",
        "smtp_password": "__UNCHANGED__",
        "hr_email_recipients": "hr-new@test.com"
    }
    
    post_res2 = client.post("/ui/app-settings/smtp", json=smtp_payload_modified)
    assert post_res2.status_code == 200
    
    # Verify password was preserved
    from app.services.crypto import decrypt_value
    pwd_cfg = db_session.query(AppConfig).filter(AppConfig.key == "smtp_password").first()
    assert decrypt_value(pwd_cfg.value) == "supersecretpassword"
    
    # Verify other values updated
    get_res2 = client.get("/ui/app-settings/smtp")
    assert get_res2.json()["smtp_host"] == "smtp.new.com"
    assert get_res2.json()["smtp_port"] == 465

