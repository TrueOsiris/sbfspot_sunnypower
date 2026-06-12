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

# InfluxDB v3 client
client = InfluxDBClient3(
    host=INFLUX_URL,
    token=INFLUX_TOKEN,
    database=INFLUX_DATABASE,
    org=INFLUX_ORG
)

def get_max_timestamp_in_influx(measurement):
    """Return the latest Unix timestamp already stored for this measurement, or None."""
    try:
        # Limit the query to the last 14 days to avoid the 5000 Parquet file limit in InfluxDB 3 Core
        query = f"SELECT max(time) AS max_time FROM \"{measurement}\" WHERE time >= now() - INTERVAL '14 days'"
        
        result = client.query(query, language="sql")
        if result and result.num_rows > 0:
            col = result.column("max_time")
            if col and len(col) > 0:
                ts = col[0].as_py()
                if ts is not None:
                    return int(ts.timestamp()) if hasattr(ts, 'timestamp') else int(ts)
    except Exception as e:
        print(f"\n  [CRITICAL ERROR] Could not read max timestamp for {measurement}!")
        print(f"  InfluxDB responded with: {e}\n")
        # Return an impossible future timestamp so we safely skip importing
        return 9999999999 
    return None

def import_table(table_name, measurement, fields, skip_new=False):
    max_timestamp = None
    if skip_new:
        max_timestamp = get_max_timestamp_in_influx(measurement)
        if max_timestamp is None:
            # Table doesn't exist at all yet
            return 0

    # Safely connect in read-only mode via URI
    conn = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Let SQLite handle the filtering
    if max_timestamp is not None:
        cursor.execute(f"SELECT * FROM {table_name} WHERE TimeStamp > ?", (max_timestamp,))
    else:
        cursor.execute(f"SELECT * FROM {table_name}")
        
    rows = cursor.fetchall()
    conn.close()

    if len(rows) == 0:
        return 0

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

    # Print the specific summary for this table since data was moved
    print(f"[{table_name} -> {measurement}] Imported {len(rows)} new records.")
    return len(rows)

if __name__ == "__main__":
    try:
        full_reimport = "--full-reimport" in sys.argv
        skip_new = not full_reimport
        total_imported = 0

        spot_fields = [
            "Pdc1", "Pdc2", "Idc1", "Idc2", "Udc1", "Udc2",
            "Pac1", "Pac2", "Pac3", "Iac1", "Iac2", "Iac3",
            "Uac1", "Uac2", "Uac3", "EToday", "ETotal",
            "Frequency", "OperatingTime", "FeedInTime", "Temperature"
        ]
        
        # We suppress the printing of the table names up front, it only prints if data is imported inside the function
        total_imported += import_table("DayData",   "sbfspot_day",   ["TotalYield", "Power"],    skip_new=skip_new)
        total_imported += import_table("MonthData", "sbfspot_month", ["TotalYield", "DayYield"], skip_new=skip_new)
        total_imported += import_table("SpotData",  "sbfspot_spot",  spot_fields,               skip_new=skip_new)

        # Only print the final summary block if actual work was done across any table
        if total_imported > 0:
            print(f"Total new records backfilled across all tables: {total_imported}")
        else:
            if not full_reimport:
                print("\nUsage Help:")
                print("  python backfill_history_to_influxdb.py                  # Incremental (default)")
                print("  python backfill_history_to_influxdb.py --full-reimport  # Import all SQLite rows")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        pass