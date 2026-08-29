FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/site-monitor.db \
    SITES_FILE=/app/sites.yaml \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY site_monitor/ ./site_monitor/
COPY scripts/ ./scripts/
COPY sites.example.yaml ./

# Sites, schedules, settings and history all live here. Mount a Coolify volume
# so none of it is lost on redeploy.
VOLUME ["/data"]

EXPOSE 8080

# The dashboard runs the scheduler in-process, so this one container is the
# whole application: no separate cron, no scheduled task to configure.
ENTRYPOINT ["python", "-m", "site_monitor"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
