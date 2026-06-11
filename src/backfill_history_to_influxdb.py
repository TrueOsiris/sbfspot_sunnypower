# src/backfill_history_to_influxdb.py
# Author: Tim Chaubet

import os
import sqlite3
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime
import pytz

# --- Configuration via Environment Variables ---
SQLITE_DB = os.getenv('SQLITE_DB', '/data/sbfspot.db')
INFLUX_URL = os.getenv('INFLUX_URL', 'http://10.10.0.6:8181')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN', 'apiv3_yD-7NhuMDjG772v0rEKY9gaXQKJjEkHLmPwAzx9RSD6CVKiWMOuVhRSCJJxtDPhZO7L6To8eGjxeNBbJCM_H7w')
INFLUX_ORG = os.getenv('INFLUX_ORG', 'chaubet')
INFLUX_DATABASE = os.getenv('INFLUX_DATABASE', 'solarpanels')

# Initialize Client
client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)

def import_table(table_name, measurement, fields):
    print(f"--- Starting Migration: {table_name} -> {measurement} ---")
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    print(f"Found {len(rows)} records in {table_name}. Importing...")
    
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

if __name__ == "__main__":
    try:
        # 1. DayData (5-min intervals)
        import_table("DayData", "sbfspot_day", ["TotalYield", "Power"])
        
        # 2. MonthData (Daily/Monthly summaries)
        import_table("MonthData", "sbfspot_month", ["TotalYield", "DayYield"])
        
        # 3. SpotData (The detailed granular data with phase voltage/current)
        spot_fields = [
            "Pdc1", "Pdc2", "Idc1", "Idc2", "Udc1", "Udc2", 
            "Pac1", "Pac2", "Pac3", "Iac1", "Iac2", "Iac3", 
            "Uac1", "Uac2", "Uac3", "EToday", "ETotal", 
            "Frequency", "OperatingTime", "FeedInTime", "Temperature"
        ]
        import_table("SpotData", "sbfspot_spot", spot_fields)
        
        print("\nAll historical data imported successfully.")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        client.close()