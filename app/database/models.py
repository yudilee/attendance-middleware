from sqlalchemy import Column, Integer, String, DateTime, Date, Float, Boolean, create_engine, ForeignKey, UniqueConstraint, Index, func, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime
import os

Base = declarative_base()


class DeviceBinding(Base):
    __tablename__ = "device_bindings"
    __table_args__ = (
        Index('idx_device_binding_employee', 'employee_id'),
        Index('idx_device_binding_device', 'device_uuid'),
    )
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, index=True, nullable=True)
    device_uuid = Column(String, index=True)
    branch_id = Column(Integer, nullable=True)                      # Deprecated — migrated to BindingBranch
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    # Track which API key was used for registration
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)
    api_key = relationship("ApiKey")

    # ── Registration workflow ──────────────────────────────────────────────
    device_label = Column(String, nullable=True)                    # e.g. "John's Samsung A55"
    registration_status = Column(String, default="pending_approval")
    # States: pending_approval → approved → active | suspended
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, nullable=True)                     # admin username
    notes = Column(String, nullable=True)
    # ── Multi-device support ───────────────────────────────────────────────
    is_active = Column(Boolean, default=True)  # Admin can toggle per-device
    # ── Push Notifications ──────────────────────────────────────────────────
    fcm_token = Column(String(500), nullable=True)                 # Firebase Cloud Messaging token
    # ── Security ────────────────────────────────────────────────────────────
    device_secret = Column(String(100), nullable=True)             # HMAC signature verification secret


class ADMSTarget(Base):
    __tablename__ = "adms_targets"
    id = Column(Integer, primary_key=True, index=True)
    server_url = Column(String, default="")
    serial_number = Column(String, default="")
    device_name = Column(String, default="Mobile Gateway")
    is_active = Column(Boolean, default=True)
    timezone_offset = Column(Integer, default=7)   # Default GMT+7 (WIB)
    last_contact = Column(DateTime, nullable=True)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="admin")  # "superadmin", "admin", "manager", "operator"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ShiftSchedule(Base):
    __tablename__ = "shift_schedules"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    start_time = Column(String(5), default="08:00")
    end_time = Column(String(5), default="17:00")
    grace_minutes = Column(Integer, default=15)
    min_work_hours = Column(Float, default=8.0)
    overtime_after_hours = Column(Float, default=9.0)
    working_days = Column(String(50), default="1,2,3,4,5")  # 1=Mon, 7=Sun or cycle indexes
    is_default = Column(Boolean, default=False)
    schedule_type = Column(String(50), default="weekly", nullable=True)  # "weekly", "cyclic"
    interval_days = Column(Integer, nullable=True)
    anchor_date = Column(Date, nullable=True)
    # Tiered Overtime Policy Engine (Phase 5B)
    overtime_multiplier_1 = Column(Float, default=1.5)
    overtime_multiplier_2 = Column(Float, default=2.0)
    overtime_threshold_2_hours = Column(Float, nullable=True)
    weekend_overtime_multiplier = Column(Float, default=2.0)
    holiday_overtime_multiplier = Column(Float, default=3.0)
    monthly_overtime_cap_hours = Column(Float, nullable=True)
    # Auto Clock-Out Configs (Phase 5C)
    auto_clockout_enabled = Column(Boolean, default=False)
    auto_clockout_buffer_minutes = Column(Integer, default=60)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    code = Column(String(50), unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True)
    shift_schedule_id = Column(Integer, ForeignKey("shift_schedules.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    shift_schedule = relationship("ShiftSchedule", foreign_keys=[shift_schedule_id])


class Branch(Base):
    """Configurable branch site for geofencing."""
    __tablename__ = "branches"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="Default Office")
    latitude = Column(Float, default=0.0)
    longitude = Column(Float, default=0.0)
    radius_meters = Column(Float, default=100.0)
    is_active = Column(Boolean, default=True)
    geofence_type = Column(String(20), default="circle", nullable=False)
    polygon_coordinates = Column(Text, nullable=True)
    qr_code_enabled = Column(Boolean, default=False, nullable=False)
    qr_code_data = Column(String(256), nullable=True)
    nfc_enabled = Column(Boolean, default=False, nullable=False)
    nfc_tag_data = Column(String(256), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    shift_schedule_id = Column(Integer, ForeignKey("shift_schedules.id"), nullable=True)
    timezone_offset = Column(Integer, default=7)
    timezone_name = Column(String(50), default="Asia/Jakarta", nullable=True)  # IANA timezone (Phase 5E)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    company = relationship("Company", foreign_keys=[company_id])
    shift_schedule = relationship("ShiftSchedule", foreign_keys=[shift_schedule_id])


class EmployeeGroup(Base):
    __tablename__ = "employee_groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=False, index=True)
    shift_schedule_id = Column(Integer, ForeignKey("shift_schedules.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    shift_schedule = relationship("ShiftSchedule", foreign_keys=[shift_schedule_id])
    branch = relationship("Branch", foreign_keys=[branch_id])


class Holiday(Base):
    __tablename__ = "holidays"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    date = Column(Date, unique=True, index=True, nullable=False)
    is_recurring = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class BranchCheckpoint(Base):
    """
    Multiple clock-in points per branch.
    
    Each branch can have multiple checkpoints (e.g., Main Gate, Building A Entrance,
    Parking Lot), each with its own GPS coordinate and radius. When validating a punch,
    the system checks if the GPS location falls within ANY checkpoint of the assigned
    branches (in addition to the branch center point).
    """
    __tablename__ = "branch_checkpoints"
    __table_args__ = (
        Index('idx_checkpoint_branch', 'branch_id'),
    )
    id = Column(Integer, primary_key=True, index=True)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=False, index=True)
    name = Column(String, nullable=False)                    # e.g., "Main Gate", "Building A"
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    radius_meters = Column(Float, default=50.0)              # Smaller radius than branch
    is_active = Column(Boolean, default=True)
    geofence_type = Column(String(20), default="circle", nullable=False)
    polygon_coordinates = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class BindingBranch(Base):
    """Many-to-many: which branches a device binding is authorized to clock in from."""
    __tablename__ = "device_branch_assignments"
    __table_args__ = (UniqueConstraint("binding_id", "branch_id"),)
    id = Column(Integer, primary_key=True, index=True)
    binding_id = Column(Integer, ForeignKey("device_bindings.id"), index=True, nullable=False)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=False)
    assigned_at = Column(DateTime, default=datetime.datetime.utcnow)


class ApiKey(Base):
    """API keys issued to mobile clients for authenticating punch requests."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    key_value = Column(String, unique=True, index=True)
    label = Column(String, default="Mobile Client")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String(45), nullable=True)
    expires_at = Column(DateTime, nullable=True)


class ADMSRegisteredEmployee(Base):
    """Track employees that have been auto-registered on the ADMS server."""
    __tablename__ = "adms_registered_employees"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, unique=True, index=True)
    employee_name = Column(String, default="Mobile User")
    registered_at = Column(DateTime, default=datetime.datetime.utcnow)


class PunchType(Base):
    """
    Admin-configurable punch types.
    Currently seeds 'In' and 'Out'. Add more via admin UI later.
    Extensible: add Break_Start, Overtime_In, etc. without code changes.
    """
    __tablename__ = "punch_types"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)      # "In", "Out", "Break_Start"
    label = Column(String)                               # "Clock In", "Clock Out"
    adms_status_code = Column(String, default="0")      # ZKTeco: 0=In, 1=Out, 4=Break
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    icon = Column(String, nullable=True)                 # "login", "logout", "coffee"
    color_hex = Column(String, nullable=True)            # "#22c55e", "#dc2626"
    requires_geofence = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True, index=True)
    adms_id = Column(String, index=True)
    employee_id = Column(String, unique=True, index=True)  # This is the PIN
    full_name = Column(String)
    department = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    employee_type = Column(String(50), default="regular")  # "regular", "internship", "daily_worker"
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    group_id = Column(Integer, ForeignKey("employee_groups.id"), nullable=True)
    shift_schedule_id = Column(Integer, ForeignKey("shift_schedules.id"), nullable=True)
    last_synced = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company", foreign_keys=[company_id])
    group = relationship("EmployeeGroup", foreign_keys=[group_id])
    shift_schedule = relationship("ShiftSchedule", foreign_keys=[shift_schedule_id])


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), ForeignKey("employees.employee_id"), nullable=False, index=True)
    leave_type = Column(String(50), nullable=False)  # 'annual', 'sick', 'permit'
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    reason = Column(String(500), nullable=True)
    status = Column(String(20), default="pending")  # pending, approved, rejected
    approved_by = Column(String(50), nullable=True)  # Admin username
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    employee = relationship("Employee", foreign_keys=[employee_id])


class LeaveBalance(Base):
    __tablename__ = "leave_balances"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), ForeignKey("employees.employee_id"), nullable=False, index=True)
    annual_total = Column(Integer, default=12)
    annual_used = Column(Integer, default=0)
    sick_total = Column(Integer, default=12)
    sick_used = Column(Integer, default=0)
    year = Column(Integer, nullable=False)

    employee = relationship("Employee", foreign_keys=[employee_id])
    
    __table_args__ = (
        UniqueConstraint('employee_id', 'year', name='uq_employee_leave_year'),
    )


class OvertimeRequest(Base):
    __tablename__ = "overtime_requests"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), ForeignKey("employees.employee_id"), nullable=False, index=True)
    date = Column(Date, nullable=False)
    hours_requested = Column(Float, nullable=False)
    reason = Column(String(500), nullable=True)
    status = Column(String(20), default="pending")  # pending, approved, rejected
    approved_by = Column(String(50), nullable=True)  # Admin / Supervisor username
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    employee = relationship("Employee", foreign_keys=[employee_id])


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    admin_username = Column(String(100), nullable=False, index=True)
    action = Column(String(150), nullable=False)  # e.g., "approved_device", "created_employee"
    target_type = Column(String(50), nullable=True)  # "employee", "branch", "device"
    target_id = Column(String(50), nullable=True)
    details = Column(Text, nullable=True)  # JSON description / details
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AppConfig(Base):
    """Global configuration settings for the middleware."""
    __tablename__ = "app_configs"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    value = Column(String)
    description = Column(String, nullable=True)


class ADMSCredential(Base):
    __tablename__ = "adms_credentials"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String)
    username = Column(String)
    password = Column(String)
    is_active = Column(Boolean, default=True)
class PunchLog(Base):
    __tablename__ = "punch_logs"
    __table_args__ = (
        Index('idx_punchlog_employee_type_date', 'employee_id', 'punch_type', 'timestamp'),
        Index('idx_punchlog_sync_status', 'adms_status'),
        Index('idx_punchlog_date', 'timestamp'),
        Index('idx_punchlog_employee_date', 'employee_id', "timestamp"),  # For daily queries
    )
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, index=True)
    device_uuid = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    latitude = Column(Float)
    longitude = Column(Float)
    is_mock_location = Column(Boolean)
    biometric_verified = Column(Boolean)
    punch_type = Column(String)                          # matches PunchType.code
    tz_offset_minutes = Column(Integer, default=420)
    adms_status = Column(String, default="pending")      # pending / uploaded / failed
    # ── Idempotency ───────────────────────────────────────────────────────
    client_punch_id = Column(String, nullable=True, unique=True, index=True)
    # ── Security / Validation ──────────────────────────────────────────────
    gps_time_validated = Column(Boolean, default=False)
    notes = Column(String, nullable=True)
    # ── Selfie / Face Verification ─────────────────────────────────────────
    selfie_filename = Column(String(500), nullable=True)  # Stored selfie image filename
    # ── ADMS ARQ Sync Tracking ─────────────────────────────────────────────
    server_sync_status = Column(String, default="pending")  # pending / synced / failed / stale
    synced_at = Column(DateTime, nullable=True)              # When it was successfully synced to ADMS
    sync_error = Column(String(500), nullable=True)          # Error message if sync failed
    sync_retry_count = Column(Integer, default=0)            # Number of retry attempts
    is_auto_generated = Column(Boolean, default=False)       # Auto clock-out punch (Phase 5C)


class EmployeeSupervisor(Base):
    """Maps supervisors to their team members."""
    __tablename__ = "employee_supervisors"

    id = Column(Integer, primary_key=True, index=True)
    supervisor_id = Column(String(50), nullable=False, index=True)  # Employee ID of the supervisor
    employee_id = Column(String(50), nullable=False, index=True)    # Employee ID of the team member
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index('idx_supervisor_mapping', 'supervisor_id', 'employee_id', unique=True),
    )


class AttendanceCorrection(Base):
    """Tracks attendance correction requests from employees."""
    __tablename__ = "attendance_corrections"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), nullable=False, index=True)
    original_punch_id = Column(Integer, ForeignKey('punch_logs.id'), nullable=True)
    correction_type = Column(String(50), nullable=False)  # 'missing_punch', 'wrong_type', 'wrong_time'
    description = Column(String(500), nullable=False)
    proposed_timestamp = Column(DateTime, nullable=True)
    proposed_punch_type = Column(String(10), nullable=True)
    status = Column(String(20), default='pending')  # 'pending', 'approved', 'rejected'
    reviewed_by = Column(String(50), nullable=True)  # Supervisor's employee_id
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class SystemErrorLog(Base):
    """Self-hosted log of unhandled exceptions and system failures."""
    __tablename__ = "system_error_logs"

    id = Column(Integer, primary_key=True, index=True)
    error_message = Column(String(500), nullable=False)
    stack_trace = Column(Text, nullable=True)
    component = Column(String(100), default="fastapi")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ScheduleAssignment(Base):
    """Maps employee roster shifts over specific date ranges (Phase 5D)."""
    __tablename__ = "schedule_assignments"
    
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), ForeignKey("employees.employee_id"), nullable=False, index=True)
    shift_schedule_id = Column(Integer, ForeignKey("shift_schedules.id"), nullable=False)
    effective_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)  # NULL = indefinite roster assignment
    created_by = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        Index('idx_schedule_assignment_lookup', 'employee_id', 'effective_date'),
    )

class Webhook(Base):
    """Webhook endpoints registered by admin to receive event notifications (Phase 5F)."""
    __tablename__ = "webhooks"
    
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500), nullable=False)
    events = Column(String(500))  # e.g., "punch.created,leave.approved"
    secret = Column(String(200))  # HMAC signing secret
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class WebhookDelivery(Base):
    """Delivery attempts of webhook payloads to endpoints (Phase 5F)."""
    __tablename__ = "webhook_deliveries"
    
    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey("webhooks.id"), nullable=False)
    event = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)
    response_status = Column(Integer, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    error = Column(String(500), nullable=True)


# ─── Database Setup ────────────────────────────────────────────────────────────

if not os.path.exists("./data"):
    os.makedirs("./data")

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/attendance.db")
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_timeout=30,
    )
    
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def run_alembic_migrations():
    """Run Alembic migrations programmatically on startup."""
    import logging
    from alembic.config import Config
    from alembic import command
    
    logger = logging.getLogger("alembic")
    try:
        # Get path to alembic.ini relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        backend_dir = os.path.dirname(os.path.dirname(current_dir))
        ini_path = os.path.join(backend_dir, "alembic.ini")
        
        # Load config and override URL from env
        alembic_cfg = Config(ini_path)
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            alembic_cfg.set_main_option("sqlalchemy.url", db_url)
            
        # Run upgrade head
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic database migrations applied successfully.")
    except Exception as e:
        logger.error(f"Failed to apply Alembic migrations: {e}")


def init_db():
    if not SQLALCHEMY_DATABASE_URL.startswith("sqlite:///:memory:"):
        try:
            run_alembic_migrations()
        except Exception as e:
            print(f"Alembic startup migration warning: {e}")
    else:
        # Fallback for SQLite in-memory / testing
        Base.metadata.create_all(bind=engine)

    # Migrate existing branch_id to device_branch_assignments
    with engine.connect() as conn:
        from sqlalchemy import text
        try:
            if engine.name == "sqlite":
                insert_stmt = "INSERT OR IGNORE"
            else:
                insert_stmt = "INSERT"

            if engine.name == "sqlite":
                conn.execute(text(f"""
                    {insert_stmt} INTO device_branch_assignments (binding_id, branch_id, assigned_at)
                    SELECT id, branch_id, created_at FROM device_bindings
                    WHERE branch_id IS NOT NULL
                      AND branch_id NOT IN (
                        SELECT branch_id FROM device_branch_assignments
                        WHERE device_branch_assignments.binding_id = device_bindings.id
                      )
                """))
            else:
                # PostgreSQL approach without dialect specific ON CONFLICT to avoid sequence issues
                conn.execute(text(f"""
                    INSERT INTO device_branch_assignments (binding_id, branch_id, assigned_at)
                    SELECT id, branch_id, created_at FROM device_bindings
                    WHERE branch_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM device_branch_assignments
                        WHERE device_branch_assignments.binding_id = device_bindings.id
                          AND device_branch_assignments.branch_id = device_bindings.branch_id
                      )
                """))
            conn.commit()
        except Exception:
            conn.rollback()
            pass  # Table or data already migrated

    db = SessionLocal()
    try:
        # Seed default Branch
        if db.query(Branch).count() == 0:
            db.add(Branch())
            db.commit()

        # Seed default ADMS target
        if db.query(ADMSTarget).count() == 0:
            db.add(ADMSTarget())
            db.commit()

        # Seed default punch types (In / Out)
        if db.query(PunchType).count() == 0:
            db.add(PunchType(
                code="In", label="Clock In", adms_status_code="0",
                display_order=0, icon="login", color_hex="#16a34a",
            ))
            db.add(PunchType(
                code="Out", label="Clock Out", adms_status_code="1",
                display_order=1, icon="logout", color_hex="#dc2626",
            ))
            db.commit()

        # Seed max_devices_per_employee config
        if not db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first():
            db.add(AppConfig(
                key="max_devices_per_employee",
                value="5",
                description="Maximum number of devices an employee can register",
            ))
            db.commit()

        # One-time API key hashing migration
        import hashlib
        keys = db.query(ApiKey).all()
        for key in keys:
            if not key.key_value.startswith("sha256:"):
                if key.key_value.startswith("atk_"):
                    hashed = hashlib.sha256(key.key_value.encode("utf-8")).hexdigest()
                    key.key_value = "sha256:" + hashed
                elif len(key.key_value) == 64:
                    key.key_value = "sha256:" + key.key_value
                else:
                    hashed = hashlib.sha256(key.key_value.encode("utf-8")).hexdigest()
                    key.key_value = "sha256:" + hashed
        db.commit()
    finally:
        db.close()
