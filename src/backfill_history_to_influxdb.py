# src/backfill_history_to_influxdb.py
# Author: Tim Chaubet

import os
import sqlite3
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime
import pytz
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

# Initialize Client
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)
query_api = client.query_api()

MEASUREMENT_ALIASES = {
    "sbfspot_day": ["sbfspot_day", "DayData"],
    "sbfspot_month": ["sbfspot_month", "MonthData"],
    "sbfspot_spot": ["sbfspot_spot", "SpotData"],
}

def get_max_timestamp_in_influx(measurement):
    """Query InfluxDB to find the maximum timestamp for a measurement."""
    try:
        aliases = MEASUREMENT_ALIASES.get(measurement, [measurement])
        for alias in aliases:
            query = f"""
            from(bucket: "{INFLUX_DATABASE}")
              |> range(start: 1970-01-01T00:00:00Z, stop: now())
              |> filter(fn: (r) => r._measurement == "{alias}")
              |> group(columns: ["_measurement"])
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            """
            result = query_api.query(org=INFLUX_ORG, query=query)

            if result:
                for table in result:
                    for record in table.records:
                        ts = record.get_time() or record.values.get("_time")
                        if ts:
                            if alias != measurement:
                                print(f"Using legacy measurement name {alias} for {measurement}.")
                            return int(ts.timestamp())
        return None
    except Exception as e:
        status = getattr(e, "status", None)
        reason = getattr(e, "reason", "")
        if status == 404 or "Not found" in str(e) or reason == "Not Found":
            return None
        print(f"Warning: Could not query max timestamp for {measurement}: {e}")
        return None

def import_table(table_name, measurement, fields, skip_new=False):
    print(f"--- Starting Migration: {table_name} -> {measurement} ---")
    
    # Only import new data if skip_new is True
    max_timestamp = None
    if skip_new:
        max_timestamp = get_max_timestamp_in_influx(measurement)
        if max_timestamp:
            print(f"Found existing data up to timestamp {max_timestamp}. Only importing newer records...")
    
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    print(f"Found {len(rows)} records in {table_name}.")
    
    # Filter rows if we're doing incremental import
    if max_timestamp:
        rows = [r for r in rows if r['TimeStamp'] > max_timestamp]
        print(f"After filtering: {len(rows)} new records to import.")
    
    if len(rows) == 0:
        print(f"No new data to import for {measurement}.")
        conn.close()
        return
    
    print(f"Importing {len(rows)} records...")
    
    points = []
    for row in rows:
        row_dict = dict(row)
        timestamp = row_dict.get('TimeStamp')
        if not timestamp:
            continue
        
        # Convert SQLite Epoch to Python Datetime
        dt = datetime.fromtimestamp(timestamp, pytz.UTC)
        
        # Use Serial as a TAG (indexed, constant)
        serial = str(row_dict.get('Serial', 'unknown'))
        
        point = Point(measurement).tag("serial", serial)
        
        # Add numeric fields (skipping strings like Status/GridRelay)
        for field in fields:
            val = row_dict.get(field)
            if val is not None and isinstance(val, (int, float)):
                point.field(field, float(val))
        
        point.time(dt)
        points.append(point)
        
        # Batch write in chunks of 1000 to keep memory low
        if len(points) >= 1000:
            write_api.write(bucket=INFLUX_DATABASE, record=points)
            points = []
            
    if points:
        write_api.write(bucket=INFLUX_DATABASE, record=points)
    
    conn.close()
    print(f"Finished {table_name}.")

def delete_measurement(measurement):
    """Delete all data for a measurement (cleanup)."""
    try:
        from influxdb_client.client.delete_api import DeleteApi
        delete_api = DeleteApi(client)
        deleted = []
        for alias in MEASUREMENT_ALIASES.get(measurement, [measurement]):
            delete_api.delete(
                org=INFLUX_ORG,
                bucket=INFLUX_DATABASE,
                start_time=datetime(1970, 1, 1, tzinfo=pytz.UTC),
                stop_time=datetime.now(pytz.UTC),
                predicate=f'_measurement="{alias}"'
            )
            deleted.append(alias)
        print(f"Deleted all data for measurement(s): {', '.join(deleted)}")
    except Exception as e:
        print(f"Error deleting {measurement}: {e}")

if __name__ == "__main__":
    try:
        # Parse command-line arguments
        full_reimport = "--full-reimport" in sys.argv
        cleanup = "--cleanup" in sys.argv
        skip_new = not full_reimport  # By default, only import new data
        
        if cleanup:
            print("=== CLEANUP MODE: Removing all old data ===")
            delete_measurement("sbfspot_day")
            delete_measurement("sbfspot_month")
            delete_measurement("sbfspot_spot")
            print("Cleanup complete. Now performing full reimport...\n")
            skip_new = False
        
        if full_reimport:
            print("=== FULL REIMPORT MODE: Replacing all data ===")
            delete_measurement("sbfspot_day")
            delete_measurement("sbfspot_month")
            delete_measurement("sbfspot_spot")
            print("Old data deleted. Starting fresh import...\n")
            skip_new = False
        else:
            print("=== INCREMENTAL MODE: Only importing new data ===\n")
        
        # 1. DayData (5-min intervals)
        import_table("DayData", "sbfspot_day", ["TotalYield", "Power"], skip_new=skip_new)
        
        # 2. MonthData (Daily/Monthly summaries)
        import_table("MonthData", "sbfspot_month", ["TotalYield", "DayYield"], skip_new=skip_new)
        
        # 3. SpotData (The detailed granular data with phase voltage/current)
        spot_fields = [
            "Pdc1", "Pdc2", "Idc1", "Idc2", "Udc1", "Udc2", 
            "Pac1", "Pac2", "Pac3", "Iac1", "Iac2", "Iac3", 
            "Uac1", "Uac2", "Uac3", "EToday", "ETotal", 
            "Frequency", "OperatingTime", "FeedInTime", "Temperature"
        ]
        import_table("SpotData", "sbfspot_spot", spot_fields, skip_new=skip_new)
        
        print("\nAll data import completed successfully.")
        print("\nUsage:")
        print("  python backfill_history_to_influxdb.py           # Incremental (new data only)")
        print("  python backfill_history_to_influxdb.py --full-reimport  # Full reimport (delete & reimport)")
        print("  python backfill_history_to_influxdb.py --cleanup        # Cleanup & full reimport")
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.close()