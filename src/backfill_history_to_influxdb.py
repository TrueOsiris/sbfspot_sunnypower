# src/backfill_history_to_influxdb.py
# Author: Tim Chaubet

import os
import sqlite3
from influxdb_client_3 import InfluxDBClient3, Point
from datetime import datetime, timezone
import sys

# --- Configuration via Environment Variables ---
SQLITE_DB = os.getenv('SQLITE_DB', '/data/sbfspot.db')
INFLUX_URL = os.getenv('INFLUX_URL', 'http://10.10.0.6:8181')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG = os.getenv('INFLUX_ORG', 'defaultorg')
INFLUX_DATABASE = os.getenv('INFLUX_DATABASE', 'solarpanels')

# Validate mandatory configuration
if not INFLUX_TOKEN:
    print("ERROR: INFLUX_TOKEN environment variable is mandatory and must be set in docker-compose.yml")
    print("Example: INFLUX_TOKEN=apiv3_your_token_here")
    sys.exit(1)

# InfluxDB v3 client: SQL queries & deletes, line protocol writes
client = InfluxDBClient3(
    host=INFLUX_URL,
    token=INFLUX_TOKEN,
    database=INFLUX_DATABASE,
    org=INFLUX_ORG
)

# In InfluxDB v3, each measurement is stored as a table.
# We check both import-assigned names and raw SQLite table names (legacy).
MEASUREMENTS = {
    "sbfspot_day":   ["sbfspot_day", "DayData"],
    "sbfspot_month": ["sbfspot_month", "MonthData"],
    "sbfspot_spot":  ["sbfspot_spot", "SpotData"],
}

def get_max_timestamp_in_influx(measurement):
    """Return the latest Unix timestamp already stored for this measurement, or None."""
    for alias in MEASUREMENTS.get(measurement, [measurement]):
        try:
            result = client.query(f'SELECT max(time) AS max_time FROM "{alias}"', language="sql")
            if result and result.num_rows > 0:
                col = result.column("max_time")
                if col and len(col) > 0:
                    ts = col[0].as_py()
                    if ts is not None:
                        if hasattr(ts, 'timestamp'):
                            return int(ts.timestamp())
                        return int(ts)
        except Exception:
            pass  # Table doesn't exist yet, try next alias
    return None

def delete_imported_tables():
    """Delete all rows from the three imported measurement tables using SQL."""
    print(f"Deleting all rows from imported tables in database '{INFLUX_DATABASE}'...")
    for measurement, aliases in MEASUREMENTS.items():
        for alias in aliases:
            try:
                client.query(f'DELETE FROM "{alias}"', language="sql")
                print(f"  Cleared: {alias}")
            except Exception as e:
                print(f"  Warning: could not clear {alias}: {e}")
    print("All tables cleared.")

def import_table(table_name, measurement, fields, skip_new=False):
    print(f"\n--- {table_name} -> {measurement} ---")

    max_timestamp = None
    if skip_new:
        max_timestamp = get_max_timestamp_in_influx(measurement)
        if max_timestamp is not None:
            readable = datetime.fromtimestamp(max_timestamp, tz=timezone.utc).isoformat()
            print(f"  Latest existing record: {readable}")
        else:
            print(f"  No existing data found in InfluxDB for this table. Skipping.")
            print(f"  (Use --full-reimport to do the first import.)")
            return

    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    conn.close()

    if max_timestamp is not None:
        rows = [r for r in rows if r['TimeStamp'] > max_timestamp]

    print(f"  Records to import: {len(rows)}")
    if len(rows) == 0:
        print(f"  Nothing new to import.")
        return

    points = []
    for row in rows:
        row_dict = dict(row)
        timestamp = row_dict.get('TimeStamp')
        if not timestamp:
            continue
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        serial = str(row_dict.get('Serial', 'unknown'))
        point = Point(measurement).tag("serial", serial)
        for field in fields:
            val = row_dict.get(field)
            if val is not None and isinstance(val, (int, float)):
                point.field(field, float(val))
        point.time(dt)
        points.append(point)

        if len(points) >= 1000:
            client.write(record=points)
            points = []

    if points:
        client.write(record=points)

    print(f"  Done.")

if __name__ == "__main__":
    try:
        full_reimport = "--full-reimport" in sys.argv
        cleanup = "--cleanup" in sys.argv

        if cleanup or full_reimport:
            mode = "CLEANUP" if cleanup else "FULL REIMPORT"
            print(f"=== {mode}: Wiping imported tables before reimport ===")
            delete_imported_tables()
            print("\nReimporting all data...")
            skip_new = False
        else:
            print("=== INCREMENTAL MODE ===")
            print("Only new records are imported. Tables with no existing InfluxDB data are skipped.\n")
            skip_new = True

        spot_fields = [
            "Pdc1", "Pdc2", "Idc1", "Idc2", "Udc1", "Udc2",
            "Pac1", "Pac2", "Pac3", "Iac1", "Iac2", "Iac3",
            "Uac1", "Uac2", "Uac3", "EToday", "ETotal",
            "Frequency", "OperatingTime", "FeedInTime", "Temperature"
        ]
        import_table("DayData",   "sbfspot_day",   ["TotalYield", "Power"],    skip_new=skip_new)
        import_table("MonthData", "sbfspot_month", ["TotalYield", "DayYield"], skip_new=skip_new)
        import_table("SpotData",  "sbfspot_spot",  spot_fields,               skip_new=skip_new)

        print("\nCompleted successfully.")
        print("\nUsage:")
        print("  python backfill_history_to_influxdb.py                  # Incremental (new records only, skips empty tables)")
        print("  python backfill_history_to_influxdb.py --full-reimport  # Wipe tables & reimport all")
        print("  python backfill_history_to_influxdb.py --cleanup        # Same as --full-reimport")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.close()