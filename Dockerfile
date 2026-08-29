FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/site-monitor.db \
    SITES_FILE=/app/sites.yaml

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY site_monitor/ ./site_monitor/

# sites.yaml is deliberately NOT baked in: this repo is public, and the site
# list names every client and their page structure. It arrives at runtime as a
# Coolify file mount at /app/sites.yaml. Only the example ships in the image.
COPY sites.example.yaml ./

# Run history lives here; mount a Coolify volume so it survives redeploys.
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "site_monitor"]
CMD ["check"]
