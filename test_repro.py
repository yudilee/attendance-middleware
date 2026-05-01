import sys, os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import create_engine, text
from app.database.models import init_db, SessionLocal, DeviceBinding

# Connect to Postgres
engine = create_engine("postgresql://attendance:secret_db_password@localhost:5432/attendance_db")

# 1. Drop tables to simulate fresh start
with engine.connect() as conn:
    conn.execute(text("DROP TABLE IF EXISTS device_branch_assignments CASCADE;"))
    conn.execute(text("DROP TABLE IF EXISTS device_bindings CASCADE;"))
    conn.execute(text("DROP TABLE IF EXISTS adms_targets CASCADE;"))
    conn.execute(text("DROP TABLE IF EXISTS punch_logs CASCADE;"))
    conn.commit()

# 2. Create OLD schema (missing columns)
with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE adms_targets (
            id SERIAL PRIMARY KEY,
            server_url VARCHAR,
            serial_number VARCHAR,
            device_name VARCHAR,
            is_active BOOLEAN,
            last_contact TIMESTAMP
        );
    """))
    conn.execute(text("""
        CREATE TABLE punch_logs (
            id SERIAL PRIMARY KEY,
            employee_id VARCHAR,
            device_uuid VARCHAR,
            timestamp TIMESTAMP,
            latitude FLOAT,
            longitude FLOAT,
            is_mock_location BOOLEAN,
            biometric_verified BOOLEAN,
            punch_type VARCHAR,
            adms_status VARCHAR
        );
    """))
    conn.execute(text("""
        CREATE TABLE device_bindings (
            id SERIAL PRIMARY KEY,
            employee_id VARCHAR,
            device_uuid VARCHAR,
            branch_id INTEGER,
            created_at TIMESTAMP,
            api_key_id INTEGER
        );
    """))
    conn.commit()

# 3. Insert some dummy data
with engine.connect() as conn:
    conn.execute(text("INSERT INTO adms_targets (server_url) VALUES ('http://test');"))
    conn.execute(text("INSERT INTO device_bindings (device_uuid) VALUES ('test-uuid');"))
    conn.commit()

# 4. Run init_db() as if we just redeployed
try:
    init_db()
    print("init_db SUCCESS!")
except Exception as e:
    print("init_db FAILED:", e)

# 5. Query device bindings like dashboard_root does
try:
    db = SessionLocal()
    devices = db.query(DeviceBinding).all()
    print("Devices queried successfully:", len(devices))
    for d in devices:
        print("Device label:", getattr(d, 'device_label', 'NOT_FOUND'))
except Exception as e:
    print("Query FAILED:", e)

