"""ARQ background worker for ADMS sync and scheduled tasks."""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

import arq
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://attendance:attendance123@db:5432/attendance_db")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

async def sync_punches_to_adms(ctx, punch_log_id: int) -> dict:
    """Push a single punch log to ADMS server. Retries with exponential backoff."""
    from app.services.adms_service import push_to_adms as do_push
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        punch = db.query(PunchLog).filter(PunchLog.id == punch_log_id).first()
        if not punch:
            return {"status": "skipped", "reason": "punch_not_found"}

        result = await do_push(punch.id, punch.employee_id, punch.timestamp, punch.punch_type, punch.tz_offset_minutes)
        if result:
            punch.server_sync_status = "synced"
            punch.synced_at = datetime.utcnow()
            db.commit()
            return {"status": "synced", "punch_id": punch_log_id}
        else:
            punch.server_sync_status = "failed"
            punch.sync_error = "ADMS push returned failure"
            punch.sync_retry_count = (punch.sync_retry_count or 0) + 1
            db.commit()
            return {"status": "failed", "punch_id": punch_log_id, "error": "ADMS push returned failure"}
    except Exception as e:
        return {"status": "error", "punch_id": punch_log_id, "error": str(e)}
    finally:
        db.close()

async def retry_failed_punches(ctx):
    """Scheduled task: retry punches with server_sync_status='failed'."""
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        failed = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "failed"
        ).limit(50).all()

        results = []
        for punch in failed:
            job = await ctx["pool"].enqueue_job("sync_punches_to_adms", punch.id)
            results.append({"punch_id": punch.id, "job_id": job.job_id})

        return {"retried": len(results), "results": results}
    finally:
        db.close()

async def adms_heartbeat(ctx):
    """Scheduled task: maintain ADMS heartbeat connection.
    
    Runs every minute via ARQ cron. Writes connection state to AppConfig
    in PostgreSQL so the web dashboard (which runs in a separate process)
    can display "Connected" / "Disconnected" correctly.
    """
    from app.services.adms_service import test_adms_connection, get_adms_config, _handshake_state
    from app.database.models import AppConfig
    from datetime import datetime
    from app.database.models import SessionLocal
    try:
        server_url, sn, device_name = get_adms_config()
        if not server_url:
            _handshake_state["handshake_done"] = False
            _handshake_state["last_error"] = "No ADMS server configured"
            return {"status": "skipped", "reason": "no_server_configured"}
        
        success, message = await test_adms_connection(server_url, sn, device_name)
        
        # ── Persist heartbeat state to DB (cross-process visibility) ──
        now_iso = datetime.utcnow().isoformat()
        db = SessionLocal()
        try:
            def upsert_config(key: str, value: str):
                existing = db.query(AppConfig).filter(AppConfig.key == key).first()
                if existing:
                    existing.value = value
                else:
                    db.add(AppConfig(key=key, value=value))
            
            if success:
                upsert_config("adms_connected", "true")
                upsert_config("adms_last_contact", now_iso)
                upsert_config("adms_last_error", "")
                _handshake_state["handshake_done"] = True
                _handshake_state["last_contact"] = datetime.utcnow()
                _handshake_state["last_error"] = None
            else:
                upsert_config("adms_connected", "false")
                upsert_config("adms_last_contact", now_iso)
                upsert_config("adms_last_error", message)
                _handshake_state["handshake_done"] = False
                _handshake_state["last_error"] = message
            
            db.commit()
        finally:
            db.close()
        
        return {"status": "ok" if success else "failed", "message": message}
    except Exception as e:
        _handshake_state["handshake_done"] = False
        _handshake_state["last_error"] = str(e)
        return {"status": "error", "error": str(e)}

async def cleanup_stale_jobs(ctx):
    """Scheduled task: mark jobs older than 7 days as stale."""
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        cutoff = datetime.utcnow() - timedelta(days=7)
        stale = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "pending",
            PunchLog.timestamp < cutoff
        ).update({"server_sync_status": "stale"})
        db.commit()
        return {"marked_stale": stale}
    finally:
        db.close()

async def send_clock_in_reminders(ctx):
    """Scheduled task: send clock-in reminders to all devices with FCM tokens."""
    from app.database.models import DeviceBinding
    from app.services.notification_service import send_clock_in_reminder
    db = SessionLocal()
    try:
        devices = db.query(DeviceBinding).filter(
            DeviceBinding.fcm_token.isnot(None),
            DeviceBinding.fcm_token != "",
            DeviceBinding.is_active == True,
        ).all()

        sent_count = 0
        for device in devices:
            success = send_clock_in_reminder(device.fcm_token)
            if success:
                sent_count += 1

        return {"sent": sent_count, "total": len(devices)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()


# Worker settings
class WorkerSettings:
    functions = [sync_punches_to_adms, retry_failed_punches, adms_heartbeat, cleanup_stale_jobs, send_clock_in_reminders]
    redis_settings = arq.connections.RedisSettings(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        database=int(os.getenv("REDIS_DB", "0"))
    )
    max_tries = 5
    max_delay = 3600  # Max retry delay: 1 hour
    min_delay = 10    # Min retry delay: 10 seconds
    backoff_coefficient = 2.0  # Exponential backoff: 10s, 20s, 40s, 80s, 160s...
    job_timeout = 30  # 30 second timeout per job
    keep_result = 3600  # Keep results for 1 hour

    # Scheduled tasks
    cron_jobs = [
        # Retry failed punches every 5 minutes
        arq.cron(retry_failed_punches, minute=5, run_at_startup=True),
        # Heartbeat every minute (run immediately on startup)
        arq.cron(adms_heartbeat, minute=set(range(60)), run_at_startup=True),
        # Cleanup stale jobs daily at midnight
        arq.cron(cleanup_stale_jobs, hour=0, minute=0),
        # Clock-in reminders every weekday at 08:00 (mon=0, tues=1, wed=2, thurs=3, fri=4)
        arq.cron(send_clock_in_reminders, hour=8, minute=0, weekday={0, 1, 2, 3, 4}),
    ]
