import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.database.models import Base

# Force paths
sqlite_url = "sqlite:///./data/attendance.db"
pg_url = os.environ.get("DATABASE_URL")

if not pg_url or not pg_url.startswith("postgresql"):
    print("Error: DATABASE_URL environment variable must be set to a postgresql URL.")
    sys.exit(1)

print(f"Migrating from {sqlite_url} to {pg_url}")

# Create engines
sqlite_engine = create_engine(sqlite_url)
pg_engine = create_engine(pg_url)

# Create tables in PG
Base.metadata.create_all(bind=pg_engine)

# Get all tables
tables = Base.metadata.sorted_tables

with sqlite_engine.connect() as sqlite_conn, pg_engine.connect() as pg_conn:
    for table in tables:
        print(f"Migrating table {table.name}...")
        
        # Check if table exists in source SQLite
        check_table = sqlite_conn.execute(
            text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table.name}'")
        ).fetchone()
        
        if not check_table:
            print(f"  Table {table.name} does not exist in source SQLite. Skipping.")
            continue

        # Clear existing data in PG table just in case
        pg_conn.execute(table.delete())
        pg_conn.commit()
        
        # Read from SQLite
        records = sqlite_conn.execute(table.select()).mappings().all()
        print(f"  Found {len(records)} records.")
        
        if records:
            # Insert into PG
            insert_statement = table.insert().values([dict(row) for row in records])
            pg_conn.execute(insert_statement)
            pg_conn.commit()
            print(f"  Successfully migrated {len(records)} records to {table.name}.")

print("Migration completed successfully!")
