import sqlite3

def migrate():
    conn = sqlite3.connect('attendance.db')
    cursor = conn.cursor()
    
    try:
        # Rename geofence_zones to branches
        cursor.execute("ALTER TABLE geofence_zones RENAME TO branches;")
        print("Renamed geofence_zones to branches")
    except sqlite3.OperationalError as e:
        print(f"Skipping table rename (already done or missing): {e}")

    try:
        # Add branch_id to device_bindings
        cursor.execute("ALTER TABLE device_bindings ADD COLUMN branch_id INTEGER NULL;")
        print("Added branch_id to device_bindings")
    except sqlite3.OperationalError as e:
        print(f"Skipping Add Column (already done or missing): {e}")

    # Set default branch to ID 1 for all existing bindings
    cursor.execute("UPDATE device_bindings SET branch_id = 1 WHERE branch_id IS NULL;")
    
    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
