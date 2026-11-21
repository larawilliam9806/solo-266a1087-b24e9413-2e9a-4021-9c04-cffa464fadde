FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SURVEY_DB=/data/survey.sqlite3

# The importer streams and keeps only small per-row state.  The process
# address space is capped (see docker-compose.yml) so a regression shows up as
# a failed import rather than an OOM-killed container.  A single malloc arena
# keeps glibc from reserving one arena per thread, which is what actually
# pushes a multi-threaded worker near the cap.
ENV MALLOC_ARENA_MAX=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

WORKDIR /srv/app
# One worker with several threads: imports yield on I/O and SQLite commits,
# and other requests (saving an existing batch) are served from other threads
# while a large import is running.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "300", "--graceful-timeout", "30", \
     "--access-logfile", "-", "main:app"]
