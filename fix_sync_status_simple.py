"""
Simple one-off script to fix existing records where adms_status='uploaded'
but server_sync_status='pending'.

Uses raw SQL to avoid ORM schema migration issues.
"""
import sqlite3
import sys
import os

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "attendance.db")

dry_run = "--execute" not in sys.argv

print(f"Database: {db_path}")
print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")
print()

if not os.path.exists(db_path):
    print(f"❌ Database not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Check if selfie_filename column exists (to understand schema state)
cursor.execute("PRAGMA table_info(punch_logs)")
columns = [row["name"] for row in cursor.fetchall()]
print(f"Columns in punch_logs: {columns}")
print()

# Case 1: adms_status='uploaded' but server_sync_status='pending'
cursor.execute(
    "SELECT id, employee_id, timestamp, adms_status, server_sync_status FROM punch_logs "
    "WHERE adms_status = 'uploaded' AND server_sync_status = 'pending'"
)
uploaded_pending = cursor.fetchall()
print(f"Case 1 — adms_status='uploaded' + server_sync_status='pending': {len(uploaded_pending)} records")
for row in uploaded_pending[:10]:
    print(f"   id={row['id']}, employee={row['employee_id']}, time={row['timestamp']}")
if len(uploaded_pending) > 10:
    print(f"   ... and {len(uploaded_pending) - 10} more")

# Case 2: adms_status='failed' but server_sync_status='pending'
cursor.execute(
    "SELECT id, employee_id, timestamp, adms_status, server_sync_status FROM punch_logs "
    "WHERE adms_status = 'failed' AND server_sync_status = 'pending'"
)
failed_pending = cursor.fetchall()
print(f"\nCase 2 — adms_status='failed' + server_sync_status='pending': {len(failed_pending)} records")
for row in failed_pending[:10]:
    print(f"   id={row['id']}, employee={row['employee_id']}, time={row['timestamp']}")
if len(failed_pending) > 10:
    print(f"   ... and {len(failed_pending) - 10} more")

# Case 3: Overall stats
cursor.execute("SELECT COUNT(*) as c FROM punch_logs")
total = cursor.fetchone()["c"]
cursor.execute("SELECT COUNT(*) as c FROM punch_logs WHERE server_sync_status = 'synced'")
synced = cursor.fetchone()["c"]
cursor.execute("SELECT COUNT(*) as c FROM punch_logs WHERE server_sync_status = 'pending'")
pending = cursor.fetchone()["c"]
cursor.execute("SELECT COUNT(*) as c FROM punch_logs WHERE server_sync_status = 'failed'")
failed = cursor.fetchone()["c"]

print(f"\n{'='*60}")
print(f"Overall Stats:")
print(f"  Total records:      {total}")
print(f"  server_sync_status  synced:  {synced}")
print(f"  server_sync_status  pending: {pending}")
print(f"  server_sync_status  failed:  {failed}")
print(f"{'='*60}")
print()

if dry_run:
    print("⚠️  DRY RUN — No changes made.")
    print("   Run with --execute to apply: python3 fix_sync_status_simple.py --execute")
    conn.close()
    sys.exit(0)

# Apply fixes
now = datetime_now = __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

fixed_uploaded = 0
if uploaded_pending:
    cursor.execute(
        "UPDATE punch_logs SET server_sync_status = 'synced', synced_at = ? "
        "WHERE adms_status = 'uploaded' AND server_sync_status = 'pending'",
        (now,)
    )
    fixed_uploaded = cursor.rowcount

fixed_failed = 0
if failed_pending:
    cursor.execute(
        "UPDATE punch_logs SET server_sync_status = 'failed', sync_error = 'Marked failed by data migration', "
        "sync_retry_count = COALESCE(sync_retry_count, 0) + 1 "
        "WHERE adms_status = 'failed' AND server_sync_status = 'pending'",
    )
    fixed_failed = cursor.rowcount

conn.commit()
conn.close()

print(f"✅ Migration complete!")
print(f"   Fixed {fixed_uploaded} records → server_sync_status='synced'")
print(f"   Fixed {fixed_failed} records → server_sync_status='failed'")
