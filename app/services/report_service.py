"""
Punch pairing, shift schedule resolution, and openpyxl Excel report generation service.
"""
import datetime
from datetime import timedelta, date, datetime as dt_class
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func

from app.database.models import (
    Employee, PunchLog, Branch, Company, EmployeeGroup,
    ShiftSchedule, Holiday, LeaveRequest, AuditLog
)

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter


def resolve_employee_shift(db: Session, employee: Employee, target_date: date) -> ShiftSchedule:
    """
    Resolves the active shift schedule for an employee on a target date based on a 5-tier hierarchy:
    1. Employee Override
    2. Group Schedule
    3. Branch Default
    4. Company Default
    5. Global Fallback
    """
    # 1. Employee Override
    if employee.shift_schedule_id:
        schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == employee.shift_schedule_id).first()
        if schedule:
            return schedule

    # 2. Group Schedule
    if employee.group_id:
        group = db.query(EmployeeGroup).filter(EmployeeGroup.id == employee.group_id).first()
        if group and group.shift_schedule_id:
            schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == group.shift_schedule_id).first()
            if schedule:
                return schedule

    # 3. Branch Default (We use their primary branch checkpoint or assigned branches)
    # Get any active branch assignment for the employee's device/bindings
    from app.database.models import DeviceBinding, BindingBranch
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.employee_id == employee.employee_id,
        DeviceBinding.is_active == True
    ).first()
    if binding:
        branch_assignment = db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).first()
        if branch_assignment:
            branch = db.query(Branch).filter(Branch.id == branch_assignment.branch_id).first()
            if branch:
                # Check branch shift
                if branch.shift_schedule_id:
                    schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == branch.shift_schedule_id).first()
                    if schedule:
                        return schedule
                # 4. Company Default (from Branch's company)
                if branch.company_id:
                    company = db.query(Company).filter(Company.id == branch.company_id).first()
                    if company and company.shift_schedule_id:
                        schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == company.shift_schedule_id).first()
                        if schedule:
                            return schedule

    # 4. Company Default (from Employee's company direct)
    if employee.company_id:
        company = db.query(Company).filter(Company.id == employee.company_id).first()
        if company and company.shift_schedule_id:
            schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == company.shift_schedule_id).first()
            if schedule:
                return schedule

    # 5. Global Fallback
    fallback = db.query(ShiftSchedule).filter(ShiftSchedule.is_default == True).first()
    if fallback:
        return fallback

    # Hardcoded default fallback
    return ShiftSchedule(
        name="Standard Office Hours",
        start_time="08:00",
        end_time="17:00",
        grace_minutes=15,
        min_work_hours=8.0,
        overtime_after_hours=9.0,
        working_days="1,2,3,4,5",
        schedule_type="weekly",
        interval_days=None,
        anchor_date=None
    )


def pair_employee_punches(
    db: Session,
    employee: Employee,
    start_date: date,
    end_date: date
) -> Dict[str, Any]:
    """
    Pairs In/Out punches for an employee daily in a specific date range,
    resolving shift schedules and detecting absences, lates, holidays, leaves, and anomalies.
    """
    # Fetch all punches in range (expanded by 1 day to catch timezone offset overlaps safely)
    db_start = datetime.datetime.combine(start_date - timedelta(days=1), datetime.time.min)
    db_end = datetime.datetime.combine(end_date + timedelta(days=1), datetime.time.max)

    punches = db.query(PunchLog).filter(
        PunchLog.employee_id == employee.employee_id,
        PunchLog.timestamp >= db_start,
        PunchLog.timestamp <= db_end
    ).order_by(PunchLog.timestamp).all()

    # Determine primary branch offset or default
    offset_hours = 7.0
    from app.database.models import DeviceBinding, BindingBranch
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.employee_id == employee.employee_id,
        DeviceBinding.is_active == True
    ).first()
    if binding:
        ba = db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).first()
        if ba:
            branch = db.query(Branch).filter(Branch.id == ba.branch_id).first()
            if branch and branch.timezone_offset is not None:
                offset_hours = float(branch.timezone_offset)

    # Group punches by local date
    punches_by_date: Dict[date, List[PunchLog]] = {}
    for p in punches:
        # Convert UTC punch timestamp to local branch time
        tz_offset = p.tz_offset_minutes if p.tz_offset_minutes is not None else int(offset_hours * 60)
        local_time = p.timestamp + timedelta(minutes=tz_offset)
        local_date = local_time.date()
        
        if start_date <= local_date <= end_date:
            if local_date not in punches_by_date:
                punches_by_date[local_date] = []
            punches_by_date[local_date].append(p)

    # Fetch holidays in range
    holidays = db.query(Holiday).filter(Holiday.date >= start_date, Holiday.date <= end_date).all()
    holiday_dates = {h.date: h.name for h in holidays}

    # Fetch approved leave requests in range
    leaves = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == employee.employee_id,
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= end_date,
        LeaveRequest.end_date >= start_date
    ).all()
    
    leave_dates = {}
    for l in leaves:
        curr = l.start_date
        while curr <= l.end_date:
            if start_date <= curr <= end_date:
                leave_dates[curr] = l.leave_type
            curr += timedelta(days=1)

    daily_records = []
    total_present = 0
    total_absent = 0
    total_late = 0
    total_hours = 0.0
    total_overtime = 0.0
    anomalies_count = 0

    curr_date = start_date
    while curr_date <= end_date:
        shift = resolve_employee_shift(db, employee, curr_date)
        day_punches = punches_by_date.get(curr_date, [])

        # Parse shift details
        working_days = [int(x.strip()) for x in shift.working_days.split(",") if x.strip().isdigit()]
        
        # Determine if shift is weekly or cyclic
        schedule_type = getattr(shift, "schedule_type", "weekly") or "weekly"
        if schedule_type == "cyclic" and shift.interval_days and shift.anchor_date:
            anchor = shift.anchor_date
            if isinstance(anchor, str):
                try:
                    anchor = datetime.datetime.strptime(anchor, "%Y-%m-%d").date()
                except ValueError:
                    anchor = None
            elif isinstance(anchor, datetime.datetime):
                anchor = anchor.date()
                
            if anchor:
                days_elapsed = (curr_date - anchor).days
                cycle_day = (days_elapsed % shift.interval_days) + 1
                is_working_day = (cycle_day in working_days)
            else:
                is_working_day = (curr_date.isoweekday() in working_days)
        else:
            is_working_day = (curr_date.isoweekday() in working_days)
        is_holiday = curr_date in holiday_dates
        is_leave = curr_date in leave_dates

        record = {
            "date": curr_date,
            "shift_name": shift.name,
            "shift_start": shift.start_time,
            "shift_end": shift.end_time,
            "first_in": None,
            "last_out": None,
            "work_hours": 0.0,
            "overtime_hours": 0.0,
            "status": "Absent",  # Default status
            "is_late": False,
            "missing_punch": False,
            "anomalies": []
        }

        # Check leaves/holidays first if absent
        if is_holiday:
            record["status"] = f"Holiday ({holiday_dates[curr_date]})"
        elif is_leave:
            record["status"] = f"Leave ({leave_dates[curr_date].capitalize()})"
        elif not is_working_day:
            record["status"] = "Rest Day"

        if day_punches:
            # Pair In/Out punches
            # Find earliest In and latest Out
            in_punches = [p for p in day_punches if p.punch_type.lower() in ["in", "check in"]]
            out_punches = [p for p in day_punches if p.punch_type.lower() in ["out", "check out"]]

            first_in_log = in_punches[0] if in_punches else None
            last_out_log = out_punches[-1] if out_punches else None

            # Get local datetime values
            if first_in_log:
                tz_in = first_in_log.tz_offset_minutes if first_in_log.tz_offset_minutes is not None else int(offset_hours * 60)
                record["first_in"] = first_in_log.timestamp + timedelta(minutes=tz_in)
                # Check for mock location or off-site geofence violations
                if first_in_log.is_mock_location:
                    record["anomalies"].append("mock_location")
                
            if last_out_log:
                tz_out = last_out_log.tz_offset_minutes if last_out_log.tz_offset_minutes is not None else int(offset_hours * 60)
                record["last_out"] = last_out_log.timestamp + timedelta(minutes=tz_out)
                if last_out_log.is_mock_location:
                    record["anomalies"].append("mock_location")

            # Validate pairing
            if first_in_log and last_out_log:
                # Proper pairing
                delta = record["last_out"] - record["first_in"]
                work_duration = max(0.0, delta.total_seconds() / 3600.0)
                record["work_hours"] = round(work_duration, 2)
                
                # Overtime
                if work_duration > shift.overtime_after_hours:
                    record["overtime_hours"] = round(work_duration - shift.overtime_after_hours, 2)
                    total_overtime += record["overtime_hours"]

                total_hours += record["work_hours"]
                record["status"] = "Present"
                total_present += 1

                # Late detection
                try:
                    sh_hour, sh_min = map(int, shift.start_time.split(":"))
                    shift_expected = dt_class.combine(curr_date, datetime.time(sh_hour, sh_min))
                    grace_time = shift_expected + timedelta(minutes=shift.grace_minutes)
                    
                    if record["first_in"] > grace_time:
                        record["is_late"] = True
                        total_late += 1
                except Exception:
                    pass

            else:
                # Missing punch anomaly
                record["missing_punch"] = True
                record["anomalies"].append("missing_punch")
                record["status"] = "Missing Punch"
                anomalies_count += 1
                total_present += 1  # Still count as present but flagged

        else:
            # No punches recorded
            if is_working_day and not is_holiday and not is_leave:
                total_absent += 1
                record["status"] = "Absent"

        daily_records.append(record)
        curr_date += timedelta(days=1)

    return {
        "employee_id": employee.employee_id,
        "full_name": employee.full_name,
        "department": employee.department,
        "employee_type": employee.employee_type or "regular",
        "present_days": total_present,
        "absent_days": total_absent,
        "late_days": total_late,
        "total_hours": round(total_hours, 2),
        "total_overtime": round(total_overtime, 2),
        "anomalies_count": anomalies_count,
        "daily_records": daily_records
    }


def generate_excel_report(
    db: Session,
    start_date: date,
    end_date: date,
    company_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    group_id: Optional[int] = None,
    department: Optional[str] = None
) -> openpyxl.Workbook:
    """
    Generates a beautifully styled, premium HR monthly/weekly Excel sheet using openpyxl.
    Features 4 detailed sheets: Summary, Daily Breakdown, Raw Logs, and Anomalies.
    """
    wb = openpyxl.Workbook()
    
    # ── Fetch filtered Employees ──
    query = db.query(Employee).filter(Employee.is_deleted == False)
    if company_id:
        query = query.filter(Employee.company_id == company_id)
    if group_id:
        query = query.filter(Employee.group_id == group_id)
    if department:
        query = query.filter(Employee.department == department)
        
    # If branch filtering is active, fetch only employees with active bindings to that branch
    if branch_id:
        from app.database.models import DeviceBinding, BindingBranch
        bindings = db.query(DeviceBinding.employee_id).outerjoin(
            BindingBranch, DeviceBinding.id == BindingBranch.binding_id
        ).filter(
            or_(DeviceBinding.branch_id == branch_id, BindingBranch.branch_id == branch_id)
        ).subquery()
        query = query.filter(Employee.employee_id.in_(bindings))

    employees = query.order_by(Employee.full_name).all()

    # Pre-calculate data for each employee
    summaries = []
    for emp in employees:
        summaries.append(pair_employee_punches(db, emp, start_date, end_date))

    # ── Styling Configs ──
    font_family = "Segoe UI"
    header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid") # Indigo 600
    stripe_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid") # Slate 50
    alert_fill = PatternFill(start_color="FEF2F2", end_color="FEF2F2", fill_type="solid") # Red 50
    warning_fill = PatternFill(start_color="FFFBEB", end_color="FFFBEB", fill_type="solid") # Amber 50
    header_font = Font(name=font_family, size=11, bold=True, color="FFFFFF")
    title_font = Font(name=font_family, size=16, bold=True, color="1E293B")
    subtitle_font = Font(name=font_family, size=10, italic=True, color="64748B")
    body_font = Font(name=font_family, size=10, color="334155")
    bold_font = Font(name=font_family, size=10, bold=True, color="1E293B")
    red_bold_font = Font(name=font_family, size=10, bold=True, color="DC2626")
    
    thin_border_side = Side(border_style="thin", color="E2E8F0")
    thin_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    thick_bottom_side = Side(border_style="medium", color="CBD5E1")
    header_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thick_bottom_side)

    # ═══════════════════ SHEET 1: SUMMARY ═══════════════════
    ws_summary = wb.active
    ws_summary.title = "Overview Summary"
    ws_summary.views.sheetView[0].showGridLines = True

    # Title Banner
    ws_summary["A1"] = "EMPLOYEE ATTENDANCE REPORT"
    ws_summary["A1"].font = title_font
    ws_summary["A2"] = f"Date Range: {start_date} to {end_date} | Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws_summary["A2"].font = subtitle_font
    
    ws_summary.row_dimensions[1].height = 28
    ws_summary.row_dimensions[2].height = 18

    # Headers
    headers_summary = ["PIN / ID", "Full Name", "Department", "Type", "Days Present", "Days Absent", "Days Late", "Total Hours", "Overtime (Hrs)", "Anomalies"]
    for col_idx, h in enumerate(headers_summary, 1):
        cell = ws_summary.cell(row=4, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_border

    ws_summary.row_dimensions[4].height = 26

    # Data Rows
    row_num = 5
    for s in summaries:
        r_cells = [
            ws_summary.cell(row=row_num, column=1, value=s["employee_id"]),
            ws_summary.cell(row=row_num, column=2, value=s["full_name"]),
            ws_summary.cell(row=row_num, column=3, value=s["department"] or "—"),
            ws_summary.cell(row=row_num, column=4, value=s["employee_type"].upper()),
            ws_summary.cell(row=row_num, column=5, value=s["present_days"]),
            ws_summary.cell(row=row_num, column=6, value=s["absent_days"]),
            ws_summary.cell(row=row_num, column=7, value=s["late_days"]),
            ws_summary.cell(row=row_num, column=8, value=s["total_hours"]),
            ws_summary.cell(row=row_num, column=9, value=s["total_overtime"]),
            ws_summary.cell(row=row_num, column=10, value=s["anomalies_count"])
        ]

        # Apply basic fonts and borders
        for cell in r_cells:
            cell.font = body_font
            cell.border = thin_border
            # Zebra striping
            if row_num % 2 == 0:
                cell.fill = stripe_fill
            # Highlight lates/anomalies
            if cell.column == 7 and s["late_days"] > 0:
                cell.font = bold_font
                cell.fill = warning_fill
            if cell.column == 10 and s["anomalies_count"] > 0:
                cell.font = red_bold_font
                cell.fill = alert_fill

        ws_summary.row_dimensions[row_num].height = 20
        row_num += 1

    # Totals/Average Row
    total_row = row_num
    ws_summary.cell(row=total_row, column=1, value="TOTAL / AVG").font = bold_font
    ws_summary.cell(row=total_row, column=1).border = thin_border
    ws_summary.cell(row=total_row, column=2, value=f"{len(summaries)} Employees").font = bold_font
    ws_summary.cell(row=total_row, column=2).border = thin_border
    
    # Formulas
    for col_idx, col_name in [(5, "E"), (6, "F"), (7, "G"), (8, "H"), (9, "I"), (10, "J")]:
        cell = ws_summary.cell(row=total_row, column=col_idx, value=f"=SUM({col_name}5:{col_name}{total_row-1})")
        cell.font = bold_font
        cell.border = thin_border
        
    ws_summary.row_dimensions[total_row].height = 22

    # ═══════════════════ SHEET 2: DAILY BREAKDOWN ═══════════════════
    ws_daily = wb.create_sheet(title="Daily Breakdown")
    ws_daily.views.sheetView[0].showGridLines = True

    ws_daily["A1"] = "DAILY ATTENDANCE LOG"
    ws_daily["A1"].font = title_font
    ws_daily["A2"] = "Detailed punch-pairings and schedule validation daily logs"
    ws_daily["A2"].font = subtitle_font
    
    ws_daily.row_dimensions[1].height = 28
    ws_daily.row_dimensions[2].height = 18

    headers_daily = ["Date", "PIN / ID", "Full Name", "Department", "Shift Schedule", "First IN", "Last OUT", "Hours", "Overtime", "Status", "Late?", "Anomalies"]
    for col_idx, h in enumerate(headers_daily, 1):
        cell = ws_daily.cell(row=4, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_border

    ws_daily.row_dimensions[4].height = 26

    daily_row_idx = 5
    for s in summaries:
        for r in s["daily_records"]:
            in_time_str = r["first_in"].strftime("%H:%M:%S") if r["first_in"] else "—"
            out_time_str = r["last_out"].strftime("%H:%M:%S") if r["last_out"] else "—"
            anom_str = ", ".join(r["anomalies"]) if r["anomalies"] else "None"

            r_cells = [
                ws_daily.cell(row=daily_row_idx, column=1, value=r["date"].strftime("%Y-%m-%d")),
                ws_daily.cell(row=daily_row_idx, column=2, value=s["employee_id"]),
                ws_daily.cell(row=daily_row_idx, column=3, value=s["full_name"]),
                ws_daily.cell(row=daily_row_idx, column=4, value=s["department"] or "—"),
                ws_daily.cell(row=daily_row_idx, column=5, value=r["shift_name"]),
                ws_daily.cell(row=daily_row_idx, column=6, value=in_time_str),
                ws_daily.cell(row=daily_row_idx, column=7, value=out_time_str),
                ws_daily.cell(row=daily_row_idx, column=8, value=r["work_hours"]),
                ws_daily.cell(row=daily_row_idx, column=9, value=r["overtime_hours"]),
                ws_daily.cell(row=daily_row_idx, column=10, value=r["status"]),
                ws_daily.cell(row=daily_row_idx, column=11, value="LATE" if r["is_late"] else "—"),
                ws_daily.cell(row=daily_row_idx, column=12, value=anom_str.upper())
            ]

            for cell in r_cells:
                cell.font = body_font
                cell.border = thin_border
                
                # Stripe color grouping by employee ID for clean visual breaks
                if int(s["employee_id"]) % 2 == 0:
                    cell.fill = stripe_fill

                # Highlights lates, leaves, missing
                if cell.column == 10:
                    if r["status"] == "Absent":
                        cell.fill = alert_fill
                        cell.font = red_bold_font
                    elif "Leave" in r["status"]:
                        cell.fill = PatternFill(start_color="EFF6FF", end_color="EFF6FF", fill_type="solid") # Blue 50
                        cell.font = Font(name=font_family, size=10, color="1D4ED8", bold=True)
                    elif "Holiday" in r["status"]:
                        cell.fill = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid") # Emerald 50
                        cell.font = Font(name=font_family, size=10, color="047857", bold=True)

                if cell.column == 11 and r["is_late"]:
                    cell.fill = warning_fill
                    cell.font = bold_font
                if cell.column == 12 and r["missing_punch"]:
                    cell.fill = alert_fill
                    cell.font = red_bold_font

            ws_daily.row_dimensions[daily_row_idx].height = 20
            daily_row_idx += 1

    # ═══════════════════ SHEET 3: RAW LOGS ═══════════════════
    ws_raw = wb.create_sheet(title="Raw Punch Logs")
    ws_raw.views.sheetView[0].showGridLines = True

    ws_raw["A1"] = "RAW PUNCH DATABASE"
    ws_raw["A1"].font = title_font
    
    headers_raw = ["Punch ID", "PIN / ID", "Full Name", "Timestamp (UTC)", "Local Time", "Punch Type", "Mock GPS?", "Validated GPS?", "Branch / Checkpoint", "ADMS Sync Status"]
    for col_idx, h in enumerate(headers_raw, 1):
        cell = ws_raw.cell(row=3, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_border

    ws_raw.row_dimensions[3].height = 26

    # Fetch raw punch logs in target window
    raw_query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id).\
        filter(PunchLog.timestamp >= db_start, PunchLog.timestamp <= db_end)
        
    if company_id:
        raw_query = raw_query.filter(Employee.company_id == company_id)
    if group_id:
        raw_query = raw_query.filter(Employee.group_id == group_id)
    if department:
        raw_query = raw_query.filter(Employee.department == department)
        
    if branch_id:
        from app.database.models import DeviceBinding, BindingBranch
        bindings = db.query(DeviceBinding.employee_id).outerjoin(
            BindingBranch, DeviceBinding.id == BindingBranch.binding_id
        ).filter(
            or_(DeviceBinding.branch_id == branch_id, BindingBranch.branch_id == branch_id)
        ).subquery()
        raw_query = raw_query.filter(PunchLog.employee_id.in_(bindings))

    raw_logs = raw_query.order_by(PunchLog.timestamp.desc()).all()

    raw_row_idx = 4
    for log, name in raw_logs:
        tz_offset = log.tz_offset_minutes if log.tz_offset_minutes is not None else 420
        local_t = log.timestamp + timedelta(minutes=tz_offset)
        
        # Get nearest branch name for reporting
        branch_desc = "Unknown"
        # Try to resolve best branch
        binding_device = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == log.device_uuid).first()
        if binding_device:
            ba = db.query(BindingBranch).filter(BindingBranch.binding_id == binding_device.id).first()
            if ba:
                branch = db.query(Branch).filter(Branch.id == ba.branch_id).first()
                if branch:
                    branch_desc = branch.name

        r_cells = [
            ws_raw.cell(row=raw_row_idx, column=1, value=log.id),
            ws_raw.cell(row=raw_row_idx, column=2, value=log.employee_id),
            ws_raw.cell(row=raw_row_idx, column=3, value=name or "—"),
            ws_raw.cell(row=raw_row_idx, column=4, value=log.timestamp.strftime("%Y-%m-%d %H:%M:%S")),
            ws_raw.cell(row=raw_row_idx, column=5, value=local_t.strftime("%Y-%m-%d %H:%M:%S")),
            ws_raw.cell(row=raw_row_idx, column=6, value=log.punch_type.upper()),
            ws_raw.cell(row=raw_row_idx, column=7, value="MOCK" if log.is_mock_location else "NORMAL"),
            ws_raw.cell(row=raw_row_idx, column=8, value="YES" if log.gps_time_validated else "NO"),
            ws_raw.cell(row=raw_row_idx, column=9, value=branch_desc),
            ws_raw.cell(row=raw_row_idx, column=10, value=log.adms_status.upper())
        ]

        for cell in r_cells:
            cell.font = body_font
            cell.border = thin_border
            if raw_row_idx % 2 == 0:
                cell.fill = stripe_fill
            if cell.column == 7 and log.is_mock_location:
                cell.fill = alert_fill
                cell.font = red_bold_font
            if cell.column == 10 and log.adms_status == "local_only":
                cell.fill = PatternFill(start_color="F0F9FF", end_color="F0F9FF", fill_type="solid") # Light Blue 50
                cell.font = Font(name=font_family, size=10, color="0369A1", bold=True)

        ws_raw.row_dimensions[raw_row_idx].height = 20
        raw_row_idx += 1

    # ═══════════════════ SHEET 4: ANOMALIES ═══════════════════
    ws_anom = wb.create_sheet(title="Security & Anomalies")
    ws_anom.views.sheetView[0].showGridLines = True

    ws_anom["A1"] = "ANOMALY & SECURITY LOG"
    ws_anom["A1"].font = title_font
    ws_anom["A2"] = "Flags missing punches, mock locations, or out-of-bounds clock-ins"
    ws_anom["A2"].font = subtitle_font

    headers_anom = ["Date / Time", "PIN / ID", "Full Name", "Type", "Anomaly Type", "Details", "Device ID", "IP / GPS Coordinates"]
    for col_idx, h in enumerate(headers_anom, 1):
        cell = ws_anom.cell(row=4, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = PatternFill(start_color="DC2626", end_color="DC2626", fill_type="solid") # Red 600
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = header_border

    ws_anom.row_dimensions[4].height = 26

    anom_row_idx = 5
    
    # 1. Add missing punches from Daily pairing records
    for s in summaries:
        for r in s["daily_records"]:
            if r["missing_punch"]:
                r_cells = [
                    ws_anom.cell(row=anom_row_idx, column=1, value=r["date"].strftime("%Y-%m-%d")),
                    ws_anom.cell(row=anom_row_idx, column=2, value=s["employee_id"]),
                    ws_anom.cell(row=anom_row_idx, column=3, value=s["full_name"]),
                    ws_anom.cell(row=anom_row_idx, column=4, value=s["employee_type"].upper()),
                    ws_anom.cell(row=anom_row_idx, column=5, value="MISSING OUT PUNCH"),
                    ws_anom.cell(row=anom_row_idx, column=6, value=f"Clocked IN at {r['first_in'].strftime('%H:%M:%S') if r['first_in'] else '—'} but failed to clock OUT."),
                    ws_anom.cell(row=anom_row_idx, column=7, value="—"),
                    ws_anom.cell(row=anom_row_idx, column=8, value="—")
                ]
                for cell in r_cells:
                    cell.font = body_font
                    cell.border = thin_border
                    cell.fill = alert_fill
                ws_anom.row_dimensions[anom_row_idx].height = 20
                anom_row_idx += 1

    # 2. Add mock locations & validated GPS anomalies from raw logs
    for log, name in raw_logs:
        if log.is_mock_location:
            tz_offset = log.tz_offset_minutes if log.tz_offset_minutes is not None else 420
            local_t = log.timestamp + timedelta(minutes=tz_offset)
            
            # Resolve type
            emp_obj = db.query(Employee).filter(Employee.employee_id == log.employee_id).first()
            emp_type = emp_obj.employee_type.upper() if emp_obj else "REGULAR"

            r_cells = [
                ws_anom.cell(row=anom_row_idx, column=1, value=local_t.strftime("%Y-%m-%d %H:%M:%S")),
                ws_anom.cell(row=anom_row_idx, column=2, value=log.employee_id),
                ws_anom.cell(row=anom_row_idx, column=3, value=name or "—"),
                ws_anom.cell(row=anom_row_idx, column=4, value=emp_type),
                ws_anom.cell(row=anom_row_idx, column=5, value="MOCK GPS TAMPERING"),
                ws_anom.cell(row=anom_row_idx, column=6, value=f"Mobile app reported mock GPS spoofing was active during this clock {log.punch_type}."),
                ws_anom.cell(row=anom_row_idx, column=7, value=log.device_uuid[:15] + "..."),
                ws_anom.cell(row=anom_row_idx, column=8, value=f"{log.latitude:.5f}, {log.longitude:.5f}")
            ]
            for cell in r_cells:
                cell.font = body_font
                cell.border = thin_border
                cell.fill = alert_fill
            ws_anom.row_dimensions[anom_row_idx].height = 20
            anom_row_idx += 1

    if anom_row_idx == 5:
        ws_anom.cell(row=5, column=1, value="No security anomalies flagged in this date window.").font = body_font

    # ── Auto-Fit Columns ──
    for ws in [ws_summary, ws_daily, ws_raw, ws_anom]:
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = str(cell.value or '')
                if cell.row < 3 and cell.column == 1:
                    continue  # Ignore banners for width
                if len(val) > max_len:
                    max_len = len(val)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

    return wb
