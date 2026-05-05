"""
Data migration: Fix existing records where adms_status='uploaded' but
server_sync_status='pending'.

This happens because push_to_adms() previously only updated adms_status
and did not update server_sync_status. Now both are set atomically, but
existing records need to be backfilled.

Usage:
    python fix_server_sync_status.py
"""
import sys
import os
from datetime import datetime

# Ensure we can import from app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database.models import SessionLocal, PunchLog


def fix_pending_sync_status(dry_run: bool = True):
    """
    Find all records where adms_status='uploaded' but server_sync_status='pending'
    and update server_sync_status to 'synced'.
    
    Also fix records where adms_status='failed' but server_sync_status='pending'.
    
    Args:
        dry_run: If True, only print what would be changed without committing.
    """
    db = SessionLocal()
    try:
        # Case 1: Uploaded but marked as pending in server_sync_status
        uploaded_pending = db.query(PunchLog).filter(
            PunchLog.adms_status == "uploaded",
            PunchLog.server_sync_status == "pending",
        ).all()
        
        # Case 2: Failed but marked as pending in server_sync_status
        failed_pending = db.query(PunchLog).filter(
            PunchLog.adms_status == "failed",
            PunchLog.server_sync_status == "pending",
        ).all()
        
        total_fixed = 0
        
        if uploaded_pending:
            print(f"Found {len(uploaded_pending)} records with adms_status='uploaded' "
                  f"but server_sync_status='pending'")
            if not dry_run:
                for log in uploaded_pending:
                    log.server_sync_status = "synced"
                    log.synced_at = datetime.utcnow()
                db.commit()
                total_fixed += len(uploaded_pending)
                print(f"  ✅ Fixed {len(uploaded_pending)} records → server_sync_status='synced'")
            else:
                print(f"  🔸 DRY RUN: Would fix {len(uploaded_pending)} records")
                for log in uploaded_pending[:5]:
                    print(f"     - PunchLog id={log.id}, employee={log.employee_id}, "
                          f"time={log.timestamp}")
                if len(uploaded_pending) > 5:
                    print(f"     ... and {len(uploaded_pending) - 5} more")
        
        if failed_pending:
            print(f"\nFound {len(failed_pending)} records with adms_status='failed' "
                  f"but server_sync_status='pending'")
            if not dry_run:
                for log in failed_pending:
                    log.server_sync_status = "failed"
                    log.sync_error = log.sync_error or "Marked as failed by data migration"
                    log.sync_retry_count = (log.sync_retry_count or 0) + 1
                db.commit()
                total_fixed += len(failed_pending)
                print(f"  ✅ Fixed {len(failed_pending)} records → server_sync_status='failed'")
            else:
                print(f"  🔸 DRY RUN: Would fix {len(failed_pending)} records")
                for log in failed_pending[:5]:
                    print(f"     - PunchLog id={log.id}, employee={log.employee_id}, "
                          f"time={log.timestamp}")
                if len(failed_pending) > 5:
                    print(f"     ... and {len(failed_pending) - 5} more")
        
        # Summary stats
        total_all = db.query(PunchLog).count()
        synced_count = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "synced"
        ).count()
        pending_count = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "pending"
        ).count()
        failed_count = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "failed"
        ).count()
        
        print(f"\n{'='*60}")
        print(f"Summary after migration:")
        print(f"  Total records:      {total_all}")
        print(f"  server_sync_status  synced:  {synced_count}")
        print(f"  server_sync_status  pending: {pending_count}")
        print(f"  server_sync_status  failed:  {failed_count}")
        print(f"{'='*60}")
        
        if dry_run:
            print(f"\n⚠️  DRY RUN - No changes were committed.")
            print(f"   Re-run with --execute to apply changes:")
            print(f"   python fix_server_sync_status.py --execute")
        else:
            print(f"\n✅ Migration complete! {total_fixed} records updated.")
        
        return total_fixed
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    fix_pending_sync_status(dry_run=dry_run)
