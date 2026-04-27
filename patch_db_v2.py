from sqlalchemy import create_engine, text
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/attendance.db")
engine = create_engine(DATABASE_URL)

def patch():
    with engine.connect() as conn:
        print(f"Patching database at {DATABASE_URL}...")
        
        # 1. Add api_key_id to device_bindings if it doesn't exist
        try:
            conn.execute(text("ALTER TABLE device_bindings ADD COLUMN api_key_id INTEGER REFERENCES api_keys(id)"))
            print("Added api_key_id column.")
        except Exception as e:
            print(f"Note: api_key_id column might already exist or error: {e}")

        # 2. Make employee_id nullable (for Postgres)
        if DATABASE_URL.startswith("postgresql"):
            try:
                conn.execute(text("ALTER TABLE device_bindings ALTER COLUMN employee_id DROP NOT NULL"))
                print("Made employee_id nullable.")
            except Exception as e:
                print(f"Error making employee_id nullable: {e}")
        
        conn.commit()
        print("Patching complete.")

if __name__ == "__main__":
    patch()
