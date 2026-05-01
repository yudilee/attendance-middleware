import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from app.database.models import engine
from sqlalchemy import text

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE adms_targets ADD COLUMN timezone_offset INTEGER DEFAULT 7;"))
        conn.commit()
        print("Success 1")
    except Exception as e:
        print("Error 1:", e)
        # notice no rollback!

    try:
        conn.execute(text("ALTER TABLE punch_logs ADD COLUMN tz_offset_minutes INTEGER DEFAULT 420;"))
        conn.commit()
        print("Success 2")
    except Exception as e:
        print("Error 2:", e)
