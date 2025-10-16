FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OS deps for lxml parser and tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates tzdata libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY app/requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

# App source
COPY app /srv/app

# Data dir
RUN mkdir -p /data
VOLUME ["/data"]

# Default env (can be overridden with .env / compose)
ENV FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8080 \
    UPDATE_CRON="*/15 * * * *" \
    YEARS_START=2018 \
    DB_PATH=/data/powerball.sqlite \
    TZ=Australia/Adelaide \
    PYTHONPATH=/srv/app

EXPOSE 8080

# Start the app (includes scheduler)
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--timeout", "120", "app:app"]
