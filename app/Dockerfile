FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && crontab /app/deploy/crontab \
    && useradd --system --uid 10001 --create-home sewasetu \
    && chown -R sewasetu:sewasetu /app /var/sewasetu /var/log/sewasetu

EXPOSE 8000
CMD ["sh", "-c", "python scripts/init_db.py && gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 4 --threads 1 --timeout 30 --access-logfile - --error-logfile - app:app"]
