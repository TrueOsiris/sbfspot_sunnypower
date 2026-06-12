# /Dockerfile
FROM python:3.12-alpine
LABEL org.opencontainers.image.authors="TrueOsiris"

WORKDIR /app

# Install dependencies using the system break flag
RUN pip3 install --no-cache-dir --break-system-packages influxdb3-python pytz

# Copy the script and entrypoint
COPY src/ /app/src/
COPY entrypoint.sh /app/

# Make the entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Set the default environment variable for the cron schedule
ENV CRON_SCHEDULE="0 * * * *"

ENTRYPOINT ["/app/entrypoint.sh"]