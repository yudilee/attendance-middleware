from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, create_engine, ForeignKey, UniqueConstraint, Index, func
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Branch(Base):
    """Configurable branch site for geofencing."""
    __tablename__ = "branches"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="Default Office")
    latitude = Column(Float, default=0.0)
    longitude = Column(Float, default=0.0)
    radius_meters = Column(Float, default=100.0)
    is_active = Column(Boolean, default=True)
    qr_code_enabled = Column(Boolean, default=False, nullable=False)
    qr_code_data = Column(String(256), nullable=True)
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
    last_synced = Column(DateTime, default=datetime.datetime.utcnow)


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

    __table_args__ = (
        Index('idx_correction_employee', 'employee_id'),
        Index('idx_correction_status', 'status'),
    )


# ─── Database Setup ────────────────────────────────────────────────────────────

if not os.path.exists("./data"):
    os.makedirs("./data")

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/attendance.db")
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
    
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)

    # ── Auto-Migration ────────────────────────────────────────────────────────
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE adms_targets ADD COLUMN timezone_offset INTEGER DEFAULT 7;",
        "ALTER TABLE punch_logs ADD COLUMN tz_offset_minutes INTEGER DEFAULT 420;",
        "ALTER TABLE branches ADD COLUMN IF NOT EXISTS qr_code_enabled BOOLEAN DEFAULT FALSE;" if engine.name != "sqlite" else "ALTER TABLE branches ADD COLUMN qr_code_enabled BOOLEAN DEFAULT 0;",
        "ALTER TABLE branches ADD COLUMN IF NOT EXISTS qr_code_data VARCHAR(256);" if engine.name != "sqlite" else "ALTER TABLE branches ADD COLUMN qr_code_data VARCHAR(256);",
        "ALTER TABLE punch_logs ADD COLUMN client_punch_id TEXT;",
        "ALTER TABLE punch_logs ADD COLUMN gps_time_validated INTEGER DEFAULT 0;",
        "ALTER TABLE punch_logs ADD COLUMN notes TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN device_label TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN registration_status TEXT DEFAULT 'pending_approval';",
        "ALTER TABLE device_bindings ADD COLUMN approved_at TIMESTAMP;",
        "ALTER TABLE device_bindings ADD COLUMN approved_by TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN notes TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN device_role TEXT DEFAULT 'primary';",
        "ALTER TABLE device_bindings ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;" if engine.name != "sqlite" else "ALTER TABLE device_bindings ADD COLUMN is_active BOOLEAN DEFAULT 1;",
        # Convert existing integer is_active to boolean in Postgres to avoid type mismatch
        "ALTER TABLE device_bindings ALTER COLUMN is_active TYPE BOOLEAN USING (is_active::integer::boolean);" if engine.name != "sqlite" else "SELECT 1;",
        "DROP INDEX IF EXISTS ix_device_bindings_employee_id;",
        # Branch & ApiKey updates
        "ALTER TABLE branches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;" if engine.name != "sqlite" else "ALTER TABLE branches ADD COLUMN updated_at TIMESTAMP;",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP;" if engine.name != "sqlite" else "ALTER TABLE api_keys ADD COLUMN last_used_at TIMESTAMP;",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_ip VARCHAR(45);" if engine.name != "sqlite" else "ALTER TABLE api_keys ADD COLUMN last_used_ip VARCHAR(45);",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;" if engine.name != "sqlite" else "ALTER TABLE api_keys ADD COLUMN expires_at TIMESTAMP;",
        "ALTER TABLE branches ALTER COLUMN is_active TYPE BOOLEAN USING (is_active::integer::boolean);" if engine.name != "sqlite" else "SELECT 1;",
        "ALTER TABLE punch_types ALTER COLUMN is_active TYPE BOOLEAN USING (is_active::integer::boolean);" if engine.name != "sqlite" else "SELECT 1;",
        "ALTER TABLE employees ALTER COLUMN is_active TYPE BOOLEAN USING (is_active::integer::boolean);" if engine.name != "sqlite" else "SELECT 1;",
        "ALTER TABLE api_keys ALTER COLUMN is_active TYPE BOOLEAN USING (is_active::integer::boolean);" if engine.name != "sqlite" else "SELECT 1;",
        # ADMS ARQ sync tracking fields
        "ALTER TABLE punch_logs ADD COLUMN IF NOT EXISTS server_sync_status VARCHAR(20) DEFAULT 'pending';" if engine.name != "sqlite" else "ALTER TABLE punch_logs ADD COLUMN server_sync_status VARCHAR(20) DEFAULT 'pending';",
        "ALTER TABLE punch_logs ADD COLUMN IF NOT EXISTS synced_at TIMESTAMP;" if engine.name != "sqlite" else "ALTER TABLE punch_logs ADD COLUMN synced_at TIMESTAMP;",
        "ALTER TABLE punch_logs ADD COLUMN IF NOT EXISTS sync_error VARCHAR(500);" if engine.name != "sqlite" else "ALTER TABLE punch_logs ADD COLUMN sync_error VARCHAR(500);",
        "ALTER TABLE punch_logs ADD COLUMN IF NOT EXISTS sync_retry_count INTEGER DEFAULT 0;" if engine.name != "sqlite" else "ALTER TABLE punch_logs ADD COLUMN sync_retry_count INTEGER DEFAULT 0;",
        # Selfie / Face Verification
        "ALTER TABLE punch_logs ADD COLUMN IF NOT EXISTS selfie_filename VARCHAR(500);" if engine.name != "sqlite" else "ALTER TABLE punch_logs ADD COLUMN selfie_filename VARCHAR(500);",
        # Push Notifications (FCM)
        "ALTER TABLE device_bindings ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(500);" if engine.name != "sqlite" else "ALTER TABLE device_bindings ADD COLUMN fcm_token VARCHAR(500);",
        # Phase 5b: Supervisor tables
        "CREATE TABLE IF NOT EXISTS employee_supervisors (id SERIAL PRIMARY KEY, supervisor_id VARCHAR(50) NOT NULL, employee_id VARCHAR(50) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(supervisor_id, employee_id));",
        "CREATE INDEX IF NOT EXISTS idx_supervisor_mapping ON employee_supervisors(supervisor_id, employee_id);",
        "CREATE INDEX IF NOT EXISTS idx_emp_supervisor ON employee_supervisors(supervisor_id);",
        "CREATE INDEX IF NOT EXISTS idx_emp_employee ON employee_supervisors(employee_id);",
        "CREATE TABLE IF NOT EXISTS attendance_corrections (id SERIAL PRIMARY KEY, employee_id VARCHAR(50) NOT NULL, original_punch_id INTEGER REFERENCES punch_logs(id), correction_type VARCHAR(50) NOT NULL, description VARCHAR(500) NOT NULL, proposed_timestamp TIMESTAMP, proposed_punch_type VARCHAR(10), status VARCHAR(20) DEFAULT 'pending', reviewed_by VARCHAR(50), reviewed_at TIMESTAMP, review_notes VARCHAR(500), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);",
        "CREATE INDEX IF NOT EXISTS idx_correction_employee ON attendance_corrections(employee_id);",
        "CREATE INDEX IF NOT EXISTS idx_correction_status ON attendance_corrections(status);",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                # For PostgreSQL, we can use IF NOT EXISTS if supported, 
                # but for ALTER TABLE ADD COLUMN it's PG 9.6+.
                # We'll stick to try/except but fix the syntax.
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                conn.rollback()
                # logger.debug(f"Migration skipped or failed: {sql} - {e}")
                pass 

        # Migrate existing branch_id to device_branch_assignments
        try:
            if engine.name == "sqlite":
                insert_stmt = "INSERT OR IGNORE"
            else:
                insert_stmt = "INSERT" # For Postgres we'll use a safer approach below without ON CONFLICT to keep it simple

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
    finally:
        db.close()
