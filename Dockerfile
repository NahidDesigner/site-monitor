FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/site-monitor.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY site_monitor/ ./site_monitor/

# Mount a Coolify persistent volume here so run history survives redeploys.
VOLUME ["/data"]

# One-shot by design: Coolify's scheduler invokes the container on a cron.
ENTRYPOINT ["python", "-m", "site_monitor"]
CMD ["check"]
