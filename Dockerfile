FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay ./relay

# Keep the database on a volume: set RELAY_DB=/data/relay.db (or /data/sandbox.db) and mount /data.
# Listening on 0.0.0.0 requires RELAY_API_KEY and DASHBOARD_PASSWORD. PORT is read from the environment.
CMD ["python", "-m", "relay", "serve", "--host", "0.0.0.0"]
