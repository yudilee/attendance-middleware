"""Tests for cyclic/alternate shift schedule configurations and reports pairing math."""
import pytest
from datetime import date
from app.database.models import ShiftSchedule, Employee
from app.services.report_service import pair_employee_punches


def test_cyclic_alternate_days_shifts(db_session):
    """
    Test a 2-day cyclic alternate shift schedule:
    - Anchor Date: 2026-05-01 (Friday)
    - Interval Length: 2 days
    - Working Days in Cycle: '1' (day 1 of cycle is work day, day 2 is rest day)
    """
    # Create the cyclic shift schedule
    alternate_shift = ShiftSchedule(
        name="Alternate Days Shift",
        start_time="08:00",
        end_time="17:00",
        working_days="1",
        schedule_type="cyclic",
        interval_days=2,
        anchor_date=date(2026, 5, 1)
    )
    db_session.add(alternate_shift)
    db_session.commit()

    # Create employee assigned to this shift
    employee = Employee(
        employee_id="DW-001",
        full_name="Alternate Daily Worker",
        employee_type="daily_worker",
        shift_schedule_id=alternate_shift.id
    )
    db_session.add(employee)
    db_session.commit()

    # Pair punches across May 1 to May 3
    start = date(2026, 5, 1)
    end = date(2026, 5, 3)
    result = pair_employee_punches(db_session, employee, start, end)

    records = result["daily_records"]
    assert len(records) == 3

    # On May 1 (Day 1 of cycle: 0 elapsed days): Working Day -> Absent (no punches)
    assert records[0]["date"] == date(2026, 5, 1)
    assert records[0]["status"] == "Absent"
    assert records[0]["shift_name"] == "Alternate Days Shift"

    # On May 2 (Day 2 of cycle: 1 elapsed day): Rest Day -> Rest Day
    assert records[1]["date"] == date(2026, 5, 2)
    assert records[1]["status"] == "Rest Day"

    # On May 3 (Day 1 of cycle: 2 elapsed days -> cycle day 1): Working Day -> Absent
    assert records[2]["date"] == date(2026, 5, 3)
    assert records[2]["status"] == "Absent"


def test_cyclic_four_on_two_off_shifts(db_session):
    """
    Test a 4-on-2-off cyclic shift schedule (6 days cycle):
    - Anchor Date: 2026-05-01
    - Interval Length: 6 days
    - Working Days: '1,2,3,4'
    """
    shift = ShiftSchedule(
        name="4-on-2-off Rotation",
        start_time="08:00",
        end_time="17:00",
        working_days="1,2,3,4",
        schedule_type="cyclic",
        interval_days=6,
        anchor_date=date(2026, 5, 1)
    )
    db_session.add(shift)
    db_session.commit()

    employee = Employee(
        employee_id="INT-999",
        full_name="Rotation Intern",
        employee_type="internship",
        shift_schedule_id=shift.id
    )
    db_session.add(employee)
    db_session.commit()

    # Query May 1 to May 7 (7 days total)
    start = date(2026, 5, 1)
    end = date(2026, 5, 7)
    result = pair_employee_punches(db_session, employee, start, end)

    records = result["daily_records"]
    assert len(records) == 7

    # May 1 (Day 1 of cycle: 0 elapsed): Work
    assert records[0]["date"] == date(2026, 5, 1)
    assert records[0]["status"] == "Absent"

    # May 4 (Day 4 of cycle: 3 elapsed): Work
    assert records[3]["date"] == date(2026, 5, 4)
    assert records[3]["status"] == "Absent"

    # May 5 (Day 5 of cycle: 4 elapsed): Rest
    assert records[4]["date"] == date(2026, 5, 5)
    assert records[4]["status"] == "Rest Day"

    # May 6 (Day 6 of cycle: 5 elapsed): Rest
    assert records[5]["date"] == date(2026, 5, 6)
    assert records[5]["status"] == "Rest Day"

    # May 7 (Day 1 of next cycle: 6 elapsed): Work
    assert records[6]["date"] == date(2026, 5, 7)
    assert records[6]["status"] == "Absent"
