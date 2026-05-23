"""Tests for Employee Self-Service Portal and Analytics dashboard."""
import pytest
from datetime import datetime, date
from fastapi.testclient import TestClient

from app.database.models import Employee, Company, Branch, DeviceBinding, BindingBranch, AdminUser, PunchLog, Base


def test_portal_login_and_dashboard(client, db_session):
    """Verify that employees can log in and access their dashboard."""
    Base.metadata.create_all(bind=db_session.bind)
    # Create test employee
    emp = Employee(
        employee_id="99999",
        full_name="Portal Test Employee",
        employee_type="regular",
        is_active=True,
        is_deleted=False
    )
    db_session.add(emp)
    db_session.commit()

    # 1. Access portal login page
    response = client.get("/portal")
    assert response.status_code == 200
    assert "Employee Portal" in response.text

    # 2. Login with valid ID
    response = client.post("/portal/login", data={"employee_id": "99999"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/portal/dashboard"
    
    # Extract session cookie
    cookies = response.cookies
    assert "portal_session" in cookies
    assert cookies["portal_session"] == "99999"

    # 3. Access portal dashboard with session cookie
    response = client.get("/portal/dashboard", cookies=cookies)
    assert response.status_code == 200
    assert "Portal Test Employee" in response.text
    assert "My Attendance" in response.text or "Attendance History" in response.text

    # 4. File a leave request
    response = client.post(
        "/portal/leave-request",
        data={
            "leave_type": "sick",
            "start_date": "2026-05-25",
            "end_date": "2026-05-26",
            "reason": "Recovering from high fever"
        },
        cookies=cookies,
        follow_redirects=False
    )
    assert response.status_code == 302
    assert "success" in response.headers["location"]

    # 5. File an attendance correction
    response = client.post(
        "/portal/correction",
        data={
            "correction_type": "missing_punch",
            "proposed_date": "2026-05-23",
            "proposed_time": "17:00",
            "proposed_punch_type": "Out",
            "description": "Forgot to clock out due to urgent team meeting."
        },
        cookies=cookies,
        follow_redirects=False
    )
    assert response.status_code == 302
    assert "success" in response.headers["location"]


def test_ui_analytics_endpoint(client, db_session):
    """Verify that the /ui/analytics endpoint calculates correct attendance stats."""
    Base.metadata.create_all(bind=db_session.bind)
    # We bypass authentication logic by creating a dummy admin session
    # or setting get_current_admin dependency override.
    # In tests, get_current_admin looks at the cookie "dashboard_session".
    # Let's mock a valid admin cookie or mock the dependency.
    from app.services.auth_ui import create_access_token
    token = create_access_token({"sub": "admin"})
    cookies = {"dashboard_session": token}

    # Create dummy admin user in database
    admin = AdminUser(username="admin", hashed_password="dummy_password", role="superadmin")
    db_session.add(admin)
    
    # Create basic data
    company = Company(name="Analytics Corp", code="AC")
    db_session.add(company)
    db_session.commit()

    branch = Branch(name="HQ Branch", company_id=company.id)
    db_session.add(branch)
    db_session.commit()

    emp1 = Employee(employee_id="E001", full_name="Alice", company_id=company.id, is_active=True, is_deleted=False)
    emp2 = Employee(employee_id="E002", full_name="Bob", company_id=company.id, is_active=True, is_deleted=False)
    db_session.add_all([emp1, emp2])
    db_session.commit()

    # Create active device bindings and branch assignments
    bind1 = DeviceBinding(employee_id="E001", device_uuid="uuid-001", is_active=True)
    bind2 = DeviceBinding(employee_id="E002", device_uuid="uuid-002", is_active=True)
    db_session.add_all([bind1, bind2])
    db_session.commit()

    bb1 = BindingBranch(binding_id=bind1.id, branch_id=branch.id)
    bb2 = BindingBranch(binding_id=bind2.id, branch_id=branch.id)
    db_session.add_all([bb1, bb2])
    db_session.commit()

    # Today's punch logs
    p1 = PunchLog(employee_id="E001", punch_type="In", timestamp=datetime.utcnow(), is_mock_location=False)
    p2 = PunchLog(employee_id="E002", punch_type="In", timestamp=datetime.utcnow(), is_mock_location=True)
    db_session.add_all([p1, p2])
    db_session.commit()

    response = client.get("/ui/analytics", cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    
    # Assert values
    assert data["headcount"] == 2
    assert data["today_present"] == 2
    assert data["today_mocks"] == 1
    assert data["attendance_rate"] == 100.0
    
    # Anomaly Watchtower assertions
    assert len(data["anomalies"]) >= 1
    assert data["anomalies"][0]["employee_name"] == "Bob"
    assert data["anomalies"][0]["anomaly_type"] == "Mock Location"
    assert data["anomalies"][0]["branch_name"] == "HQ Branch"
