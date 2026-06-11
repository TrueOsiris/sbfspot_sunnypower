#!/bin/sh
# /app/entrypoint.sh

# Set default schedule to every hour if the variable is empty
SCHEDULE=${CRON_SCHEDULE:-"0 * * * *"}

echo "Configuring cron schedule to: $SCHEDULE"
echo "$SCHEDULE python3 /app/src/backfill_history_to_influxdb.py > /proc/1/fd/1 2>/proc/1/fd/2" > /etc/crontabs/root

echo "Starting initial run..."
python3 /app/src/backfill_history_to_influxdb.py

echo "Initial run complete. Starting cron daemon in the background..."
exec crond -f -l 2