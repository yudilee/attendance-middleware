"""Tests for hierarchical shift resolution and company-branch validation."""
import pytest
from datetime import date
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.database.models import (
    ShiftSchedule, Company, Branch, EmployeeGroup, Employee, DeviceBinding, BindingBranch, ApiKey
)
from app.services.report_service import resolve_employee_shift


def test_hierarchical_shift_resolution(db_session):
    """
    Verify hierarchical shift resolution priority order:
    1. Employee Override
    2. Group Schedule
    3. Branch Default
    4. Company Default
    5. Global Fallback
    """
    # 5. Global Fallback
    global_default = ShiftSchedule(
        name="Global Default Shift",
        start_time="09:00",
        end_time="18:00",
        is_default=True
    )
    db_session.add(global_default)
    db_session.commit()

    # Create company, branch, group, employee objects
    company_shift = ShiftSchedule(name="Company Shift", start_time="08:00", end_time="17:00")
    branch_shift = ShiftSchedule(name="Branch Shift", start_time="07:30", end_time="16:30")
    group_shift = ShiftSchedule(name="Group Shift", start_time="07:00", end_time="16:00")
    employee_shift = ShiftSchedule(name="Employee Override Shift", start_time="10:00", end_time="19:00")

    db_session.add_all([company_shift, branch_shift, group_shift, employee_shift])
    db_session.commit()

    # Create company
    company = Company(name="Test Company", code="TC", shift_schedule_id=company_shift.id)
    db_session.add(company)
    db_session.commit()

    # Create branch
    branch = Branch(name="Test Branch", company_id=company.id, shift_schedule_id=branch_shift.id)
    db_session.add(branch)
    db_session.commit()

    # Create group
    group = EmployeeGroup(name="Test Group", branch_id=branch.id, shift_schedule_id=group_shift.id)
    db_session.add(group)
    db_session.commit()

    # Create employee
    employee = Employee(
        employee_id="12345",
        full_name="John Doe",
        company_id=company.id,
        group_id=group.id,
        shift_schedule_id=employee_shift.id
    )
    db_session.add(employee)
    db_session.commit()

    # Create active device binding and branch assignment (so Branch Default is resolvable)
    binding = DeviceBinding(employee_id="12345", device_uuid="dev-123", is_active=True)
    db_session.add(binding)
    db_session.commit()

    bb = BindingBranch(binding_id=binding.id, branch_id=branch.id)
    db_session.add(bb)
    db_session.commit()

    target_date = date(2026, 5, 23)

    # 1. With everything set: Employee Override wins
    resolved = resolve_employee_shift(db_session, employee, target_date)
    assert resolved.name == "Employee Override Shift"

    # 2. Clear employee override: Group wins
    employee.shift_schedule_id = None
    db_session.commit()
    resolved = resolve_employee_shift(db_session, employee, target_date)
    assert resolved.name == "Group Shift"

    # 3. Clear group shift: Branch wins
    group.shift_schedule_id = None
    db_session.commit()
    resolved = resolve_employee_shift(db_session, employee, target_date)
    assert resolved.name == "Branch Shift"

    # 4. Clear branch shift: Company wins
    branch.shift_schedule_id = None
    db_session.commit()
    resolved = resolve_employee_shift(db_session, employee, target_date)
    assert resolved.name == "Company Shift"

    # 5. Clear company shift: Global fallback wins
    company.shift_schedule_id = None
    db_session.commit()
    resolved = resolve_employee_shift(db_session, employee, target_date)
    assert resolved.name == "Global Default Shift"


def test_device_branch_company_validation(client, db_session):
    """
    Test validation rule: An employee cannot be assigned to a branch belonging to a different company.
    """
    from app.services.auth_ui import create_access_token
    from app.database.models import AdminUser

    # Create admin
    admin = AdminUser(username="testadmin", hashed_password="hashed_placeholder")
    db_session.add(admin)
    db_session.commit()

    # Authenticate via cookie
    token = create_access_token(data={"sub": "testadmin"})
    client.cookies.set("dashboard_session", token)

    # 1. Create two companies
    co1 = Company(name="Company A", code="COA")
    co2 = Company(name="Company B", code="COB")
    db_session.add_all([co1, co2])
    db_session.commit()

    # 2. Create branch under Company B
    branch_b = Branch(name="Branch B under Co B", company_id=co2.id)
    db_session.add(branch_b)
    db_session.commit()

    # 3. Create employee under Company A
    emp = Employee(employee_id="99999", full_name="Company A Worker", company_id=co1.id)
    db_session.add(emp)
    
    # 4. Create active device binding for this employee
    binding = DeviceBinding(employee_id="99999", device_uuid="dev-999", is_active=True)
    db_session.add(binding)
    db_session.commit()

    # 5. Attempting to assign Branch B to Employee A's device should throw a validation error (400)
    response = client.post(
        f"/ui/devices/{binding.id}/branches/{branch_b.id}",
        follow_redirects=False
    )
    assert response.status_code == 400
    assert "company mismatch" in response.json()["detail"].lower()

