from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime
import os

Base = declarative_base()


class DeviceBinding(Base):
    __tablename__ = "device_bindings"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, unique=True, index=True)
    device_uuid = Column(String, index=True)
    branch_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    # ── Registration workflow ──────────────────────────────────────────────
    device_label = Column(String, nullable=True)                    # e.g. "John's Samsung A55"
    registration_status = Column(String, default="pending_approval")
    # States: pending_approval → approved → active | suspended
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, nullable=True)                     # admin username
    notes = Column(String, nullable=True)
    # ── Multi-device support ───────────────────────────────────────────────
    device_role = Column(String, default="primary")                 # primary | backup
    is_active_device = Column(Boolean, default=True)


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
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ApiKey(Base):
    """API keys issued to mobile clients for authenticating punch requests."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    key_value = Column(String, unique=True, index=True)
    label = Column(String, default="Mobile Client")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


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

    # ── Auto-Migration for SQLite ───────────────────────────────────────────
    # SQLAlchemy create_all() does not add new columns to existing tables.
    # We manually add them here for production self-healing.
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE adms_targets ADD COLUMN timezone_offset INTEGER DEFAULT 7;",
        "ALTER TABLE punch_logs ADD COLUMN tz_offset_minutes INTEGER DEFAULT 420;",
        "ALTER TABLE punch_logs ADD COLUMN client_punch_id TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN device_label TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN registration_status TEXT DEFAULT 'pending_approval';",
        "ALTER TABLE device_bindings ADD COLUMN approved_at DATETIME;",
        "ALTER TABLE device_bindings ADD COLUMN approved_by TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN notes TEXT;",
        "ALTER TABLE device_bindings ADD COLUMN device_role TEXT DEFAULT 'primary';",
        "ALTER TABLE device_bindings ADD COLUMN is_active_device INTEGER DEFAULT 1;",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

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
    finally:
        db.close()
